"""Windows portable launcher. --self-test does not use a MAX session."""
from __future__ import annotations
import os
import sys

def self_test() -> int:
    import tempfile
    from pathlib import Path
    from chempi.max_client import max_command
    from chempi.database import ArchiveDB
    from chempi.cli import default_root
    from PyQt6.QtWidgets import QApplication
    try:
        command = max_command()
        if len(command) != 2 or not all(Path(part).is_file() for part in command):
            raise RuntimeError("Bundled Node.js or MAX CLI entry missing")
        root = Path(sys.executable).resolve().parent
        if not (root / "portable.flag").is_file():
            raise RuntimeError("Portable flag missing")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder:
            db = ArchiveDB(Path(folder))
            try:
                db.list_chats()
            finally:
                db.close()
        if default_root() != root / "data":
            raise RuntimeError("Portable data folder incorrect")
        return 0
    except Exception as error:
        print("CHEMPI_PORTABLE_SELFTEST_FAILED: " + str(error), file=sys.stderr)
        return 1

if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--self-test":
        raise SystemExit(self_test())
    from chempi.gui import main
    raise SystemExit(main())
