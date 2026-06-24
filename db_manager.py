import sqlite3
import os
import json
from datetime import datetime
from typing import List, Dict, Optional, Any

import content_filter

DB_FILE = 'scraper_state.db'

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            start_url TEXT,
            status TEXT,
            total_scraped INTEGER DEFAULT 0,
            base_url TEXT,
            dynamic_root TEXT,
            base_path TEXT
        )
    ''')
    for col in ['dynamic_root TEXT', 'base_path TEXT']:
        try:
            cursor.execute(f'ALTER TABLE tasks ADD COLUMN {col}')
        except sqlite3.OperationalError:
            pass

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS urls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            url TEXT,
            parent_url TEXT,
            status TEXT,
            title TEXT,
            saved_file TEXT,
            error_msg TEXT,
            content_hash TEXT,
            last_crawled_at TEXT,
            UNIQUE(task_id, url)
        )
    ''')
    for col in ['content_hash TEXT', 'last_crawled_at TEXT', 'saved_file TEXT',
                'priority INTEGER DEFAULT 10']:
        try:
            cursor.execute(f'ALTER TABLE urls ADD COLUMN {col}')
        except sqlite3.OperationalError:
            pass
    # Index to make priority-ordered dequeue and content-hash dedup fast at scale.
    try:
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_urls_dequeue '
            'ON urls(task_id, status, priority, id)'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_urls_hash ON urls(task_id, content_hash)'
        )
    except sqlite3.OperationalError:
        pass

    # Cache DeepSeek site analysis per domain
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS site_analysis (
            domain TEXT PRIMARY KEY,
            selectors_json TEXT,
            analyzed_at TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS wiki_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT,
            operation TEXT,
            detail TEXT,
            created_at TEXT
        )
    ''')

    conn.commit()
    conn.close()

def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=10.0)
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.row_factory = sqlite3.Row
    return conn

# ── Tasks ────────────────────────────────────────────────────────────────────

def create_task(task_id: str, start_url: str, base_url: str):
    conn = get_db_connection()
    conn.execute(
        'INSERT OR IGNORE INTO tasks (task_id, start_url, status, base_url) VALUES (?,?,?,?)',
        (task_id, start_url, 'running', base_url)
    )
    conn.execute(
        'INSERT OR IGNORE INTO urls (task_id, url, parent_url, status) VALUES (?,?,?,?)',
        (task_id, start_url, None, 'pending')
    )
    conn.commit()
    conn.close()

def update_task_status(task_id: str, status: str):
    conn = get_db_connection()
    conn.execute('UPDATE tasks SET status=? WHERE task_id=?', (status, task_id))
    conn.commit()
    conn.close()

def update_task_dynamic_root(task_id: str, dynamic_root: str):
    conn = get_db_connection()
    conn.execute('UPDATE tasks SET dynamic_root=? WHERE task_id=?', (dynamic_root, task_id))
    conn.commit()
    conn.close()

def update_task_base_path(task_id: str, base_path: str):
    conn = get_db_connection()
    conn.execute('UPDATE tasks SET base_path=? WHERE task_id=?', (base_path, task_id))
    conn.commit()
    conn.close()

def get_task(task_id: str) -> Optional[dict]:
    conn = get_db_connection()
    row = conn.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_active_tasks() -> List[dict]:
    """Return tasks that are running or paused."""
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM tasks WHERE status IN ('running','paused')"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ── URLs ─────────────────────────────────────────────────────────────────────

def get_and_lock_pending_url(task_id: str) -> "Optional[tuple[str, Optional[str]]]":
    """Return (url, parent_url) for the next pending URL, or None.
    Ordering: priority DESC then id ASC — article/detail pages (high priority)
    are crawled before list/index pages, while id ASC keeps breadth-first FIFO
    within a priority band, avoiding depth-first spirals that starve workers."""
    conn = None
    try:
        conn = get_db_connection()
        conn.execute("BEGIN EXCLUSIVE")
        row = conn.execute(
            'SELECT url, parent_url FROM urls WHERE task_id=? AND status=? '
            'ORDER BY priority DESC, id ASC LIMIT 1',
            (task_id, 'pending')
        ).fetchone()
        if row:
            url = row['url']
            conn.execute(
                'UPDATE urls SET status=? WHERE task_id=? AND url=?',
                ('processing', task_id, url)
            )
            conn.commit()
            return url, row['parent_url']
        conn.commit()
        return None
    except Exception as e:
        if conn:
            conn.rollback()
        raise e
    finally:
        if conn:
            conn.close()

def reset_processing_urls(task_id: str):
    conn = get_db_connection()
    conn.execute(
        "UPDATE urls SET status='pending' WHERE task_id=? AND status='processing'",
        (task_id,)
    )
    conn.commit()
    conn.close()

def get_active_count(task_id: str) -> int:
    conn = get_db_connection()
    row = conn.execute(
        "SELECT COUNT(*) as c FROM urls WHERE task_id=? AND status IN ('pending','processing')",
        (task_id,)
    ).fetchone()
    conn.close()
    return row['c'] if row else 0

def mark_url_scraped(task_id: str, url: str, title: str = None, saved_file: str = None,
                     content_hash: str = None, bump_count: bool = True):
    ts = datetime.utcnow().isoformat()
    conn = get_db_connection()
    conn.execute(
        '''UPDATE urls SET status='scraped', title=?, saved_file=?, content_hash=?,
           last_crawled_at=? WHERE task_id=? AND url=?''',
        (title, saved_file, content_hash, ts, task_id, url)
    )
    # bump_count=False for re-crawls of unchanged pages (update_mode) — the
    # page was already counted when first scraped; counting it again would
    # inflate total_scraped past the actual number of saved files on disk.
    if bump_count:
        conn.execute(
            'UPDATE tasks SET total_scraped=total_scraped+1 WHERE task_id=?', (task_id,)
        )
    conn.commit()
    conn.close()

def mark_url_failed(task_id: str, url: str, error_msg: str):
    conn = get_db_connection()
    conn.execute(
        "UPDATE urls SET status='failed', error_msg=? WHERE task_id=? AND url=?",
        (error_msg, task_id, url)
    )
    conn.commit()
    conn.close()

def mark_url_filtered(task_id: str, url: str, reason: str):
    conn = get_db_connection()
    conn.execute(
        "UPDATE urls SET status='filtered', error_msg=? WHERE task_id=? AND url=?",
        (reason, task_id, url)
    )
    conn.commit()
    conn.close()

def get_url_content_hash(task_id: str, url: str) -> Optional[str]:
    conn = get_db_connection()
    row = conn.execute(
        "SELECT content_hash FROM urls WHERE task_id=? AND url=? AND status='scraped'",
        (task_id, url)
    ).fetchone()
    conn.close()
    return row['content_hash'] if row else None

def add_discovered_urls(task_id: str, parent_url: str, new_urls: List[str]):
    """Queue newly-discovered URLs. Structurally non-content URLs (login/search/
    share/feeds…) are dropped here so they never cost a fetch; each kept URL is
    tagged with a crawl priority (article/detail > default > home > list) that
    drives dequeue ordering. INSERT OR IGNORE + UNIQUE(task_id,url) prevents
    re-queuing a URL already seen (anti-duplicate traversal)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    for u in new_urls:
        if content_filter.should_skip_url(u):
            continue
        cursor.execute(
            'INSERT OR IGNORE INTO urls (task_id, url, parent_url, status, priority) '
            'VALUES (?,?,?,?,?)',
            (task_id, u, parent_url, 'pending', content_filter.url_priority(u))
        )
    conn.commit()
    conn.close()


def content_hash_seen(task_id: str, content_hash: str,
                      exclude_url: str = None) -> bool:
    """True if some already-scraped URL in this task saved identical content
    (same hash). Lets the crawler skip re-downloading the same article reached
    via a different URL — anti-duplicate DOWNLOAD on top of anti-duplicate
    traversal."""
    if not content_hash:
        return False
    conn = get_db_connection()
    if exclude_url:
        row = conn.execute(
            "SELECT 1 FROM urls WHERE task_id=? AND content_hash=? AND status='scraped' "
            "AND url<>? LIMIT 1",
            (task_id, content_hash, exclude_url)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM urls WHERE task_id=? AND content_hash=? AND status='scraped' LIMIT 1",
            (task_id, content_hash)
        ).fetchone()
    conn.close()
    return row is not None

def get_url_tree(task_id: str) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    rows = conn.execute(
        'SELECT url, parent_url, status, title FROM urls WHERE task_id=?', (task_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def clear_task_data(task_id: str):
    conn = get_db_connection()
    conn.execute('DELETE FROM tasks WHERE task_id=?', (task_id,))
    conn.execute('DELETE FROM urls WHERE task_id=?', (task_id,))
    conn.commit()
    conn.close()

# ── Site Analysis ─────────────────────────────────────────────────────────────

def get_site_analysis(domain: str) -> Optional[dict]:
    conn = get_db_connection()
    row = conn.execute(
        'SELECT selectors_json FROM site_analysis WHERE domain=?', (domain,)
    ).fetchone()
    conn.close()
    if row:
        try:
            return json.loads(row['selectors_json'])
        except Exception:
            return None
    return None

def save_site_analysis(domain: str, selectors: dict):
    conn = get_db_connection()
    conn.execute(
        '''INSERT OR REPLACE INTO site_analysis (domain, selectors_json, analyzed_at)
           VALUES (?,?,?)''',
        (domain, json.dumps(selectors, ensure_ascii=False), datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

# ── Wiki ──────────────────────────────────────────────────────────────────────

def log_wiki_operation(domain: str, operation: str, detail: dict):
    conn = get_db_connection()
    conn.execute(
        'INSERT INTO wiki_log (domain, operation, detail, created_at) VALUES (?,?,?,?)',
        (domain, operation, json.dumps(detail, ensure_ascii=False), datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

# Initialize on import
init_db()
