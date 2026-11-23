"""CLI also works without PyQt6 for repeatable, testable backup jobs."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
import os

from .database import ArchiveDB
from .exporter import export_html, export_jsonl
from .max_client import MaxClient, MaxClientError, launch_setup_terminal
from .service import ArchiveService


def default_root() -> Path:
    override = os.environ.get('CHEMPI_ARCHIVE_DIR')
    if override:
        return Path(override).expanduser()
    if getattr(sys, 'frozen', False):
        exe_folder = Path(sys.executable).resolve().parent
        if (exe_folder / 'portable.flag').is_file():
            return exe_folder / 'data'
    return Path.home() / 'Documents' / 'Chempi-MAX'


def parser():
    p=argparse.ArgumentParser(prog='chempi',description='Local MAX chat backup, SQLite + files')
    p.add_argument('--root',type=Path,default=default_root(),help='Local archive directory')
    p.add_argument('--profile',default='personal',help='MAX CLI local profile name')
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('setup',help='Open MAX QR login terminal')
    sub.add_parser('chats',help='Refresh chat list')
    s=sub.add_parser('sync',help='Download history into SQLite incrementally')
    s.add_argument('chat_id');s.add_argument('--passes',type=int,default=4)
    d=sub.add_parser('download',help='Download only selected missing attachments')
    d.add_argument('chat_id');d.add_argument('--kind',choices=['all','video_circle','video','photo','voice','audio','file','sticker','unknown'],default='video_circle')
    d.add_argument('--since',help='ISO8601 date/time');d.add_argument('--limit',type=int,default=200)
    d.add_argument('--verify',action='store_true',help='Check SHA-256 for downloaded files first')
    e=sub.add_parser('export',help='Export already stored chat, no network')
    e.add_argument('chat_id');e.add_argument('--format',choices=['html','jsonl','both'],default='both')
    a=sub.add_parser('audit',help='Check local message coverage, missing fields and files')
    a.add_argument('chat_id')
    i=sub.add_parser('inventory',help='Hash existing downloads; no unverified automatic matching')
    i.add_argument('folder',type=Path)
    imp=sub.add_parser('import',help='Import a MAX CLI JSONL history export')
    imp.add_argument('jsonl',type=Path);imp.add_argument('--chat-id',default=None)
    sub.add_parser('gui',help='Open desktop app (requires PyQt6)')
    return p


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        if args.command=='gui':
            from .gui import main as launch
            return launch(args.root,args.profile)
        if args.command=='setup':
            launch_setup_terminal(args.profile)
            print('Login window opened. Scan QR using MAX.')
            return 0
        online=args.command in ('chats','sync','download')
        client=MaxClient(args.profile) if online else None
        service=ArchiveService(args.root,client)
        try:
            if args.command=='chats':
                output=service.refresh_chats(print)
            elif args.command=='sync':
                output=service.sync_chat(args.chat_id,args.passes,print)
            elif args.command=='download':
                output=service.download_media(args.chat_id,args.kind,args.since,args.limit,print,verify=args.verify)
            elif args.command=='audit':
                output=service.db.audit(args.chat_id)
            elif args.command=='inventory':
                output={'indexed':service.inventory_existing(args.folder,print)}
            elif args.command=='import':
                output={'imported_and_updated':service.import_jsonl(args.jsonl,args.chat_id)}
            elif args.command=='export':
                output={}
                if args.format in ('html','both'):
                    output['html']=str(export_html(service.db,args.chat_id))
                if args.format in ('jsonl','both'):
                    output['jsonl']=str(export_jsonl(service.db,args.chat_id))
            print(json.dumps(output,ensure_ascii=False,default=str,indent=2))
            return 0
        finally:
            service.close()
    except (MaxClientError,ValueError,OSError) as exc:
        print(f'ERROR: {exc}',file=sys.stderr)
        return 2

if __name__=='__main__':
    raise SystemExit(main())
