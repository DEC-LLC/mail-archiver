"""Batched IMAP sync — added 1.0.15.

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

Progress dict fields fed to `progress_cb` on every state change:
    batch_current, batch_total, batch_folder_count, batch_msg_estimate,
    state ('measuring' | 'syncing' | 'batch_retry' | 'batch_failed' | 'done'),
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
                 threshold: int) -> List[List[str]]:
    """Pack folders into batches of <= threshold messages each.

    Smallest-first greedy: sort ascending by message count, fill each
    batch until adding the next folder would exceed threshold. A single
    folder larger than threshold gets its own batch (we can't split
    inside a folder — mbsync's internal FETCH pipelining handles that).
    Empty folders still count as "work" but contribute 0 messages —
    they pack tightly at the head.
    """
    ordered = sorted(folders, key=lambda t: (t[1], t[0]))
    batches: List[List[str]] = []
    current: List[str] = []
    current_msgs = 0
    for name, count in ordered:
        # Single folder larger than threshold — its own batch.
        if count > threshold:
            if current:
                batches.append(current)
                current, current_msgs = [], 0
            batches.append([name])
            continue
        if current and current_msgs + count > threshold:
            batches.append(current)
            current, current_msgs = [], 0
        current.append(name)
        current_msgs += count
    if current:
        batches.append(current)
    return batches


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
    batches: List[List[str]],
    subprocess_kwargs: dict,
    progress_cb: Optional[Callable[[dict], None]] = None,
    line_cb: Optional[Callable[[str], None]] = None,
    per_batch_timeout: int = 3600,
) -> int:
    """Run one mbsync process per batch. Retry each failed batch once.

    Returns exit code:
        0 — every batch succeeded
        2 — at least one batch failed after retry (partial success)

    subprocess_kwargs is the same dict shape app.py builds for its
    kernel-setuid mbsync run (user=uid, group=gid, cwd=home, env={}).
    We copy it per batch and override the cmd/rc arguments.

    line_cb receives each stderr line (already tail-clipped to 200) so
    app.py's existing _parse_mbsync_line + last_line SSE plumbing keeps
    working per-batch.
    """
    with open(mbsyncrc_path) as f:
        source_rc_text = f.read()
    ownership = None
    if 'user' in subprocess_kwargs and 'group' in subprocess_kwargs:
        ownership = (subprocess_kwargs['user'], subprocess_kwargs['group'])

    total = len(batches)
    any_failed = False
    for idx, folder_names in enumerate(batches, 1):
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
                })
            attempts = 0
            batch_ok = False
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
        finally:
            try:
                os.unlink(rc_path)
            except OSError:
                pass
    return 2 if any_failed else 0
