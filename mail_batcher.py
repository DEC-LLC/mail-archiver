"""Batched IMAP sync — added 1.0.15; intra-folder chunking added 1.0.17.

For large accounts (Gmail 100k+, Exchange folder-heavy), a single mbsync
run opens ONE IMAP connection and keeps it open for the entire sync.
Server-side idle timeouts, TLS session expiry, and network dropouts on
multi-hour syncs surface to the user as "Sync failed" with no partial
progress preserved.

The batcher solves this by:

  1. Opening ONE cheap IMAP connection up front to `STATUS` every folder.
  2. If total message count <= threshold — return None so the caller
     runs the normal single-mbsync path (zero overhead on small accounts).
  3. Otherwise partition folders (smallest-first, greedy) into batches
     each <= threshold, then invoke mbsync once per batch with a temp
     mbsyncrc that overrides `Patterns` to just that batch's folders.
     Fresh mbsync process = fresh IMAP connection = fresh TLS session.
  4. Retry a failed batch once. After the retry, log the failure and
     continue with the NEXT batch. Partial progress > total failure.

1.0.17 — intra-folder chunked sync:

  When a single-folder batch exceeds MAIL_ARCHIVER_CHUNK_THRESHOLD msgs,
  run mbsync in a time-boxed chunk loop instead of one long invocation.
  mbsync commits maildir state atomically per message, so re-running it
  after a partial fetch picks up where the previous run stopped. Between
  chunks we get a fresh IMAP connection + fresh TLS session + reset
  server-side idle counter. Aborts after MAX_STUCK_CHUNKS consecutive
  chunks with zero new messages landed.

Progress dict fields fed to `progress_cb` on every state change:
    batch_current, batch_total, batch_folder_count, batch_msg_estimate,
    folder_name, folder_msg_total, folder_msg_done,
    folder_chunk_current, folder_chunk_wall_seconds,
    state ('measuring' | 'syncing' | 'chunk' | 'batch_retry' |
           'batch_failed' | 'done'),
    last_line (mbsync stderr tail, one line).

Per-batch mbsync stderr is also parsed by the caller (app.py's existing
`_parse_mbsync_line`) via a line callback — this module doesn't duplicate
that parsing; it delegates line handling back to the caller so the SSE
plumbing in app.py stays the single source of truth for the C:/B:/M:
format.
"""

from __future__ import annotations

import imaplib
import os
import re
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple


class BatcherError(Exception):
    pass


# --- Folder measurement --------------------------------------------------


def _connect_imap(host: str, port: int, tls_mode: str) -> imaplib.IMAP4:
    """Open an IMAP connection using the same TLS mode mbsync would use."""
    tls_mode = (tls_mode or 'ssl').lower()
    if tls_mode == 'ssl':
        return imaplib.IMAP4_SSL(host, port, timeout=30)
    conn = imaplib.IMAP4(host, port, timeout=30)
    if tls_mode == 'starttls':
        conn.starttls()
    return conn


def _authenticate(conn: imaplib.IMAP4, email: str, credential: str,
                  auth_type: str) -> None:
    if auth_type == 'oauth2':
        # XOAUTH2 SASL: `user=<email>\x01auth=Bearer <token>\x01\x01`
        auth_string = f'user={email}\x01auth=Bearer {credential}\x01\x01'
        conn.authenticate('XOAUTH2', lambda _: auth_string.encode())
    else:
        conn.login(email, credential)


_LIST_RE = re.compile(
    r'\((?P<attrs>[^)]*)\)\s+"(?P<sep>[^"]*)"\s+(?P<name>.*)'
)


def _parse_list_line(line: bytes) -> Optional[str]:
    """LIST response → folder name (or None for \\Noselect entries)."""
    if isinstance(line, bytes):
        line = line.decode('utf-8', errors='replace')
    m = _LIST_RE.match(line.strip())
    if not m:
        return None
    if '\\Noselect' in m.group('attrs'):
        return None
    name = m.group('name').strip()
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1]
    return name


