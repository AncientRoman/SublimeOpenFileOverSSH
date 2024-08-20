import os #temp file removal
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
from functools import singledispatch

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
 * The other InputHandlers are for the extra actions (glob and new).
 *
 * The top of this file includes ssh and popen args including the multiplexing information.
 * The custom SshShell class below those functions handles the Input Pallet's persistent ssh connection.
"""


SETTINGS_FILE = "OpenFileOverSSH.sublime-settings"

isWindows = (sublime.platform() == "windows")

viewToShell = {} #Maps view.id() to an sshShell. Allows multiple files to be opened using the same SshShell


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

	args = ["-T"] #Non-interactive mode. While non-interactive will be the default, we'll get a unable to allocate tty error message without this

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
@singledispatch
def makeErrorText(sshStderr, sshRetCode, title):

	error = sshStderr and sshStderr.replace("\r", "").rstrip("\n") #ssh's output to stderr has line endings of CRLF per ssh specs. Remove trailing new line too
	errType = 'ssh' if sshRetCode == 255 or sshRetCode == None else 'remote'

	if error:
		msg = f"{title}.\n\nCode: {sshRetCode} ({errType})\nError: {error}"
		lower = error.lower()
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

@makeErrorText.register(bytes)
@makeErrorText.register(bytearray)
def _(sshStderr, sshRetCode, title):
	return makeErrorText(sshStderr.decode(), sshRetCode, title)




#handles the input pallet's ssh shell
class SshShell():
	"""
	 * all methods of this class including the constructor are blocking accept for isAlive()
	 * after the constructor returns, ssh has either errored or is connected to remote and ready to receive commands
	"""

	setupCmds = [
		"export LC_TIME=POSIX" #set ls -l to output a standardized time format
	]
	seekingString = b"A random string for seeking"

	def __init__(self, userAndServer):

		self.shell = subprocess.Popen(["ssh", *getSshArgs(), userAndServer], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=getStartupInfo())
		ret = self.runCmd("; ".join(self.setupCmds), retErr=True) #read past all login information and run the setupCmds; will block until completed or error

		"""
		 * Theoretically if ret is false, isAlive should also be false.
		 * However on windows this is not always the case.
		 * For example, when attempting to use multiplexing or on another ssh error (host key, password auth) in windows:
		 *    ret is false because the read() returned EOF
		 *    isAlive() is true (because ssh hasn't exited yet??)
		 *    if this isn't caught, the next write()/flush() will error with broken pipe (which is EINVAL on python windows)
		 * Leave it up to windows to make code complicated :(
		"""
		if self.isAlive() and not ret == False:
			self.thread = threading.Thread(target=self.shell.stderr.read, daemon=True) #consume sterr so a full pipe doesn't block our process
			self.thread.start()
			self.error = None
		else:
			self.error = self.shell.stderr.read().decode().replace("\r", "").rstrip("\n") #ssh's output to stderr has line endings of CRLF per ssh specs. Remove trailing new line too
			if self.isAlive(): #ensure the process is dead (in case we got here through ret being False); this is needed because isAlive is used to check for errors
				self.close(timeout=0.25)


	@property
	def retCode(self):
		return self.shell.returncode

	@staticmethod
	def trimNewLine(str):
		return str.rstrip(b"\n").rstrip(b"\r");

	@classmethod
	def getSeekingString(cls):

		return cls.seekingString + ("".join([random.choice(string.ascii_uppercase + string.digits) for _ in range(30)])).encode()

	def isAlive(self):
		return self.shell.poll() == None

	def runCmd(self, cmd="", retErr=False): #set retErr to True to return False on error instead of displaying error dialogs

		if cmd != "":
			cmd += "; "
		cmd = cmd.encode()
		seekingString = self.getSeekingString()
		try:
			self.shell.stdin.write(cmd + b"echo '" + seekingString + b"'\n")
			self.shell.stdin.flush()
		except (BrokenPipeError, OSError) as e: #Will catch closed pipe errors if ssh has terminated (BrokenPipeError on unix, OSError EINVAL on windows)
			if retErr:
				return False
			sublime.error_message(makeErrorText(str(e), self.retCode, f"Unable to communicate with the server (write)"))
			raise e

		lines = []

		while True:

			line = self.shell.stdout.readline()
			if len(line) == 0: #EOF i.e. error
				if retErr:
					return False
				sublime.error_message(makeErrorText(None, self.retCode, f"Unable to communicate with the server (read)"))
				break

			lines.append(self.trimNewLine(line))
			if lines[-1] == seekingString:
				break;

		return [x.decode() for x in lines[:-1]]

	def readFile(self, filePath):

		filePath = ("~/" if filePath[0] != "/" else "") + shlex.quote(filePath) #if filePath doesn't start with a /, then its assumed filePath is relative to ~, because the current directory can be anywhere
		filePath = filePath.encode()
		seekingString = self.getSeekingString()
		try:
			self.shell.stdin.write(b"cat " + filePath + b"; echo -e '\\n" + seekingString + b"'\n") #need the \n so seeking string is on its own line
			self.shell.stdin.flush()
		except (BrokenPipeError, OSError) as e:
			print(f"OpenFileOverSSH: write error in SshShell readFile(): {str(e)}")
			pass #continue because readline() below will return EOF which will trigger the error text

		lines = []

		while True:

			line = self.shell.stdout.readline()
			if len(line) == 0: #EOF i.e. error
				if len(viewToShell) < 2: #i.e. this is the only or last file being opened at the same time
					sublime.error_message(makeErrorText(None, self.retCode, f"Unable to communicate with the server (file read)"))
				#need three lines to always make it past the return slicing
				lines.insert(0, b"\n")
				lines.insert(0, b"Warning: an error or connection loss occurred while reading this file (" + filePath + b") from the remote server.\nThis file may be wrong or incomplete.\nDO NOT SAVE THIS FILE unless you are sure it's correct and complete.\nYou may reopen this file to ensure correctness with the `File:Revert` command from the command pallet\n\n")
				lines.insert(0, b"\n")
				break

			lines.append(line)
			if lines[-1][:-1] == seekingString:
				break;

		return b"".join(lines[:-1])[:-1] #remove \n added by echo

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


#input pallet server input
class serverInputHandler(sublime_plugin.TextInputHandler):

	def __init__(self, argz):

		super().__init__()

		self.argz = argz
		self.ssh = None

	@staticmethod
	def isSyntaxOk(text):

		return len(text) > 2 and text[-1] == ":" and ("@" not in text or text.count("@") == 1 and text[0] != "@" and text[-2] != "@")

	#gray placeholder text
	def placeholder(self):

		return "user@remote.server:"

	#previous value
	def initial_text(self):

		return sublime.load_settings(SETTINGS_FILE).get("server", "")

	#syntax check
	def preview(self, text):

		if not self.isSyntaxOk(text):
			return "Invalid Server Syntax"

		if "@" not in text:
			return "Server Input Valid for Default Username"


		return "Server Input Valid"

	#check server
	def validate(self, text):

		if self.isSyntaxOk(text):

			server = text[:-1]
			self.ssh = SshShell(server)

			if self.ssh.isAlive(): #check if not dead
				return True

			#the dialog looks kinda ugly, but I can't think of a better way
			sublime.error_message(makeErrorText(self.ssh.error, self.ssh.retCode, f"Could not connect to {server}"))

		return False

	#save value
	def confirm(self, text):

		sublime.load_settings(SETTINGS_FILE).set("server", text)
		sublime.save_settings(SETTINGS_FILE)

		self.argz["server"] = text[:-1]
		self.argz["sshShell"] = self.ssh

	#close ssh
	def cancel(self):

		if self.ssh:
			self.ssh.close()

	#file selection
	def next_input(self, args):

		return pathInputHandler(self.argz)

#input pallet action glob input
class globInputHandler(sublime_plugin.TextInputHandler):

	def __init__(self, argz):

		super().__init__()

		self.argz = argz
		self.ssh = argz["sshShell"]

	@staticmethod
	def isSyntaxOk(text):

		globs = text.split(" ")

		for glob in globs:
			if not "*" in glob:
				return False #every space-separated pattern must have a *

		return len(glob) > 0 #must be at least one pattern

	def getMatchingFiles(self, text):

		return self.ssh.runCmd(f"/bin/ls -1Lpd -- {text} | grep -v /$")

	#gray placeholder text
	def placeholder(self):

		return "*.c h*.h"

	#previous value
	def initial_text(self):

		return sublime.load_settings(SETTINGS_FILE).get("glob", "")

	#syntax check
	def preview(self, text):

		if not self.isSyntaxOk(text):
			return "Invalid Glob Syntax"

		return "Glob Input Valid"

	#check matches
	def validate(self, text):

		if self.isSyntaxOk(text):

			if len(self.getMatchingFiles(text)) > 0:
				return True

			sublime.error_message("No files were found matching the pattern{} '{}'".format("s" if len(text.split(" ")) > 1 else "", text)) #the dialog looks ugly, but I can't think of a better way

		return False

	#update values
	def confirm(self, text):

		settings = sublime.load_settings(SETTINGS_FILE)
		settings.set("path", self.argz["path"])
		settings.set("glob", text)
		sublime.save_settings(SETTINGS_FILE)

		self.argz["paths"] = ["".join(self.argz["path"][:-1] + [file]) for file in self.getMatchingFiles(text)]

	#pop()
	def cancel(self):

		self.argz["path"].pop() #pop off the * that got us here

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

		if "" in path[:-1]: #disallow multiple or initial slashes
			return (None, file, None)

		folders = "/".join(path[:-1])
		if len(folders) > 0:
			folders += "/"

		return (path, file, folders)

	def fileExists(self, text):

		return len(self.ssh.runCmd("/bin/ls -d " + shlex.quote(text))) > 0

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

			if not self.fileExists(path[0]):
				return True

			#It technically doesn't matter if the file/folder already exists cause it'll get opened correctly; but since the user is expecting to make a new file/folder I'll let them know.
			sublime.error_message(f"The file/folder '{path[0]}' already exists.")

		return False

	#update values
	def confirm(self, text):

		#because we create the new folder in this function, that makes re-editing this input handler difficult
		#    which means that bringing the user into the newly created folder doesn't work because the user might try to go back up the input handler stack
		#so instead of dealing with that, I'll just pop this handler off the stack and let the user select the new folders themselves
		#I will however set the previous selections (in the settings file) to the new folders so at least they'll be the defaults

		path, file, folders = self.splitPath(text)

		self.argz["path"].pop() #remove the new file that got us here

		#make the folders
		if len(folders) > 0:
			self.ssh.runCmd("mkdir -p " + shlex.quote(folders))
			self.argz["path"].extend([folder + "/" for folder in path[:-1]]) #add the new folders to the path

		#open the file
		if len(file) > 0:
			self.argz["path"].append(pathInputHandler.Action.NEW) #make it look like the user selected new in the new (or old) folder(s)
			self.argz["paths"] = ["".join(self.argz["path"][:-1]) + file]
			sublime.load_settings(SETTINGS_FILE).set("path", self.argz["path"])
		else:
			sublime.load_settings(SETTINGS_FILE).set("path", self.argz["path"]) #set the previous selection(s) to the new folders
			for i in range(len(path[:-1])): #remove the new folders in the current path
				self.argz["path"].pop()

		sublime.save_settings(SETTINGS_FILE)

	#pop()
	def cancel(self):

		self.argz["path"].pop() #pop off the New File that got us here

	#all done
	def next_input(self, args):

		file = self.splitPath(args["new"])[1]

		return None if len(file) > 0 else sublime_plugin.BackInputHandler()

#input pallet path input
class pathInputHandler(sublime_plugin.ListInputHandler):

	class Kind(tuple, Enum):

		FILE = (sublime.KindId.COLOR_YELLOWISH, "f", "")
		FOLDER = (sublime.KindId.COLOR_CYANISH, "F", "")
		ACTION = (sublime.KindId.COLOR_PURPLISH, "-", "")
		ERROR = (sublime.KindId.COLOR_REDISH, "!", "")

	#ListInputItem value must be a sublime.Value so this class helps store InputHandlers in the ListInputItem
	class Action(int, Enum):

		GLOB = 1, "Open Multiple Files with a Glob", globInputHandler
		NEW = 2, "Make a New File or Folder Here", newInputHandler

		def __new__(cls, val, preview, handler):

			obj = int.__new__(cls, val)
			obj._value_ = val
			obj.preview = preview
			obj.handler = handler
			return obj


	def __init__(self, argz):

		super().__init__()

		if not "path" in argz:
			argz["path"] = []

		self.argz = argz
		self.ssh = argz["sshShell"]
		self.settings = sublime.load_settings(SETTINGS_FILE)

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

		files = self.ssh.runCmd("/bin/ls -1Lp -lgo")[1:] #skip the total line
		hasAFile = False
		self.error = None

		for i, fileInfo in enumerate(files):

			fileInfo = fileInfo.split(maxsplit=6) #perms, links, bytes, dt1, dt2, dt3, name; requires LC_TIME=POSIX
			if len(fileInfo) != 7:
				if not self.error:
					print(f"OpenFileOverSSH: Unrecognized ls output (partial):\n{chr(10).join([file for file in files if isinstance(file, str)])}") #char(10) is \n cause can't use a \ in an f string expr
				self.error = f"Unrecognized file info (skipping): {files[i]} : {fileInfo}"
				print(f"OpenFileOverSSH: {self.error}")


			file = fileInfo[-1]
			isFolder = self.isFolder(file)
			if isFolder:
				try:
					size = int(fileInfo[1]) - 2 #number of sub-directories
				except ValueError:
					size = ""
			else:
				try:
					size = self.prettySize(int(fileInfo[2]))
				except ValueError:
					size = fileInfo[2]

				hasAFile = True

			files[i] = sublime.ListInputItem(file, file, annotation=f"->{size}" if isFolder else size, kind=self.Kind.FOLDER if isFolder else self.Kind.FILE)

		if hasAFile:
			files.append(sublime.ListInputItem("*", self.Action.GLOB, annotation="Pattern", kind=self.Kind.ACTION))

		files.append(sublime.ListInputItem("New", self.Action.NEW, annotation="Create", kind=self.Kind.ACTION))

		if self.error:
			files.insert(0, sublime.ListInputItem("WARNING: A Parsing Error Occurred and some Entries are Missing or Wrong", None, annotation="Warning", kind=self.Kind.ERROR))


		try:
			files = (files, next(i for i,f in enumerate(files) if f.value == self.settings.get("path", [])[len(self.argz["path"])])) #default selection
		except (StopIteration, IndexError): #StopIteration if no value from the generator, IndexError if the settings path isn't long enough
			pass


		return files

	#gray placeholder text
	def placeholder(self):

		return "file"

	#folder, file, or action
	def preview(self, value):

		if value == None:
			return self.error or "No items found. You can use the New action to create a file."
		if not self.isPath(value):
			return self.Action(value).preview
		elif self.isFolder(value):
			return "Enter Folder"
		else:
			return "Open File"

	#disallow selection of errors
	def validate(self, value):

		return value != None

	#update values
	def confirm(self, value):

		self.argz["path"].append(value)

		if not self.isPath(value):
			pass
		elif self.isFolder(value):
			self.ssh.runCmd("cd " + shlex.quote(value))
		else:
			#save path when done as opposed to as we go because this how sublime does it with internal commands
			self.settings.set("path", self.argz["path"])
			sublime.save_settings(SETTINGS_FILE)
			self.argz["paths"] = ["".join(self.argz["path"])]

	#cd ..
	def cancel(self):

		try:
			path = self.argz["path"].pop()
			if self.isPath(path) and self.isFolder(path):
				self.ssh.runCmd("cd ..")
		except IndexError: #Nothing to pop
			pass

	#continue if folder or action
	def next_input(self, args):

		value = args["path"]

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

		useShell = len(self.argz["paths"]) > 1 or "ControlMaster=auto" not in getSshArgs() #i.e. isMultipleFiles || isMultiplexingDisabled

		for path in self.argz["paths"]:

			#open a temp file with the correct extension
			#I can't just make a new file because I want the syntax to be set based on the remote file's extension
			ext = path.rfind(".")
			ext = path[ext:] if ext != -1 else ""
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

		self.argz = {}
		return serverInputHandler(self.argz)




class sofosCheekyMakeDirtyCommand(sublime_plugin.TextCommand):
	def run(self, edit):
		self.view.insert(edit, 0, " ")
		self.view.erase(edit, sublime.Region(0, 1))

#sets the view's buffer to the remote file's contents
class openFileOverSshTextCommand(sublime_plugin.TextCommand):

	#an edit object is required for modifying a view/buffer and a text command is the only valid way to get one in sublime text 3

	def run(self, edit):

		if self.view.id() in viewToShell:

			txt = viewToShell[self.view.id()].readFile(self.view.settings().get("ssh_path"))
			del viewToShell[self.view.id()] #remove ref so the shell can close

		else:
			p = subprocess.Popen(["ssh", *getSshArgs(), self.view.settings().get("ssh_server"), "cat " + shlex.quote(self.view.settings().get("ssh_path"))], stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=getStartupInfo())
			txt, err = p.communicate()

			if p.returncode != 0 and not (p.returncode == 1 and b"No such file or directory" in err): #ok to open a non existent file
				path = f"{self.view.settings().get('ssh_server')}:{self.view.settings().get('ssh_path')}"
				txt = (makeErrorText(err, p.returncode, f"Unable to open this remote file ({path})") + "\n\nDO NOT SAVE THIS FILE because the remote file contents may be destroyed.\nOnce you fix the problem, run the `File:Revert` command from the command pallet to reopen this file.").encode()
				sublime.error_message(makeErrorText(err, p.returncode, f"Unable to open remote file {path}"))

		self.view.set_encoding("UTF-8")
		self.view.replace(edit, sublime.Region(0, self.view.size()), str(txt, "UTF-8", "ignore"))

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
		return True;


	def doHacks(self):

		#Sublime Text 4 is smarter than Sublime Text 3
		#Sublime Text 4 recognizes if its target (set by view.retarget()) does not exist,
		#   and if it doesn't it will show the choose a save location dialog before on_pre_save is called
		#So I will not have a chance to make the temp file and retarget to it
		#However, for some reason, if you do view.set_name before a view.retarget, sublime won't realize the file doesn't exit
		#So we can get back to Sublime Text 3's behavior
		#But but, the name has to be different each time set_name() is called
		#Idk man, its weird
		self.viewName = not self.viewName
		self.view.set_name(str(self.viewName)) #sets the name so retarget() will behave (see above)
		self.view.retarget(self.view.settings().get("ssh_fake_path")) #sets the view/buffer path so the file name looks nice
		self.view.set_reference_document(self.diffRef) #set the diff ref to the saved original file (otherwise the diffs are all messed up)
		if not self.dirtyWhenDoHacks:
			self.view.set_scratch(True) #on_mod sets this to false
		else:
			self.view.run_command("sofos_cheeky_make_dirty") #after a save the view will not be dirty so fix that
			self.dirtyWhenDoHacks = False


	def on_load(self, setDiffRef=True):

		self.view.run_command("open_file_over_ssh_text") #open dat remote file

		self.view.sel().clear() #erase selections (the whole view will be selected, idk why)
		self.view.sel().add(sublime.Region(0, 0)) #put cursor on first line (default sublime behavior when a normal file is opened)

		if setDiffRef:
			self.diffRef = self.view.substr(sublime.Region(0, self.view.size())) #save the contents of the buffer in order to mimic sublime's incremental diff on a normal file

		self.doHacks()

	def on_revert(self):

		#this handles the revert command run from the command pallet
		#its not perfect because on_mod is called right after on_revert finishes so the buffer will look dirty
		#the previous selections are also gone by the time on_revert gets called so I can't save them

		self.on_load(False)

	def on_pre_save(self):

		#this gets called after the save dialog has exited when this file is not view.retarget()'ed (see openFileOverSshCommand.run() and doHacks())

		#two ways I could write changes to the remote file
		#
		#1. (what I am currently doing), uses pre_save
		#   use ssh and copy stdin to the remote file i.e. erase remote file with stdin
		#   stdin is set to the buffer contents using the argument to popen.communicate()
		#
		#2. (not sure which is better), would use post_save
		#   after the file is saved to the temp file, scp to the temp file to the remote location
		#   just like anyone would do normally when they wanted to copy a local file to a remote location

		p = subprocess.Popen(["ssh", *getSshArgs(), self.view.settings().get("ssh_server"), "cat > " + shlex.quote(self.view.settings().get('ssh_path'))], stdin=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=getStartupInfo()) #ssh cp stdin to remote file
		_, err = p.communicate(self.view.substr(sublime.Region(0, self.view.size())).encode("UTF-8")) #set stdin to the buffer contents

		if p.returncode != 0:
			sublime.error_message(makeErrorText(err, p.returncode, f"Unable to save remote file {self.view.settings().get('ssh_server')}:{self.view.settings().get('ssh_path')}"))
			self.dirtyWhenDoHacks = True

		#The Windows Python temporary files cannot be opened by another process before close() (see tempfile.NamedTemporaryFile docs)
		#So on a Mac/Linux:
		#   Make the file (delete=True), retarget sublime, then close() the file which deletes it
		#On Windows:
		#   Make the file with delete=False, close() the file (does not delete it), retarget sublime, delete file with os.remove()
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
