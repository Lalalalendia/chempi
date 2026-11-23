"""Offline checks for embedded MAX command and portable archive location."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from chempi.max_client import MaxClient, MaxClientError, max_command
from chempi.cli import default_root

class PortableTests(unittest.TestCase):
    def test_embedded_runtime_is_selected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            node = root / "runtime" / "node.exe"
            script = root / "runtime" / "node_modules" / "@wirecat" / "max-cli" / "dist" / "bin" / "max.js"
            script.parent.mkdir(parents=True)
            node.write_bytes(b"")
            script.write_bytes(b"")
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(root / "Chempi.exe")):
                self.assertEqual(max_command(), [str(node), str(script)])
                self.assertEqual(MaxClient().command_prefix, [str(node), str(script)])

    def test_incomplete_bundle_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(Path(folder) / "Chempi.exe")):
                with self.assertRaises(MaxClientError):
                    max_command()

    def test_portable_archive_dir(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "portable.flag").write_text("portable")
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(root / "Chempi.exe")):
                self.assertEqual(default_root(), root / "data")
                with patch.dict("os.environ", {"CHEMPI_ARCHIVE_DIR": str(root / "external")}):
                    self.assertEqual(default_root(), root / "external")

if __name__ == "__main__":
    unittest.main()
