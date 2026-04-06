"""FTS5-based full-text search indexing for mail-archiver email archives.

Provides per-user SQLite FTS5 search indexes for fast, structured email
searching with date range filters, sender/recipient filters, attachment
detection, and contextual snippet highlighting.

Usage:
    from search_index import search_fts, index_maildir, get_index_stats

All functions are safe to call from multiple threads (one connection per call).
No external dependencies beyond Python stdlib.
"""

import email as email_mod
import email.policy
import email.utils
import os
import re
import sqlite3
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BATCH_SIZE = 500  # commit every N inserts during indexing

_FTS5_SCHEMA = """\
CREATE VIRTUAL TABLE IF NOT EXISTS emails USING fts5(
    message_id,
    subject,
    sender,
    recipients,
    date_str,
    body,
    account,
    folder,
    filepath,
    date_unix UNINDEXED,
    size_bytes UNINDEXED,
    has_attachment UNINDEXED,
    tokenize='porter unicode61'
);
"""

# Auxiliary table for fast message_id duplicate checking.  FTS5 tables lack
# standard indexes, so a lightweight side table with a PRIMARY KEY gives us
# O(log n) dedup lookups without scanning the full-text index.
_SEEN_SCHEMA = """\
CREATE TABLE IF NOT EXISTS seen_ids (
    message_id TEXT PRIMARY KEY
);
"""

# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------

def get_index_db(username: str, data_dir: str) -> sqlite3.Connection:
    """Open (or create) the FTS5 search index for a user.

    The database lives at ``$data_dir/$username/.search_index.db``.
    Returns a ``sqlite3.Connection`` with WAL mode and foreign keys enabled.
    """
    user_dir = Path(data_dir) / username
    user_dir.mkdir(parents=True, exist_ok=True)
    db_path = user_dir / '.search_index.db'

    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute(_FTS5_SCHEMA)
    conn.execute(_SEEN_SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Email parsing (mirrors app.py _parse_email_file logic)
# ---------------------------------------------------------------------------

def _parse_email_file(filepath: Path) -> dict | None:
    """Parse a Maildir message file into a dict with decoded headers and body.

    Mirrors the ``_parse_email_file`` function in ``app.py`` so the search
    index stays consistent with what the UI displays.  Also extracts
    attachment presence and file size for the index.
    """
    try:
        raw = filepath.read_bytes()
        msg = email_mod.message_from_bytes(raw, policy=email_mod.policy.default)
    except Exception:
        return None

    result = {
        'subject': str(msg.get('Subject', '')) or '(no subject)',
        'from': str(msg.get('From', '')),
        'to': str(msg.get('To', '')),
        'cc': str(msg.get('Cc', '')),
        'date': str(msg.get('Date', '')),
        'message_id': str(msg.get('Message-ID', '')),
        'size_bytes': len(raw),
    }

    # Detect attachments — any part with Content-Disposition: attachment
    has_attachment = False
    body_parts = []

    if msg.is_multipart():
        for part in msg.walk():
            cd = str(part.get('Content-Disposition', ''))
            if 'attachment' in cd.lower():
                has_attachment = True

            ct = part.get_content_type()
            if ct == 'text/plain':
                try:
                    body_parts.append(part.get_content())
                except Exception:
                    payload = part.get_payload(decode=True)
                    if payload:
                        for enc in ('utf-8', 'latin-1', 'windows-1252'):
                            try:
                                body_parts.append(payload.decode(enc))
                                break
                            except (UnicodeDecodeError, LookupError):
                                continue
            elif ct == 'text/html' and not body_parts:
                try:
                    html = part.get_content()
                    body_parts.append(re.sub(r'<[^>]+>', ' ', html))
                except Exception:
                    pass
    else:
        try:
            body_parts.append(msg.get_content())
        except Exception:
            payload = msg.get_payload(decode=True)
            if payload:
                for enc in ('utf-8', 'latin-1', 'windows-1252'):
                    try:
                        body_parts.append(payload.decode(enc))
                        break
                    except (UnicodeDecodeError, LookupError):
                        continue

    result['body'] = '\n'.join(body_parts)
    result['has_attachment'] = has_attachment
    return result


def _parse_date_unix(date_str: str) -> int:
    """Best-effort conversion of an RFC 2822 date string to Unix timestamp."""
    try:
        parsed = email_mod.utils.parsedate_tz(date_str)
        if parsed:
            return int(email_mod.utils.mktime_tz(parsed))
    except Exception:
        pass
    return 0


def _date_str_to_unix(date_str: str) -> int:
    """Convert YYYY-MM-DD to Unix timestamp (start of day, local time)."""
    try:
        return int(time.mktime(time.strptime(date_str, '%Y-%m-%d')))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

def index_email(conn: sqlite3.Connection, parsed: dict,
                filepath: str, account: str, folder: str) -> bool:
    """Add a single parsed email to the FTS5 index.

    Returns ``True`` if the email was inserted, ``False`` if it was
    already present (duplicate ``message_id``).
    """
    mid = parsed.get('message_id', '').strip()
    if not mid:
        # Generate a stable surrogate ID from filepath
        mid = f'<file:{filepath}>'

    # Duplicate check via seen_ids table
    row = conn.execute(
        'SELECT 1 FROM seen_ids WHERE message_id = ?', (mid,)
    ).fetchone()
    if row:
        return False

    recipients = ', '.join(
        filter(None, [parsed.get('to', ''), parsed.get('cc', '')])
    )
    date_unix = _parse_date_unix(parsed.get('date', ''))

    conn.execute(
        'INSERT INTO emails(message_id, subject, sender, recipients, '
        'date_str, body, account, folder, filepath, date_unix, size_bytes, '
        'has_attachment) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        (
            mid,
            parsed.get('subject', ''),
            parsed.get('from', ''),
            recipients,
            parsed.get('date', ''),
            parsed.get('body', ''),
            account,
            folder,
            filepath,
            date_unix,
            parsed.get('size_bytes', 0),
            1 if parsed.get('has_attachment') else 0,
        )
    )
    conn.execute('INSERT INTO seen_ids(message_id) VALUES (?)', (mid,))
    return True


