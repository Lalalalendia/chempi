import tempfile
import unittest
from pathlib import Path
from chempi.service import ArchiveService
from chempi.max_client import MaxClientError

class EmptyFirstClient:
    def __init__(self):self.step=0
    def store_export(self, chat, path):
        if not self.step:raise MaxClientError('no messages cached')
        path.write_text('{"id":"1","chat_id":"c","text":"Hi","timestamp":"2026-01-01T00:00:00Z"}\n')
    def sync_page(self, chat, batch_size=60, progress=None):
        self.step=1
        if progress: progress('MAX: first batch completed')
    def coverage(self, chat):return {'complete': self.step>0}

class FirstRun(unittest.TestCase):
    def test_empty_cache_can_sync(self):
        with tempfile.TemporaryDirectory() as temp:
            client=EmptyFirstClient()
            service=ArchiveService(Path(temp),client)
            try:
                result=service.sync_chat('c')
                # On next sync this can be imported from the local client store.
                self.assertEqual(result['state'],'full')
            finally:service.close()

if __name__=='__main__':unittest.main()