def measure_folders(host: str, port: int, tls_mode: str, email: str,
                    credential: str, auth_type: str,
                    folder_pattern: str = '*') -> List[Tuple[str, int]]:
    """Return [(folder_name, message_count), ...] via one IMAP connection.

    Raises BatcherError on connect/auth failure. On per-folder STATUS
    failure returns 0 for that folder rather than aborting the whole
    measurement (a single broken folder shouldn't kill batching).
    """
    conn = None
    try:
        conn = _connect_imap(host, port, tls_mode)
        _authenticate(conn, email, credential, auth_type)
        typ, data = conn.list('', folder_pattern or '*')
        if typ != 'OK':
            raise BatcherError(f'LIST failed: {typ}')
        folders: List[str] = []
        for raw in data or []:
            if raw is None:
                continue
            name = _parse_list_line(raw)
            if name:
                folders.append(name)
        out: List[Tuple[str, int]] = []
        for name in folders:
            quoted = f'"{name}"' if any(c in name for c in ' "\\') else name
            try:
                typ, sdata = conn.status(quoted, '(MESSAGES)')
            except imaplib.IMAP4.error:
                out.append((name, 0))
                continue
            if typ != 'OK' or not sdata:
                out.append((name, 0))
                continue
            raw = sdata[0]
            if isinstance(raw, bytes):
                raw = raw.decode('utf-8', errors='replace')
            m = re.search(r'MESSAGES\s+(\d+)', raw)
            out.append((name, int(m.group(1)) if m else 0))
        return out
    except (imaplib.IMAP4.error, OSError) as e:
        raise BatcherError(f'IMAP {email}: {e}')
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass


# --- Batch planning (pure function — unit-testable) ----------------------


def plan_batches(folders: Iterable[Tuple[str, int]],
                 threshold: int) -> List[List[Tuple[str, int]]]:
    """Pack folders into batches of <= threshold messages each.

    Smallest-first greedy: sort ascending by message count, fill each
    batch until adding the next folder would exceed threshold. A single
    folder larger than threshold gets its own batch. Empty folders
    still count as "work" but contribute 0 messages — they pack tightly
    at the head.

    Returns list-of-list-of-(name, count) so downstream (run_batched_sync)
    can decide per-batch whether to invoke the intra-folder chunk loop
    (1.0.17) for a batch that is a single folder larger than the chunk
    threshold. JSON-transported across the drop-priv boundary safely —
    tuples round-trip as [name, count] lists which the batcher unpacks.
    """
    ordered = sorted(folders, key=lambda t: (t[1], t[0]))
    batches: List[List[Tuple[str, int]]] = []
    current: List[Tuple[str, int]] = []
    current_msgs = 0
    for name, count in ordered:
        # Single folder larger than threshold — its own batch.
        if count > threshold:
            if current:
                batches.append(current)
                current, current_msgs = [], 0
            batches.append([(name, count)])
            continue
        if current and current_msgs + count > threshold:
            batches.append(current)
            current, current_msgs = [], 0
        current.append((name, count))
        current_msgs += count
    if current:
        batches.append(current)
    return batches


def _normalize_batch(batch) -> List[Tuple[str, int]]:
    """Accept either List[Tuple[str,int]] or List[List[str,int]] (post-JSON)
    or legacy List[str] and return uniform (name, count) tuples.
    Legacy strings default count=0 (chunking won't engage — safe fallback).
    """
    out: List[Tuple[str, int]] = []
    for item in batch:
        if isinstance(item, (list, tuple)):
            if len(item) >= 2:
                out.append((str(item[0]), int(item[1])))
            elif len(item) == 1:
                out.append((str(item[0]), 0))
        else:
            out.append((str(item), 0))
    return out


# --- Intra-folder chunked sync (1.0.17) ----------------------------------

# Env-tunable defaults. Chunk threshold is DELIBERATELY smaller than the
# batch threshold — a batch is a network-boundary optimization; a chunk
# is a wall-clock-boundary optimization. Folders in the 2500..5000 range
# benefit little from chunking (single mbsync usually finishes < CHUNK_WALL)
# but folders in the 5k..500k range benefit massively.

CHUNK_THRESHOLD_DEFAULT = 2500
CHUNK_WALL_SECONDS_DEFAULT = 180  # 3 min per chunk — bounds worst-case loss
MAX_STUCK_CHUNKS = 2              # abort after 2 consecutive no-progress
CHUNK_HARD_TIMEOUT_MARGIN = 30    # seconds beyond wall before hard-kill


