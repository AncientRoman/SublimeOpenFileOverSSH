# SublimeOpenFileOverSSH v1.0
A Sublime Text 3 Plugin that allows a file to be opened on a Remote Machine over ssh and seamlessly edited and saved back to the remote machine

## Installation
Clone or dowload this repo into the Sublime Packages folder.<br>
This plugin should work on MacOS, Linux, and Windows.

## Usage
Run the Open File Over SSH command from the command palette.<br>
To do that, open Sublime's Command Palette (ctrl-shft-p), type in Open File Over SSH, press enter, type in the scp-like path to your file, and press enter.

## How it Works
When a remote file is opened, the contents of the file is copied into the buffer.<br>
When the file is saved, the buffer is copied back into the remote file, and sublime is given a temporary file to save to which is later deleted.<br>
The file transfering is done using Popen's stdin and stdout to ssh, not scp.

## Important
You should setup ssh public/private key login to your remote machine.<br>
It may work without it but I haven't tried.
