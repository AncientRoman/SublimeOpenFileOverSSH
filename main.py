import os #temp file removal and path splitting
import math #pretty size calcs
import shlex #shell arg escaping
import string #random string creation
import random #random string creation
import sublime
import tempfile
import threading #stderr consuming
import subprocess #popen
import sublime_plugin
from enum import Enum

"""
 * Hey there!
 * Welcome to my plugin.
 * This file contains all of the python code for both the view handlers and input pallet command handlers.
 * Here's how I recommend reading/learning this code.
 *
 * The bottom of this file contains the ViewEventListener and TextCommand which is where the magic happens.
 * I'd start there.
 * The ViewEventListener handles saving and modifying the view while the TextCommand handles loading the view's contents.
 *
 * The middle of this file has the Input Pallet stuff which is best read starting with the serverInputHandler and then the pathInputHandler.
 * The other InputHandlers are for the extra actions (glob, new, and options).
 *
 * The top of this file includes ssh and popen args which contain the multiplexing information.
 * The custom SshShell class below those functions handles the Input Pallet's persistent ssh connection.
"""


SETTINGS_FILE = "OpenFileOverSSH.sublime-settings"

isWindows = (sublime.platform() == "windows")

viewToShell = {} #Maps view.id() to an SshShell. Allows multiple files to be opened using the same SshShell


#gets the required startup info for Popen
def getStartupInfo():

	#On Windows, the command shell is opened while the Popen command is running and this fixes that
	startupinfo = None
	if isWindows:
		startupinfo = subprocess.STARTUPINFO()
		startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

	return startupinfo

#gets the appropriate ssh args using the settings file
def getSshArgs():

	settings = sublime.load_settings(SETTINGS_FILE)

	args = ["-T"] #Non-interactive mode. While non-interactive will be the default, we'll get an unable to allocate tty error message without this

	if not settings.get("useOpenSshConfigArgs", True):
		return args

	#no user input
	args.extend(["-o", "BatchMode=yes"]) #Batch mode is StrictHostKeyChecking=yes (as opposed to ask) and PreferredAuthentications=publickey, i.e. no user input

	#host keys
	keyChecking = settings.get("hostKeyChecking", None)
	if keyChecking != None:
		if isinstance(keyChecking, bool) or keyChecking in ["yes", "no", "accept-new"]:
			args.extend(["-o", f"StrictHostKeyChecking={keyChecking}"])
			if not keyChecking or keyChecking == "no":
				args.extend(["-o", "UserKnownHostsFile=/dev/null"]) #don't save keys if key checking is disabled
		else:
			print(f"OpenFileOverSSH: Unrecognized hostKeyChecking setting ({keyChecking}), falling back to default")

	#timeout
	timeout = settings.get("timeout", 7)
	if timeout != None:
		if not (isinstance(timeout, int) or isinstance(timeout, str) and timeout.isdecimal()):
			print(f"OpenFileOverSSH: Unrecognized timeout setting ({timeout}), falling back to default")
			timeout = 7
		args.extend(["-o", f"ConnectTimeout={timeout}"]) #not specifying this uses system tcp timeout

	#multiplexing
	persist = settings.get("multiplexing", "5m" if not isWindows else False) #OpenSSH_for_Windows (as of 8/2024) does not support multiplexing
	if persist not in [None, False, 0, "0"]:

		if isinstance(persist, bool) and persist: #True == 1 so must check if its a bool
			persist = "5m"
		if not (isinstance(persist, int) or isinstance(persist, str) and (persist.isdecimal() or persist[-1] in ["m", "s"] and persist[:-1].isdecimal())):
			print(f"OpenFileOverSSH: Unrecognized multiplexing setting ({persist}), falling back to default")
			persist = "5m"

		#Using %C to both escape special characters and obfuscate the connection details
		#Auto creates a new master socket if it doesn't exist
		args.extend(["-o", "ControlPath=~/.ssh/SOFOS_cm-%C", "-o", "ControlMaster=auto", "-o", f"ControlPersist={persist}"])

	return args

#makes an error string for an ssh error. Uses the new single-dispatch (overload) functions to accept string or bytes
def makeErrorText(title, sshRetCode, sshStderr):

	if isinstance(sshStderr, bytes) or isinstance(sshStderr, bytearray):
		sshStderr = sshStderr.decode()

	error = sshStderr and sshStderr.replace("\r", "").rstrip("\n") #ssh's output to stderr has line endings of CRLF per ssh specs. Remove trailing new line too
	errType = "ssh" if sshRetCode == 255 or sshRetCode == None else "posix signal" if sshRetCode < 0 else "remote"

	if error:
		msg = f"{title}.\n\nCode: {sshRetCode} ({errType})\nError: {error}"
		lower = error.casefold()
		if errType == "ssh":
			if "host key verification failed" in lower:
				msg += "\n\nSSH to this server with your terminal to verify the host key or change the hostKeyChecking setting."
			if "permission denied" in lower:
				msg += "\n\nYou must setup ssh public key authentication with this server for this plugin to work."
			if "timed out" in lower:
				msg += "\n\nThe timeout time can be changed in this plugin's settings if needed."
			if "getsockname failed: bad file descriptor" in lower and isWindows:
				msg += "\n\nThis is likely from SSH not supporting multiplexing on windows. You can disable multiplexing in the settings file."
		return msg
	else:
		return f"{title}.\nAn unknown {errType} error occurred.\nError Code: {sshRetCode}"




