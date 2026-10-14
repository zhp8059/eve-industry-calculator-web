' EVE industry calculator - silent start (no console window)
' Uses pythonw.exe so no DOS window appears; logs go to the logs folder.
Option Explicit
Dim fso, sh, root, py, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = root
If fso.FileExists(root & "\runtime\pythonw.exe") Then
  py = """" & root & "\runtime\pythonw.exe"""
ElseIf fso.FileExists(root & "\runtime\python.exe") Then
  py = """" & root & "\runtime\python.exe"""
Else
  py = "pythonw"
End If
cmd = py & " -m app.main"
' 0 = hidden window, False = do not wait
sh.Run cmd, 0, False
