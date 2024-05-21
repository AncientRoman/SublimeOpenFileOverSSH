import os #temp file removal
import shlex #shell arg escaping
import string #random string creation
import random #random string creation
import sublime
import tempfile
import subprocess #popen
import sublime_plugin

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


SETTINGS_FILE = "SublimeOpenFileOverSSH.sublime-settings"

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

#gets the appropriate multiplexing args for ssh from the settings file
def getMultiplexingArgs():

    settings = sublime.load_settings(SETTINGS_FILE)
    persist = "5m"

    if settings.has("multiplexing"):

        persist = settings["multiplexing"]
        if persist in [None, False, 0, "0"]:
            return []
        if isinstance(persist, bool) and persist: #True == 1 so must check if its a bool
            persist = "5m"
        elif not (isinstance(persist, int) or isinstance(persist, str) and (persist.isdecimal() or persist[-1] in ["m", "s"] and persist[:-1].isdecimal())):
            print(f"SublimeOpenFileOverSSH: Unrecognized multiplexing setting ({persist}), falling back to default")
            persist = "5m"

    #Using %C to both escape special characters and obfuscate the connection details
    #Auto creates a new master socket if it doesn't exist
    return ["-o", "ControlPath=~/.ssh/SOFOS_cm-%C", "-o", "ControlMaster=auto", "-o", f"ControlPersist={persist}"]




#handles the input pallet's ssh shell
class SshShell():

    seekingString = b"A random string for seeking"

    def __init__(self, userAndServer):

        self.shell = subprocess.Popen(["ssh", *getMultiplexingArgs(), userAndServer], stdin=subprocess.PIPE, stdout=subprocess.PIPE, startupinfo=getStartupInfo())

    @staticmethod
    def trimNewLine(str):
        return str.rstrip(b"\n").rstrip(b"\r");

    @classmethod
    def getSeekingString(cls):

        return cls.seekingString + ("".join([random.choice(string.ascii_uppercase + string.digits) for _ in range(30)])).encode()

    def isAlive(self):
        return self.shell.poll() == None

    def runCmd(self, cmd=""):

        if cmd != "":
            cmd += "; "
        cmd = cmd.encode()
        seekingString = self.getSeekingString()
        self.shell.stdin.write(cmd + b"echo '" + seekingString + b"'\n")
        self.shell.stdin.flush()

        lines = []

        while True:

            lines.append(self.trimNewLine(self.shell.stdout.readline()))
            if lines[-1] == seekingString:
                break;

        return [x.decode() for x in lines[:-1]]

    def readFile(self, filePath):

        filePath = ("~/" if filePath[0] != "/" else "") + shlex.quote(filePath) #if filePath doesn't start with a /, then its assumed filePath is relative to ~, because the current directory can be anywhere
        filePath = filePath.encode()
        seekingString = self.getSeekingString()
        self.shell.stdin.write(b"cat " + filePath + b"; echo -e '\\n" + seekingString + b"'\n") #need the \n so seeking string is on its own line
        self.shell.stdin.flush()

        lines = []

        while True:

            lines.append(self.shell.stdout.readline())
            if lines[-1][:-1] == seekingString:
                break;

        return b"".join(lines[:-1])[:-1] #remove \n added by echo

    def close(self):

        if self.isAlive():
            self.shell.stdin.write(b"exit\n")
            self.shell.stdin.flush()
            self.shell.stdin.close()
            print("SublimeOpenFileOverSSH: Waiting for ssh to exit")
            self.shell.wait()
            print("SublimeOpenFileOverSSH: ssh finished with return code %d" % self.shell.returncode)

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

        return "@" in text and text[-1] == ":"

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

        return "Server Input Valid"

    #check server
    def validate(self, text):

        if self.isSyntaxOk(text):

            self.ssh = SshShell(text[:-1])

            if self.ssh.isAlive(): #check if not dead
                return True

            sublime.error_message("The specified server, {}, was not found".format(text)) #the dialog looks ugly, but I can't think of a better way

        return False

    #save value
    def confirm(self, text):

        sublime.load_settings(SETTINGS_FILE).set("server", text)
        sublime.save_settings(SETTINGS_FILE)

        self.ssh.runCmd() #get ssh shell ready for commands

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

        return self.ssh.runCmd(f"/bin/ls -1Lpd {text} | egrep -v /$")

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
            self.argz["path"].append(pathInputHandler.actions.new) #make it look like the user selected new in the new (or old) folder(s)
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

    fileKind = (sublime.KindId.COLOR_YELLOWISH, "f", "")
    folderKind = (sublime.KindId.COLOR_CYANISH, "F", "")
    actionKind = (sublime.KindId.COLOR_PURPLISH, "-", "")

    #ListInputItem value must be a sublime.Value so this class helps store InputHandlers in the ListInputItem
    class actions:

        glob = 1
        new = 2

        info = {
            glob: {"preview": "Open Multiple Files with a Glob", "handler": globInputHandler},
            new: {"preview": "Make a New File or Folder Here", "handler": newInputHandler}
        }


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

    #ls, actions, and initial selection
    def list_items(self):

        files = self.ssh.runCmd("/bin/ls -1Lp")
        hasAFile = False

        for i, file in enumerate(files):

            isFolder = self.isFolder(file)
            files[i] = sublime.ListInputItem(file, file, annotation="->" if isFolder else "", kind=self.folderKind if isFolder else self.fileKind)
            if not isFolder:
                hasAFile = True

        if hasAFile:
            files.append(sublime.ListInputItem("*", self.actions.glob, annotation="Pattern", kind=self.actionKind))

        files.append(sublime.ListInputItem("New", self.actions.new, annotation="Create", kind=self.actionKind))


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
            return "No matches found. You can use the New action to create a file."
        if not self.isPath(value):
            return self.actions.info[value]["preview"]
        elif self.isFolder(value):
            return "Enter Folder"
        else:
            return "Open File"

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
            return self.actions.info[value]["handler"](self.argz)
        if self.isFolder(value):
            return pathInputHandler(self.argz)

        return None


