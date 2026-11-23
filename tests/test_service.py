import hashlib
import tempfile
import unittest
from pathlib import Path

from chempi.service import ArchiveService,digest_file,sniff_extension
from chempi.max_client import MaxClientError


def message(mid,at='2026-03-07T13:01:00Z',kind='video_circle'):
    return {'id':mid,'chat_id':'chat-1','sender_name':'Vera','timestamp':at,
            'text':'ok','attachments':[{'kind':kind,'id':'media-22'}]}


class FakeMax:
    def __init__(self):
        self.step=0
        self.download_calls=0
        self.cached_messages=set()
    def chats(self):
        return [{'id':'chat-1','title':'Family','kind':'group'}]
    def sync_page(self,chat_id,batch_size=60,progress=None):
        self.step+=1
        if progress: progress(f'MAX: {self.step} messages fetched in current batch')
    def coverage(self,chat_id):
        return {'complete':self.step>=2, 'messages':self.step}
    def store_export(self,chat_id,path):
        import json
        path.write_text('\n'.join(json.dumps(r) for r in [message(i) for i in range(1,self.step+1)]),encoding='utf-8')
    def media_download(self,chat_id,mid,pos,folder,offline=False):
        folder.mkdir(parents=True,exist_ok=True)
        if offline:
            if str(mid) not in self.cached_messages:
                raise MaxClientError('attachment not saved in local cache')
            path=folder/f'selected-{pos}.bin'
            path.write_bytes(b'\x00\x00\x00\x18ftypmp42fake')
            return {'saved':1,'files':[{'path':str(path)}]}
        self.download_calls+=1
        self.cached_messages.add(str(mid))
        path=folder/'all.bin'
        path.write_bytes(b'\x00\x00\x00\x18ftypmp42fake')
        return {'saved':1,'files':[{'path':str(path)}]}


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
    def test_album_downloads_online_once_and_saves_each_position(self):
        item = message(7, kind='photo')
        item['attachments'] = [{'kind':'photo','id':'first'},{'kind':'photo','id':'second'}]
        self.srv.db.add_messages([item])
        got = self.srv.download_media('chat-1','photo')
        self.assertEqual(got['queued'],2)
        self.assertEqual(got['saved'],2)
        self.assertEqual(got['failed'],0)
        self.assertEqual(self.client.download_calls,1)
        files=list((Path(self.tmp.name)/'media'/'chat-1'/'photo').glob('*'))
        self.assertEqual(len(files),2)
        second = self.srv.download_media('chat-1','photo')
        self.assertEqual(second['queued'],0)
        self.assertEqual(self.client.download_calls,1)

    def test_systemic_media_failure_stops_after_three(self):
        class BrokenMax(FakeMax):
            def media_download(self,chat_id,mid,pos,folder,offline=False):
                self.download_calls += 1
                raise MaxClientError('permission_error: read-only export folder not allowed')
        self.srv.close()
        broken=BrokenMax()
        self.srv=ArchiveService(Path(self.tmp.name),broken)
        self.srv.db.add_messages([message(i,kind='photo') for i in range(1,21)])
        lines=[]
        outcome=self.srv.download_media('chat-1','photo',limit=20,progress=lines.append)
        self.assertEqual(outcome['queued'],20)
        self.assertTrue(outcome['aborted'])
        self.assertEqual(outcome['saved'],0)
        self.assertEqual(outcome['failed'],3)
        self.assertEqual(len(self.srv.db.media_queue('chat-1','photo')),20)
        self.assertTrue(any('Auto-stopped after 3 identical errors' in x for x in lines))

    def test_cli_sync_complete_flag_is_authoritative(self):
        class FullSync(FakeMax):
            def coverage(self,chat_id):
                # Store status doesn't carry the sync operation's complete flag.
                return {'item': {'messages':self.step,'oldest_at':'2023-01-01'}}
            def sync_page(self,chat_id,batch_size=60,progress=None):
                self.step += 1
                return {'item':{'chat':chat_id,'fetched':1,'complete':True,'ranges':[{'from':1,'to':1}]}}
        self.srv.close()
        client=FullSync()
        self.srv=ArchiveService(Path(self.tmp.name),client)
        self.srv.refresh_chats()
        out=self.srv.sync_chat('chat-1',max_pages=10)
        self.assertEqual(out['total'],1)
        self.assertEqual(out['state'],'full')
        self.assertEqual(client.step,1)
        report=self.srv.db.audit('chat-1')['coverage']
        self.assertEqual(report['state'],'full')

    def test_nested_cli_json_complete_is_seen(self):
        from chempi.service import _explicit_complete
        self.assertTrue(_explicit_complete({'item':{'complete':True, 'fetched':0}}))
        self.assertFalse(_explicit_complete({'item':{'complete':False, 'fetched':0}}))
        self.assertFalse(_explicit_complete({'item':{'complete':True,'stopped':True}}))

    def test_stop_between_short_history_batches(self):
        self.srv.refresh_chats()
        requested = [False]
        def notify(message):
            if message.startswith('Batch 1/10 saved:'):
                requested[0] = True
        result = self.srv.sync_chat('chat-1', max_pages=10,
                                    progress=notify, cancelled=lambda: requested[0])
        self.assertEqual(self.client.step, 1)
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['total'], 1)

    def test_salvage_cached_pages_after_interrupt(self):
        class Interrupted(FakeMax):
            def sync_page(self,chat_id,batch_size=60,progress=None):
                self.step += 1
                if progress: progress('MAX: 1 messages fetched in current batch')
                raise MaxClientError('MAX CLI was interrupted (0xC000013A)')
        client = Interrupted()
        self.srv.close()
        self.srv = ArchiveService(Path(self.tmp.name), client)
        self.srv.refresh_chats()
        history = []
        with self.assertRaises(MaxClientError):
            self.srv.sync_chat('chat-1', max_pages=4, progress=history.append)
        self.assertEqual(self.srv.db.audit('chat-1')['total'], 1)
        self.assertEqual(self.srv.db.audit('chat-1')['coverage']['state'], 'partial')
        self.assertTrue(any('Recovered CLI cache:' in m for m in history))

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
