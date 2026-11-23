import json
import tempfile
import unittest
from pathlib import Path

from chempi.model import attachment_kind, normalize_message, parse_jsonl, parse_time


class ModelTests(unittest.TestCase):
    def test_circles_only_when_explicit(self):
        for item in ({'kind':'video_circle'}, {'kind':'video','round':True},
                     {'kind':'video', 'metadata':{'isRound':True}},
                     {'type':'video_note'}):
            self.assertEqual(attachment_kind(item),'video_circle')
        self.assertEqual(attachment_kind({'kind':'video'}),'video')
        self.assertEqual(attachment_kind({'kind':'bogus'}),'unknown')

    def test_sender_and_time_are_not_invented(self):
        row=normalize_message({'id':'100','chat_id':'7','text':'renamed chat','kind':'control'})
        self.assertEqual(row['id'],'100')
        self.assertIsNone(row['sender_name'])
        self.assertIsNone(row['timestamp'])
        self.assertEqual(row['kind'],'control')

    def test_timestamps_utc(self):
        self.assertEqual(parse_time('2026-01-02T12:03:00+03:00')[:19], '2026-01-02T09:03:00')
        self.assertIsNone(parse_time('22:03'))

    def test_jsonl_envelope(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'input.jsonl'
            p.write_text('\n'.join([
                json.dumps({'type':'item','data':{'id':12,'chat_id':8,'text':'first'}}),
                json.dumps({'type':'batch_summary','count':1}),
                json.dumps({'id':13,'chat_id':8,'text':'second'}),
            ]),encoding='utf-8')
            items=list(parse_jsonl(p))
            self.assertEqual([x['id'] for x in items],[12,13])


if __name__=='__main__':unittest.main()
