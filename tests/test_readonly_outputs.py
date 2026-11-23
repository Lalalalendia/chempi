"""Protect remote MAX from writes while permitting local archive files.

WireCat's MAX_READ_ONLY=1 rejects --output and --output-dir unless
MAX_WORK_FOLDER encloses the chosen output. Never disable the lock for media.
"""
from __future__ import annotations
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chempi.max_client import MaxClient


class WorkFolderTests(unittest.TestCase):
    def test_readonly_store_export_uses_only_export_parent(self):
        seen = []
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / "staged" / "history.jsonl"
            output.parent.mkdir()
            def runner(command, **options):
                seen.append((command, options["env"].copy()))
                Path(command[command.index("--output") + 1]).write_text('{"type":"end"}\n', encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout='{"item":{"path":"ok"}}', stderr="")
            cli = MaxClient(executable="max", runner=runner)
            cli.store_export("some-chat", output)
            self.assertEqual(len(seen), 1)
            command, env = seen[0]
            self.assertEqual(env["MAX_READ_ONLY"], "1")
            self.assertEqual(Path(env["MAX_WORK_FOLDER"]), output.parent.resolve())
            self.assertEqual(command[command.index("--output") + 1], str(output))

    def test_readonly_media_download_uses_only_download_folder(self):
        seen = []
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def runner(command, **options):
                seen.append((command, options["env"].copy()))
                return SimpleNamespace(returncode=0, stdout='{"item":{"saved":1}}', stderr="")
            cli = MaxClient(executable="max", runner=runner)
            cli.media_download("some-chat", "message-1", 2, root / "staging")
            command, env = seen[0]
            self.assertEqual(env["MAX_READ_ONLY"], "1")
            self.assertEqual(Path(env["MAX_WORK_FOLDER"]), (root / "staging").resolve())
            self.assertEqual(command[command.index("--attachment") + 1], "2")

    def test_plain_commands_cannot_inherit_unrestricted_folder(self):
        seen = []
        def runner(command, **options):
            seen.append(options["env"])
            return SimpleNamespace(returncode=0, stdout='{"item":{"messages":1}}', stderr="")
        with patch.dict("os.environ", {"MAX_WORK_FOLDER": "C:/sensitive"}, clear=False):
            cli = MaxClient(executable="max", runner=runner)
            cli.coverage("some-chat")
        self.assertEqual(seen[0]["MAX_READ_ONLY"], "1")
        self.assertNotIn("MAX_WORK_FOLDER", seen[0])


if __name__ == "__main__":
    unittest.main()
