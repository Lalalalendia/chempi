"""Always allow an offline export, including after a partial sync."""
from __future__ import annotations
from pathlib import Path
from tempfile import NamedTemporaryFile
import html
import json
import os

from .database import ArchiveDB
from .model import safe_label

STYLE = '''body{font-family:system-ui,sans-serif;background:#f4f6f9;color:#182337;margin:0;padding:20px}main{max-width:920px;margin:auto}article{background:#fff;border:1px solid #dde3ec;border-radius:12px;margin:10px 0;padding:14px}header{font-size:13px;color:#576476}.message{white-space:pre-wrap;overflow-wrap:anywhere;margin-top:7px}.files{margin-top:10px;font-size:13px}a{color:#165bb0}small{color:#596575}'''


def _atomic_write(path: Path, producer):
    path.parent.mkdir(parents=True,exist_ok=True)
    with NamedTemporaryFile('w',dir=path.parent,encoding='utf-8',delete=False,suffix='.part',newline='\n') as tmp:
        part=Path(tmp.name)
        try:
            producer(tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
        except BaseException:
            part.unlink(missing_ok=True)
            raise
    os.replace(part,path)
    return path


def _message_files(db: ArchiveDB, row):
    return list(db.connection.execute('SELECT * FROM attachments WHERE chat_id=? AND message_id=? ORDER BY position',
                                      (row['chat_id'],row['message_id'])))


def export_jsonl(db: ArchiveDB, chat_id: str) -> Path:
    path=db.root/'exports'/(safe_label(chat_id)+'-messages.jsonl')
    def output(stream):
        for row in db.messages(chat_id):
            data=dict(row)
            data['attachments']=[dict(v) for v in _message_files(db,row)]
            stream.write(json.dumps(data,ensure_ascii=False,sort_keys=True)+'\n')
    return _atomic_write(path,output)


def export_html(db: ArchiveDB, chat_id: str) -> Path:
    path=db.root/'exports'/(safe_label(chat_id)+'-messages.html')
    audit=db.audit(chat_id)
    title_row=db.connection.execute('SELECT title FROM chats WHERE chat_id=?',(chat_id,)).fetchone()
    title=title_row['title'] if title_row else chat_id
    def output(stream):
        stream.write('<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>'+html.escape(title)+'</title><style>'+STYLE+'</style></head><body><main>')
        stream.write('<h1>'+html.escape(title)+'</h1>')
        stream.write('<p>'+str(audit['total'])+' messages. Coverage: '+html.escape((audit['coverage'] or {}).get('state','unknown'))+'</p>')
        for row in db.messages(chat_id):
            sender=html.escape(row['sender_name'] or 'Sender unknown')
            sent=html.escape(row['sent_at'] or 'Time unknown')
            kind=html.escape(row['kind'])
            body=html.escape(row['body'] or '')
            stream.write(f'<article><header>{sender} &middot; {sent} &middot; {kind}</header><div class="message">{body}</div>')
            files=_message_files(db,row)
            if files:
                stream.write('<div class="files">')
                for file in files:
                    label=html.escape(file['original_name'] or file['kind'])
                    rel=file['local_path']
                    if rel:
                        filepath=(db.root/rel).resolve() if not Path(rel).is_absolute() else Path(rel)
                        if filepath.is_file():
                            # URL is relative to exports/; build a clean file:// URL if external.
                            from urllib.parse import quote
                            href=filepath.as_uri()
                            stream.write(f'<div><a rel="noreferrer" href="{html.escape(href,quote=True)}">{label}</a></div>')
                            continue
                    stream.write(f'<div>{label} (not downloaded)</div>')
                stream.write('</div>')
            stream.write('</article>\n')
        stream.write('</main></body></html>')
    return _atomic_write(path,output)