#handles the input pallet's ssh shell
class SshShell():
	"""
	 * all methods of this class including the constructor are blocking accept for isAlive()
	 * after the constructor returns, ssh has either errored or is connected to remote and ready to receive commands
	"""

	setupCmds = [
		"export LC_TIME=POSIX" #set ls -l to output a standardized time format
	]

	def __init__(self, userAndServer):

		self.shell = subprocess.Popen(["ssh", *getSshArgs(), userAndServer], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=getStartupInfo())
		_, code, _ = self.runCmd("; ".join(self.setupCmds)) #read past all login information and run the setupCmds; will block until completed or error

		"""
		 * Theoretically if ret is false, isAlive should also be false.
		 * However on windows this is not always the case.
		 * For example, on initial ssh error (host key, password auth, multiplexing) in windows:
		 *    ret is false because the read() returned EOF
		 *    isAlive() is true (because ssh hasn't exited yet??)
		 *    if this isn't caught, the next write()/flush() will error with broken pipe (which is EINVAL on python windows)
		 * Leave it up to windows to make code complicated :(
		"""
		if self.isAlive() and code != 255:
			self.thread = threading.Thread(target=self.shell.stderr.read, daemon=True) #consume sterr so a full pipe doesn't block our process
			self.thread.start()
			self.error = None
		else:
			self.error = self.shell.stderr.read().decode().replace("\r", "").rstrip("\n") #ssh's output to stderr has line endings of CRLF per ssh specs. Remove trailing new line too
			if self.isAlive(): #ensure the process is dead (in case we got here through ret being False); this is needed because isAlive is used to check for errors
				self.close(timeout=0.25)

	@staticmethod
	def quote(str): #shlex.quote but an empty string stays empty
		return str and shlex.quote(str)

	@property
	def retCode(self):
		return self.shell.returncode

	@classmethod
	def _genSeekingStr(cls):
		return "A random string for seeking" + ("".join([random.choice(string.ascii_uppercase + string.digits) for _ in range(30)]))

	def isAlive(self):
		return self.shell.poll() == None

	def runCmd(self, cmd, splitLines=True, decode=True, *, throwOnSshErr=False): #returns: (stdout, retCode, stderr)

		"""
		 * As of right now, stderr will usually be None to indicate unable to read stderr
		 *
		 * Until this function can actually return stderr, throwOnSshErr will be available
		 * When True, this function will display an error message and raise an exception if an Ssh Error (i.e. connection dropped) occurs
		 * Use this to avoid needing to error check in calling code
		"""

		#write
		seekingString = self._genSeekingStr()
		if cmd != "":
			cmd += "; "
		cmd += f"printf \"\\n$?\\n{seekingString}\\n\"\n" #printf "\n retCode \n seekingStr \n"
		seekingString = seekingString.encode()

		try:
			self.shell.stdin.write(cmd.encode())
			self.shell.stdin.flush()
		except (BrokenPipeError, OSError) as e: #will catch closed pipe errors if ssh has terminated (BrokenPipeError on unix, OSError EINVAL on windows)

			self.shell.poll() #set returncode if its available (on windows its prolly not)
			if throwOnSshErr:
				sublime.error_message(makeErrorText("Lost connection to the server (write)", self.retCode or 255, str(e)))
				raise Exception("Ssh Connection Drop")

			stderr = "Connection lost: " + str(e)
			return ([] if splitLines else "" if decode else b"", self.retCode or 255, stderr if decode else stderr.encode())


		#read
		lines = []
		while True:

			line = self.shell.stdout.readline()

			if len(line) == 0: #EOF i.e. error

				self.shell.poll()
				if throwOnSshErr:
					sublime.error_message(makeErrorText("Lost connection to the server (read)", self.retCode or 255, "Encountered EOF"))
					raise Exception("Ssh Connection Drop")

				stderr = "Connection lost: encountered EOF during read"
				return ([] if splitLines else "" if decode else b"", self.retCode or 255, stderr if decode else stderr.encode())

			if line[:-1] == seekingString:
				break;

			if splitLines:
				line = line.rstrip(b"\n")
			if decode:
				line = line.decode()
			lines.append(line)


		#return
		retCode = int(lines.pop())

		if len(lines[-1]) == (not splitLines): #remove the extra \n added with printf
			lines.pop()
		elif not splitLines:
			lines[-1] = lines[-1][:-1]

		return (lines if splitLines else ("" if decode else b"").join(lines), retCode, None)

	def close(self, timeout=None):

		#close/kill ssh
		if self.isAlive():

			try:
				self.shell.stdin.write(b"exit\n")
				self.shell.stdin.flush()
			except (BrokenPipeError, OSError):
				pass
			try:
				self.shell.stdin.close()
			except (BrokenPipeError, OSError):
				pass

			print("OpenFileOverSSH: Waiting for ssh to exit...")
			try:
				self.shell.wait(timeout)
			except TimeoutExpired:
				print("OpenFileOverSSH: ssh exit timed out, killing...")
				self.ssh.terminate()
				try:
					self.shell.wait(timeout)
				except TimeoutExpired:
					self.shell.kill()
					self.shell.wait()
			print("OpenFileOverSSH: ssh finished with return code %d" % self.shell.returncode)


		#clean up
		try:
			self.thread.join() #join thread to free up resources
			print("OpenFileOverSSH: ssh thread joined")
		except AttributeError:
			print("OpenFileOverSSH: no ssh thread cleanup needed")

	def __del__(self):

		self.close()


