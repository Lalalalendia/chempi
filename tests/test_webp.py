"""Regression checks for real-world MAX WebP images mislabeled *.bin.

Test data are synthetic RIFF/WebP signatures; no personal chat files are committed.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chempi.service import ArchiveService, digest_file, sniff_extension
from chempi.max_client import MaxClientError


WEBP = b'RIFF' + (20).to_bytes(4, 'little') + b'WEBP' + b'VP8X' + bytes(20)


def message(kind='photo'):
    return {'id':'msg1', 'chat_id':'grp', 'timestamp':'2026-07-15T16:15:00Z',
            'text':'test', 'attachments':[{'kind':kind}]}


class WebpClient:
    def __init__(self):
        self.downloads = 0
        self.fetched = False

    def media_download(self,chat_id,message_id,position,folder,offline=False):
        folder.mkdir(parents=True,exist_ok=True)
        if offline:
            if not self.fetched:
                raise MaxClientError('not found in local store')
            target = folder / 'saved.bin'
            target.write_bytes(WEBP)
            return {'item':{'files':[{'path':str(target)}]}}
        self.downloads += 1
        self.fetched = True
        target = folder / 'downloaded.bin'
        target.write_bytes(WEBP)
        return {'item':{'files':[{'path':str(target)}]}}


class WebpRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.client=WebpClient()
        self.service=ArchiveService(Path(self.temp.name),self.client)
    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_webp_riff_signature_detected_without_mime(self):
        path=Path(self.temp.name)/'image.bin'
        path.write_bytes(WEBP)
        self.assertEqual(sniff_extension(path), '.webp')
        self.assertEqual(sniff_extension(path,mime_type='video/mp4'),'.webp')

    def test_new_photo_is_saved_as_webp_not_bin(self):
        self.service.db.add_messages([message('photo')])
        result=self.service.download_media('grp','photo')
        self.assertEqual(result['saved'],1)
        self.assertEqual(result['failed'],0)
        self.assertEqual(self.client.downloads,1)
        row=self.service.db.connection.execute(
            "SELECT status,local_path FROM attachments WHERE chat_id='grp'").fetchone()
        self.assertEqual(row['status'],'downloaded')
        self.assertTrue(row['local_path'].endswith('.webp'))
        self.assertEqual((Path(self.temp.name)/row['local_path']).read_bytes(),WEBP)

    def test_existing_webp_bin_is_repaired_without_network(self):
        self.service.db.add_messages([message('sticker')])
        old=Path(self.temp.name)/'media'/'grp'/'sticker'/'sample.bin'
        old.parent.mkdir(parents=True)
        old.write_bytes(WEBP)
        original_time=1760000000
        import os
        os.utime(old,(original_time,original_time))
        size,sha=digest_file(old)
        self.service.db.mark_downloaded('grp','msg1',1,str(old.relative_to(self.temp.name)),size,sha)
        logs=[]
        requeued=self.service.verify_recorded_media('grp',progress=logs.append)
        self.assertEqual(requeued,0)
        row=self.service.db.connection.execute(
            "SELECT status,local_path,sha256 FROM attachments WHERE chat_id='grp'").fetchone()
        self.assertEqual(row['status'],'downloaded')
        self.assertEqual(row['sha256'],sha)
        corrected=Path(self.temp.name)/row['local_path']
        self.assertEqual(corrected.suffix,'.webp')
        self.assertTrue(corrected.exists())
        self.assertEqual(corrected.read_bytes(),WEBP)
        self.assertEqual(int(corrected.stat().st_mtime),original_time)
        self.assertFalse(old.exists())
        self.assertEqual(self.client.downloads,0)
        self.assertTrue(any('Repaired 1 saved WebP' in line for line in logs))

    def test_video_preview_is_not_marked_downloaded(self):
        self.service.db.add_messages([message('video')])
        result=self.service.download_media('grp','video')
        self.assertEqual(result['saved'],0)
        self.assertEqual(result['failed'],1)
        row=self.service.db.connection.execute(
            "SELECT status,last_error FROM attachments WHERE chat_id='grp'").fetchone()
        self.assertEqual(row['status'],'failed')
        self.assertIn('image/thumbnail',row['last_error'])
        self.assertEqual(self.client.downloads,1)

    def test_already_downloaded_webp_video_is_requeued_not_deleted(self):
        self.service.db.add_messages([message('video')])
        old=Path(self.temp.name)/'media'/'grp'/'video'/'preview.bin'
        old.parent.mkdir(parents=True)
        old.write_bytes(WEBP)
        size,sha=digest_file(old)
        self.service.db.mark_downloaded('grp','msg1',1,str(old.relative_to(self.temp.name)),size,sha)
        count=self.service.verify_recorded_media('grp')
        self.assertEqual(count,1)
        row=self.service.db.connection.execute(
            "SELECT status,last_error FROM attachments WHERE chat_id='grp'").fetchone()
        self.assertEqual(row['status'],'pending')
        self.assertIn('image/preview',row['last_error'])
        self.assertTrue(old.is_file())


if __name__ == '__main__':
    unittest.main()
