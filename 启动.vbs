' EVE 工业计算器 —— 无窗口启动（不显示 DOS 窗口）
' 用 pythonw.exe 启动，控制台不会出现；日志写在 logs\ 文件夹里。
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
' 0 = 隐藏窗口；False = 不等待
sh.Run cmd, 0, False