#shared arguments for command pallet handlers. Acts as a dictionary with special path and session settings features
class Argz(dict):

	settings = sublime.load_settings(SETTINGS_FILE)
	SESS_SETTINGS_DATA = [("pathChecking", True), ("hiddenFiles", "showHiddenFiles", False)]

	def __init__(self):

		super().__init__() #extend dict

		#session settings
		self.settings = {}
		for item in self.SESS_SETTINGS_DATA:
			pref = self.__class__.settings.get(item[0 if len(item) == 2 else 1])
			self.settings[item[0]] = pref if pref != None else item[-1]

		self._path = [] #array of tuples or strings containing path components (folders or files); each item is one InputHandler
		self._strPath = "" #string representation of path
		self._flatLen = 0 #length of flattened _path
		self._oldPath = self.__class__.settings.setdefault("path", [])[:] #default selection flat path


	"""
	 * This class is all about path and strPath
	 * strPath could just be created from path every time using a flatten method,
	 *     but instead this uses caching and does not allow direct editing of path
	 * Use path: append, pop, and len to interact with path
	 * Use nextPath for path auto completion
	 * Use savePath to write the flattened path to SETTINGS_FILE for use in next session's auto completion
	"""

	@property
	def strPath(self):
		return self._strPath

	@property
	def nextPath(self): #returns the default next path component, which is the previously selected path (either from a past session or a pathPop())

		try:
			return self._oldPath[self._flatLen]
		except IndexError:
			return None

	def pathAppend(self, val):

		self._path.append(val)

		try:
			self._strPath += "".join(val)
		except TypeError: #val is not a string
			pass

		val = list(val) if isinstance(val, tuple) else [val,]
		if self._oldPath[self._flatLen:self._flatLen+len(val)] != val: #keep _oldPath up to date with current selections if they differ from previous ones
			self._oldPath[self._flatLen:] = val
			if self._oldPath == self.__class__.settings["path"][:len(self._oldPath)]: #if _oldPath starts to match settings[path] again, go back to using settings[path]
				self._oldPath = self.__class__.settings["path"][:]
		self._flatLen += len(val)

	def pathPop(self):

		popped = self._path.pop()

		try:
			self._strPath = self._strPath[:-len("".join(popped))]
		except TypeError:
			pass

		self._flatLen -= len(popped) if isinstance(popped, tuple) else 1

		return popped

	def pathLen(self):

		return len(self._path)

	def savePath(self):

		self.__class__.settings["path"] = self._oldPath #oldPath happens to be the current flattened path when a file (or action) is selected
		sublime.save_settings(SETTINGS_FILE)




#input pallet server input
class serverInputHandler(sublime_plugin.TextInputHandler):

	def __init__(self, argz):

		super().__init__()

		self.argz = argz
		self.ssh = None
		self.settings = sublime.load_settings(SETTINGS_FILE)

	@staticmethod
	def checkSyntax(text): #false (0): invalid, 1: user/server, 2: server, 3: folder path, 4: file path

		if not (
			len(text) >= 2 and
			":" in text and
			("@" not in text or text.count("@") == 1 and text[0] != "@" and text.index("@") < text.index(":") - 1)
		):
			return False

		if text[-1] == ":":
			return 1 if "@" in text else 2
		else:
			return 3 if text[-1] == "/" else 4

	#gray placeholder text
	def placeholder(self):

		return "user@remote.server:"

	#previous value
	def initial_text(self):

		return self.settings.get("server", "")

	#syntax check
	def preview(self, text):

		type = self.checkSyntax(text)

		if not type:
			return "Invalid Server Syntax"

		ret = "Server Input Valid"
		if type == 2:
			ret += "for Default Username"
		elif type == 3:
			ret += "; Open File Browser at Folder"
		elif type == 4:
			ret += "; Open/Create File"

		return ret

	#check server
	def validate(self, text):

		type = self.checkSyntax(text)

		if not type:
			return False
		if type == 4:
			return True

		server = text[:text.index(":")]
		ssh = SshShell(server)

		if not ssh.isAlive(): #check if not dead
			#the dialog looks kinda ugly, but I can't think of a better way
			sublime.error_message(makeErrorText(f"Could not connect to {server}", ssh.retCode, ssh.error))
			return False

		if type == 3 and self.argz.settings["pathChecking"]:

			path = text[text.index(":") + 1:]
			echoCode = "printf \"$?\\n\""
			[e, d], x, _ = ssh.runCmd(f"test -e {path.rstrip('/')}; {echoCode}; test -d {path}; {echoCode}; test -x {path}")

			if e == "1": #greater than 1 means test errored
				msg = "No such file or directory"
			elif d == "1":
				msg = "Not a directory"
			elif x == 1:
				msg = "Permission denied"
			else:
				msg = None

			if msg:
				sublime.error_message(f"Unable to access (open) '{path}'\n({msg})")
				return False

		self.ssh = ssh #only save if it'll be used
		return True

	#save value
	def confirm(self, text):

		self.settings.set("server", text)
		sublime.save_settings(SETTINGS_FILE)

		sep = text.index(":")
		self.argz["server"] = text[:sep]
		self.argz["sshShell"] = self.ssh

		if text[-1] == "/": #type 3
			self.argz.pathAppend(tuple(comp + "/" for comp in text[sep + 1:].split("/")[:-1]))
		elif text[-1] != ":": #type 4
			self.argz["paths"] = [text[sep + 1:]]
		self.argz["hasStartDir"] = text[-1] == "/" #need to know if type 3 for ../ handling

	#close ssh
	def cancel(self):

		if self.ssh:
			self.ssh.close()

	#file selection
	def next_input(self, args):

		return pathInputHandler(self.argz) if not "paths" in self.argz else None

