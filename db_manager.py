import sqlite3
import os
import json
from typing import List, Dict, Optional, Any

DB_FILE = 'scraper_state.db'

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Create tasks table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            start_url TEXT,
            status TEXT, -- 'running', 'paused', 'completed', 'failed', 'stopped'
            total_scraped INTEGER DEFAULT 0,
            base_url TEXT
        )
    ''')

    # Create urls table representing the data chain and tree
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS urls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            url TEXT,
            parent_url TEXT,
            status TEXT, -- 'pending', 'scraped', 'failed'
            title TEXT,
            saved_folder TEXT,
            error_msg TEXT,
            content_type TEXT, -- 'node' or 'article'
            UNIQUE(task_id, url)
        )
    ''')

    # Try adding content_type to existing databases
    try:
        cursor.execute('ALTER TABLE urls ADD COLUMN content_type TEXT')
    except sqlite3.OperationalError:
        pass # Column already exists

    conn.commit()
    conn.close()

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

# Database helper functions

def create_task(task_id: str, start_url: str, base_url: str):
    conn = get_db_connection()
    conn.execute(
        'INSERT OR IGNORE INTO tasks (task_id, start_url, status, base_url) VALUES (?, ?, ?, ?)',
        (task_id, start_url, 'running', base_url)
    )
    # Add initial URL to the urls table
    conn.execute(
        'INSERT OR IGNORE INTO urls (task_id, url, parent_url, status) VALUES (?, ?, ?, ?)',
        (task_id, start_url, None, 'pending')
    )
    conn.commit()
    conn.close()

def update_task_status(task_id: str, status: str):
    conn = get_db_connection()
    conn.execute('UPDATE tasks SET status = ? WHERE task_id = ?', (status, task_id))
    conn.commit()
    conn.close()

def get_task(task_id: str):
    conn = get_db_connection()
    task = conn.execute('SELECT * FROM tasks WHERE task_id = ?', (task_id,)).fetchone()
    conn.close()
    return dict(task) if task else None

def get_pending_url(task_id: str) -> Optional[str]:
    """Get the most recently added pending URL to simulate Depth-First Search (DFS LIFO stack)."""
    conn = get_db_connection()
    row = conn.execute(
        'SELECT url FROM urls WHERE task_id = ? AND status = ? ORDER BY id DESC LIMIT 1',
        (task_id, 'pending')
    ).fetchone()
    conn.close()
    return row['url'] if row else None

def mark_url_scraped(task_id: str, url: str, title: str = None, saved_folder: str = None, content_type: str = None):
    conn = get_db_connection()
    conn.execute(
        'UPDATE urls SET status = ?, title = ?, saved_folder = ?, content_type = ? WHERE task_id = ? AND url = ?',
        ('scraped', title, saved_folder, content_type, task_id, url)
    )
    conn.execute(
        'UPDATE tasks SET total_scraped = total_scraped + 1 WHERE task_id = ?',
        (task_id,)
    )
    conn.commit()
    conn.close()

def mark_url_failed(task_id: str, url: str, error_msg: str):
    conn = get_db_connection()
    conn.execute(
        'UPDATE urls SET status = ?, error_msg = ? WHERE task_id = ? AND url = ?',
        ('failed', error_msg, task_id, url)
    )
    conn.commit()
    conn.close()

def add_discovered_urls(task_id: str, parent_url: str, new_urls: List[str]):
    conn = get_db_connection()
    cursor = conn.cursor()
    for new_url in new_urls:
        cursor.execute(
            'INSERT OR IGNORE INTO urls (task_id, url, parent_url, status) VALUES (?, ?, ?, ?)',
            (task_id, new_url, parent_url, 'pending')
        )
    conn.commit()
    conn.close()

def get_url_tree(task_id: str) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    rows = conn.execute('SELECT url, parent_url, status, title, content_type FROM urls WHERE task_id = ?', (task_id,)).fetchall()
    conn.close()
    return [dict(row) for row in rows]

def clear_task_data(task_id: str):
    conn = get_db_connection()
    conn.execute('DELETE FROM tasks WHERE task_id = ?', (task_id,))
    conn.execute('DELETE FROM urls WHERE task_id = ?', (task_id,))
    conn.commit()
    conn.close()

# Initialize on import
init_db()
