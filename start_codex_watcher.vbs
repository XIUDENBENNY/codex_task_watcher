Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
batPath = scriptDir & "\start_codex_watcher.bat"

' 0 = hidden window, False = do not wait
shell.Run """" & batPath & """ --tray", 0, False