#input pallet action glob input
class globInputHandler(sublime_plugin.TextInputHandler):

	def __init__(self, argz):

		super().__init__()

		self.argz = argz
		self.ssh = argz["sshShell"]
		self.settings = sublime.load_settings(SETTINGS_FILE)

	@staticmethod
	def isSyntaxOk(text):

		globs = text.split(" ")

		for glob in globs:
			if not "*" in glob:
				return False #every space-separated pattern must have a *

		return True

	def getMatchingPaths(self, text):

		path = self.ssh.quote(self.argz.strPath)
		globs = [path + glob for glob in text.split()]

		return [path for path in self.ssh.runCmd(f"/bin/ls -1Lpd -- {' '.join(globs)}", throwOnSshErr=True)[0] if not path.endswith("/")] #will return full paths

	#gray placeholder text
	def placeholder(self):

		return "*.c h*.h"

	#previous value
	def initial_text(self):

		return self.settings.get("glob", "")

	#syntax check
	def preview(self, text):

		if not self.isSyntaxOk(text):
			return "Invalid Glob Syntax"

		return "Glob Input Valid"

	#check matches
	def validate(self, text):

		if self.isSyntaxOk(text):

			if len(self.getMatchingPaths(text)) > 0:
				return True

			sublime.error_message("No files were found matching the pattern{} '{}'".format("s" if len(text.split(" ")) > 1 else "", text)) #the dialog looks ugly, but I can't think of a better way

		return False

	#update values
	def confirm(self, text):

		self.argz.savePath()
		self.settings.set("glob", text)
		sublime.save_settings(SETTINGS_FILE)

		self.argz["paths"] = self.getMatchingPaths(text)

	#pop()
	def cancel(self):

		self.argz.pathPop() #pop off the * that got us here

	#all done
	def next_input(self, args):

		return None

#input pallet action new file input
class newInputHandler(sublime_plugin.TextInputHandler):

	def __init__(self, argz):

		super().__init__()

		self.argz = argz
		self.ssh = argz["sshShell"]

	@staticmethod
	def splitPath(text): #returns (path, file, folders)

		if len(text) == 0:
			return (None, None, None)

		path = text.split("/")
		file = path[-1]
		folders = path[:-1]

		if "" in folders: #disallow multiple or initial slashes
			return (None, file, None)

		path = [folder + "/" for folder in folders] + [file]
		folders = "".join(path[:-1])

		return (path, file, folders)

	def fileExists(self, file):

		path = self.ssh.quote(self.argz.strPath + file)
		return len(self.ssh.runCmd("/bin/ls -d -- " + path, throwOnSshErr=True)[0]) > 0

	#gray placeholder text
	def placeholder(self):

		return "newFolder/newFile.txt"

	#path check
	def preview(self, text):

		path, file, folders = self.splitPath(text)
		if path == None:
			return None if file == None else "Invalid Path"

		#New File {} in New Folder(s) {}
		text = ""
		if len(file) > 0:
			text = f"New File {{{file}}}" + (" in " if len(folders) > 0 else "")
		if len(folders) > 0:
			text += f"New Folder{'s' if len(path) > 2 else ''} {{{folders}}}"

		return text

	#check new file/folder
	def validate(self, text):

		path = self.splitPath(text)[0]
		if path != None:

			path = path[0].rstrip("/")
			if not self.fileExists(path):
				return True

			#It technically doesn't matter if the file/folder already exists cause it'll get opened correctly; but since the user is expecting to make a new file/folder I'll let them know.
			sublime.error_message(f"The file/folder {{{path}}} already exists.")

		return False

	#update values
	def confirm(self, text):

		#because we create the new folder in this function, that makes re-editing this input handler difficult
		#    which means that bringing the user into the newly created folder doesn't work because the user might try to go back up the input handler stack
		#so instead of dealing with that, I'll just pop this handler off the stack and let the user select the new folders themselves
		#I will however set the previous selections (in the settings file) to the new folders so at least they'll be the defaults

		path, file, folders = self.splitPath(text)

		self.argz.pathPop() #remove the new file that got us here

		#make the folders
		if len(folders) > 0:
			self.ssh.runCmd("mkdir -p -- " + self.ssh.quote(self.argz.strPath + folders), throwOnSshErr=True)
			self.argz.pathAppend(path[:-1]) #add the new folders to the path

		#open the file
		if len(file) > 0:
			self.argz.pathAppend(pathInputHandler.Action.NEW) #make it look like the user selected new in the new (or old) folder(s)
			self.argz.savePath()
			self.argz["paths"] = [self.argz.strPath + file]
		else:
			self.argz.savePath() #set the previous selection(s) to the new folders
			self.argz.pathPop() #remove the new folders in the current path

	#pop()
	def cancel(self):

		self.argz.pathPop() #pop off the New File that got us here

	#all done
	def next_input(self, args):

		file = self.splitPath(args["new"])[1]

		return None if len(file) > 0 else sublime_plugin.BackInputHandler()