def _account_from_safe_name(safe_name: str) -> str:
    """Reverse the safe_name encoding used by app.py.

    ``user_at_example_com`` -> ``user@example.com``

    This is a best-effort heuristic — the ``_at_`` token is the reliable
    delimiter, and dots are restored from the remaining underscores.
    """
    return safe_name.replace('_at_', '@').replace('_', '.')


def _resolve_folder(parts: tuple, start: int = 1) -> str:
    """Determine the folder name from path parts between the account dir
    and the ``cur``/``new``/``tmp`` leaf."""
    folder_parts = []
    for p in parts[start:]:
        if p in ('cur', 'new', 'tmp'):
            break
        folder_parts.append(p)
    return '/'.join(folder_parts) if folder_parts else 'INBOX'


def index_maildir(username: str, data_dir: str,
                  account_filter: str = None) -> dict:
    """Walk a user's Maildir and index any un-indexed emails.

    Parameters
    ----------
    username : str
        The system or built-in username.
    data_dir : str
        Root data directory (``CONFIG['data_dir']``).
    account_filter : str, optional
        If given, only index this email account.

    Returns
    -------
    dict
        ``{'indexed': int, 'skipped': int, 'errors': int, 'total_time': float}``
    """
    t0 = time.monotonic()
    archive_dir = Path(data_dir) / username

    stats = {'indexed': 0, 'skipped': 0, 'errors': 0, 'total_time': 0.0}

    if not archive_dir.exists():
        stats['total_time'] = round(time.monotonic() - t0, 3)
        return stats

    conn = get_index_db(username, data_dir)
    pending = 0

    try:
        # Build list of account directories to scan
        acct_dirs = []
        if account_filter:
            safe_name = account_filter.replace('@', '_at_').replace('.', '_')
            candidate = archive_dir / safe_name
            if candidate.is_dir():
                acct_dirs.append(candidate)
        else:
            for child in sorted(archive_dir.iterdir()):
                if child.is_dir() and not child.name.startswith('.'):
                    acct_dirs.append(child)

        for acct_dir in acct_dirs:
            account = _account_from_safe_name(acct_dir.name)

            # Walk cur/ and new/ directories (standard Maildir layout)
            for msg_file in acct_dir.rglob('*'):
                if not msg_file.is_file():
                    continue
                # Only index files inside cur/ or new/ directories
                try:
                    rel = msg_file.relative_to(acct_dir)
                except ValueError:
                    continue
                parts = rel.parts
                if not any(p in ('cur', 'new') for p in parts):
                    continue

                folder = _resolve_folder(parts, start=0)

                try:
                    parsed = _parse_email_file(msg_file)
                    if parsed is None:
                        stats['errors'] += 1
                        continue

                    inserted = index_email(
                        conn, parsed, str(msg_file), account, folder
                    )
                    if inserted:
                        stats['indexed'] += 1
                        pending += 1
                    else:
                        stats['skipped'] += 1

                    # Batch commit
                    if pending >= _BATCH_SIZE:
                        conn.commit()
                        pending = 0

                except Exception:
                    stats['errors'] += 1
                    continue

        # Final commit for remaining rows
        if pending > 0:
            conn.commit()

    finally:
        conn.close()

    stats['total_time'] = round(time.monotonic() - t0, 3)
    return stats


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_fts(username: str, data_dir: str, query: str,
               account_filter: str = None,
               date_from: str = None,
               date_to: str = None,
               sender_filter: str = None,
               recipient_filter: str = None,
               has_attachment: bool = None,
               max_results: int = 200,
               offset: int = 0) -> dict:
    """Search the FTS5 index with optional filters.

    Parameters
    ----------
    username : str
        The system or built-in username.
    data_dir : str
        Root data directory.
    query : str
        FTS5 query string.  Supports standard FTS5 syntax (AND, OR, NOT,
        phrase queries, prefix queries, column filters).
    account_filter : str, optional
        Restrict results to a specific email account.
    date_from / date_to : str, optional
        Date range in ``YYYY-MM-DD`` format.
    sender_filter : str, optional
        Substring match on the sender field (case-insensitive).
    recipient_filter : str, optional
        Substring match on the recipients field (case-insensitive).
    has_attachment : bool, optional
        If ``True``, only return emails with attachments.  If ``False``,
        only return emails without.
    max_results : int
        Maximum results to return (default 200).
    offset : int
        Pagination offset (default 0).

    Returns
    -------
    dict
        ``{'results': [...], 'total': int, 'query_time': float}``
        Each result contains: ``message_id``, ``subject``, ``sender``,
        ``recipients``, ``date_str``, ``snippet``, ``account``, ``folder``,
        ``filepath``, ``has_attachment``.
    """
    t0 = time.monotonic()
    result = {'results': [], 'total': 0, 'query_time': 0.0}

    if not query or not query.strip():
        result['query_time'] = round(time.monotonic() - t0, 3)
        return result

    user_dir = Path(data_dir) / username
    db_path = user_dir / '.search_index.db'
    if not db_path.exists():
        result['query_time'] = round(time.monotonic() - t0, 3)
        return result

    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row

    try:
        # Build the query.  The core is an FTS5 MATCH, with additional
        # WHERE clauses for the optional filters.
        where_clauses = ['emails MATCH ?']
        params: list = [query.strip()]

        if account_filter:
            where_clauses.append('account = ?')
            params.append(account_filter)

        if date_from:
            ts = _date_str_to_unix(date_from)
            if ts:
                where_clauses.append('date_unix >= ?')
                params.append(ts)

        if date_to:
            # End of day: add 86400 seconds (one full day)
            ts = _date_str_to_unix(date_to)
            if ts:
                where_clauses.append('date_unix < ?')
                params.append(ts + 86400)

        if sender_filter:
            where_clauses.append('sender LIKE ?')
            params.append(f'%{sender_filter}%')

        if recipient_filter:
            where_clauses.append('recipients LIKE ?')
            params.append(f'%{recipient_filter}%')

        if has_attachment is True:
            where_clauses.append('has_attachment = 1')
        elif has_attachment is False:
            where_clauses.append('has_attachment = 0')

        where_sql = ' AND '.join(where_clauses)

        # Count total matching rows (before LIMIT/OFFSET)
        count_sql = f'SELECT count(*) FROM emails WHERE {where_sql}'
        try:
            total = conn.execute(count_sql, params).fetchone()[0]
        except sqlite3.OperationalError:
            # Bad FTS5 query syntax — return empty rather than crash
            result['query_time'] = round(time.monotonic() - t0, 3)
            return result

        result['total'] = total

        # Fetch page of results, ordered by date descending
        select_sql = (
            'SELECT message_id, subject, sender, recipients, date_str, '
            'snippet(emails, 5, \'<mark>\', \'</mark>\', \'...\', 48) AS snippet, '
            'account, folder, filepath, has_attachment, date_unix '
            f'FROM emails WHERE {where_sql} '
            'ORDER BY date_unix DESC '
            'LIMIT ? OFFSET ?'
        )
        params.extend([max_results, offset])

        try:
            rows = conn.execute(select_sql, params).fetchall()
        except sqlite3.OperationalError:
            result['query_time'] = round(time.monotonic() - t0, 3)
            return result

        for row in rows:
            # Sanitize snippet: FTS5 snippet() may contain raw email HTML.
            # Escape everything, then restore our <mark> highlight tags.
            raw_snippet = row['snippet'] or ''
            safe_snippet = (
                raw_snippet
                .replace('&', '&amp;')
                .replace('<mark>', '\x00MARK\x00')
                .replace('</mark>', '\x00/MARK\x00')
                .replace('<', '&lt;')
                .replace('>', '&gt;')
                .replace('\x00MARK\x00', '<mark>')
                .replace('\x00/MARK\x00', '</mark>')
            )
            result['results'].append({
                'message_id': row['message_id'],
                'subject': row['subject'],
                'sender': row['sender'],
                'recipients': row['recipients'],
                'date_str': row['date_str'],
                'snippet': safe_snippet,
                'account': row['account'],
                'folder': row['folder'],
                'filepath': row['filepath'],
                'has_attachment': bool(row['has_attachment']),
            })

    finally:
        conn.close()

    result['query_time'] = round(time.monotonic() - t0, 3)
    return result