#the command that is run from the command pallet
class openFileOverSshCommand(sublime_plugin.WindowCommand):

    def run(self, **args):

        #because everything happens synchronously, using the open sshShell is faster than multiplexing by a noticeable amount when opening 10+ files
        #however, using the sshShell requires that the file must not contain a line with the seeking string
        #while that is an unlikely occurrence, I will use multiplexing if its available when opening a single file to ensure a non-glob is always opened correctly

        useShell = len(self.argz["paths"]) > 1 or len(getMultiplexingArgs()) == 0 #i.e. isMultipleFiles || isMultiplexingDisabled

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




#sets the view's buffer to the remote file's contents
class openFileOverSshTextCommand(sublime_plugin.TextCommand):

    #an edit object is required for modifying a view/buffer and a text command is the only valid way to get one in sublime text 3

    def run(self, edit):

        if self.view.id() in viewToShell:

            txt = viewToShell[self.view.id()].readFile(self.view.settings().get("ssh_path"))
            del viewToShell[self.view.id()] #remove ref so the shell can close

        else:
            p = subprocess.Popen(["ssh", *getMultiplexingArgs(), self.view.settings().get("ssh_server"), "cat " + shlex.quote(self.view.settings().get('ssh_path'))], stdout=subprocess.PIPE, startupinfo=getStartupInfo())
            txt, _ = p.communicate()

        self.view.set_encoding("UTF-8")
        self.view.replace(edit, sublime.Region(0, self.view.size()), str(txt, "UTF-8", "ignore"))

#takes care of writing the file to the remote location and keeping track of modifications
class openFileOverSshEventListener(sublime_plugin.ViewEventListener):

    def __init__ (self, view):

        self.view = view
        self.diffRef = ""
        self.viewName = True #name has to change each time its set

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
        self.view.set_scratch(True) #on_mod sets this to false


    def on_load(self):

        self.view.run_command("open_file_over_ssh_text") #open dat remote file

        self.view.sel().clear() #erase selections (the whole view will be selected, idk why)
        self.view.sel().add(sublime.Region(0, 0)) #put cursor on first line (default sublime behavior when a normal file is opened)

        self.diffRef = self.view.substr(sublime.Region(0, self.view.size())) #save the contents of the buffer in order to mimic sublime's incremental diff on a normal file

        self.doHacks()

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

        p = subprocess.Popen(["ssh", *getMultiplexingArgs(), self.view.settings().get("ssh_server"), "cat > " + shlex.quote(self.view.settings().get('ssh_path'))], stdin=subprocess.PIPE, startupinfo=getStartupInfo()) #ssh cp stdin to remote file
        p.communicate(self.view.substr(sublime.Region(0, self.view.size())).encode("UTF-8")) #set stdin to the buffer contents

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
