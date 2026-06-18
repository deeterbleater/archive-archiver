import sqlite3
from datetime import datetime
import os

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "archive_works.db")

def get_connection():
    """Returns a connection to the SQLite database."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initializes the SQLite database tables."""
    conn = get_connection()
    cursor = conn.cursor()
    
    # Create works table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS works (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        author TEXT,
        search_query TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    # Create files table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        work_id INTEGER NOT NULL,
        site TEXT NOT NULL,
        format TEXT NOT NULL,
        url TEXT NOT NULL,
        file_size TEXT,
        download_source TEXT,
        download_url TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (work_id) REFERENCES works (id) ON DELETE CASCADE,
        UNIQUE(work_id, format, url)
    )
    """)
    
    conn.commit()
    conn.close()

def add_work(title, author=None, search_query=None):
    """
    Inserts a new work or returns the ID of an existing one with matching title/author.
    """
    # Cast lists/objects to string
    if isinstance(title, list):
        title = ", ".join(str(item) for item in title)
    else:
        title = str(title)
        
    if isinstance(author, list):
        author = ", ".join(str(item) for item in author)
    elif author is not None:
        author = str(author)
        
    if isinstance(search_query, list):
        search_query = ", ".join(str(item) for item in search_query)
    elif search_query is not None:
        search_query = str(search_query)

    conn = get_connection()
    cursor = conn.cursor()
    
    # Check if work already exists
    if author:
        cursor.execute("SELECT id FROM works WHERE title = ? AND author = ?", (title, author))
    else:
        cursor.execute("SELECT id FROM works WHERE title = ?", (title,))
        
    row = cursor.fetchone()
    if row:
        work_id = row[0]
    else:
        cursor.execute(
            "INSERT INTO works (title, author, search_query) VALUES (?, ?, ?)",
            (title, author, search_query)
        )
        work_id = cursor.lastrowid
        conn.commit()
        
    conn.close()
    return work_id

def add_file(work_id, site, format, url, file_size=None, download_source=None, download_url=None):
    """
    Adds a file record for a work. Performs an upsert if the file already exists.
    """
    # Safe casting
    site = str(site)
    format = str(format)
    url = str(url)
    if file_size is not None:
        file_size = str(file_size)
    if download_source is not None:
        download_source = str(download_source)
    if download_url is not None:
        download_url = str(download_url)
        
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
    INSERT INTO files (work_id, site, format, url, file_size, download_source, download_url)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(work_id, format, url) DO UPDATE SET
        file_size = excluded.file_size,
        download_source = excluded.download_source,
        download_url = excluded.download_url,
        site = excluded.site
    """, (work_id, site, format, url, file_size, download_source, download_url))
    
    conn.commit()
    conn.close()

def get_stats():
    """Returns database statistics."""
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM works")
    total_works = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM files")
    total_files = cursor.fetchone()[0]
    
    cursor.execute("SELECT site, COUNT(*) FROM files GROUP BY site")
    files_by_site = {row[0]: row[1] for row in cursor.fetchall()}
    
    conn.close()
    return {
        "total_works": total_works,
        "total_files": total_files,
        "files_by_site": files_by_site
    }

def get_works_by_queries(queries):
    """
    Returns works and their associated files that match a list of search queries.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # Generate placeholders like (?, ?, ?)
    placeholders = ",".join("?" for _ in queries)
    
    # Fetch works
    cursor.execute(
        f"SELECT * FROM works WHERE search_query IN ({placeholders})",
        queries
    )
    works_rows = cursor.fetchall()
    
    results = []
    for w_row in works_rows:
        work = dict(w_row)
        cursor.execute("SELECT * FROM files WHERE work_id = ?", (work["id"],))
        files_rows = cursor.fetchall()
        work["files"] = [dict(f) for f in files_rows]
        results.append(work)
        
    conn.close()
    return results

