import sublime_plugin
import sublime
import tempfile
import subprocess
import os
from time import sleep

settingsFile = "SublimeOpenFileOverSSH.sublime-settings"
isWindows = (sublime.platform() == "windows")

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

    def isAlive(self):
        return self.shell.poll() == None

    def runCmd(self, cmd = ""):

        if cmd != "":
            cmd += "; "
        cmd = cmd.encode()
        self.shell.stdin.write(cmd + b"echo '" + self.seekingString + b"'\n")
        self.shell.stdin.flush()

        lines = []

        while True:

            lines.append(self.trimNewLine(self.shell.stdout.readline()))
            if lines[-1].startswith(self.seekingString):
                break;

        return [x.decode() for x in lines[1:-1]]

    def close(self):

        self.shell.stdin.write(b"exit\n")
        self.shell.stdin.flush()
        self.shell.stdin.close()
        print("Waiting for ssh to exit")
        self.shell.wait()
        print("ssh finished with return code %d" % self.shell.returncode)

#input pallet server input
class serverInputHandler(sublime_plugin.TextInputHandler):

    def __init__(self, argz):

        super().__init__()

        self.argz = argz

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

    def cancel(self):
        self.ssh.close()

    #file selection
    def next_input(self, args):

        self.argz["server"] = args["server"][:-1]

        return pathInputHandler(self.argz, self.ssh)

#inputer pallet path input
class pathInputHandler(sublime_plugin.ListInputHandler):

    def __init__(self, argz, sshShell):

        super().__init__()

        if not "path" in argz:
            argz["path"] = ""

        self.argz = argz
        self.ssh = sshShell

    @staticmethod
    def isFolder(value):
        return value.endswith("/")

    def list_items(self):
        return self.ssh.runCmd("ls -1p")

    #gray placeholder text
    def placeholder(self):
        return "file"

    #folder or file
    def preview(self, value):
        return "Enter Folder" if self.isFolder(value) else "Open File"

    def cancel(self):
        self.ssh.runCmd("cd ..")

    #continue if folder
    def next_input(self, args):

        self.argz["path"] += args["path"]

        if not self.isFolder(args["path"]):

            self.ssh.close()
            return None

        self.ssh.runCmd("cd " + args["path"])

        return pathInputHandler(self.argz, self.ssh)


#the command that is run from the command pallet
class openFileOverSshCommand(sublime_plugin.WindowCommand):

    def run(self, **args):

        #open a temp file with the correct extension
        #I can't just make a new file because I want the syntax to be set based on the remote file's extension
        ext = self.argz["path"].rfind(".")
        ext = self.argz["path"][ext:] if ext != -1 else ""
        file = tempfile.NamedTemporaryFile(suffix=ext)
        view = self.window.open_file(file.name)

        #used to set the view/buffer path which prevents the save dialog from showing up on file save
        #It also looks nice when you right click on the file name at the top of the window
        #...well as nice as it can ;)
        view.settings().set("ssh_fake_path", self.argz["server"] + "/" + self.argz["path"])
        view.settings().set("ssh_server", self.argz["server"])
        view.settings().set("ssh_path", self.argz["path"])
        view.set_status("ssh_true", "Saving to " + self.argz["server"] + self.argz["path"])

        file.close()

    def input(self, args):

        self.argz = {}
        return serverInputHandler(self.argz)

#sets the view's buffer to the remote file's contents
class openFileOverSshTextCommand(sublime_plugin.TextCommand):

    #an edit object is required for modifying a view/buffer and a text command is the only valid way to get one in sublime text 3

    def run(self, edit):

        p = subprocess.Popen(['ssh', self.view.settings().get("ssh_server"), "cat", self.view.settings().get("ssh_path")], stdout=subprocess.PIPE, startupinfo=getStartupInfo())
        txt, _ = p.communicate()

        self.view.set_encoding('UTF-8')
        self.view.replace(edit, sublime.Region(0, self.view.size()), str(txt, "UTF-8"))

#takes care of writing the file to the remote location and keeping track of modifications
class openFileOverSshEventListener(sublime_plugin.ViewEventListener):

    def __init__ (self, view):

        self.view = view
        self.diffRef = ""

    @classmethod
    def is_applicable(cls, settings):
        return settings.has("ssh_server") and settings.has("ssh_path")

    @classmethod
    def applies_to_primary_view_only(cls):
        return True;


    def doHacks(self):

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
