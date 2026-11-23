import hashlib
import tempfile
import unittest
from pathlib import Path

from chempi.database import ArchiveDB
from chempi.exporter import export_html,export_jsonl


def row(mid, text='hello', at='2026-01-01T10:00:00Z'):
    return {'id':mid,'chat_id':77,'text':text,'timestamp':at,'sender_name':'Anna',
            'attachments':[{'kind':'video_circle','id':'c-42','mimeType':'video/mp4'}]}


class DbTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=ArchiveDB(Path(self.tmp.name))
    def tearDown(self):
        self.db.close();self.tmp.cleanup()

    def test_upsert_preserves_media_and_idempotent_messages(self):
        self.assertEqual(self.db.add_messages([row(1),row(1)])[0],2)
        self.assertEqual(self.db.audit('77')['total'],1)
        self.assertEqual(len(self.db.media_queue('77','video_circle')),1)
        self.db.mark_downloaded('77','1',1,'media/sample.mp4',5,'abc')
        self.db.add_messages([row(1,'edited')])
        self.assertEqual(self.db.audit('77')['total'],1)
        stored=self.db.connection.execute('SELECT body FROM messages').fetchone()[0]
        self.assertEqual(stored,'edited')
        self.assertEqual(self.db.connection.execute('SELECT status FROM attachments').fetchone()[0],'downloaded')

    def test_sql_transaction_failure_rolls_back(self):
        self.db.add_messages([row(1)])
        before=self.db.audit('77')['total']
        try:
            with self.db.transaction():
                self.db.connection.execute("UPDATE messages SET body='broken' WHERE chat_id=?",('77',))
                raise RuntimeError('interrupt')
        except RuntimeError:pass
        self.assertEqual(self.db.audit('77')['total'],before)

    def test_offline_exports_and_xss_escaping(self):
        self.db.add_messages([row(1,'<img src=x onerror=alert(1)>')])
        html_path=export_html(self.db,'77')
        self.assertIn('&lt;img',html_path.read_text(encoding='utf-8'))
        self.assertNotIn('<img src=x',html_path.read_text(encoding='utf-8'))
        self.assertIn('"message_id": "1"',export_jsonl(self.db,'77').read_text(encoding='utf-8'))

    def test_repair_missing_media(self):
        self.db.add_messages([row(1)])
        self.db.mark_downloaded('77','1',1,'media/nowhere.mp4',5,'abc')
        from chempi.service import ArchiveService
        srv=ArchiveService(Path(self.tmp.name))
        try:self.assertEqual(srv.verify_recorded_media('77'),1)
        finally:srv.close()
        self.assertEqual(self.db.connection.execute('SELECT status FROM attachments').fetchone()[0],'pending')

if __name__=='__main__':unittest.main()
