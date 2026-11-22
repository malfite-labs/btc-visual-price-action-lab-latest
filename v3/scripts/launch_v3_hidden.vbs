Option Explicit

Dim shell, fso, root, launcher, powershell, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(WScript.ScriptFullName)
launcher = fso.BuildPath(root, "scripts\launch_v3.ps1")
powershell = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")
command = Chr(34) & powershell & Chr(34) & " -NoProfile -WindowStyle Hidden -File " & Chr(34) & launcher & Chr(34)

' Hide the shell host. The Tkinter console is launched separately with pythonw.
shell.Run command, 0, False
