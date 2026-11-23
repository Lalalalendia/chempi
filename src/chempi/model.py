"""Normalize unofficial MAX JSON without inventing missing facts."""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import hashlib
import json
import re


def first(item: dict, *keys: str):
    for key in keys:
        if key in item and item[key] is not None:
            return item[key]
    return None


def unwrap(item: Any) -> dict | None:
    if not isinstance(item, dict):
        return None
    if item.get('type') in {'end', 'batch_summary', 'progress', 'error'}:
        return None
    if item.get('type') == 'item' and isinstance(item.get('data'), dict):
        return item['data']
    if 'id' in item and (first(item, 'chat_id', 'chatId') is not None or 'deleted' in item):
        return item
    return None


def parse_jsonl(path: Path):
    with path.open('r', encoding='utf-8-sig') as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f'Invalid JSONL line {line_no}: {error}') from error
            for entry in value if isinstance(value, list) else [value]:
                row = unwrap(entry)
                if row is not None:
                    yield row


def text_id(value: Any) -> str | None:
    if isinstance(value, (str, int)) and str(value).strip():
        return str(value)
    return None


def parse_time(value: Any) -> str | None:
    if isinstance(value, (int, float)):
        try:
            sec = value / 1000 if abs(value) > 10**11 else value
            return datetime.fromtimestamp(sec, timezone.utc).isoformat(timespec='milliseconds')
        except (OverflowError, ValueError, OSError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        raw = value.strip()
        if raw.isdigit():
            return parse_time(int(raw))
        dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            return None
        return dt.astimezone(timezone.utc).isoformat(timespec='milliseconds')
    except ValueError:
        return None


CIRCLES = {'video_circle', 'video-circle', 'video_message', 'videomessage', 'video_note', 'round_video', 'roundvideo', 'circle', 'videonote'}
KIND_MAP = {
    'photo': 'photo', 'image': 'photo', 'picture': 'photo',
    'video': 'video', 'movie': 'video', 'gif': 'video', 'animation': 'video',
    'voice': 'voice', 'voice_message': 'voice', 'voice-note': 'voice',
    'audio': 'audio', 'music': 'audio', 'file': 'file', 'document': 'file',
    'doc': 'file', 'link': 'link', 'sticker': 'sticker',
}


def attachment_kind(value: dict) -> str:
    meta = first(value, 'metadata', 'meta', 'video')
    tags = [first(value, 'kind', 'type', 'media_kind', 'mediaType', 'attachment_type')]
    if isinstance(meta, dict):
        tags.extend(first(meta, k) for k in ('kind', 'type', 'mediaType'))
    if any(isinstance(t, str) and t.lower().replace(' ', '_') in CIRCLES for t in tags):
        return 'video_circle'
    for flag in ('round', 'is_round', 'isRound', 'is_video_note', 'isVideoMessage', 'video_message', 'circular'):
        if value.get(flag) is True or isinstance(meta, dict) and meta.get(flag) is True:
            return 'video_circle'
    raw = first(value, 'kind', 'type', 'media_kind', 'mediaType', 'attachment_type')
    if not isinstance(raw, str):
        return 'unknown'
    return KIND_MAP.get(raw.strip().lower().replace(' ', '_'), 'unknown')


def normalize_attachment(value: dict, position: int) -> dict:
    provider = first(value, 'providerRef', 'provider_ref')
    if not isinstance(provider, dict):
        provider = {}
    name = first(value, 'name', 'filename', 'file_name')
    mime = first(value, 'mimeType', 'mime_type', 'mime')
    return {
        'position': position,
        'kind': attachment_kind(value),
        'name': str(name) if name is not None else None,
        'mime_type': str(mime) if mime is not None else None,
        'remote_id': text_id(first(value, 'id', 'fileId', 'videoId') or first(provider, 'fileId', 'videoId')),
        'url': first(value, 'url', 'source_url'),
        'raw_json': json.dumps(value, ensure_ascii=False, sort_keys=True),
    }


def normalize_message(row: dict, fallback_chat_id: str | None = None) -> dict | None:
    mid = text_id(first(row, 'id', 'message_id', 'messageId'))
    chat_id = text_id(first(row, 'chat_id', 'chatId')) or fallback_chat_id
    if not mid or not chat_id:
        return None
    sender = first(row, 'sender_name', 'senderName', 'author')
    if isinstance(sender, dict):
        sender = first(sender, 'name', 'title')
    body = first(row, 'text', 'body', 'content')
    if isinstance(body, dict):
        body = first(body, 'text', 'body')
    media = first(row, 'attachments', 'media', 'files')
    if not isinstance(media, list):
        media = []
    reactions = first(row, 'reactions')
    return {
        'id': mid, 'chat_id': chat_id,
        'sender_id': text_id(first(row, 'sender_id', 'senderId', 'user_id')),
        'sender_name': str(sender) if sender not in (None, '') else None,
        'timestamp': parse_time(first(row, 'timestamp', 'sent_at', 'date', 'created_at')),
        'edited_at': parse_time(first(row, 'edited_at', 'editedAt')),
        'text': str(body) if body is not None else None,
        'kind': str(first(row, 'kind', 'message_type', 'type') or 'message'),
        'deleted': row.get('deleted') is True,
        'reply_to': text_id(first(row, 'reply_to', 'replyTo', 'reply_to_id')),
        'reactions_json': json.dumps(reactions, ensure_ascii=False) if reactions is not None else None,
        'raw_json': json.dumps(row, ensure_ascii=False, sort_keys=True),
        'attachments': [normalize_attachment(v, i + 1) for i, v in enumerate(media) if isinstance(v, dict)],
    }


def safe_label(value: str, max_len: int = 72) -> str:
    result = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', '_', str(value)).strip(' .')
    return (result[:max_len].rstrip(' .') or 'chat')


def stable_file_stem(chat_id: str, message_id: str, position: int) -> str:
    return hashlib.sha256(f'{chat_id}:{message_id}:{position}'.encode()).hexdigest()[:24]
