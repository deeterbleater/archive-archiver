import os
import sqlite3

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

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS downloads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id INTEGER NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK (status IN ('pending', 'downloading', 'downloaded', 'failed')),
        bucket_uri TEXT,
        storage_key TEXT,
        sha256 TEXT,
        bytes INTEGER,
        content_type TEXT,
        http_status INTEGER,
        attempts INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        downloaded_at TIMESTAMP,
        FOREIGN KEY (file_id) REFERENCES files (id) ON DELETE CASCADE
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS extractions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        download_id INTEGER NOT NULL,
        extractor TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'processed', 'failed', 'skipped')),
        text_uri TEXT,
        text_sha256 TEXT,
        char_count INTEGER,
        category TEXT,
        warnings TEXT,
        error TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        processed_at TIMESTAMP,
        FOREIGN KEY (download_id) REFERENCES downloads (id) ON DELETE CASCADE,
        UNIQUE(download_id, extractor)
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

    cursor.execute("SELECT status, COUNT(*) FROM downloads GROUP BY status")
    downloads_by_status = {row[0]: row[1] for row in cursor.fetchall()}

    cursor.execute("SELECT status, COUNT(*) FROM extractions GROUP BY status")
    extractions_by_status = {row[0]: row[1] for row in cursor.fetchall()}
    
    conn.close()
    return {
        "total_works": total_works,
        "total_files": total_files,
        "files_by_site": files_by_site,
        "downloads_by_status": downloads_by_status,
        "extractions_by_status": extractions_by_status,
    }

def get_pending_download_files(limit=10):
    """Returns file records that have not been downloaded successfully."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
    SELECT
        files.*,
        works.title,
        works.author,
        downloads.status AS download_status,
        downloads.attempts AS download_attempts
    FROM files
    JOIN works ON works.id = files.work_id
    LEFT JOIN downloads ON downloads.file_id = files.id
    WHERE
        COALESCE(files.download_url, files.url, '') <> ''
        AND (downloads.id IS NULL OR downloads.status = 'failed')
    ORDER BY files.id ASC
    LIMIT ?
    """, (limit,))

    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

def mark_download_started(file_id):
    """Creates or updates the download row when a worker starts a file."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO downloads (file_id, status, attempts, updated_at)
    VALUES (?, 'downloading', 1, CURRENT_TIMESTAMP)
    ON CONFLICT(file_id) DO UPDATE SET
        status = 'downloading',
        attempts = downloads.attempts + 1,
        error = NULL,
        updated_at = CURRENT_TIMESTAMP
    """, (file_id,))
    conn.commit()
    conn.close()

def mark_download_succeeded(file_id, bucket_uri, storage_key, sha256, byte_count, content_type=None, http_status=None):
    """Persists the raw-object location and hash for a completed download."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE downloads
    SET
        status = 'downloaded',
        bucket_uri = ?,
        storage_key = ?,
        sha256 = ?,
        bytes = ?,
        content_type = ?,
        http_status = ?,
        error = NULL,
        updated_at = CURRENT_TIMESTAMP,
        downloaded_at = CURRENT_TIMESTAMP
    WHERE file_id = ?
    """, (bucket_uri, storage_key, sha256, byte_count, content_type, http_status, file_id))
    conn.commit()
    conn.close()

def mark_download_failed(file_id, error, http_status=None):
    """Records a failed download attempt without deleting prior manifest data."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE downloads
    SET
        status = 'failed',
        error = ?,
        http_status = ?,
        updated_at = CURRENT_TIMESTAMP
    WHERE file_id = ?
    """, (str(error)[:1000], http_status, file_id))
    conn.commit()
    conn.close()

def get_pending_extractions(limit=10, extractor="plaintext.v1"):
    """Returns downloaded objects that have not been processed by this extractor."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
    SELECT
        downloads.*,
        files.work_id,
        files.site,
        files.format,
        files.url,
        files.download_url,
        works.title,
        works.author,
        extractions.status AS extraction_status
    FROM downloads
    JOIN files ON files.id = downloads.file_id
    JOIN works ON works.id = files.work_id
    LEFT JOIN extractions
      ON extractions.download_id = downloads.id
     AND extractions.extractor = ?
    WHERE
        downloads.status = 'downloaded'
        AND (extractions.id IS NULL OR extractions.status = 'failed')
    ORDER BY downloads.id ASC
    LIMIT ?
    """, (extractor, limit))

    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

def mark_extraction_started(download_id, extractor):
    """Creates or updates the extraction row when text processing starts."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO extractions (download_id, extractor, status, updated_at)
    VALUES (?, ?, 'processing', CURRENT_TIMESTAMP)
    ON CONFLICT(download_id, extractor) DO UPDATE SET
        status = 'processing',
        error = NULL,
        updated_at = CURRENT_TIMESTAMP
    """, (download_id, extractor))
    conn.commit()
    conn.close()

def mark_extraction_succeeded(download_id, extractor, text_uri, text_sha256, char_count, category, warnings=None):
    """Persists a plaintext object and lightweight category for a raw download."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE extractions
    SET
        status = 'processed',
        text_uri = ?,
        text_sha256 = ?,
        char_count = ?,
        category = ?,
        warnings = ?,
        error = NULL,
        updated_at = CURRENT_TIMESTAMP,
        processed_at = CURRENT_TIMESTAMP
    WHERE download_id = ? AND extractor = ?
    """, (text_uri, text_sha256, char_count, category, warnings, download_id, extractor))
    conn.commit()
    conn.close()

def mark_extraction_skipped(download_id, extractor, reason):
    """Marks a downloaded object as intentionally skipped by this extractor."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE extractions
    SET
        status = 'skipped',
        warnings = ?,
        error = NULL,
        updated_at = CURRENT_TIMESTAMP,
        processed_at = CURRENT_TIMESTAMP
    WHERE download_id = ? AND extractor = ?
    """, (str(reason)[:1000], download_id, extractor))
    conn.commit()
    conn.close()

def mark_extraction_failed(download_id, extractor, error):
    """Records a failed text extraction attempt."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE extractions
    SET
        status = 'failed',
        error = ?,
        updated_at = CURRENT_TIMESTAMP
    WHERE download_id = ? AND extractor = ?
    """, (str(error)[:1000], download_id, extractor))
    conn.commit()
    conn.close()

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
