"""Qt Widgets shell. Network and device operations run off the GUI thread."""
import json
from html import escape
from pathlib import Path
import sys
import threading


def main():
    # Frozen native-messaging entry runs without loading Qt or opening a window.
    flag = next((item for item in ('--native-host','--native-host-uninstall') if item in sys.argv), None)
    if flag:
        from saygo.integrations.browser_bridge import main as host_main
        sys.argv = [sys.argv[0], 'host' if flag == '--native-host' else 'uninstall', *sys.argv[sys.argv.index(flag)+1:]]
        return host_main()
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    app.setApplicationName('Saygo Desktop')
    app.setApplicationDisplayName('Saygo Desktop')
    app.setOrganizationName('Saygo')
    window = make_window()
    window.show()
    if '--smoke-test' in sys.argv:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(300, app.quit)
    return app.exec()


def make_window():
    from PySide6.QtCore import Qt, QThread, Signal
    from PySide6.QtGui import QPixmap, QDesktopServices
    from PySide6.QtCore import QUrl
    from PySide6.QtWidgets import (QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,
        QTabWidget,QLineEdit,QPushButton,QLabel,QPlainTextEdit,QCheckBox,QComboBox,
        QListWidget,QListWidgetItem,QAbstractItemView,QSplitter,QFileDialog,QInputDialog,QMessageBox,
        QDialog,QTextBrowser,QFrame)
    from saygo.desktop.model import load_settings, load_key, save_settings, VisionModel
    from saygo.desktop.runner import TaskLoop
    from saygo.runtime import interactive
    from saygo.devices import control, service

    class Composer(QPlainTextEdit):
        submitted = Signal()

        def keyPressEvent(self, event):
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.submitted.emit()
            else:
                super().keyPressEvent(event)

    class Worker(QThread):
        result = Signal(object)
        update = Signal(object)
        failed = Signal(str)
        def __init__(self, function):
            super().__init__(); self.function = function
        def run(self):
            try:
                self.result.emit(self.function(self.update.emit))
            except Exception as exc:
                self.failed.emit(str(exc))

    class Window(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle('Saygo'); self.resize(1220,840); self.setMinimumSize(900,640)
            self.worker = None; self.task_id = None; self.task_state = None
            self.pause_event = threading.Event(); self.stop_event = threading.Event()
            self.buttons = []; self.dialogs = {}; self.last_transcript = ''
            shell=QWidget(); self.setCentralWidget(shell)
            body=QHBoxLayout(shell); body.setContentsMargins(0,0,0,0); body.setSpacing(0)
            self.sidebar=QFrame(); self.sidebar.setObjectName('sidebar'); self.sidebar.setFixedWidth(230)
            self.side=QVBoxLayout(self.sidebar); self.side.setContentsMargins(18,24,18,18); self.side.setSpacing(12)
            brand=QLabel('◉  Saygo'); brand.setObjectName('brand'); self.side.addWidget(brand)
            self.button('＋  新对话',self.new_chat,self.side)
            self.side.addWidget(QLabel('最近对话'))
            self.tasks=QListWidget(); self.tasks.setObjectName('history'); self.side.addWidget(self.tasks,1)
            self.tasks.itemClicked.connect(lambda item:self.load_task())
            self.button('设备与连接',lambda:self.dialogs['设备连接'].show(),self.side)
            self.button('模型设置',lambda:self.dialogs['模型设置'].show(),self.side)
            body.addWidget(self.sidebar)
            self.chat=QWidget(); body.addWidget(self.chat,1)
            self.settings_page(); self.devices_page(); self.task_page()
            self.setStyleSheet('''QMainWindow,QWidget { background:#ffffff; color:#242424; font-size:14px; font-family:"Segoe UI","Microsoft YaHei UI",sans-serif; }
                QFrame#sidebar,QFrame#sidebar QLabel { background:#f7f7f8; }
                QLabel#brand { font-size:25px; font-weight:700; padding-bottom:18px; }
                QLabel#title { font-size:25px; font-weight:600; }
                QLabel#muted { color:#777777; font-size:12px; }
                QLineEdit,QPlainTextEdit,QComboBox { background:white; border:1px solid #dedede; border-radius:10px; padding:10px; selection-background-color:#d3e9df; }
                QPushButton { background:#f2f2f2; border:0; border-radius:8px; padding:10px 12px; text-align:left; }
                QPushButton:hover { background:#e7e7e7; } QPushButton:disabled { color:#aaaaaa; }
                QPushButton#send { background:#202020; color:white; border-radius:18px; text-align:center; }
                QPushButton#send:disabled { background:#b5b5b5; }
                QListWidget { border:0; background:#f7f7f8; outline:0; }
                QListWidget::item { padding:12px 6px; border-radius:6px; }
                QListWidget::item:selected { background:#e7e7e9; color:#202020; }
                QTextBrowser { border:0; background:white; padding:12px; }
                QStatusBar { color:#777777; font-size:12px; border-top:1px solid #eeeeee; }
                QFrame#composer { border:1px solid #dedede; border-radius:18px; }
                QFrame#composer QPlainTextEdit { border:0; }
            ''')
            try:
                data = load_settings(); self.base.setText(data['base_url']); self.model.setText(data['model'])
                self.remember.setChecked(data.get('remember',False)); self.key.setText(load_key(data))
            except Exception as exc:
                self.statusBar().showMessage(str(exc))
            self.refresh_sessions(); self.refresh_tasks()

        def page(self, name, heading, description):
            dialog=QDialog(self); dialog.setWindowTitle(name); dialog.resize(820,560)
            self.dialogs[name]=dialog
            layout=QVBoxLayout(dialog); layout.setContentsMargins(24,24,24,24); layout.setSpacing(18)
            title=QLabel(heading); title.setObjectName('title'); layout.addWidget(title)
            subtitle=QLabel(description); subtitle.setWordWrap(True); layout.addWidget(subtitle)
            return layout

        def button(self, text, callback, layout):
            button=QPushButton(text); button.clicked.connect(lambda checked=False: callback())
            layout.addWidget(button); self.buttons.append(button); return button

        def settings_page(self):
            layout=self.page('模型设置','连接你的模型','支持带图像输入的 OpenAI 兼容接口。任务截图会发送至你填写的服务。')
            form=QFormLayout(); layout.addLayout(form)
            self.base=QLineEdit(); self.base.setPlaceholderText('https://服务地址/v1')
            self.model=QLineEdit(); self.model.setPlaceholderText('模型名称')
            self.key=QLineEdit(); self.key.setEchoMode(QLineEdit.EchoMode.Password)
            self.remember=QCheckBox('在本机系统密钥存储中记住 API Key')
            form.addRow('API 地址',self.base); form.addRow('模型',self.model); form.addRow('API Key',self.key); form.addRow('',self.remember)
            row=QHBoxLayout(); layout.addLayout(row)
            self.button('保存配置',self.save_model,row); self.button('测试图像连接',self.test_model,row)
            self.button('删除已保存的 Key',self.delete_key,row)
            layout.addWidget(QLabel('不勾选“记住”时，Key 仅在本次运行内存中使用。无需配置 .env。'))
            layout.addStretch()

        def model_values(self):
            return {'base_url':self.base.text().strip(),'model':self.model.text().strip()}, self.key.text()

        def save_model(self):
            settings,key=self.model_values(); remember=self.remember.isChecked()
            self.job(lambda emit: (save_settings(settings,key,remember), {'message':'模型配置已保存'})[1])

        def test_model(self):
            settings,key=self.model_values()
            self.job(lambda emit: VisionModel(settings,key).test())

        def delete_key(self):
            from saygo.desktop.model import secure_backend, credential_id
            settings,key=self.model_values()
            def remove(emit):
                backend=secure_backend(); ident=credential_id(settings)
                if backend.get_password('Saygo Desktop',ident): backend.delete_password('Saygo Desktop',ident)
                save_settings(settings,'',False)
                return {'message':'已删除系统密钥；当前输入框内容仍仅在内存中'}
            self.remember.setChecked(False); self.job(remove)

        def devices_page(self):
            layout=self.page('设备连接','选择操作目标','命名会话与 CLI、MCP 共用。浏览器使用扩展连接；桌面窗口支持 Windows 和 macOS。')
            form=QFormLayout(); layout.addLayout(form)
            self.platform=QComboBox(); self.platform.addItems(['browser','desktop','android','ios'])
            self.session=QLineEdit('desktop-web'); self.target=QLineEdit(); self.target.setPlaceholderText('桌面：窗口标题或 App 名；手机：设备 ID；浏览器：留空')
            self.page_id=QComboBox(); self.page_id.addItem('先连接浏览器，再选择网页',None)
            self.background=QCheckBox('Windows / macOS 后台输入（macOS 需已打开窗口；不支持的动作会报错）')
            form.addRow('平台',self.platform); form.addRow('会话名称',self.session); form.addRow('目标',self.target); form.addRow('浏览器页面',self.page_id); form.addRow('',self.background)
            row=QHBoxLayout(); layout.addLayout(row)
            self.button('扫描设备 / 窗口',lambda:self.job(lambda emit:control.discover('all')),row)
            self.button('连接 / 选择页面',self.connect_device,row)
            self.button('安装浏览器桥接',self.install_bridge,row)
            self.button('查看已保存会话',lambda:self.job(lambda emit:{'sessions':control.sessions()}),row)
            self.device_output=QPlainTextEdit(); self.device_output.setReadOnly(True); layout.addWidget(self.device_output)

        def connect_device(self):
            from argparse import Namespace
            kind=self.platform.currentText(); target=self.target.text().strip()
            args=Namespace(platform=kind,session=self.session.text().strip(),app=target or None,
                device=target or None,server_url=None,team_id=None,background=self.background.isChecked(),
                process_name=None,launch=None,new_window=False,new_window_args=[],backend='extension' if kind=='browser' else None,
                bridge_directory=None,page_id=self.page_id.currentData())
            self.job(lambda emit:control.connect(args))

        def install_bridge(self):
            def install(emit):
                import base64,hashlib,shutil
                from saygo.integrations.browser_setup import setup
                from saygo.integrations.browser_bridge import atomic_json
                from saygo.platforms import device_session as ds
                source=Path(__file__).parent/'assets/extension'
                if not source.exists(): source=Path(__file__).resolve().parents[2]/'extensions/saygo-browser'
                if not source.exists(): source=Path(sys.prefix)/'share/saygo/browser-extension'
                manifest=json.loads((source/'manifest.json').read_text())
                eid=''.join(chr(97+int(c,16)) for c in hashlib.sha256(base64.b64decode(manifest['key'])).hexdigest()[:32])
                result=setup(eid)
                target=Path(result['directory'])/'extension'; shutil.copytree(source,target,dirs_exist_ok=True)
                ds.STATE_DIR.parent.mkdir(parents=True,exist_ok=True); atomic_json(ds.STATE_DIR.parent/'browser-bridge.json',result)
                display=str(target)
                if result.get('host')=='windows':
                    from saygo.devices.mobile_host import wsl_path
                    display=wsl_path(target,windows=True)
                return {'message':'打开 chrome://extensions，开启开发者模式并加载以下目录，然后在扩展中点击 Connect。','extension_directory':display,**result}
            self.job(install)

        def task_page(self):
            layout=QVBoxLayout(self.chat); layout.setContentsMargins(30,20,30,16); layout.setSpacing(12)
            header=QHBoxLayout(); layout.addLayout(header)
            title=QLabel('Saygo'); title.setObjectName('title'); header.addWidget(title); header.addStretch()
            self.button('操作画面',lambda:self.preview_dialog.show(),header)
            self.button('导出对话',self.export_task,header)
            self.task_status=QLabel('新对话'); self.task_status.setObjectName('muted'); layout.addWidget(self.task_status)
            self.timeline=QTextBrowser(); self.timeline.setOpenExternalLinks(False); layout.addWidget(self.timeline,1)
            self.preview_dialog=QDialog(self); self.preview_dialog.setWindowTitle('最新操作画面'); self.preview_dialog.resize(860,600)
            preview_layout=QVBoxLayout(self.preview_dialog)
            self.preview=QLabel('执行任务后，这里显示最新观察画面'); self.preview.setMinimumSize(400,260)
            self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter); preview_layout.addWidget(self.preview)
            actions=QHBoxLayout(); layout.addLayout(actions)
            self.pause_button=QPushButton('暂停'); self.pause_button.clicked.connect(self.request_pause); actions.addWidget(self.pause_button)
            self.stop_button=QPushButton('停止'); self.stop_button.clicked.connect(self.request_stop); actions.addWidget(self.stop_button)
            self.resolve_button=self.button('核对操作结果',self.resolve_task,actions); actions.addStretch()
            self.button('选择操作目标',lambda:self.targets_dialog.show(),actions)
            self.targets_dialog=QDialog(self); self.targets_dialog.setWindowTitle('选择操作目标'); self.targets_dialog.resize(420,400)
            targets=QVBoxLayout(self.targets_dialog); targets.addWidget(QLabel('选择本次任务可以操作的会话（可多选）'))
            self.sessions=QListWidget(); self.sessions.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection); targets.addWidget(self.sessions)
            self.button('刷新列表',self.refresh_sessions,targets)
            self.button('完成',self.targets_dialog.accept,targets)
            frame=QFrame(); frame.setObjectName('composer'); composer=QVBoxLayout(frame); layout.addWidget(frame)
            self.goal=Composer(); self.goal.submitted.connect(self.send_message)
            self.goal.setPlaceholderText('给 Saygo 发送任务…'); self.goal.setFixedHeight(80); composer.addWidget(self.goal)
            bottom=QHBoxLayout(); composer.addLayout(bottom)
            self.target_label=QLabel('请先选择操作目标'); self.target_label.setObjectName('muted'); bottom.addWidget(self.target_label,1)
            self.send_button=self.button('发送  ↑',self.send_message,bottom); self.send_button.setObjectName('send')
            self.sessions.itemSelectionChanged.connect(self.update_target_label)
            hint=QLabel('Enter 发送 · Shift + Enter 换行 · Saygo 会根据截图操作你选择的设备'); hint.setObjectName('muted')
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter); layout.addWidget(hint)
            self.welcome()

        def welcome(self):
            self.last_transcript=''
            for button in (self.pause_button,self.stop_button,self.resolve_button):button.hide()
            self.timeline.setHtml('<div style="text-align:center;margin-top:100px"><h1>今天想完成什么？</h1>'
                '<p style="color:#777">把任务交给 Saygo，一起观察每一步。</p>'
                '<p style="color:#999">连接模型和设备后，在下方描述你的任务。</p></div>')

        def new_chat(self):
            if self.worker:return
            if self.task_state and self.task_state['status'] not in ('succeeded','cancelled'):
                QMessageBox.information(self,'任务尚未结束','请先停止当前任务，再开始新对话。'); return
            self.task_id=None; self.task_state=None; self.goal.clear(); self.preview.clear()
            self.task_status.setText('新对话'); self.welcome()

        def update_target_label(self):
            names=[item.text() for item in self.sessions.selectedItems()]
            self.target_label.setText(' · '.join(names) if names else '请先选择操作目标')

        def refresh_sessions(self):
            selected={item.text() for item in self.sessions.selectedItems()}
            self.sessions.clear()
            for row in control.sessions():
                if not row.get('disconnected') and not row.get('error') and (row.get('platform')!='browser' or row.get('backend')=='extension'):
                    item=QListWidgetItem(row['session']); self.sessions.addItem(item); item.setSelected(row['session'] in selected)
            self.update_target_label()

        def refresh_tasks(self):
            self.tasks.clear()
            for row in reversed(interactive.call('list')['tasks']):
                state=interactive.call('status',row['id'])
                if not state.get('desktop_goal'):continue
                item=QListWidgetItem(state['desktop_goal'].replace('\n',' ')[:28])
                item.setData(Qt.ItemDataRole.UserRole,row['id']); item.setToolTip(state['desktop_goal'])
                self.tasks.addItem(item)
                if row['id']==self.task_id:self.tasks.setCurrentItem(item)

        def send_message(self):
            if self.worker:return
            if not self.goal.toPlainText().strip():return
            if self.task_state and self.task_state['status'] in ('waiting_for_human','idle'):
                self.resume_task(self.goal.toPlainText().strip()); return
            if self.task_state and self.task_state['status']=='needs_review':
                QMessageBox.information(self,'需要核对','请先点击“核对操作结果”，确认上一次动作是否完成。'); return
            self.start_task()

        def start_task(self):
            settings,key=self.model_values(); goal=self.goal.toPlainText().strip()
            bindings={f'target_{i+1}':item.text() for i,item in enumerate(self.sessions.selectedItems())}
            if not bindings:
                self.targets_dialog.show(); self.statusBar().showMessage('选择至少一个操作目标，然后发送任务'); return
            if not settings['model'] or not key:
                self.dialogs['模型设置'].show(); self.statusBar().showMessage('请先填写模型和 API Key'); return
            self.goal.clear()
            def start(emit):
                loop=TaskLoop(VisionModel(settings,key)); state=loop.create(goal,bindings); emit(state)
                return loop.run(state['id'],self.pause_event,self.stop_event,emit)
            self.job(start,loop=True)

        def load_task(self):
            if self.worker:return
            item=self.tasks.currentItem()
            task_id=item.data(Qt.ItemDataRole.UserRole) if item else None
            if task_id: self.job(lambda emit:interactive.call('recover',task_id))

        def resume_task(self, note=None):
            if not self.task_id: return
            settings,key=self.model_values(); task_id=self.task_id
            if note is None:
                note,ok=QInputDialog.getText(self,'继续任务','人工操作或核对说明：',text='已核对，可以继续')
                if not ok or not note.strip(): return
            self.goal.clear()
            def resume(emit):
                loop=TaskLoop(VisionModel(settings,key)); state=loop.runtime.store.get(task_id)
                if state['status']=='waiting_for_human': loop.runtime.resume_task(task_id,note)
                else:
                    with loop.runtime.store.guard(task_id):
                        state=loop.runtime.store.get(task_id)
                        loop.runtime.store.save(state,'desktop_user_message',{'note':note})
                return loop.run(task_id,self.pause_event,self.stop_event,emit)
            self.job(resume,loop=True)

        def resolve_task(self):
            if not self.task_id: return
            outcome,ok=QInputDialog.getItem(self,'核对动作','根据实际结果选择：',['completed','not_executed'],0,False)
            if not ok:return
            note,ok=QInputDialog.getText(self,'核对证据','你观察到了什么？')
            if ok and note.strip():
                task_id=self.task_id; self.job(lambda emit:interactive.call('resolve',task_id,outcome=outcome,note=note))

        def export_task(self):
            if not self.task_id:return
            path,_=QFileDialog.getSaveFileName(self,'导出任务证据','saygo-task.zip','ZIP (*.zip)')
            if path:
                task_id=self.task_id; self.job(lambda emit:interactive.call('export',task_id,out=path))

        def request_pause(self):
            if self.worker:
                self.pause_event.set(); self.statusBar().showMessage('正在暂停：等待当前调用结束，之后不会派发新动作')
            elif self.task_id:
                task_id=self.task_id; self.job(lambda emit:interactive.call('handoff',task_id,instructions='用户请求人工接管'))

        def request_stop(self):
            if self.worker:
                self.stop_event.set(); self.statusBar().showMessage('正在停止：等待当前调用结束')
            elif self.task_id:
                task_id=self.task_id; self.job(lambda emit:interactive.call('cancel',task_id,note='用户停止'))

        def job(self, function, loop=False):
            if self.worker:return
            self.pause_event.clear(); self.stop_event.clear()
            self.goal.setEnabled(False); self.tasks.setEnabled(False)
            for button in self.buttons:button.setEnabled(False)
            self.pause_button.setEnabled(loop); self.stop_button.setEnabled(loop)
            self.worker=Worker(function); self.worker.result.connect(self.show_result); self.worker.update.connect(self.show_result)
            self.worker.failed.connect(self.show_error); self.worker.finished.connect(self.job_done)
            self.statusBar().showMessage('正在执行…'); self.worker.start()

        def job_done(self):
            worker=self.worker; self.worker=None; worker.deleteLater()
            self.goal.setEnabled(True); self.tasks.setEnabled(True)
            for button in self.buttons:button.setEnabled(True)
            self.pause_button.setEnabled(True);self.stop_button.setEnabled(True)
            self.refresh_sessions();self.refresh_tasks()

        def show_error(self,message):
            self.statusBar().showMessage(message)
            QMessageBox.warning(self,'操作未完成',message)
            if self.task_id:
                self.show_result(interactive.call('status',self.task_id))

        def show_result(self,value):
            if not isinstance(value,dict):return
            if value.get('mode')=='interactive':
                self.task_id=value['id']; self.task_state=value
                self.pause_button.setVisible(value['status']=='idle')
                self.stop_button.setVisible(value['status'] not in ('succeeded','cancelled'))
                self.resolve_button.setVisible(value['status']=='needs_review')
                statuses={'idle':'执行中' if self.worker else '可以继续','waiting_for_human':'等待你回复','needs_review':'需要核对操作结果','succeeded':'已完成','cancelled':'已停止'}
                self.task_status.setText(f"{statuses.get(value['status'],value['status'])} · 已记录 {value.get('cursor',0)} 步")
                observations=list(value.get('observations',{}).values())
                if observations:
                    pix=QPixmap(observations[-1]['path']); self.preview.setPixmap(pix.scaled(self.preview.size(),Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))
                else:
                    self.preview.setText('暂无当前操作画面')
                rows=interactive.call('timeline',self.task_id)['timeline']
                self.render_conversation(value,rows)
                self.statusBar().showMessage(value.get('error') or value.get('human',{}).get('instructions','') if value.get('human') else value.get('error') or value['status'])
            else:
                if 'pages' in value:
                    selected=value.get('page_id') or self.page_id.currentData()
                    self.page_id.clear(); self.page_id.addItem('请选择要操作的网页',None)
                    for page in value['pages']:
                        self.page_id.addItem(page.get('title','网页')+' · '+page.get('url',''),page['page_id'])
                    index=self.page_id.findData(selected)
                    if index>=0:self.page_id.setCurrentIndex(index)
                self.device_output.setPlainText(json.dumps(value,ensure_ascii=False,indent=2))
                self.statusBar().showMessage(value.get('message') or value.get('error') or '操作完成')

        def render_conversation(self,state,rows):
            def bubble(role,text):
                user=role=='你'
                return ('<table width="100%" cellspacing="0" cellpadding="14"><tr>'
                        + ('<td width="16%"></td>' if user else '')
                        + '<td bgcolor="'+('#f4f4f4' if user else '#ffffff')+'">'
                        + '<p style="color:#888;font-size:12px">'+role+'</p>'
                        + '<p style="line-height:150%">'+escape(str(text)).replace('\n','<br>')+'</p></td></tr></table><br>')
            content=bubble('你',state.get('desktop_goal',''))
            for row in rows:
                if row.get('note'):
                    role='你' if row['kind'] in ('human_returned','desktop_user_message') else 'Saygo'
                    content+=bubble(role,row['note'])
                elif row.get('error'):
                    content+=bubble('Saygo','操作未完成：'+str(row['error']))
            if state.get('human'):
                content+=bubble('Saygo',state['human'].get('instructions','请核对后回复，继续任务。'))
            if state.get('error'):content+=bubble('Saygo',str(state['error']))
            if content!=self.last_transcript:
                self.last_transcript=content; self.timeline.setHtml(content)
                self.timeline.verticalScrollBar().setValue(self.timeline.verticalScrollBar().maximum())
            self.goal.setPlaceholderText('回复 Saygo，补充说明并继续任务…' if state['status'] in ('waiting_for_human','idle') else '给 Saygo 发送新任务…')

        def closeEvent(self,event):
            if self.worker:
                self.stop_event.set(); event.ignore()
                self.statusBar().showMessage('正在停止；当前调用结束后可关闭窗口')
            else:event.accept()

    return Window()


if __name__ == '__main__':
    raise SystemExit(main())
