import sublime_plugin
import sublime
import tempfile
import subprocess
import os
from time import sleep

settingsFile = "ssh.sublime-settings"
isWindows = (sublime.platform() == "windows")


def getStartupInfo():

    #On Windows, the command shell is opened while the Popen command is running and this fixes that
    startupinfo = None
    if isWindows:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    return startupinfo


#gets the text input from the command pallet
class serverAndPathInputHandler(sublime_plugin.TextInputHandler):

    def placeholder(self):
        return "user@remote.server:path/to/file" #the syntax for ssh, scp, and rsync

    def initial_text(self):
        return sublime.load_settings(settingsFile).get("serverAndPath", "")

    def preview(self, text):

        #make sure the syntax is followed

        if not "@" in text or not ":" in text:
            return "invalid syntax"

        return "syntax valid"

    def validate(self, text):

        #is the syntax ok?
        try:
            server, path = text.split(":")
        except:
            return False


        #check if the file exists
        p = subprocess.Popen(['ssh', server, "test", "-f", path, "&>", "/dev/null"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=getStartupInfo()) #ssh file test
        p.communicate()

        if(p.returncode != 0):

            sublime.error_message("The specified file, {}, was not found".format(path)) #the dialog looks ugly, but I can't think of a better way
            return False

        return True

    def confirm(self, text):

        sublime.load_settings(settingsFile).set("serverAndPath", text)
        sublime.save_settings(settingsFile)

#the command that is run from the command pallet
class openFileOverSshCommand(sublime_plugin.WindowCommand):

    def run(self, server_and_path):

        server, path = server_and_path.split(":") #get the server/user and the path

        #open a temp file with the correct extension
        #I can't just make a new file because I want the syntax to be set based on the remote file's extension
        ext = path.rfind(".")
        ext = path[ext:] if ext != -1 else ""
        file = tempfile.NamedTemporaryFile(suffix=ext)
        view = self.window.open_file(file.name)

        #used to set the view/buffer path which prevents the save dialog from showing up on file save
        #It also looks nice when you right click on the file name at the top of the window
        #...well as nice as it can ;)
        view.settings().set("ssh_fake_path", server + "/" + path)
        view.settings().set("ssh_server", server)
        view.settings().set("ssh_path", path)
        view.set_status("ssh_true", "Saving to " + server_and_path)

        file.close()

    def input(self, args):
        return serverAndPathInputHandler()

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
        #return True
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
