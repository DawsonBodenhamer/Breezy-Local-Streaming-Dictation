Option Explicit

Dim shell, command, exitCode
Set shell = CreateObject("WScript.Shell")
command = "powershell.exe -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File """ _
    & CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName) _
    & "\supervisor.ps1"" run"
exitCode = shell.Run(command, 0, True)
WScript.Quit exitCode