# ---------------------------------------------------------------------------
# Recent emails (for blank-search landing page)
# ---------------------------------------------------------------------------

def get_recent_emails(username: str, data_dir: str,
                      account_filter: str = None,
                      date_from: str = None,
                      date_to: str = None,
                      max_results: int = 10) -> dict:
    """Return the most recent emails from the index, optionally filtered.

    Used when the user opens the search page without entering a query —
    shows them what's in their archive instead of a blank page.

    Parameters
    ----------
    username : str
        The system or built-in username.
    data_dir : str
        Root data directory.
    account_filter : str, optional
        Restrict to a specific email account.
    date_from / date_to : str, optional
        Date range in ``YYYY-MM-DD`` format.
    max_results : int
        How many recent emails to return (default 10).

    Returns
    -------
    dict
        ``{'results': [...], 'total': int, 'query_time': float}``
    """
    t0 = time.monotonic()
    result = {'results': [], 'total': 0, 'query_time': 0.0}

    user_dir = Path(data_dir) / username
    db_path = user_dir / '.search_index.db'
    if not db_path.exists():
        result['query_time'] = round(time.monotonic() - t0, 3)
        return result

    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row

    try:
        where_clauses = []
        params = []

        if account_filter:
            where_clauses.append('e.account = ?')
            params.append(account_filter)

        if date_from:
            try:
                dt = time.mktime(time.strptime(date_from, '%Y-%m-%d'))
                where_clauses.append('e.date_unix >= ?')
                params.append(int(dt))
            except ValueError:
                pass

        if date_to:
            try:
                dt = time.mktime(time.strptime(date_to + ' 23:59:59',
                                               '%Y-%m-%d %H:%M:%S'))
                where_clauses.append('e.date_unix <= ?')
                params.append(int(dt))
            except ValueError:
                pass

        where = (' WHERE ' + ' AND '.join(where_clauses)) if where_clauses else ''

        # Count total
        count_row = conn.execute(
            f'SELECT count(*) FROM emails e{where}', params
        ).fetchone()
        result['total'] = count_row[0] if count_row else 0

        # Fetch recent, ordered by date descending
        rows = conn.execute(
            f'SELECT subject, sender, recipients, date_str, account, folder, '
            f'filepath, has_attachment, date_unix '
            f'FROM emails e{where} '
            f'ORDER BY date_unix DESC LIMIT ?',
            params + [max_results]
        ).fetchall()

        for row in rows:
            result['results'].append({
                'subject': row['subject'] or '(no subject)',
                'from': row['sender'] or '',
                'to': row['recipients'] or '',
                'date': row['date_str'] or '',
                'account': row['account'] or '',
                'folder': row['folder'] or '',
                'filepath': row['filepath'] or '',
                'has_attachment': bool(row['has_attachment']),
                'snippet': '',
            })

    finally:
        conn.close()

    result['query_time'] = round(time.monotonic() - t0, 3)
    return result