#input pallet action session options
class optionsInputHandler(sublime_plugin.ListInputHandler):

	"""
	 * This seems like overkill for toggling hidden files (it's only real use) but it has to be something like this.
	 * Toggling hidden files has to trigger another call to list_items() which Sublime does not provide an api for.
	 * list_items() is only called when a handler is first shown or when the next handler is popped off the stack.
	 * So this options handler will be that handler that is popped off the stack.
	 *
	 * Another option would be to push a duplicate listInputHandler on the stack when hidden files are toggled.
	 * Unfortunately this messes up the handler stack with a duplicate handler that causes issues when popping particularly with the .. directory.
	"""

	class Option(str, Enum):

		#action has to be a string function name because optionsInputHandler doesn't exist as a type yet.
		#...forward declaration issues. Literally the one bad thing about python...rip

		HIDDEN = (
			"Toggle Hidden Files",
			"dot files",
			".",
			lambda settings: f"{'Hide' if settings['hiddenFiles'] else 'Show'} Hidden Files (dot files)",
			"toggleHiddenFiles"
		)
		PCHECKING = (
			"Toggle Path Checking",
			"error time",
			"✓",
			lambda settings: f"Show Errors {'After' if settings['pathChecking'] else 'Before'} Selection",
			"togglePathChecking"
		)

		def __new__(cls, text, annotation, kLetter, preview, action):

			obj = str.__new__(cls, text)
			obj._value_ = text
			obj.annotation = annotation
			obj.kLetter = kLetter
			obj.preview = preview
			obj.action = action
			return obj


	@staticmethod
	def toggleHiddenFiles(settings):
		settings["hiddenFiles"] = not settings["hiddenFiles"]

	@staticmethod
	def togglePathChecking(settings):
		settings["pathChecking"] = not settings["pathChecking"]


	def __init__(self, argz):

		super().__init__()

		self.settings = argz.settings

	#show all Options
	def list_items(self):

		kColor = pathInputHandler.Kind.ACTION[0]
		kOther = pathInputHandler.Kind.ACTION[2:]

		return [sublime.ListInputItem(option.value, option.value, annotation=option.annotation, kind=(kColor, option.kLetter) + kOther) for option in self.Option]

	#display preview
	def preview(self, value):

		preview = self.Option(value).preview
		return preview(self.settings) if callable(preview) else preview

	#run action
	def confirm(self, value):

		try:
			getattr(self, self.Option(value).action)(self.settings)
		except AttributeError:
			print(f"OpenFileOverSSH: Bad Option function name: {self.Option(value).action}")

	#pop back
	def next_input(self, args):

		return sublime_plugin.BackInputHandler()

