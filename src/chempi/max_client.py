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
import re
import threading
from collections import deque


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

    @staticmethod
    def _runtime_environment(work_folder=None):
        env = os.environ.copy()
        # MAX CLI read/download functions must not send, edit or delete messages.
        # Setup/login runs separately and is intentionally not subject to this.
        env['MAX_READ_ONLY'] = '1'
        env['MAX_NO_UPDATE_CHECK'] = '1'
        # Under the WireCat read-only lock, --output/--output-dir is
        # explicitly refused unless MAX_WORK_FOLDER encloses the output.
        # Each caller authorizes only its own temporary subfolder.
        env.pop('MAX_WORK_FOLDER', None)
        if work_folder is not None:
            directory = Path(work_folder).resolve(strict=True)
            if not directory.is_dir():
                raise MaxClientError('MAX download work folder must be a directory')
            env['MAX_WORK_FOLDER'] = str(directory)
        return env

    @staticmethod
    def _windows_flags():
        return subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

    @staticmethod
    def _failed_command(returncode, operation):
        code = returncode & 0xffffffff
        if code == 0xC000013A:
            return MaxClientError(
                'MAX CLI was interrupted by a Windows console-control event '
                '(0xC000013A). Already fetched pages may be in its local store. '
                'Restart sync; Chempi will import them without duplicates.'
            )
        if returncode == 8:
            return MaxClientError('MAX rate limit reached. Retry after cooldown.')
        if returncode == 4:
            return MaxClientError('MAX authorization required. Sign in again.')
        return MaxClientError(f'MAX CLI {operation} failed (exit {returncode}). '
                              'Existing archive data were not deleted.')

    def run(self, *arguments, timeout=3600, json_output=True, work_folder=None):
        """Small read-only CLI calls. History sync has its own live-progress path."""
        command = [*self.command_prefix, self.profile, *map(str, arguments)]
        if json_output:
            command.append('--json')
        try:
            response = self.runner(command, capture_output=True, text=True,
                                   encoding='utf-8', errors='replace', timeout=timeout,
                                   check=False, env=self._runtime_environment(work_folder),
                                   creationflags=self._windows_flags())
        except subprocess.TimeoutExpired as error:
            raise MaxClientError(f'MAX CLI timeout during {arguments[:2]}') from error
        if response.returncode:
            raise self._failed_command(response.returncode, ' '.join(map(str, arguments[:2])))
        return extract_payload(response.stdout) if json_output else response.stdout

    def _streaming_sync(self, command, progress, timeout=1800):
        """Drain stderr as it arrives instead of waiting minutes inside run().

        max-cli emits JSONL progress on stderr and one JSON result on stdout.
        Only numeric progress is shown; no chat content is ever logged here.
        """
        stdout_parts = []
        stderr_tail = deque(maxlen=8)
        stdout_oversize = [False]
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding='utf-8',
                errors='replace', bufsize=1, env=self._runtime_environment(),
                creationflags=self._windows_flags(),
            )
        except OSError as error:
            raise MaxClientError(f'Cannot start MAX CLI: {type(error).__name__}') from error

        def read_stdout():
            length = 0
            for line in process.stdout:
                length += len(line)
                if length <= 4_000_000:
                    stdout_parts.append(line)
                else:
                    stdout_oversize[0] = True

        def read_stderr():
            previous = None
            for line in process.stderr:
                value = line.strip()
                if not value:
                    continue
                stderr_tail.append(value[:180])
                found = None
                try:
                    payload = json.loads(value)
                except (ValueError, TypeError):
                    payload = None
                if isinstance(payload, dict) and payload.get('event') == 'progress':
                    if payload.get('step') == 'sync':
                        count = payload.get('fetched')
                        if isinstance(count, int) and count >= 0:
                            found = f'MAX: {count} messages fetched in current batch'
                else:
                    match = re.search(r'(\d+) messages so far, back to (\d{4}-\d{2}-\d{2})', value)
                    if match:
                        found = f'MAX: {match.group(1)} messages; oldest so far {match.group(2)}'
                if found and found != previous and progress:
                    previous = found
                    progress(found)

        out_thread = threading.Thread(target=read_stdout, daemon=True)
        err_thread = threading.Thread(target=read_stderr, daemon=True)
        out_thread.start()
        err_thread.start()
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.wait()
            raise MaxClientError('MAX sync batch timed out; locally cached messages can be recovered') from error
        finally:
            out_thread.join(timeout=5)
            err_thread.join(timeout=5)
            process.stdout.close()
            process.stderr.close()

        if code != 0:
            raise self._failed_command(code, 'messages sync')
        if stdout_oversize[0]:
            raise MaxClientError('MAX CLI returned unexpectedly large sync result')
        return extract_payload(''.join(stdout_parts))

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
        self.run('store','export',chat_id,'--format','jsonl','--output',str(path),
                 timeout=1800,work_folder=path.parent)
        if not path.is_file():
            raise MaxClientError('MAX did not create export file')
        return path

    def sync_page(self, chat_id: str, batch_size=60, progress=None):
        """Fetch at most two MAX pages per command, so Stop is responsive.

        Each repeated command resumes history based on the MAX CLI local store.
        Progress is forwarded immediately from stderr, not after completion.
        """
        if not 1 <= batch_size <= 1200:
            raise ValueError('Invalid MAX sync batch size')
        command = [*self.command_prefix, self.profile, 'messages', 'sync',
                   str(chat_id), '--full-history', '--limit', str(batch_size), '--json']
        return self._streaming_sync(command, progress, timeout=1800)

    def coverage(self, chat_id: str):
        return self.run('store','status',chat_id,timeout=90)

    def all_coverage(self):
        return self.run('store','chats','status',timeout=90)

    def media_download(self, chat_id: str, message_id: str, position: int, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        # This command downloads only one attachment (1-based file position).
        return self.run('messages','download',chat_id,message_id,
                        '--attachment',str(position),'--output-dir',str(folder),
                        timeout=3600,work_folder=folder)


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