def _count_maildir_messages(maildir_path: str) -> int:
    """Count files in `maildir_path/cur` + `maildir_path/new` — fast
    single-level enumeration, no walk into subfolders (mbsync stores
    each folder as its own maildir with its own cur/new).
    """
    total = 0
    for sub in ('cur', 'new'):
        try:
            total += sum(1 for _ in os.scandir(
                os.path.join(maildir_path, sub)))
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            pass
    return total


def _maildir_path_for_folder(maildir_base: str, folder_name: str) -> str:
    """SubFolders Verbatim: mbsync stores each folder as a subdir named
    exactly like the IMAP folder. Slashes in the folder name become path
    separators (matches mbsync's on-disk layout with Verbatim mode).
    """
    return os.path.join(maildir_base, folder_name)


def _run_folder_chunked(
    *,
    rc_path: str,
    channel: str,
    folder_name: str,
    folder_total: int,
    maildir_path: str,
    subprocess_kwargs: dict,
    progress_cb: Optional[Callable[[dict], None]],
    line_cb: Optional[Callable[[str], None]],
    chunk_wall: int,
    max_stuck: int = MAX_STUCK_CHUNKS,
) -> int:
    """Time-boxed loop of mbsync invocations against a single huge folder.

    Each iteration:
      1. Count maildir files (before)
      2. Run mbsync with a wall-clock cap of `chunk_wall` seconds
      3. Count maildir files (after) — delta > 0 means real progress
      4. If mbsync exited 0 cleanly, we're done
      5. If delta == 0 for `max_stuck` consecutive chunks, abort
      6. Otherwise loop — fresh mbsync = fresh IMAP + TLS session

    Returns 0 on success, non-zero on final failure. `line_cb` receives
    every stderr line from every chunk so the existing SSE parsing
    (C:/B:/M:) keeps working — EXCEPT: during chunked runs we mark the
    progress dict with `chunk_mode=True` so line_cb consumers (see
    app.py `_line_cb`) can skip overwriting folder_msg_done with mbsync's
    own C:/M: totals. Chunk-loop before/after counts are the truth here
    because mbsync's totals reset every chunk to the folder's full size.
    """
    chunk = 0
    stuck = 0
    while True:
        chunk += 1
        before = _count_maildir_messages(maildir_path)
        if progress_cb:
            progress_cb({
                'state': 'chunk',
                'chunk_mode': True,
                'folder_name': folder_name,
                'folder_msg_total': folder_total,
                'folder_msg_done': before,
                'folder_chunk_current': chunk,
                'folder_chunk_wall_seconds': chunk_wall,
            })
        kw = dict(subprocess_kwargs)
        kw['stdout'] = subprocess.PIPE
        kw['stderr'] = subprocess.STDOUT
        kw['bufsize'] = 1
        kw['text'] = True
        kw.pop('capture_output', None)
        kw.pop('timeout', None)
        cmd = ['mbsync', '-V', '-c', rc_path, channel]
        proc = subprocess.Popen(cmd, **kw)
        start = time.time()
        wall_expired = False
        try:
            for raw in proc.stdout:
                line = raw.rstrip('\n')
                if line_cb:
                    try:
                        line_cb(line)
                    except Exception:
                        pass
                if time.time() - start > chunk_wall:
                    # Send TERM, then a moment later verify.
                    wall_expired = True
                    proc.terminate()
                    try:
                        proc.wait(timeout=CHUNK_HARD_TIMEOUT_MARGIN)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    break
        finally:
            try:
                proc.wait(timeout=CHUNK_HARD_TIMEOUT_MARGIN)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        after = _count_maildir_messages(maildir_path)
        # `delta` can be negative if a concurrent cleanup ran between
        # our before/after (unlikely — we hold the sync — but be safe):
        # treat negative delta as no progress but do NOT double-count.
        delta = max(0, after - before)
        if progress_cb:
            progress_cb({
                'state': 'chunk',
                'chunk_mode': True,
                'folder_name': folder_name,
                'folder_msg_total': folder_total,
                'folder_msg_done': after,
                'folder_chunk_current': chunk,
                'folder_chunk_wall_seconds': chunk_wall,
            })
        # Clean exit AND not wall-timeout AND we're at target or beyond
        # (or mbsync said "nothing to do"): done.
        if proc.returncode == 0 and not wall_expired:
            if progress_cb:
                # Clear chunk_mode when done so post-chunk line_cb writes
                # (unlikely; mbsync exited) don't get suppressed.
                progress_cb({'state': 'chunk', 'chunk_mode': False,
                             'folder_name': folder_name})
            return 0
        # If we've caught up to (or past) the target and no error, done.
        if after >= folder_total and proc.returncode in (0, None) \
                and not wall_expired:
            if progress_cb:
                progress_cb({'state': 'chunk', 'chunk_mode': False,
                             'folder_name': folder_name})
            return 0
        if delta == 0:
            stuck += 1
            if stuck >= max_stuck:
                if progress_cb:
                    progress_cb({'state': 'chunk', 'chunk_mode': False,
                                 'folder_name': folder_name})
                return proc.returncode if proc.returncode else 2
        else:
            stuck = 0
        # loop — next mbsync opens a fresh IMAP connection


