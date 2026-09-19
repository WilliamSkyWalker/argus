### TC-WIN-001  在记事本中输入并验证文本
- **Priority**: P0
- **Automation**: auto
- **Platform**: windows
- **Reset before**: relaunch
- **Feature**: Windows 记事本自动化演示
- **Steps**:
  Given 一个全新启动的空白记事本窗口已打开
  When 用户输入文本 "Argus Windows runner verified"
  Then 编辑区域第一行显示文本 "Argus Windows runner verified"
  When 用户按下 Ctrl+S 保存快捷键
  Then 弹出"另存为"对话框，标题栏显示"另存为"或"Save As"
  When 用户先将保存位置切换到"桌面"（Desktop），确认地址栏/导航栏已选中桌面后，在文件名输入框中输入 "argus_win_demo.txt"，最后点击"保存"（Save）按钮
  Then 对话框已关闭，记事本标题栏显示文件名"argus_win_demo.txt"（不再是"Untitled"），编辑区仍可见之前输入的文本
  When 用户点击关闭记事本窗口（标题栏右上角关闭按钮）
  Then 记事本窗口已完全关闭（文件已保存无未保存更改，关闭时不应再弹出确认对话框），屏幕上不再显示记事本编辑区或标题栏