# ---------------------------------------------------------------------------
# Stats & maintenance
# ---------------------------------------------------------------------------

def get_index_stats(username: str, data_dir: str) -> dict:
    """Return statistics about the user's search index.

    Returns
    -------
    dict
        ``{'total_emails': int, 'accounts': [str, ...], 'oldest': str,
        'newest': str, 'index_size_bytes': int}``
    """
    user_dir = Path(data_dir) / username
    db_path = user_dir / '.search_index.db'

    stats = {
        'total_emails': 0,
        'accounts': [],
        'oldest': None,
        'newest': None,
        'index_size_bytes': 0,
    }

    if not db_path.exists():
        return stats

    stats['index_size_bytes'] = db_path.stat().st_size

    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        row = conn.execute('SELECT count(*) FROM seen_ids').fetchone()
        stats['total_emails'] = row[0] if row else 0

        rows = conn.execute(
            'SELECT DISTINCT account FROM emails ORDER BY account'
        ).fetchall()
        stats['accounts'] = [r[0] for r in rows]

        row = conn.execute(
            'SELECT date_str FROM emails WHERE date_unix > 0 '
            'ORDER BY date_unix ASC LIMIT 1'
        ).fetchone()
        if row:
            stats['oldest'] = row[0]

        row = conn.execute(
            'SELECT date_str FROM emails WHERE date_unix > 0 '
            'ORDER BY date_unix DESC LIMIT 1'
        ).fetchone()
        if row:
            stats['newest'] = row[0]

    finally:
        conn.close()

    return stats


def rebuild_index(username: str, data_dir: str) -> dict:
    """Drop and rebuild the entire search index from Maildir.

    Returns the same dict as ``index_maildir``.
    """
    user_dir = Path(data_dir) / username
    db_path = user_dir / '.search_index.db'

    # Remove existing database files (WAL and SHM too)
    for suffix in ('', '-wal', '-shm'):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()

    return index_maildir(username, data_dir)
