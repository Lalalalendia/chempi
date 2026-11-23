"""Incremental MAX history import and file queue. No use of undocumented browser DOM."""
from __future__ import annotations
from datetime import datetime
from pathlib import Path
from typing import Callable
import hashlib
import json
import mimetypes
import os
import shutil
import tempfile

from .database import ArchiveDB, utcnow
from .max_client import MaxClient, MaxClientError
from .model import first, parse_jsonl, parse_time, safe_label, stable_file_stem, text_id

Progress = Callable[[str], None]


def noop(_):
    pass


def digest_file(path: Path) -> tuple[int, str]:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return path.stat().st_size, h.hexdigest()


def sniff_extension(path: Path, fallback_name: str | None = None, mime_type: str | None = None) -> str:
    with path.open('rb') as stream:
        head = stream.read(24)
    if head.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if head[:6] in (b'GIF87a', b'GIF89a'):
        return '.gif'
    if len(head) >= 12 and head[4:8] == b'ftyp':
        return '.mp4'
    if head.startswith(b'\x1a\x45\xdf\xa3'):
        return '.webm'
    if head.startswith(b'OggS'):
        return '.ogg'
    if head.startswith(b'RIFF') and head[8:12] == b'WAVE':
        return '.wav'
    if head.startswith(b'%PDF'):
        return '.pdf'
    if head.startswith(b'ID3'):
        return '.mp3'
    if mime_type:
        ext = mimetypes.guess_extension(mime_type.split(';')[0].strip())
        if ext and ext.lower() in {'.opus','.mp3','.ogg','.pdf','.mp4','.webm','.flac','.wav','.zip','.txt','.jpg','.png','.webp'}:
            return ext.lower()
    # Never trust a remote filename as a media type without checking its bytes.
    return '.bin'


def _explicit_complete(status: dict) -> bool:
    if not isinstance(status, dict):
        return False
    for key in ('complete','fully_fetched','history_complete','fetched_all','back_to_start'):
        if status.get(key) is True:
            return True
    for key in ('coverage','held'):
        part = status.get(key)
        if isinstance(part, dict) and _explicit_complete(part):
            return True
    return status.get('state') in ('full', 'fetched')