#input pallet path input
class pathInputHandler(sublime_plugin.ListInputHandler):

	parentDir = "../"

	class Kind(tuple, Enum):

		FILE = (sublime.KindId.COLOR_YELLOWISH, "f", "")
		FOLDER = (sublime.KindId.COLOR_CYANISH, "F", "")
		ACTION = (sublime.KindId.COLOR_PURPLISH, "-", "")
		CONFUSED = (sublime.KindId.COLOR_ORANGISH, "?", "")
		ERROR = (sublime.KindId.COLOR_REDISH, "!", "")

	#ListInputItem value must be a sublime.Value so this class helps store InputHandlers in the ListInputItem
	class Action(int, Enum):

		GLOB = 1, "Open Multiple Files with a Glob", globInputHandler
		NEW = 2, "Make a New File or Folder Here", newInputHandler
		OPTIONS = 3, lambda self: f"Edit Session Options such as {'hiding' if self.argz.settings['hiddenFiles'] else 'showing'} hidden files", optionsInputHandler

		def __new__(cls, val, preview, handler):

			obj = int.__new__(cls, val)
			obj._value_ = val
			obj.preview = preview
			obj.handler = handler
			return obj


	def __init__(self, argz):

		super().__init__()

		self.argz = argz
		self.ssh = argz["sshShell"]

	@staticmethod
	def isPath(value):
		return isinstance(value, str)

	@staticmethod
	def isFolder(strValue):
		return strValue.endswith("/")

	@staticmethod
	def prettySize(bytes):

		if bytes == 0:
			return "0"
		sizes = ("B", "K", "M", "G", "T", "P", "E", "Z", "Y") #future proof lol
		i = int(math.floor(math.log(bytes, 1024))) #uses powers of 2 e.g. MiB
		p = math.pow(1024, i)
		s = bytes / p
		return f"{int(round(s)) if s.is_integer() else round(s, 1)}{sizes[i]}"

	#ls, actions, and initial selection
	def list_items(self):

		#setup
		path = self.ssh.quote(self.argz.strPath)
		cmd = f"/bin/ls -1Lp -lgo {'-a' if self.argz.settings['hiddenFiles'] else ''} -- {path}"
		files, retCode, err = self.ssh.runCmd(cmd)
		files = files[1:] #skip the total line
		items = []
		hasFile = False
		self.error = None

		#check ls
		if retCode != 0 and len(files) == 0: #ls can fail on one file, return a failed code, and list the other files normally. Usually caught by lsConfused logic

			self.error = err or self.ssh.runCmd(f"{cmd} 2>&1", False)[0]

			msg = f"ERROR: Failed to list files in {path if path else '~'} : "
			lower = self.error.casefold()
			if retCode == 255 or retCode < 0:
				sublime.error_message(makeErrorText("Lost connection to the server", retCode, self.error))
				msg += "Connection lost"
			elif "not a directory" in lower:
				msg += "Not a directory"
			elif "no such file or directory" in lower:
				msg += "No such file or directory"
			elif "permission denied" in lower:
				msg += "Permission denied"
			else:
				msg += "Unrecognized error"

			self.error = f"Exit code {retCode}; " + self.error
			return [sublime.ListInputItem(msg, None, annotation="Error", kind=self.Kind.ERROR)]

		#do
		for file in files:

			#split
			fileInfo = file.split(maxsplit=6) #perms, links, bytes, dt1, dt2, dt3, name; requires LC_TIME=POSIX
			lsConfused = False

			#check
			if len(fileInfo) == 5 and fileInfo[1] == fileInfo[2] == fileInfo[3] == "?":
				lsConfused = True
			elif len(fileInfo) != 7:
				if not self.error:
					print(f"OpenFileOverSSH: Unrecognized ls output:\n{chr(10).join(files)}") #char(10) is \n cause can't use a \ in an f string expr
				self.error = f"Unrecognized file info (skipping): {file} : {fileInfo}"
				print(f"OpenFileOverSSH: {self.error}")
				continue

			#parse
			file = fileInfo[-1]
			if file == "./":
				continue #pointless to select current directory
			isFolder = self.isFolder(file)

			if isFolder: #folder
				try:
					size = int(fileInfo[1]) - 2 #number of sub-directories
				except ValueError:
					size = "?"

				kind = self.Kind.FOLDER

			else: #file
				try:
					size = self.prettySize(int(fileInfo[2]))
				except ValueError:
					size = fileInfo[2]

				kind = self.Kind.FILE
				hasFile = True

			if lsConfused: #confused e.g. link with deleted source (will trigger the file branch above)
				kind = self.Kind.CONFUSED

			#item
			items.append(sublime.ListInputItem(file, file, annotation=f"->{size}" if isFolder else size, kind=kind))


		#actions and warnings
		if not self.argz.pathLen():
			items.append(sublime.ListInputItem("/", "/", annotation="Root Dir", kind=self.Kind.ACTION))
		if hasFile:
			items.append(sublime.ListInputItem("*", self.Action.GLOB, annotation="Pattern", kind=self.Kind.ACTION))
		items.append(sublime.ListInputItem("New", self.Action.NEW, annotation="Create", kind=self.Kind.ACTION))
		items.append(sublime.ListInputItem("Options", self.Action.OPTIONS, annotation="Session Prefs", kind=self.Kind.ACTION))

		if self.error:
			items.insert(0, sublime.ListInputItem("WARNING: A Parsing Error Occurred and some Entries are Missing or Wrong", None, annotation="Warning", kind=self.Kind.ERROR))


		#default selection
		try:
			if self.argz.nextPath != None:
				items = (items, next(i for i,item in enumerate(items) if item.value == self.argz.nextPath))
		except StopIteration: #no matching value
			pass

		return items

	#gray placeholder text
	def placeholder(self):

		return "file"

	#folder, file, or action
	def preview(self, value):

		if value == None:
			return self.error or "No items found. You can use the New action to create a file."
		if not self.isPath(value):
			preview = self.Action(value).preview
			return preview(self) if callable(preview) else preview
		elif self.isFolder(value):
			return "Enter Folder"
		else:
			return "Open File"

	#check file/folder
	def validate(self, value):

		if value == None:
			return False

		if self.isPath(value) and self.argz.settings["pathChecking"]:

			path = self.ssh.quote(self.argz.strPath + value)
			_, code, _ = self.ssh.runCmd(f"test -{'x' if self.isFolder(value) else 'r'} {path}")

			if code == 1: #greater than 1 means test errored
				sublime.error_message(f"Unable to access ({'open' if self.isFolder(value) else 'read'}) '{value}'\n(Permission Denied)")
				return False

		return True

	#push/update
	def confirm(self, value):

		#next_input cannot determine if ../ should BackInputHandler because the path will already be updated, so it'll use self.popped
		self.popped = value == self.parentDir and self.argz.pathLen() > self.argz["hasStartDir"] and not self.argz.strPath.endswith(self.parentDir) #if ../ should pop
		if not self.popped:
			if value != self.Action.OPTIONS: #options is a one-off that shouldn't be used as an autocompleted path (and doesn't need to be append-ed)
				self.argz.pathAppend(value)
		else:
			self.argz.pathPop()

		if self.isPath(value) and not self.isFolder(value):

			#save path when done as opposed to as we go because this how sublime does it with internal commands
			self.argz.savePath()

			self.argz["paths"] = [self.argz.strPath]

	#pop
	def cancel(self):

		try:
			path = self.argz.pathPop()
		except IndexError: #Nothing to pop
			pass

	#continue if folder or action
	def next_input(self, args):

		value = args["path"]

		if self.popped:
			return sublime_plugin.BackInputHandler()
		if not self.isPath(value):
			return self.Action(value).handler(self.argz)
		if self.isFolder(value):
			return pathInputHandler(self.argz)

		return None


