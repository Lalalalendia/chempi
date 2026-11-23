"""Small subprocess adapter around independently installed @wirecat/max-cli.

No auth tokens are copied into Chempi: MAX client keeps its own keyring/session.
All commands here are read or local export/download only.
"""
from __future__ import annotations
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys


class MaxClientError(RuntimeError):
    pass


def max_command() -> list[str]:
    """Locate the embedded Node/MAX runtime before checking global installs."""
    if getattr(sys, "frozen", False):
        root = Path(sys.executable).resolve().parent
        node = root / "runtime" / "node.exe"
        entry = root / "runtime" / "node_modules" / "@wirecat" / "max-cli" / "dist" / "bin" / "max.js"
        if node.is_file() and entry.is_file():
            return [str(node), str(entry)]
        # An incomplete portable package must not silently execute a different
        # globally installed client with possibly different behavior.
        raise MaxClientError("Portable runtime incomplete: node.exe or MAX CLI missing. Re-extract the complete ZIP.")
    for item in (('max.cmd', 'max.exe', 'max') if os.name == 'nt' else ('max',)):
        path = shutil.which(item)
        if path:
            return [path]
    raise MaxClientError('MAX CLI not found. Install Node.js 22.16+ and run: npm install -g @wirecat/max-cli')


def max_executable() -> str:
    """Legacy helper for callers that need to display the executable path."""
    return max_command()[0]


def extract_payload(raw: str):
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise MaxClientError('MAX client did not return valid JSON') from error
    if isinstance(data, dict) and data.get('error'):
        raise MaxClientError(str(data['error'])[:500])
    return data


class MaxClient:
    def __init__(self, profile='personal', executable: str | None = None, runner=None):
        if not profile.replace('-', '').replace('_', '').isalnum():
            raise ValueError('Invalid profile name')
        self.profile = profile
        self.command_prefix = [executable] if executable else max_command()
        self.executable = self.command_prefix[0]
        self.runner = runner or subprocess.run

    def run(self, *arguments, timeout=3600, json_output=True):
        command = [*self.command_prefix, self.profile, *map(str,arguments)]
        if json_output:
            command.append('--json')
        try:
            response = self.runner(command, capture_output=True, text=True,
                                   encoding='utf-8', errors='replace', timeout=timeout, check=False)
        except subprocess.TimeoutExpired as error:
            raise MaxClientError(f'Timeout during {arguments[0:2]}') from error
        if response.returncode:
            # Avoid showing raw traceback / message content in GUI. Tool stdout can
            # contain private messages; show only a redacted error category.
            raw = (response.stderr or '')[-1500:]
            if response.returncode == 8:
                raise MaxClientError('MAX rate limit reached. Retry after its cooldown.')
            message = 'authorization required' if response.returncode == 4 else 'command failed'
            raise MaxClientError(f'{message} (exit {response.returncode}; {arguments[0:2]}). '+raw[:250])
        return extract_payload(response.stdout) if json_output else response.stdout

    def chats(self):
        data = self.run('chats','list','--all')
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ('items', 'chats', 'data', 'rows'):
                if isinstance(data.get(key), list):
                    return data[key]
        raise MaxClientError('Unexpected chats format; update CLI adapter')

    def store_export(self, chat_id: str, path: Path):
        # target path must not exist: max refuses to overwrite it.
        if path.exists():
            raise FileExistsError(path)
        self.run('store','export',chat_id,'--format','jsonl','--output',str(path),timeout=1800)
        if not path.is_file():
            raise MaxClientError('MAX did not create export file')
        return path

    def sync_page(self, chat_id: str, batch_size=1200):
        return self.run('messages','sync',chat_id,'--full-history','--limit',str(batch_size), timeout=1800)

    def coverage(self, chat_id: str):
        return self.run('store','status',chat_id,timeout=90)

    def all_coverage(self):
        return self.run('store','chats','status',timeout=90)

    def media_download(self, chat_id: str, message_id: str, position: int, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        # This command downloads only one attachment (1-based file position).
        return self.run('messages','download',chat_id,message_id,
                        '--attachment',str(position),'--output-dir',str(folder),timeout=3600)


def launch_setup_terminal(profile='personal'):
    """Open an interactive terminal for QR setup; don't touch credentials."""
    command = [*max_command(), profile, 'setup', '--agent', 'none']
    if os.name == 'nt':
        # A separate visible console displays the QR code; nothing is copied
        # into the app's log or SQLite. list2cmdline handles paths with spaces.
        line = subprocess.list2cmdline(command)
        return subprocess.Popen(['cmd.exe', '/d', '/k', line],
                                creationflags=subprocess.CREATE_NEW_CONSOLE)
    for terminal in ('x-terminal-emulator', 'gnome-terminal', 'konsole', 'xterm'):
        exe = shutil.which(terminal)
        if exe:
            if terminal == 'gnome-terminal':
                return subprocess.Popen([exe, '--', *command])
            return subprocess.Popen([exe, '-e', *command])
    raise MaxClientError(f'No terminal found. Run manually: {subprocess.list2cmdline(command)}')
