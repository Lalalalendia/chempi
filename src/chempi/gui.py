"""Optional desktop shell. All synchronization runs off the Qt UI thread."""
from __future__ import annotations
from pathlib import Path
from threading import Event
import json
import os
import subprocess
import sys

try:
    from PyQt6.QtCore import QThread, pyqtSignal
    from PyQt6.QtWidgets import (QApplication,QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,
        QLabel,QPushButton,QComboBox,QListWidget,QSpinBox,QCheckBox,QPlainTextEdit,
        QMessageBox,QFileDialog,QLineEdit)
except ImportError as exc:
    raise RuntimeError('GUI needs PyQt6: pip install -e ".[gui]"') from exc

from .cli import default_root
from .max_client import MaxClient, launch_setup_terminal
from .service import ArchiveService
from .exporter import export_html, export_jsonl


class Worker(QThread):
    progress = pyqtSignal(str)
    success = pyqtSignal(object)
    failure = pyqtSignal(str)

    def __init__(self, work):
        super().__init__()
        self.work=work
        self.cancel=Event()

    def run(self):
        try:
            self.success.emit(self.work(self.progress.emit,self.cancel.is_set))
        except Exception as exc:
            self.failure.emit(str(exc)[:500])


class MainWindow(QMainWindow):
    def __init__(self, root: Path, profile: str):
        super().__init__()
        self.root=Path(root)
        self.profile=profile
        self.worker=None
        self.setWindowTitle('Chempi - MAX Archive 0.1')
        self.resize(950,720)
        parent=QWidget()
        outer=QVBoxLayout(parent)
        outer.addWidget(QLabel('Archive: '+str(self.root)))
        self.status = QLabel('Idle - no operation running')
        outer.addWidget(self.status)
        line=QHBoxLayout()
        self.login=QPushButton('MAX login (QR)')
        self.refresh=QPushButton('Refresh chat list')
        self.stop=QPushButton('Stop after current item')
        self.stop.setEnabled(False)
        line.addWidget(self.login);line.addWidget(self.refresh);line.addWidget(self.stop)
        outer.addLayout(line)
        outer.addWidget(QLabel('Choose a chat:'))
        self.chat_list=QListWidget()
        outer.addWidget(self.chat_list,stretch=1)
        opts=QHBoxLayout()
        opts.addWidget(QLabel('Media kind:'))
        self.kind=QComboBox()
        for key,description in [
            ('video_circle','Video circles only'),('video','Ordinary videos'),('photo','Photos'),
            ('voice','Voice notes'),('audio','Audio'),('file','Documents'),
            ('sticker','Stickers'),('unknown','Unclassified'),('all','All files')]:
            self.kind.addItem(description,key)
        opts.addWidget(self.kind)
        opts.addWidget(QLabel('Max downloads:'))
        self.limit=QSpinBox();self.limit.setRange(1,100000);self.limit.setValue(200)
        opts.addWidget(self.limit)
        self.verify=QCheckBox('Verify existing SHA-256')
        opts.addWidget(self.verify)
        outer.addLayout(opts)
        dates=QHBoxLayout()
        dates.addWidget(QLabel('Download since (optional ISO date):'))
        self.since=QLineEdit()
        self.since.setPlaceholderText('2026-01-01T00:00:00+00:00')
        dates.addWidget(self.since)
        dates.addWidget(QLabel('History batches (up to 60 messages each):'))
        self.passes=QSpinBox();self.passes.setRange(1,1000);self.passes.setValue(20)
        dates.addWidget(self.passes)
        outer.addLayout(dates)
        actions=QHBoxLayout()
        self.sync=QPushButton('Sync messages')
        self.download=QPushButton('Download missing files')
        self.export=QPushButton('Export HTML + JSONL')
        self.audit=QPushButton('Audit current chat')
        for button in (self.sync,self.download,self.export,self.audit):
            actions.addWidget(button)
        outer.addLayout(actions)
        misc=QHBoxLayout()
        self.inventory=QPushButton('Index files already downloaded')
        self.open=QPushButton('Open archive folder')
        misc.addWidget(self.inventory);misc.addWidget(self.open)
        outer.addLayout(misc)
        self.log=QPlainTextEdit();self.log.setReadOnly(True)
        outer.addWidget(self.log,stretch=1)
        self.setCentralWidget(parent)
        self.login.clicked.connect(self.start_login)
        self.refresh.clicked.connect(self.refresh_list)
        self.stop.clicked.connect(self.cancel_job)
        self.sync.clicked.connect(self.sync_chat)
        self.download.clicked.connect(self.download_files)
        self.export.clicked.connect(self.export_chat)
        self.audit.clicked.connect(self.audit_chat)
        self.inventory.clicked.connect(self.index_files)
        self.open.clicked.connect(self.open_folder)
        self._busy_buttons = [self.login,self.refresh,self.sync,self.download,
                              self.export,self.audit,self.inventory]
        self.update_list()

    def message(self, txt):
        text = str(txt)
        if text.startswith(('MAX:', 'History batch ', 'Batch ')):
            self.status.setText(text)
        self.log.appendPlainText(text)

    def selected_chat(self):
        item=self.chat_list.currentItem()
        if not item:
            QMessageBox.information(self,'Select chat','Select a chat first.')
            return None
        return item.data(256) # Qt.UserRole

    def start_job(self, callback):
        if self.worker and self.worker.isRunning():
            self.message('Another operation is already running')
            return
        self.worker=Worker(callback)
        for button in self._busy_buttons:
            button.setEnabled(False)
        self.chat_list.setEnabled(False)
        self.status.setText('Running - waiting for first MAX response...')
        self.message('Operation started. You can stop after the current short batch.')
        self.worker.progress.connect(self.message)
        self.worker.success.connect(self.finished_job)
        self.worker.failure.connect(lambda text: self.finished_job({'error':text}))
        self.stop.setEnabled(True)
        self.worker.start()

    def finished_job(self,result):
        self.message(json.dumps(result,ensure_ascii=False,default=str,indent=2))
        self.stop.setEnabled(False)
        for button in self._busy_buttons:
            button.setEnabled(True)
        self.chat_list.setEnabled(True)
        self.status.setText('Operation finished' if not (isinstance(result,dict) and result.get('error')) else 'Operation failed; existing data preserved')
        self.update_list()

    def cancel_job(self):
        if self.worker and self.worker.isRunning() and not self.worker.cancel.is_set():
            self.worker.cancel.set()
            self.stop.setEnabled(False)
            self.status.setText('Stopping after current batch...')
            self.message('Stop requested. Waiting for current batch; previous data stay saved.')

    def start_login(self):
        try:
            launch_setup_terminal(self.profile)
            self.message('QR terminal opened. Finish MAX login locally, then refresh chats.')
        except Exception as exc:
            self.message(str(exc))

    def update_list(self):
        selected=self.chat_list.currentItem().data(256) if self.chat_list.currentItem() else None
        self.chat_list.clear()
        try:
            with_db=ArchiveService(self.root)
            try: chats=with_db.db.list_chats()
            finally: with_db.close()
            for chat in chats:
                from PyQt6.QtWidgets import QListWidgetItem
                label=f'{chat["title"]} | {chat["messages"]} messages | {chat["coverage"]}'
                item=QListWidgetItem(label)
                item.setData(256,chat['chat_id'])
                self.chat_list.addItem(item)
                if chat['chat_id']==selected:self.chat_list.setCurrentItem(item)
        except Exception as exc:
            self.message(f'Could not load local chats: {exc}')

    def refresh_list(self):
        def work(log,cancel):
            service=ArchiveService(self.root,MaxClient(self.profile))
            try: return service.refresh_chats(log)
            finally: service.close()
        self.start_job(work)

    def sync_chat(self):
        chat=self.selected_chat()
        if not chat:return
        passes=self.passes.value()
        def work(log,cancel):
            service=ArchiveService(self.root,MaxClient(self.profile))
            try: return service.sync_chat(chat,passes,log,cancel)
            finally: service.close()
        self.start_job(work)

    def download_files(self):
        chat=self.selected_chat()
        if not chat:return
        kind=self.kind.currentData();limit=self.limit.value();verify=self.verify.isChecked();since=self.since.text().strip() or None
        def work(log,cancel):
            service=ArchiveService(self.root,MaxClient(self.profile))
            try:return service.download_media(chat,kind,since,limit,log,cancel,verify)
            finally:service.close()
        self.start_job(work)

    def export_chat(self):
        chat=self.selected_chat()
        if not chat:return
        def work(log,cancel):
            service=ArchiveService(self.root)
            try:return {'html':str(export_html(service.db,chat)),'jsonl':str(export_jsonl(service.db,chat))}
            finally:service.close()
        self.start_job(work)

    def audit_chat(self):
        chat=self.selected_chat()
        if not chat:return
        service=ArchiveService(self.root)
        try:self.message(json.dumps(service.db.audit(chat),ensure_ascii=False,indent=2))
        finally:service.close()

    def index_files(self):
        folder=QFileDialog.getExistingDirectory(self,'Select already downloaded media')
        if not folder:return
        def work(log,cancel):
            service=ArchiveService(self.root)
            try:return {'indexed':service.inventory_existing(Path(folder),log)}
            finally:service.close()
        self.start_job(work)

    def open_folder(self):
        import webbrowser
        self.root.mkdir(parents=True,exist_ok=True)
        webbrowser.open(self.root.as_uri())

    def closeEvent(self,event):
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self,'Operation running','Stop the running operation before closing. Files already saved remain on disk.')
            event.ignore()
        else:
            super().closeEvent(event)


def main(root=None,profile='personal'):
    app=QApplication.instance() or QApplication(sys.argv)
    window=MainWindow(root or default_root(),profile)
    window.show()
    return app.exec()

if __name__=='__main__':
    raise SystemExit(main())