class ArchiveService:
    def __init__(self, root: Path, client: MaxClient | None = None):
        self.db = ArchiveDB(root)
        self.client = client

    def close(self):
        self.db.close()

    def refresh_chats(self, progress: Progress = noop):
        if self.client is None:
            raise MaxClientError('MAX client is not configured')
        chats = self.client.chats()
        for row in chats:
            chat_id = text_id(first(row, 'id', 'chat_id', 'chatId'))
            if chat_id:
                self.db.upsert_chat(chat_id, first(row,'title','name') or 'Unknown chat',first(row,'kind','type'))
        progress(f'Found {len(chats)} chats')
        return self.db.list_chats()

    def import_jsonl(self, path: Path, chat_id: str | None = None):
        return self.db.add_messages(parse_jsonl(path), fallback_chat_id=chat_id)

    def _export_and_import(self, chat_id: str):
        with tempfile.TemporaryDirectory(prefix='chempi-export-',dir=self.db.root) as directory:
            path = Path(directory) / 'history.jsonl'
            self.client.store_export(chat_id,path)
            return self.import_jsonl(path,chat_id)

    def sync_chat(self, chat_id: str, max_pages=4, progress: Progress = noop, cancelled=lambda: False):
        """Each pass fetches at most 1200. `max_pages` bounds runs, not server messages.
        Coverage is never guessed from a scrollbar or last page count.
        """
        if self.client is None:
            raise MaxClientError('MAX client is not configured')
        if max_pages < 1 or max_pages > 100:
            raise ValueError('max_pages must be between 1 and 100')
        with self.db.transaction():
            cur = self.db.connection.execute('INSERT INTO sync_runs(chat_id,started_at,status) VALUES(?,?,?)',
                                             (chat_id,utcnow(),'running'))
            job = cur.lastrowid
        imported = 0
        state = 'partial'
        error = None
        try:
            # First preserve whatever was synced by an earlier attempt.
            try:
                previous, _ = self._export_and_import(chat_id)
                progress(f'Local cache imported: {previous} records')
            except MaxClientError:
                # Fresh logins can have zero cached messages, and export may
                # reject an empty archive. A network sync must still proceed.
                progress('No locally exportable history yet; fetching from MAX')
            unchanged = 0
            for i in range(max_pages):
                if cancelled():
                    progress('Cancelled between pages; persisted records retained')
                    break
                status = self.client.coverage(chat_id)
                if _explicit_complete(status):
                    state = 'full'
                    break
                before = self.db.audit(chat_id)['total']
                self.client.sync_page(chat_id)
                count, _ = self._export_and_import(chat_id)
                after = self.db.audit(chat_id)['total']
                imported += max(0, after - before)
                progress(f'Pass {i+1}/{max_pages}: {after} stored, {after-before} new')
                status = self.client.coverage(chat_id)
                if _explicit_complete(status):
                    state = 'full'
                    break
                unchanged = unchanged+1 if after == before else 0
                if unchanged >= 2:
                    progress('No further messages received; completeness unconfirmed')
                    break
            raw = self.client.coverage(chat_id)
            if _explicit_complete(raw):
                state = 'full'
            self.db.set_coverage(chat_id,state,raw if isinstance(raw,dict) else {'raw':raw})
        except Exception as exc:
            error = str(exc)[:1000]
            progress(f'Sync stopped: {error}')
            # No manufactured success: report state unknown on error.
            self.db.set_coverage(chat_id,'partial',{'error': error})
        finally:
            with self.db.transaction():
                self.db.connection.execute('UPDATE sync_runs SET finished_at=?,imported=?,status=?,error=? WHERE id=?',
                                          (utcnow(),imported,'failed' if error else state,error,job))
        if error:
            raise MaxClientError(error)
        return {'chat_id':chat_id,'imported':imported,'state':state,'total':self.db.audit(chat_id)['total']}

    def verify_recorded_media(self, chat_id: str):
        """Only mark missing or corrupt files for re-download."""
        fixed = 0
        rows = list(self.db.connection.execute('''SELECT chat_id,message_id,position,local_path,file_size,sha256
            FROM attachments WHERE chat_id=? AND status='downloaded' ''',(chat_id,)))
        for row in rows:
            path = Path(row['local_path']) if row['local_path'] else None
            if path and not path.is_absolute():
                path = self.db.root / path
            valid = bool(path and path.exists() and path.is_file())
            if valid and row['sha256']:
                size, digest = digest_file(path)
                valid = size == row['file_size'] and digest == row['sha256']
            if not valid:
                with self.db.transaction():
                    self.db.connection.execute('''UPDATE attachments SET status='pending',last_error='Local file missing or corrupt'
                        WHERE chat_id=? AND message_id=? AND position=?''',
                        (row['chat_id'],row['message_id'],row['position']))
                fixed += 1
        return fixed

    def _extract_download_file(self, directory: Path, result):
        files = [path for path in directory.rglob('*') if path.is_file() and path.stat().st_size > 0]
        if len(files) != 1:
            raise MaxClientError(f'Expected exactly one media file; found {len(files)}')
        return files[0]

    def download_media(self, chat_id: str, kind='all', since=None, limit=200,
                       progress: Progress = noop, cancelled=lambda: False, verify=False):
        if self.client is None:
            raise MaxClientError('MAX client is not configured')
        if kind not in {'all','video_circle','video','photo','voice','audio','file','sticker','unknown'}:
            raise ValueError('Unknown media kind')
        if limit < 1 or limit > 100000:
            raise ValueError('Invalid download limit')
        if verify:
            count = self.verify_recorded_media(chat_id)
            progress(f'Requeued missing or corrupt files: {count}')
        queue = self.db.media_queue(chat_id,kind,since)[:limit]
        summary = {'queued':len(queue),'saved':0,'failed':0,'cancelled':False}
        for index,att in enumerate(queue,1):
            if cancelled():
                summary['cancelled'] = True
                break
            if att['kind'] in {'link','unknown'}:
                self.db.mark_failed(chat_id,att['message_id'],att['position'],
                                    'No verified downloadable media type; examine source JSON')
                summary['failed'] += 1
                continue
            rel_folder = Path('media') / safe_label(chat_id,32) / att['kind']
            target_dir = self.db.root / rel_folder
            target_dir.mkdir(parents=True,exist_ok=True)
            try:
                with tempfile.TemporaryDirectory(prefix='chempi-file-',dir=self.db.root) as directory:
                    tmpdir = Path(directory)
                    result = self.client.media_download(chat_id,att['message_id'],att['position'],tmpdir)
                    source = self._extract_download_file(tmpdir,result)
                    size,digest = digest_file(source)
                    if size == 0:
                        raise MaxClientError('Empty download')
                    suffix = sniff_extension(source,att['original_name'],att['mime_type'])
                    name = stable_file_stem(chat_id,att['message_id'],att['position'])+suffix
                    target = target_dir / name
                    if target.exists():
                        existing_size,existing_hash=digest_file(target)
                        if (existing_size,existing_hash) != (size,digest):
                            name=stable_file_stem(chat_id,att['message_id'],att['position'])+'-'+digest[:10]+suffix
                            target=target_dir/name
                    if not target.exists():
                        # replace on the same disk: tempdir is placed under archive root.
                        os.replace(source,target)
                    if att['sent_at']:
                        try:
                            stamp = datetime.fromisoformat(att['sent_at']).timestamp()
                            os.utime(target,(stamp,stamp))
                        except (OSError,OverflowError,ValueError):
                            pass
                    relative = str(target.relative_to(self.db.root)).replace('\\','/')
                    self.db.mark_downloaded(chat_id,att['message_id'],att['position'],relative,size,digest)
                    summary['saved'] += 1
            except Exception as error:
                self.db.mark_failed(chat_id,att['message_id'],att['position'],str(error))
                summary['failed'] += 1
            progress(f'Media {index}/{len(queue)}: saved={summary["saved"]}, failed={summary["failed"]}')
        return summary

    def inventory_existing(self, path: Path, progress: Progress = noop):
        path = Path(path).expanduser().resolve(strict=True)
        total = 0
        for file in path.rglob('*'):
            if not file.is_file() or file.is_symlink() or file.suffix.lower() in {'.sqlite','.db','.wal','.shm'}:
                continue
            size,digest = digest_file(file)
            with self.db.transaction():
                self.db.connection.execute('''INSERT INTO file_inventory(absolute_path,sha256,file_size,scanned_at)
                     VALUES(?,?,?,?) ON CONFLICT(absolute_path) DO UPDATE SET
                     sha256=excluded.sha256,file_size=excluded.file_size,scanned_at=excluded.scanned_at''',
                     (str(file),digest,size,utcnow()))
            total += 1
            if total % 100 == 0:
                progress(f'Indexed {total} existing files')
        progress(f'Indexed {total} existing files; ambiguous filename matches are not auto-deduplicated')
        return total
