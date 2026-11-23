import hashlib
import tempfile
import unittest
from pathlib import Path

from chempi.service import ArchiveService,digest_file,sniff_extension


def message(mid,at='2026-03-07T13:01:00Z',kind='video_circle'):
    return {'id':mid,'chat_id':'chat-1','sender_name':'Vera','timestamp':at,
            'text':'ok','attachments':[{'kind':kind,'id':'media-22'}]}


class FakeMax:
    def __init__(self):
        self.step=0
        self.download_calls=0
    def chats(self):
        return [{'id':'chat-1','title':'Family','kind':'group'}]
    def sync_page(self,chat_id):
        self.step+=1
    def coverage(self,chat_id):
        return {'complete':self.step>=2, 'messages':self.step}
    def store_export(self,chat_id,path):
        import json
        path.write_text('\n'.join(json.dumps(r) for r in [message(i) for i in range(1,self.step+1)]),encoding='utf-8')
    def media_download(self,chat_id,mid,pos,folder):
        self.download_calls+=1
        (folder/'raw.bin').write_bytes(b'\x00\x00\x00\x18ftypmp42fake')
        return {'saved':1,'files':[{'path':str(folder/'raw.bin')} ]}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.client=FakeMax()
        self.srv=ArchiveService(Path(self.tmp.name),self.client)
    def tearDown(self):self.srv.close();self.tmp.cleanup()
    def test_full_sync_pages_and_resume(self):
        self.srv.refresh_chats()
        got=self.srv.sync_chat('chat-1',max_pages=4)
        self.assertEqual(got['state'],'full')
        self.assertEqual(got['total'],2)
        got2=self.srv.sync_chat('chat-1',max_pages=4)
        self.assertEqual(got2['total'],2)
        self.assertEqual(self.srv.db.audit('chat-1')['coverage']['state'],'full')
    def test_circle_only_skip_when_already_downloaded(self):
        self.srv.db.add_messages([message(1),message(2,kind='photo')])
        before=self.srv.db.media_queue('chat-1','video_circle')
        self.assertEqual(len(before),1)
        result=self.srv.download_media('chat-1','video_circle')
        self.assertEqual(result['saved'],1)
        self.assertEqual(self.client.download_calls,1)
        second=self.srv.download_media('chat-1','video_circle')
        self.assertEqual(second['queued'],0)
        self.assertEqual(self.client.download_calls,1)
        path=self.srv.db.connection.execute('SELECT local_path FROM attachments WHERE kind=?',('video_circle',)).fetchone()[0]
        saved=Path(self.tmp.name)/path
        self.assertTrue(saved.exists())
        self.assertEqual(int(saved.stat().st_mtime),1772888460)
    def test_cancel_after_first(self):
        self.srv.db.add_messages([message(1),message(2)])
        count=[0]
        def cancelled():
            count[0]+=1
            return count[0]>1
        result=self.srv.download_media('chat-1','all',cancelled=cancelled)
        self.assertEqual(result['saved'],1)
        self.assertTrue(result['cancelled'])
        self.assertEqual(len(self.srv.db.media_queue('chat-1','all')),1)

if __name__=='__main__':unittest.main()
