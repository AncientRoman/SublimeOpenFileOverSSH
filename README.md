# SublimeOpenFileOverSSH v1.3.0
A Sublime Text 4 Plugin that allows a file to be opened on a Remote Machine over ssh and seamlessly edited and saved back to the remote machine

## Installation
Clone or download this repo into the Sublime Packages folder.<br>
This plugin should work on MacOS, Linux, and Windows.

## Usage
Trigger the remote file selection using one of these options.
* Press _cmd-shift-o_ or _wndws-shift-o_
* Use the _File > Open via SSH_ menu item
* Run the _Open File Over SSH_ command from the command palette

Once triggered, input the server path and browse/open remote files as follows.
1. Type in the scp-like path to your server (`user@server.ext:`), and press enter
2. Once the server is validated and connected, a list input will appear which allows you to choose a folder or a file
3. Continue browsing the file system on your server until you find the file you want to open
4. Or select the star (\*) option to enter a pattern like `\*.c \*.h`
5. Enjoy finally being able to edit a remote file in sublime (2505 students amirite)

## How it Works
When a remote file is opened, the contents of the file is copied into the buffer.<br>
When the file is saved, the buffer is copied back into the remote file, and sublime is given a temporary file to save to which is later deleted.<br>
The file transferring is done using Popen's stdin and stdout to ssh, not scp.

The file selection is done by opening an ssh connection after the server is input and `ls` is used to populate the folder/file list on demand.

## Important
You should setup ssh public/private key login to your remote machine.<br>
It may work without it but I haven't tried.

## Settings
Ssh's multiplexing feature is used to speed up connection and authentication when browsing and saving files in short succession.<br>
The default connection keep alive time is 5 minutes and you can change this by adding the `multiplexing` key in the .sublime-settings file for this package.<br>
The .sublime-settings file will be created inside of the User folder in the Sublime Packages folder after you connect to your first server.<br>
The `multiplexing` key accepts keep alive (ControlPersist) times in the `120s` or `5m` formats.<br>
If your system doesn't support multiplexing or you'd like to disable it for security reasons, set `multiplexing` to `false`.


## What's New?
v1.3: Support for Sublime Text 4<br>
v1.2.2: Fixed files in subfolders being opened when a glob was used<br>
v1.2.1: Fixed crashing sublime when a remote file was opened that did not have a new line at the end<br>
v1.2: The wildcard (\*) option opens a text input that accepts one or more glob patterns such as \*.c h\* and opening multiple files is wayyyy faster<br>
v1.1: Added a file browser like interface to the Open File File Over SSH command in the command pallet<br>
v1.0: First version with Windows support
