"""SQLite is authoritative for chat metadata; media bytes stay on disk."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
import json
import sqlite3

from .model import normalize_message

SCHEMA = '''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS chats (
    chat_id TEXT PRIMARY KEY, title TEXT NOT NULL, kind TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS messages (
    chat_id TEXT NOT NULL, message_id TEXT NOT NULL,
    sender_id TEXT, sender_name TEXT, sent_at TEXT, edited_at TEXT,
    kind TEXT NOT NULL, body TEXT, deleted INTEGER NOT NULL DEFAULT 0,
    reply_to TEXT, reactions_json TEXT, raw_json TEXT NOT NULL,
    PRIMARY KEY(chat_id, message_id),
    FOREIGN KEY(chat_id) REFERENCES chats(chat_id)
);
CREATE INDEX IF NOT EXISTS messages_order ON messages(chat_id, sent_at, message_id);
CREATE TABLE IF NOT EXISTS attachments (
    chat_id TEXT NOT NULL, message_id TEXT NOT NULL, position INTEGER NOT NULL,
    kind TEXT NOT NULL, original_name TEXT, mime_type TEXT, remote_id TEXT,
    source_url TEXT, raw_json TEXT NOT NULL,
    local_path TEXT, file_size INTEGER, sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    last_error TEXT, downloaded_at TEXT,
    PRIMARY KEY(chat_id, message_id, position),
    FOREIGN KEY(chat_id,message_id) REFERENCES messages(chat_id,message_id)
);
CREATE INDEX IF NOT EXISTS attachments_queue ON attachments(status, kind, chat_id);
CREATE TABLE IF NOT EXISTS file_inventory (
    absolute_path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, file_size INTEGER NOT NULL,
    scanned_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS inventory_hash ON file_inventory(sha256,file_size);
CREATE TABLE IF NOT EXISTS coverage (
    chat_id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'unknown',
    report_json TEXT NOT NULL DEFAULT '{}', checked_at TEXT NOT NULL,
    FOREIGN KEY(chat_id) REFERENCES chats(chat_id)
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL, started_at TEXT NOT NULL,
    finished_at TEXT, imported INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL, error TEXT
);
'''


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class ArchiveDB:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'media').mkdir(exist_ok=True)
        (self.root / 'exports').mkdir(exist_ok=True)
        self.path = self.root / 'max.sqlite'
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA busy_timeout=30000')
        self.connection.executescript(SCHEMA)

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def transaction(self):
        try:
            yield self.connection
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def upsert_chat(self, chat_id: str, title: str | None = None, kind: str | None = None):
        with self.transaction():
            self.connection.execute('''
                INSERT INTO chats (chat_id,title,kind) VALUES (?,?,?)
                ON CONFLICT(chat_id) DO UPDATE SET
                  title=CASE WHEN excluded.title = 'Unknown chat' THEN chats.title ELSE excluded.title END,
                  kind=COALESCE(excluded.kind,chats.kind),updated_at=CURRENT_TIMESTAMP
            ''', (chat_id, title or 'Unknown chat', kind))

    def add_messages(self, items: Iterable[dict], fallback_chat_id: str | None = None) -> tuple[int,int]:
        """Commit in bounded batches so interrupted imports keep earlier work."""
        added = ignored = 0
        batch = []
        for item in items:
            value = normalize_message(item, fallback_chat_id)
            if value:
                batch.append(value)
                if len(batch) >= 250:
                    added += self._save_batch(batch)
                    batch.clear()
            else:
                ignored += 1
        if batch:
            added += self._save_batch(batch)
        return added, ignored

    def _save_batch(self, rows: list[dict]) -> int:
        with self.transaction():
            before = self.connection.total_changes
            for row in rows:
                chat_id = row['chat_id']
                self.connection.execute('INSERT OR IGNORE INTO chats(chat_id,title) VALUES (?,?)', (chat_id, 'Unknown chat'))
                self.connection.execute('''
                    INSERT INTO messages(chat_id,message_id,sender_id,sender_name,sent_at,edited_at,kind,body,
                                         deleted,reply_to,reactions_json,raw_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(chat_id,message_id) DO UPDATE SET
                       sender_id=COALESCE(excluded.sender_id,messages.sender_id),
                       sender_name=COALESCE(excluded.sender_name,messages.sender_name),
                       sent_at=COALESCE(excluded.sent_at,messages.sent_at),
                       edited_at=COALESCE(excluded.edited_at,messages.edited_at),
                       kind=excluded.kind,body=COALESCE(excluded.body,messages.body),
                       deleted=excluded.deleted,
                       reply_to=COALESCE(excluded.reply_to,messages.reply_to),
                       reactions_json=COALESCE(excluded.reactions_json,messages.reactions_json),
                       raw_json=excluded.raw_json
                ''', (chat_id,row['id'],row['sender_id'],row['sender_name'],row['timestamp'],row['edited_at'],
                      row['kind'],row['text'],int(row['deleted']),row['reply_to'],row['reactions_json'],row['raw_json']))
                for att in row['attachments']:
                    self.connection.execute('''
                        INSERT INTO attachments(chat_id,message_id,position,kind,original_name,mime_type,remote_id,source_url,raw_json)
                        VALUES (?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(chat_id,message_id,position) DO UPDATE SET
                          kind=excluded.kind,original_name=COALESCE(excluded.original_name,attachments.original_name),
                          mime_type=COALESCE(excluded.mime_type,attachments.mime_type),
                          remote_id=COALESCE(excluded.remote_id,attachments.remote_id),
                          source_url=COALESCE(excluded.source_url,attachments.source_url),raw_json=excluded.raw_json
                    ''', (chat_id,row['id'],att['position'],att['kind'],att['name'],att['mime_type'],
                          att['remote_id'],att['url'],att['raw_json']))
            return len(rows)

    def list_chats(self):
        return [dict(r) for r in self.connection.execute('''
            SELECT c.chat_id,c.title,c.kind,COUNT(m.message_id) AS messages,
                   COALESCE(v.state,'unknown') AS coverage
            FROM chats c LEFT JOIN messages m ON m.chat_id=c.chat_id
            LEFT JOIN coverage v ON v.chat_id=c.chat_id
            GROUP BY c.chat_id ORDER BY c.updated_at DESC,c.title
        ''')]

    def messages(self, chat_id: str):
        return self.connection.execute('''
            SELECT * FROM messages WHERE chat_id=?
            ORDER BY sent_at IS NULL,sent_at,message_id
        ''', (chat_id,))

    def media_queue(self, chat_id: str, kind: str = 'all', since: str | None = None, include_failed=True):
        args = [chat_id]
        where = ['a.chat_id=?', "a.status != 'downloaded'"]
        if not include_failed:
            where.append("a.status = 'pending'")
        if kind != 'all':
            where.append('a.kind=?')
            args.append(kind)
        if since:
            where.append('m.sent_at>=?')
            args.append(since)
        return [dict(r) for r in self.connection.execute('''
            SELECT a.*,m.sent_at,c.title AS chat_title FROM attachments a
            JOIN messages m ON a.chat_id=m.chat_id AND a.message_id=m.message_id
            JOIN chats c ON c.chat_id=a.chat_id WHERE '''+' AND '.join(where)+' ORDER BY m.sent_at,a.message_id,a.position',args)]

    def mark_downloaded(self, chat_id: str, message_id: str, position: int, relative_path: str, size: int, digest: str):
        with self.transaction():
            self.connection.execute('''
                UPDATE attachments SET local_path=?,file_size=?,sha256=?,status='downloaded',
                     last_error=NULL,downloaded_at=?
                WHERE chat_id=? AND message_id=? AND position=?
            ''',(relative_path,size,digest,utcnow(),chat_id,message_id,position))

    def mark_failed(self, chat_id: str, message_id: str, position: int, reason: str):
        with self.transaction():
            self.connection.execute('''
                UPDATE attachments SET status='failed',last_error=?
                WHERE chat_id=? AND message_id=? AND position=?
            ''',(reason[:1000],chat_id,message_id,position))

    def media_stats(self, chat_id: str):
        return [dict(x) for x in self.connection.execute('''
          SELECT kind,status,COUNT(*) AS count FROM attachments WHERE chat_id=? GROUP BY kind,status ORDER BY kind,status
        ''',(chat_id,))]

    def set_coverage(self, chat_id: str, state: str, report: dict):
        if state not in {'full','partial','unknown'}:
            raise ValueError('Invalid coverage state')
        with self.transaction():
            self.connection.execute('''INSERT INTO coverage(chat_id,state,report_json,checked_at) VALUES(?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET state=excluded.state,report_json=excluded.report_json,
                   checked_at=excluded.checked_at''', (chat_id,state,json.dumps(report,ensure_ascii=False),utcnow()))

    def audit(self, chat_id: str):
        counts = self.connection.execute('''SELECT COUNT(*) AS total,
           SUM(CASE WHEN sent_at IS NULL THEN 1 ELSE 0 END) AS dates_missing,
           SUM(CASE WHEN sender_name IS NULL THEN 1 ELSE 0 END) AS authors_missing,
           MIN(sent_at) AS earliest,MAX(sent_at) AS latest FROM messages WHERE chat_id=?''',(chat_id,)).fetchone()
        coverage = self.connection.execute('SELECT * FROM coverage WHERE chat_id=?',(chat_id,)).fetchone()
        return {**dict(counts), 'coverage': dict(coverage) if coverage else None,
                'media':self.media_stats(chat_id), 'chat_id':chat_id}
