"""MAX sync subprocess tests: live progress, safe output, Windows control-exit."""
from __future__ import annotations

import os
import sys
import unittest

from chempi.max_client import MaxClient, MaxClientError


class SyncStreamingTests(unittest.TestCase):
    def test_live_progress_and_json_result(self):
        client = MaxClient(executable=sys.executable)
        script = (
            'import json,os,sys,time;'
            'print(json.dumps({"event":"progress","step":"sync","fetched":30}), file=sys.stderr, flush=True);'
            'print("30 messages so far, back to 2026-11-19",file=sys.stderr,flush=True);'
            'print(json.dumps({"event":"progress","step":"sync","fetched":60}), file=sys.stderr, flush=True);'
            'print(json.dumps({"fetched":60,"readonly":os.environ.get("MAX_READ_ONLY")}),flush=True)'
        )
        client.command_prefix = [sys.executable, "-u", "-c", script]
        logs = []
        answer = client.sync_page("my-chat", batch_size=60, progress=logs.append)
        self.assertEqual(answer["fetched"], 60)
        self.assertEqual(answer["readonly"], "1")
        self.assertTrue(any("30 messages fetched" in message for message in logs))
        self.assertTrue(any("oldest so far 2026-11-19" in message for message in logs))
        self.assertTrue(any("60 messages fetched" in message for message in logs))

    def test_control_exit_message_does_not_leak_terminal_log(self):
        error = MaxClient._failed_command(3221225786, "messages sync")
        self.assertIsInstance(error, MaxClientError)
        self.assertIn("interrupted", str(error))
        self.assertIn("0xC000013A", str(error))

    def test_sync_rejects_large_batch(self):
        client = MaxClient(executable=sys.executable)
        with self.assertRaises(ValueError):
            client.sync_page("chat", batch_size=1201)


if __name__ == "__main__":
    unittest.main()