#the command that is run from the command pallet
class openFileOverSshCommand(sublime_plugin.WindowCommand):

	def run(self, **args):

		#because everything happens synchronously, using the open sshShell is faster than multiplexing by a noticeable amount when opening 10+ files
		#however, using the sshShell requires that the file must not contain a line with the seeking string
		#while that is an unlikely occurrence, I will use multiplexing if its available when opening a single file to ensure a non-glob is always opened correctly

		useShell = self.argz["sshShell"] and (len(self.argz["paths"]) > 1 or "ControlMaster=auto" not in getSshArgs()) #i.e. isMultipleFiles || isMultiplexingDisabled

		for path in self.argz["paths"]:

			#open a temp file with the correct extension
			#I can't just make a new file because I want the syntax to be set based on the remote file's extension
			_, ext = os.path.splitext(path)
			file = tempfile.NamedTemporaryFile(suffix=ext)
			view = self.window.open_file(file.name)

			if useShell:
				viewToShell[view.id()] = self.argz["sshShell"]

			#used to set the view/buffer path which prevents the save dialog from showing up on file save
			#it also looks nice when you right click on the file name at the top of the window
			#...well as nice as it can ;)
			view.settings().set("ssh_fake_path", self.argz["server"] + "/" + path)
			view.settings().set("ssh_server", self.argz["server"])
			view.settings().set("ssh_path", path)
			view.set_status("ssh_true", "Saving to " + self.argz["server"] + ":" + path)

			file.close()

		del self.argz["sshShell"] #remove this ref so the SshShell.__del__() function is called

	def input(self, args):

		self.argz = Argz()
		return serverInputHandler(self.argz)




class sofosCheekyMakeDirtyCommand(sublime_plugin.TextCommand):
	def run(self, edit):
		self.view.insert(edit, 0, " ")
		self.view.erase(edit, sublime.Region(0, 1))

