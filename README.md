# SublimeOpenFileOverSSH
A Sublime Text 3 Plugin that allows a file to be opened on a Remote Machine over ssh and seamlessly edited and saved back to the remote machine

## Installation
Clone or dowload this repo into the Sublime Packages folder.<br>
This plugin should work on MacOS, Linux, and Windows.

## Usage
Run the Open File Over SSH command from the command palette.
1. Open Sublime's Command Palette (ctrl-shft-p)
2. Type in Open File Over SSH, press enter
3. Type in the scp-like path to your server (user@server.ext:), and press enter
4. Once the server is validated, a list input will appear which allows you to choose a folder or a file
5. Continue browsing the file system on your server until you find the file you want to open
6. Select the file
7. Enjoy finally being able to edit a remote file in sublime (2505 students amirite)

## How it Works
When a remote file is opened, the contents of the file is copied into the buffer.<br>
When the file is saved, the buffer is copied back into the remote file, and sublime is given a temporary file to save to which is later deleted.<br>
The file transfering is done using Popen's stdin and stdout to ssh, not scp.

The file selection is done by opening an ssh connection when the server is given and `ls` is used to populate the folder/file list on demand

## Important
You should setup ssh public/private key login to your remote machine.<br>
It may work without it but I haven't tried.