# --- Batched sync driver -------------------------------------------------


def _write_batch_rc(source_rc_text: str, channel: str,
                    folder_names: List[str]) -> str:
    """Rewrite the given mbsyncrc so the matching Channel's Patterns line
    is replaced by the batch's folder list. Returns the temp file path.

    Only the Channel section whose name matches `channel` is edited.
    Everything else (IMAPAccount, IMAPStore, MaildirStore, other
    Channels) is preserved verbatim.
    """
    quoted = ' '.join(f'"{n}"' if any(c in n for c in ' "\\') else n
                      for n in folder_names)
    out_lines: List[str] = []
    in_target = False
    replaced = False
    for line in source_rc_text.splitlines():
        stripped = line.strip()
        if stripped.startswith('Channel '):
            in_target = stripped.split(None, 1)[1].strip() == channel
        if in_target and stripped.startswith('Patterns '):
            out_lines.append(f'Patterns {quoted}')
            replaced = True
            continue
        out_lines.append(line)
    if not replaced:
        # No Patterns line existed — append at end of the matched Channel
        # block. Simplest: append at document tail after the last Channel.
        out_lines.append(f'Patterns {quoted}')
    fd, path = tempfile.mkstemp(prefix='mail-archiver-batch-',
                                suffix='.mbsyncrc', text=True)
    with os.fdopen(fd, 'w') as f:
        f.write('\n'.join(out_lines) + '\n')
    os.chmod(path, 0o600)
    return path


