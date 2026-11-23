# Chempi MAX Archive

Local-first MAX archive (Windows desktop + CLI): SQLite messages, separate media files,
SHA-256 verification, selective downloads and offline HTML / JSONL exports.

**Status:** first working development version, tested with a mocked MAX client.
**Not yet validated against a live account, nor against real video circles.**

## How it works

Chempi does not guess message authors or timestamps from browser HTML. It uses the
separately installed, unofficial [@wirecat/max-cli](https://github.com/WireCatLabs/max-cli),
which reads authorized MAX account history and maintains its own authenticated session.
Chempi calls only the read, local-export and local-download commands. MAX credentials
are stored by max-cli, never copied to the Chempi database.

```
MAX (QR login in max-cli)
  -> max-cli local store -> offline JSONL -> Chempi SQLite
  -> media queue -> media/<chat-id>/<type>/<stable-id>.<ext>
  -> offline exports/*.html and *.jsonl
```

## Portable Windows EXE (no Python/Node installation)

The [Windows portable build](.github/workflows/build-portable.yml) publishes an
experimental `Chempi-MAX-Windows-portable.zip` as a GitHub prerelease and
as a downloadable GitHub Actions artifact after successful smoke tests.

1. Extract the **whole** `Chempi` folder and run `Chempi.exe`. Keep `_internal/`
   and `runtime/` together with it. The EXE alone is not enough.
2. Click `MAX login (QR)`, scan the QR in the new console. This is a fresh
   local session managed by the bundled MAX CLI, not Chrome cookie extraction.
3. Refresh chats, sync messages, and choose `Video circles only` to download
   only unarchived files whose metadata identifies them as circles.
4. Your SQLite database, media and exports stay in `Chempi/data/` beside
   the EXE; **preserve this folder when updating**. You can set the
   `CHEMPI_ARCHIVE_DIR` environment variable to use a different directory.

This is a portable application **folder**, not a one-file self-extractor.
It includes Node.js, a pinned `@wirecat/max-cli`, Python and Qt libraries;
installing Python or Node yourself is unnecessary. The MAX CLI may store its
own login credentials separately in your Windows user profile. **Real MAX
login and videonote downloads have not been validated in CI.**

## Windows quick start (development from source)

1. Install Python 3.11+ and Node.js 22.16+ or 24+.
2. Install MAX CLI yourself from its trusted source: `npm install -g @wirecat/max-cli`.
3. Clone or download this repo; double-click `START-WINDOWS.bat`.
4. In Chempi click **MAX login (QR)**. Scan the QR code displayed in the new terminal
   and finish the MAX authorization. The login persists unless MAX revokes it.
5. Click **Refresh chat list**, select a chat and click **Sync messages**. Repeat
   until coverage is explicitly confirmed as `full`. A successful single pass
   is **not** proof of complete history.
6. Choose **Video circles only** or another media type, then click **Download
   missing files**. Existing Chempi downloads are skipped by default.
7. Click **Export HTML + JSONL** at any point, even with partially synced history.

The source build defaults to `%USERPROFILE%\\Documents\\Chempi-MAX`; the portable EXE stores them beside the program under `Chempi/data/`.
Never commit your archive, browser profile, token, or downloaded files to Git.

### Command line (without PyQt6)

```powershell
py -3 -m pip install -e .
py -3 -m chempi setup
py -3 -m chempi chats
py -3 -m chempi sync CHAT_ID --passes 4
py -3 -m chempi audit CHAT_ID
py -3 -m chempi download CHAT_ID --kind video_circle --limit 200
py -3 -m chempi download CHAT_ID --kind video --verify
py -3 -m chempi export CHAT_ID --format both
py -3 -m chempi inventory "D:\\Existing MAX Downloads"
```

### Storage

* `max.sqlite` is the source of truth for chat, message and media metadata.
* `media/` holds original downloaded bytes, **not** a SQLite BLOB.
* `exports/` holds readable HTML and machine-readable JSONL.
* `file_inventory` records hashes of earlier downloads. It does **not** assume
  two files with similar names belong to the same message; matching unindexed
  old media requires a verified message ID or digest.

A file's filesystem modification time is set to the message's original server
send time when that time is available. We never rewrite embedded EXIF or MP4 tags.

## Honest limitations

* MAX CLI and the underlying protocol are **unofficial** and can change or
  stop working; using them may conflict with MAX service rules.
* Authorization happens via a local QR login in **max-cli**, **not** via the
  already-authorized Chrome profile. Browser cookies are never extracted.
* `video_circle` is only recognized when the source payload contains an
  explicit circle/video-note marker. Some actual circles may arrive as plain
  `video`; those require investigation from real sanitized metadata.
* Actual downloadability of circles, old videos and voice notes depends on
  what MAX still serves and max-cli's support for that media type.
* The `--attachment` position follows MAX CLI file numbering. If the client
  returns multiple files for one requested item, Chempi rejects attribution
  rather than silently attaching the wrong file.
* An `unknown` attachment is never downloaded as a video by guessing.
* A successful `sync` is not proof of completeness. `audit` shows missing
  timestamps, authors, metadata coverage and media status.
* The first app version indexes previously downloaded files by SHA-256 but
  **does not automatically claim them as corresponding to messages** without
  identity evidence. It will not silently skip a different video.
* Live account login, real server history, media streaming and Windows UI
  need a real Windows/MAX validation run. CI uses deterministic fake data.

## Security

No telemetry, server, tracking, account-token upload, remote script injection,
or write/delete-message actions exist in Chempi. The MAX CLI is a separate
open-source dependency; review its permissions and source before authorization.
Chempi sends no outbound network request itself. Set max-cli message permissions
read-only if you give other software access to its session.

Never upload a real `max.sqlite` or MAX authentication state into bug reports.

## Tests

`python -m unittest discover -s tests -v`
