import os
import sublime
import string
import random
import tempfile
import subprocess
import sublime_plugin


WINDOWS_LINE_ENDING = b'\r\n'
UNIX_LINE_ENDING = b'\n'

settingsFile = "SublimeOpenFileOverSSH.sublime-settings"
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

#handles the input pallet's ssh shell
class SshShell():

    seekingString = b"A random string for seeking"

    def __init__(self, userAndServer):

        self.shell = subprocess.Popen(['ssh', "-tt", userAndServer], stdin=subprocess.PIPE, stdout=subprocess.PIPE, startupinfo=getStartupInfo())
        self.shell.stdout.readline() #run it

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
            if lines[-1].startswith(seekingString):
                break;

        return [x.decode() for x in lines[1:-1]]

    def readFile(self, filePath):

        if filePath[0] not in ["/", "~"]: #path needs to be absolute because the current directory can be anywhere
            filePath = "~/" + filePath

        filePath = filePath.encode()
        seekingString = self.getSeekingString()
        self.shell.stdin.write(b"cat " + filePath + b"; echo -e '\\n" + seekingString + b"'\n") #need the \n so seeking string is on its own line
        self.shell.stdin.flush()

        lines = []

        while True:

            lines.append(self.shell.stdout.readline())
            if lines[-1].startswith(seekingString):
                break;

        return b"".join(lines[1:-1])[0:-2] #remove \n added by echo (two bytes in UTF-8)

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

        return "@" in text and ":" in text

    #gray placeholder text
    def placeholder(self):

        return "user@remote.server:"

    #previous value
    def initial_text(self):

        return sublime.load_settings(settingsFile).get("server", "")

    #syntax check
    def preview(self, text):

        if not self.isSyntaxOk(text):
            return "invalid syntax"

        return "syntax valid"

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

        sublime.load_settings(settingsFile).set("server", text)
        sublime.save_settings(settingsFile)

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

#inputer pallet path input
class pathInputHandler(sublime_plugin.ListInputHandler):

    def __init__(self, argz, oldPath = None):

        super().__init__()

        if not "path" in argz:
            argz["path"] = []

        if oldPath == None:
            oldPath = sublime.load_settings(settingsFile).get("path", []) #load once here because confirm overwrites all the paths

        self.argz = argz
        self.ssh = argz["sshShell"]
        self.oldPath = oldPath

    @staticmethod
    def isFolder(value):

        return value.endswith("/")

    @staticmethod
    def isGlob(value):

        return value == "*"

    #ls, glob, and initial selection
    def list_items(self):

        files = self.ssh.runCmd("/bin/ls -1Lp")

        for file in files:
            if not self.isFolder(file):
                files.append("*")
                break

        try:
            files = (files, files.index(self.oldPath[len(self.argz["path"])])) #default selection
        except (ValueError, IndexError):
            pass

        return files

    #gray placeholder text
    def placeholder(self):

        return "file"

    #folder, file, or glob
    def preview(self, value):

        if self.isFolder(value):
            return "Enter Folder"
        elif self.isGlob(value):
            return "Input Glob"
        else:
            return "Open File"

    #save value
    def confirm(self, value):

        self.argz["path"].append(value)

        sublime.load_settings(settingsFile).set("path", self.argz["path"])
        sublime.save_settings(settingsFile)

        if self.isFolder(value):
            self.ssh.runCmd("cd " + value)
        elif not self.isGlob(value):
            self.argz["paths"] = ["".join(self.argz["path"])]

    #cd ..
    def cancel(self):

        if len(self.argz["path"]) > 0 and not self.isGlob(self.argz["path"].pop()):
            self.ssh.runCmd("cd ..")

    #continue if folder or glob
    def next_input(self, args):

        if self.isFolder(args["path"]):
            return pathInputHandler(self.argz, self.oldPath)
        if self.isGlob(args["path"]):
            return globInputHandler(self.argz)
        return None


#input pallet glob input
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
        return self.ssh.runCmd("/usr/bin/ls -1Lpd {} 2> /dev/null | egrep -v /$".format(text)) #ignore ls's error when nothing is found

    #gray placeholder text
    def placeholder(self):

        return "*.c h*.h"

    #previous value
    def initial_text(self):

        return sublime.load_settings(settingsFile).get("glob", "")

    #syntax check
    def preview(self, text):

        if not self.isSyntaxOk(text):
            return "invalid syntax"

        return "syntax valid"

    #check server
    def validate(self, text):

        if self.isSyntaxOk(text):

            if len(self.getMatchingFiles(text)) > 0:
                return True

            sublime.error_message("No files were found matching the pattern{} '{}'".format("s" if len(text.split(" ")) > 1 else "", text)) #the dialog looks ugly, but I can't think of a better way

        return False

    #save value
    def confirm(self, text):

        sublime.load_settings(settingsFile).set("glob", text)
        sublime.save_settings(settingsFile)

        self.argz["paths"] = ["".join(self.argz["path"][:-1] + [file]) for file in self.getMatchingFiles(text)]

    #pop()
    def cancel(self):

        self.argz["path"].pop() #pop off the * that got us here

    #file selection
    def next_input(self, args):

        return None


#the command that is run from the command pallet
class openFileOverSshCommand(sublime_plugin.WindowCommand):

    def run(self, **args):

        for path in self.argz["paths"]:

            #open a temp file with the correct extension
            #I can't just make a new file because I want the syntax to be set based on the remote file's extension
            ext = path.rfind(".")
            ext = path[ext:] if ext != -1 else ""
            file = tempfile.NamedTemporaryFile(suffix=ext)
            view = self.window.open_file(file.name)

            viewToShell[view.id()] = self.argz["sshShell"]

            #used to set the view/buffer path which prevents the save dialog from showing up on file save
            #It also looks nice when you right click on the file name at the top of the window
            #...well as nice as it can ;)
            view.settings().set("ssh_fake_path", self.argz["server"] + "/" + path)
            view.settings().set("ssh_server", self.argz["server"])
            view.settings().set("ssh_path", path)
            view.set_status("ssh_true", "Saving to " + self.argz["server"] + path)

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

            txt = viewToShell[self.view.id()].readFile(self.view.settings().get("ssh_path")).replace(WINDOWS_LINE_ENDING, UNIX_LINE_ENDING)
            del viewToShell[self.view.id()] #remove ref so the shell can close

        else:
            p = subprocess.Popen(["ssh", self.view.settings().get("ssh_server"), "cat", self.view.settings().get("ssh_path")], stdout=subprocess.PIPE, startupinfo=getStartupInfo())
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
        self.view.set_name(str(self.viewName)) #Sets the name so retarget() will behave (see above)
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

        #this gets called after the save dialog has exited when this file is not view.retarget() (see openFileOverSshCommand.run)

        #two ways I could write changes to remote file
        #
        #1. (what I am currently doing), uses pre_save
        #   use ssh and copy stdin to the remote file i.e. erase remote file with stdin
        #   stdin is set to the buffer contents using the argument to popen.communicate()
        #
        #2. (not sure which is better), would use post_save
        #   after the file is saved to the temp file, scp to the temp file to the remote location
        #   just like anyone would do normally when they wanted to copy a local file to a remote location

        p = subprocess.Popen(['ssh', self.view.settings().get("ssh_server"), "cp /dev/stdin " + self.view.settings().get("ssh_path")], stdin=subprocess.PIPE, startupinfo=getStartupInfo()) #ssh cp stdin to remote file
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