#sets the view's buffer to the remote file's contents
class openFileOverSshTextCommand(sublime_plugin.TextCommand):

	#an edit object is required for modifying a view/buffer and a text command is the only valid way to get one in sublime text 3/4

	def run(self, edit):

		cmd = "cat -- " + shlex.quote(self.view.settings().get("ssh_path"))
		setRO = False

		#read
		if self.view.id() in viewToShell:

			txt, code, err = viewToShell[self.view.id()].runCmd(cmd, False, False)
			if code != 0 and err == None:
				err, _, _ = viewToShell[self.view.id()].runCmd(f"{cmd} 2>&1", False, False)
			del viewToShell[self.view.id()] #remove ref so the shell can close

		else:

			p = subprocess.Popen(["ssh", *getSshArgs(), self.view.settings().get("ssh_server"), cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=getStartupInfo())
			txt, err = p.communicate()
			code = p.returncode


		#error
		if code != 0 and not (code == 1 and b"No such file or directory" in err): #ok to open a non existent file
			path = f"{self.view.settings().get('ssh_server')}:{self.view.settings().get('ssh_path')}"
			sshErr = code == 255 or code < 0
			txt = (
				makeErrorText(f"{'Failed' if sshErr else 'Unable'} to open this remote file ({path})", code, err) +
				"\n\nYou can try to open this file again with the File > Revert File menu item. (The command pallet `File: Revert` will not work due to a bug in Sublime)"
			).encode()
			setRO = True
			if not sshErr or len(viewToShell) == 0: #i.e. not ssh error or this is the last file being opened at the same time
				sublime.error_message(makeErrorText(f"Unable to open remote file {path}", code, err))

		#write
		self.view.set_read_only(False)
		self.view.set_encoding("UTF-8")
		self.view.replace(edit, sublime.Region(0, self.view.size()), str(txt, "UTF-8", "ignore"))

		if setRO:
			self.view.set_read_only(True)

#takes care of writing the file to the remote location and keeping track of modifications
class openFileOverSshEventListener(sublime_plugin.ViewEventListener):

	def __init__ (self, view):

		self.view = view
		self.diffRef = ""
		self.viewName = True #name has to change each time its set
		self.dirtyWhenDoHacks = False #used to not set_scratch(True) on failed save

	@classmethod
	def is_applicable(cls, settings):
		return settings.has("ssh_server") and settings.has("ssh_path")

	@classmethod
	def applies_to_primary_view_only(cls):
		return True


	def doHacks(self):

		"""
		 * Sublime Text 4 is smarter than Sublime Text 3
		 * Sublime Text 4 recognizes if its target (set by view.retarget()) does not exist,
		 *     and if it doesn't it will show the choose a save location dialog before on_pre_save is called
		 * So I will not have a chance to make the temp file and retarget to it
		 * However, for some reason, if you do view.set_name before a view.retarget, sublime won't realize the file doesn't exit
		 * So we can get back to Sublime Text 3's behavior
		 * But but, the name has to be different each time set_name() is called
		 * Idk man, its weird
		"""
		self.viewName = not self.viewName
		self.view.set_name(str(self.viewName)) #sets the name so retarget() will behave (see above)
		self.view.retarget(self.view.settings().get("ssh_fake_path")) #sets the view/buffer path so the file name looks nice
		self.view.set_reference_document(self.diffRef) #set the diff ref to the saved original file (otherwise the diffs are all messed up)
		if not self.dirtyWhenDoHacks:
			self.view.set_scratch(True) #on_mod sets this to false
		else:
			self.view.run_command("sofos_cheeky_make_dirty") #after a save the view will not be dirty so fix that
			self.dirtyWhenDoHacks = False


	def on_load(self):

		self.view.run_command("open_file_over_ssh_text") #open dat remote file

		self.view.sel().clear() #erase selections (the whole view will be selected, idk why)
		self.view.sel().add(sublime.Region(0, 0)) #put cursor on first line (default sublime behavior when a normal file is opened)

		self.diffRef = self.view.substr(sublime.Region(0, self.view.size())) #save the contents of the buffer in order to mimic sublime's incremental diff on a normal file

		self.doHacks()

	def on_revert(self, prevSel=None):

		#this (is supposed to) handle the revert command run from the command pallet (or File menu)

		"""
		 * Reverting is very finicky (and I would argue buggy)
		 *
		 * on_revert is called after the view is reverted (as opposed to having a pre_ and post_) so:
		 *     The previous selections are gone by the time on_revert gets called so they can't be saved
		 *
		 * After a revert, on_modified gets called (after on_revert) so the buffer will look dirty for remote files
		 *
		 * Reverting does not call on_revert in these conditions:
		 *     The buffer is empty and the view's target does not exist (i.e. empty remote buffer)
		 *     The buffer is read_only and the view's target does not exist (i.e. remote file error text)
		 * These cases can still be caught with on_text_command but:
		 *     Commands run from the command pallet are not caught by on_text_command (like wtf 🤦‍♂️)
		 *         See: https://github.com/sublimehq/sublime_text/issues/2234 (was opened in 2018 lol. Its never getting fixed :()
		 *
		 *
		 * So on_text_command will be used to capture the reverts it can (all but command pallet)
		 * on_revert will get the rest that it can (command pallet accept for the exceptions noted above)
		 * on_text_command will save the selections so they can be restored (even tho sublime kinda tries to keep them, they get messed up in remote files for some reason)
		 *
		 *
		 * Ideally (i.e. if that github issue ever gets fixed) on_text_command would catch all reverts so that:
		 *     Sublime never even tries to load a non-existent file
		 *     All the weird reverting with bad targets doesn't affect remote files
		 *
		"""

		self.on_load()

		if prevSel:
			self.view.sel().clear()
			self.view.sel().add_all(prevSel)

	def on_text_command(self, command_name, args):

		#used to handle non command pallet reverts (see comment in on_revert)

		if command_name == "revert":

			self.on_revert(list(self.view.sel()) if not self.view.is_read_only() else None) #don't save error text selection
			return ("SOFOS_NOOP", {}) #sublime ignores non-existent commands

	def on_pre_save(self):

		#this gets called after the save dialog has exited when this file is not view.retarget()'ed (see openFileOverSshCommand.run() and doHacks())

		"""
		 * two ways I could write changes to the remote file
		 *
		 * 1. (what I am currently doing), uses pre_save
		 *     use ssh and copy stdin to the remote file i.e. erase remote file with stdin
		 *     stdin is set to the buffer contents using the argument to popen.communicate()
		 *
		 * 2. (not sure which is better), would use post_save
		 *     after the file is saved to the temp file, scp to the temp file to the remote location
		 *     just like anyone would do normally when they wanted to copy a local file to a remote location
		"""

		if not self.view.is_read_only(): #don't save the error message lol

			p = subprocess.Popen(["ssh", *getSshArgs(), self.view.settings().get("ssh_server"), "cat > " + shlex.quote(self.view.settings().get("ssh_path"))], stdin=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=getStartupInfo()) #ssh cp stdin to remote file
			_, err = p.communicate(self.view.substr(sublime.Region(0, self.view.size())).encode("UTF-8")) #set stdin to the buffer contents

			if p.returncode != 0:
				sublime.error_message(makeErrorText(f"Unable to save remote file {self.view.settings().get('ssh_server')}:{self.view.settings().get('ssh_path')}", p.returncode, err))
				self.dirtyWhenDoHacks = True

		else:
			print("OpenFileOverSSH: not saving read only buffer (error message)")

		"""
		 * The Windows Python temporary files cannot be opened by another process before close() (see tempfile.NamedTemporaryFile docs)
		 * So on a Mac/Linux:
		 *     Make the file (delete=True), retarget sublime, then close() the file which deletes it
		 * On Windows:
		 *     Make the file with delete=False, close() the file (does not delete it), retarget sublime, delete file with os.remove()
		"""
		self.file = tempfile.NamedTemporaryFile(delete=(not isWindows)) #make a temp file to save to because you cannot stop sublime saving to a file
		if isWindows:
			self.file.close()
		self.view.retarget(self.file.name) #tell sublime to save to the temp file ;)

	def on_post_save(self):

		#erase dat fake file dough, hehe, take that sublime
		if isWindows:
			os.remove(self.file.name)
		else:
			self.file.close()

		self.doHacks()

	def on_modified(self):

		if self.view.is_scratch():
			self.view.set_scratch(False)
