' Launch the ACR telemetry logger with no console window.
' Intended for the Windows Startup folder (shell:startup).
' Output goes to runs\logger.log rather than a terminal.

Dim shell, here
Set shell = CreateObject("WScript.Shell")
here = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
shell.Run """" & here & "\start-logger.cmd""", 0, False