def run_batched_sync(
    *,
    channel: str,
    mbsyncrc_path: str,
    batches,
    subprocess_kwargs: dict,
    progress_cb: Optional[Callable[[dict], None]] = None,
    line_cb: Optional[Callable[[str], None]] = None,
    per_batch_timeout: int = 3600,
    maildir_base: Optional[str] = None,
    chunk_threshold: Optional[int] = None,
    chunk_wall_seconds: Optional[int] = None,
) -> int:
    """Run one mbsync process per batch. Retry each failed batch once.

    `batches` is List[List[Tuple[str,int]]] as emitted by plan_batches
    (accepts JSON-transported List[List[List[str,int]]] too — see
    _normalize_batch). For a batch containing a single folder whose
    message count exceeds `chunk_threshold` (1.0.17), the batch is run
    via _run_folder_chunked — a time-boxed chunk loop that opens a fresh
    IMAP connection every `chunk_wall_seconds` and resumes via mbsync's
    native maildir state. `maildir_base` is required for chunking
    (SubFolders Verbatim maps folder_name to <maildir_base>/<folder_name>);
    if not supplied, chunking is disabled even for oversized batches.

    Returns exit code:
        0 — every batch succeeded
        2 — at least one batch failed after retry (partial success)

    subprocess_kwargs is the same dict shape app.py builds for its
    kernel-setuid mbsync run (user=uid, group=gid, cwd=home, env={}).
    We copy it per batch and override the cmd/rc arguments.

    line_cb receives each stderr line so app.py's existing
    _parse_mbsync_line + last_line SSE plumbing keeps working per-batch.
    """
    if chunk_threshold is None:
        chunk_threshold = int(os.environ.get(
            'MAIL_ARCHIVER_CHUNK_THRESHOLD',
            str(CHUNK_THRESHOLD_DEFAULT)))
    if chunk_wall_seconds is None:
        chunk_wall_seconds = int(os.environ.get(
            'MAIL_ARCHIVER_CHUNK_WALL_SECONDS',
            str(CHUNK_WALL_SECONDS_DEFAULT)))

    with open(mbsyncrc_path) as f:
        source_rc_text = f.read()
    ownership = None
    if 'user' in subprocess_kwargs and 'group' in subprocess_kwargs:
        ownership = (subprocess_kwargs['user'], subprocess_kwargs['group'])

    total = len(batches)
    any_failed = False
    for idx, raw_batch in enumerate(batches, 1):
        batch = _normalize_batch(raw_batch)
        folder_names = [n for n, _ in batch]
        # A batch is chunk-eligible ONLY when it holds one folder AND that
        # folder is bigger than the chunk threshold AND we know where to
        # count messages on disk. Multi-folder batches always run flat.
        chunkable = (
            len(batch) == 1
            and batch[0][1] > chunk_threshold
            and maildir_base is not None
            and chunk_wall_seconds > 0
        )
        rc_path = _write_batch_rc(source_rc_text, channel, folder_names)
        try:
            if ownership is not None:
                try:
                    os.chown(rc_path, ownership[0], ownership[1])
                except OSError:
                    pass
            if progress_cb:
                progress_cb({
                    'state': 'syncing',
                    'batch_current': idx,
                    'batch_total': total,
                    'batch_folder_count': len(folder_names),
                    'batch_msg_estimate': sum(c for _, c in batch),
                })
            batch_ok = False
            if chunkable:
                folder_name, folder_total = batch[0]
                maildir_path = _maildir_path_for_folder(
                    maildir_base, folder_name)
                rc = _run_folder_chunked(
                    rc_path=rc_path,
                    channel=channel,
                    folder_name=folder_name,
                    folder_total=folder_total,
                    maildir_path=maildir_path,
                    subprocess_kwargs=subprocess_kwargs,
                    progress_cb=progress_cb,
                    line_cb=line_cb,
                    chunk_wall=chunk_wall_seconds,
                )
                batch_ok = (rc == 0)
                if not batch_ok and progress_cb:
                    # Chunked loop already did its own internal "retry"
                    # (each chunk is effectively a retry); no separate
                    # batch-level retry needed.
                    progress_cb({
                        'state': 'batch_failed',
                        'batch_current': idx,
                        'batch_total': total,
                        'batch_folder_count': 1,
                    })
            else:
                attempts = 0
                while attempts < 2:
                    attempts += 1
                    kw = dict(subprocess_kwargs)
                    kw['stdout'] = subprocess.PIPE
                    kw['stderr'] = subprocess.STDOUT
                    kw['bufsize'] = 1
                    kw['text'] = True
                    kw.pop('capture_output', None)
                    kw.pop('timeout', None)
                    cmd = ['mbsync', '-V', '-c', rc_path, channel]
                    proc = subprocess.Popen(cmd, **kw)
                    start = time.time()
                    try:
                        for raw in proc.stdout:
                            line = raw.rstrip('\n')
                            if line_cb:
                                try:
                                    line_cb(line)
                                except Exception:
                                    pass
                            if time.time() - start > per_batch_timeout:
                                proc.kill()
                                break
                    finally:
                        proc.wait()
                    if proc.returncode == 0:
                        batch_ok = True
                        break
                    if attempts < 2 and progress_cb:
                        progress_cb({
                            'state': 'batch_retry',
                            'batch_current': idx,
                            'batch_total': total,
                        })
                if not batch_ok:
                    any_failed = True
                    if progress_cb:
                        progress_cb({
                            'state': 'batch_failed',
                            'batch_current': idx,
                            'batch_total': total,
                            'batch_folder_count': len(folder_names),
                        })
            if chunkable and not batch_ok:
                any_failed = True
        finally:
            try:
                os.unlink(rc_path)
            except OSError:
                pass
    return 2 if any_failed else 0
