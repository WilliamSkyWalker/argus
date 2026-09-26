"""Qt Widgets shell. Network and device operations run off the GUI thread."""
import json
from pathlib import Path
import sys
import threading


def main():
    # Frozen native-messaging entry runs without loading Qt or opening a window.
    flag = next((item for item in ('--native-host','--native-host-uninstall') if item in sys.argv), None)
    if flag:
        from argus.integrations.browser_bridge import main as host_main
        sys.argv = [sys.argv[0], 'host' if flag == '--native-host' else 'uninstall', *sys.argv[sys.argv.index(flag)+1:]]
        return host_main()
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    app.setApplicationName('Argus Desktop')
    app.setOrganizationName('Argus')
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
        QListWidget,QAbstractItemView,QSplitter,QFileDialog,QInputDialog,QMessageBox)
    from argus.desktop.model import load_settings, load_key, save_settings, VisionModel
    from argus.desktop.runner import TaskLoop
    from argus.runtime import interactive
    from argus.devices import control, service

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
            self.setWindowTitle('Argus · 桌面任务助手'); self.resize(1180,800)
            self.worker = None; self.task_id = None
            self.pause_event = threading.Event(); self.stop_event = threading.Event()
            self.buttons = []
            self.tabs = QTabWidget(); self.setCentralWidget(self.tabs)
            self.statusBar().showMessage('就绪 · 先配置模型，再连接目标')
            self.settings_page(); self.devices_page(); self.task_page()
            self.setStyleSheet('''QMainWindow,QWidget { background:#f5f7fb; color:#172b4d; font-size:14px; }
                QLineEdit,QPlainTextEdit,QListWidget,QComboBox { background:white; border:1px solid #cbd5e1; border-radius:5px; padding:7px; }
                QPushButton { background:#e3eaf5; border:0; border-radius:6px; padding:9px 15px; }
                QPushButton:hover { background:#cbdaf1; } QPushButton:disabled { color:#94a3b8; }
                QTabBar::tab { padding:14px 30px; } QTabBar::tab:selected { color:#2563eb; background:white; }
                QLabel#title { font-size:24px; font-weight:600; padding:12px 0; }''')
            try:
                data = load_settings(); self.base.setText(data['base_url']); self.model.setText(data['model'])
                self.remember.setChecked(data.get('remember',False)); self.key.setText(load_key(data))
            except Exception as exc:
                self.statusBar().showMessage(str(exc))
            self.refresh_sessions(); self.refresh_tasks()

        def page(self, name, heading, description):
            page=QWidget(); layout=QVBoxLayout(page); layout.setContentsMargins(24,20,24,20)
            title=QLabel(heading); title.setObjectName('title'); layout.addWidget(title)
            subtitle=QLabel(description); subtitle.setWordWrap(True); layout.addWidget(subtitle)
            self.tabs.addTab(page,name)
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
            from argus.desktop.model import secure_backend, credential_id
            settings,key=self.model_values()
            def remove(emit):
                backend=secure_backend(); ident=credential_id(settings)
                if backend.get_password('Argus Desktop',ident): backend.delete_password('Argus Desktop',ident)
                save_settings(settings,'',False)
                return {'message':'已删除系统密钥；当前输入框内容仍仅在内存中'}
            self.remember.setChecked(False); self.job(remove)

        def devices_page(self):
            layout=self.page('设备连接','选择操作目标','命名会话与 CLI、MCP 共用。浏览器使用扩展连接；桌面窗口支持 Windows 和 macOS。')
            form=QFormLayout(); layout.addLayout(form)
            self.platform=QComboBox(); self.platform.addItems(['browser','desktop','android','ios'])
            self.session=QLineEdit('desktop-web'); self.target=QLineEdit(); self.target.setPlaceholderText('桌面：窗口标题或 App 名；手机：设备 ID；浏览器：留空')
            self.page_id=QComboBox(); self.page_id.addItem('先连接浏览器，再选择网页',None)
            self.background=QCheckBox('Windows 使用后台窗口输入（不支持的动作会报错）')
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
                from argus.integrations.browser_setup import setup
                from argus.integrations.browser_bridge import atomic_json
                from argus.platforms import device_session as ds
                source=Path(__file__).parent/'assets/extension'
                if not source.exists(): source=Path(__file__).resolve().parents[2]/'extensions/argus-browser'
                if not source.exists(): source=Path(sys.prefix)/'share/argus/browser-extension'
                manifest=json.loads((source/'manifest.json').read_text())
                eid=''.join(chr(97+int(c,16)) for c in hashlib.sha256(base64.b64decode(manifest['key'])).hexdigest()[:32])
                result=setup(eid)
                target=Path(result['directory'])/'extension'; shutil.copytree(source,target,dirs_exist_ok=True)
                ds.STATE_DIR.parent.mkdir(parents=True,exist_ok=True); atomic_json(ds.STATE_DIR.parent/'browser-bridge.json',result)
                display=str(target)
                if result.get('host')=='windows':
                    from argus.devices.mobile_host import wsl_path
                    display=wsl_path(target,windows=True)
                return {'message':'打开 chrome://extensions，开启开发者模式并加载以下目录，然后在扩展中点击 Connect。','extension_directory':display,**result}
            self.job(install)

        def task_page(self):
            layout=self.page('任务工作台','描述任务，观察执行','首次运行请使用测试应用。暂停在当前调用返回后生效；已经派发的动作不会撤销或自动重放。')
            self.goal=QPlainTextEdit(); self.goal.setPlaceholderText('例如：在浏览器里打开产品设置，检查通知选项是否开启。'); self.goal.setMaximumHeight(90); layout.addWidget(self.goal)
            splitter=QSplitter(); layout.addWidget(splitter)
            left=QWidget(); column=QVBoxLayout(left); splitter.addWidget(left)
            column.addWidget(QLabel('绑定会话（可多选）'))
            self.sessions=QListWidget(); self.sessions.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection); column.addWidget(self.sessions)
            self.button('刷新会话',self.refresh_sessions,column)
            self.button('创建并执行',self.start_task,column)
            self.tasks=QComboBox(); column.addWidget(self.tasks)
            self.button('刷新任务列表',self.refresh_tasks,column)
            self.button('加载任务与最新观察',self.load_task,column)
            self.button('继续执行',self.resume_task,column)
            self.button('核对不确定动作',self.resolve_task,column)
            self.button('导出记录',self.export_task,column)
            self.pause_button=QPushButton('暂停 / 人工接管'); self.pause_button.clicked.connect(self.request_pause); column.addWidget(self.pause_button)
            self.stop_button=QPushButton('停止任务'); self.stop_button.clicked.connect(self.request_stop); column.addWidget(self.stop_button)
            right=QWidget(); output=QVBoxLayout(right); splitter.addWidget(right); splitter.setStretchFactor(1,3)
            self.task_status=QLabel('尚未创建任务'); output.addWidget(self.task_status)
            self.preview=QLabel('操作画面'); self.preview.setMinimumSize(400,260); self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter); output.addWidget(self.preview,2)
            self.timeline=QPlainTextEdit(); self.timeline.setReadOnly(True); output.addWidget(self.timeline,1)

        def refresh_sessions(self):
            self.sessions.clear()
            for row in control.sessions():
                if not row.get('disconnected') and not row.get('error') and (row.get('platform')!='browser' or row.get('backend')=='extension'):
                    self.sessions.addItem(row['session'])

        def refresh_tasks(self):
            self.tasks.clear()
            for row in reversed(interactive.call('list')['tasks']): self.tasks.addItem(row['id']+' · '+row['status'],row['id'])

        def start_task(self):
            settings,key=self.model_values(); goal=self.goal.toPlainText().strip()
            bindings={f'target_{i+1}':item.text() for i,item in enumerate(self.sessions.selectedItems())}
            def start(emit):
                loop=TaskLoop(VisionModel(settings,key)); state=loop.create(goal,bindings); emit(state)
                return loop.run(state['id'],self.pause_event,self.stop_event,emit)
            self.job(start,loop=True)

        def load_task(self):
            task_id=self.tasks.currentData()
            if task_id: self.job(lambda emit:interactive.call('recover',task_id))

        def resume_task(self):
            if not self.task_id: return
            settings,key=self.model_values(); task_id=self.task_id
            note,ok=QInputDialog.getText(self,'继续任务','人工操作或核对说明：',text='已核对，可以继续')
            if not ok or not note.strip(): return
            def resume(emit):
                loop=TaskLoop(VisionModel(settings,key)); state=loop.runtime.store.get(task_id)
                if state['status']=='waiting_for_human': loop.runtime.resume_task(task_id,note)
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
            path,_=QFileDialog.getSaveFileName(self,'导出任务证据','argus-task.zip','ZIP (*.zip)')
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
            for button in self.buttons:button.setEnabled(False)
            self.pause_button.setEnabled(loop); self.stop_button.setEnabled(loop)
            self.worker=Worker(function); self.worker.result.connect(self.show_result); self.worker.update.connect(self.show_result)
            self.worker.failed.connect(self.show_error); self.worker.finished.connect(self.job_done)
            self.statusBar().showMessage('正在执行…'); self.worker.start()

        def job_done(self):
            worker=self.worker; self.worker=None; worker.deleteLater()
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
                self.task_id=value['id']; self.goal.setPlainText(value.get('desktop_goal',''))
                self.task_status.setText(f"{value['id']} · {value['status']} · 已记录 {value.get('cursor',0)} 步")
                observations=list(value.get('observations',{}).values())
                if observations:
                    pix=QPixmap(observations[-1]['path']); self.preview.setPixmap(pix.scaled(self.preview.size(),Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))
                rows=interactive.call('timeline',self.task_id)['timeline']
                self.timeline.setPlainText('\n'.join(json.dumps(row,ensure_ascii=False) for row in rows[-60:]))
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

        def closeEvent(self,event):
            if self.worker:
                self.stop_event.set(); event.ignore()
                self.statusBar().showMessage('正在停止；当前调用结束后可关闭窗口')
            else:event.accept()

    return Window()


if __name__ == '__main__':
    raise SystemExit(main())
