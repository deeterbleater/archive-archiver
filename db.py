import os
import sqlite3

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "archive_works.db")

def get_connection():
    """Returns a connection to the SQLite database."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def _ensure_column(cursor, table, column, definition):
    cursor.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cursor.fetchall()}
    if column not in existing:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

def init_db():
    """Initializes the SQLite database tables."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version TEXT PRIMARY KEY,
        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
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
        final_url TEXT,
        etag TEXT,
        last_modified TEXT,
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

    _ensure_column(cursor, "downloads", "final_url", "TEXT")
    _ensure_column(cursor, "downloads", "etag", "TEXT")
    _ensure_column(cursor, "downloads", "last_modified", "TEXT")
    _ensure_column(cursor, "files", "trust_level", "TEXT NOT NULL DEFAULT 'trusted'")
    _ensure_column(cursor, "downloads", "scan_status", "TEXT")
    _ensure_column(cursor, "downloads", "scan_engine", "TEXT")
    _ensure_column(cursor, "downloads", "scan_signature", "TEXT")
    _ensure_column(cursor, "downloads", "quarantine_uri", "TEXT")
    _ensure_column(cursor, "downloads", "raw_archive_uri", "TEXT")
    _ensure_column(cursor, "downloads", "raw_archive_status", "TEXT")
    _ensure_column(cursor, "downloads", "raw_archive_error", "TEXT")
    _ensure_column(cursor, "downloads", "raw_archived_at", "TIMESTAMP")
    _ensure_column(cursor, "downloads", "local_raw_deleted_at", "TIMESTAMP")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS corpus_specs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        selection_json TEXT NOT NULL,
        ordering_strategy TEXT NOT NULL,
        normalizer_version TEXT NOT NULL,
        substitutions_sha256 TEXT NOT NULL,
        substitutions_json TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS corpus_builds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        spec_id INTEGER NOT NULL,
        manifest_sha256 TEXT NOT NULL UNIQUE,
        manifest_uri TEXT NOT NULL,
        corpus_uri TEXT NOT NULL,
        item_count INTEGER NOT NULL,
        total_chars INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (spec_id) REFERENCES corpus_specs (id) ON DELETE RESTRICT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS corpus_items (
        build_id INTEGER NOT NULL,
        item_index INTEGER NOT NULL,
        extraction_id INTEGER NOT NULL,
        work_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        author TEXT,
        text_uri TEXT NOT NULL,
        text_sha256 TEXT NOT NULL,
        transformed_sha256 TEXT NOT NULL,
        char_count INTEGER NOT NULL,
        PRIMARY KEY (build_id, item_index),
        FOREIGN KEY (build_id) REFERENCES corpus_builds (id) ON DELETE CASCADE,
        FOREIGN KEY (extraction_id) REFERENCES extractions (id) ON DELETE RESTRICT,
        FOREIGN KEY (work_id) REFERENCES works (id) ON DELETE RESTRICT
    )
    """)

    cursor.execute("""
    INSERT OR IGNORE INTO schema_migrations(version)
    VALUES ('001_manifest_download_process_corpus')
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

def add_file(work_id, site, format, url, file_size=None, download_source=None, download_url=None, trust_level="trusted"):
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
    trust_level = str(trust_level or "trusted")
        
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
    INSERT INTO files (work_id, site, format, url, file_size, download_source, download_url, trust_level)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(work_id, format, url) DO UPDATE SET
        file_size = excluded.file_size,
        download_source = excluded.download_source,
        download_url = excluded.download_url,
        site = excluded.site,
        trust_level = excluded.trust_level
    """, (work_id, site, format, url, file_size, download_source, download_url, trust_level))
    
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

    cursor.execute("SELECT COALESCE(scan_status, 'unscanned'), COUNT(*) FROM downloads GROUP BY COALESCE(scan_status, 'unscanned')")
    scans_by_status = {row[0]: row[1] for row in cursor.fetchall()}

    cursor.execute("SELECT COALESCE(raw_archive_status, 'local'), COUNT(*) FROM downloads GROUP BY COALESCE(raw_archive_status, 'local')")
    raw_archives_by_status = {row[0]: row[1] for row in cursor.fetchall()}

    cursor.execute("SELECT COUNT(*) FROM corpus_builds")
    total_corpus_builds = cursor.fetchone()[0]
    
    conn.close()
    return {
        "total_works": total_works,
        "total_files": total_files,
        "files_by_site": files_by_site,
        "downloads_by_status": downloads_by_status,
        "extractions_by_status": extractions_by_status,
        "scans_by_status": scans_by_status,
        "raw_archives_by_status": raw_archives_by_status,
        "total_corpus_builds": total_corpus_builds,
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

def mark_download_succeeded(
    file_id,
    bucket_uri,
    storage_key,
    sha256,
    byte_count,
    content_type=None,
    http_status=None,
    final_url=None,
    etag=None,
    last_modified=None,
    scan_status=None,
    scan_engine=None,
    scan_signature=None,
    quarantine_uri=None,
):
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
        final_url = ?,
        etag = ?,
        last_modified = ?,
        http_status = ?,
        scan_status = ?,
        scan_engine = ?,
        scan_signature = ?,
        quarantine_uri = ?,
        error = NULL,
        updated_at = CURRENT_TIMESTAMP,
        downloaded_at = CURRENT_TIMESTAMP
    WHERE file_id = ?
    """, (
        bucket_uri,
        storage_key,
        sha256,
        byte_count,
        content_type,
        final_url,
        etag,
        last_modified,
        http_status,
        scan_status,
        scan_engine,
        scan_signature,
        quarantine_uri,
        file_id,
    ))
    conn.commit()
    conn.close()

def mark_download_failed(file_id, error, http_status=None, scan_status=None, scan_engine=None, scan_signature=None, quarantine_uri=None):
    """Records a failed download attempt without deleting prior manifest data."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE downloads
    SET
        status = 'failed',
        error = ?,
        http_status = ?,
        scan_status = COALESCE(?, scan_status),
        scan_engine = COALESCE(?, scan_engine),
        scan_signature = COALESCE(?, scan_signature),
        quarantine_uri = COALESCE(?, quarantine_uri),
        updated_at = CURRENT_TIMESTAMP
    WHERE file_id = ?
    """, (str(error)[:1000], http_status, scan_status, scan_engine, scan_signature, quarantine_uri, file_id))
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


def get_raw_archive_candidates(limit=10):
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
        works.author
    FROM downloads
    JOIN files ON files.id = downloads.file_id
    JOIN works ON works.id = files.work_id
    JOIN extractions ON extractions.download_id = downloads.id
    WHERE
        downloads.status = 'downloaded'
        AND downloads.bucket_uri LIKE 'file:%'
        AND downloads.raw_archive_uri IS NULL
        AND downloads.local_raw_deleted_at IS NULL
        AND extractions.status = 'processed'
    GROUP BY downloads.id
    ORDER BY downloads.id ASC
    LIMIT ?
    """, (limit,))
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


def mark_raw_archive_succeeded(download_id, raw_archive_uri, delete_local=False):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE downloads
    SET
        raw_archive_uri = ?,
        raw_archive_status = 'archived',
        raw_archive_error = NULL,
        raw_archived_at = CURRENT_TIMESTAMP,
        local_raw_deleted_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE local_raw_deleted_at END,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = ?
    """, (raw_archive_uri, 1 if delete_local else 0, download_id))
    conn.commit()
    conn.close()


def mark_raw_archive_failed(download_id, error):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE downloads
    SET
        raw_archive_status = 'failed',
        raw_archive_error = ?,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = ?
    """, (str(error)[:1000], download_id))
    conn.commit()
    conn.close()

def get_processed_extractions(category=None, site=None, query=None, limit=None):
    """Returns extracted plaintext rows eligible for deterministic corpus builds."""
    conn = get_connection()
    cursor = conn.cursor()

    filters = ["extractions.status = 'processed'", "extractions.text_uri IS NOT NULL"]
    params = []

    if category:
        filters.append("extractions.category = ?")
        params.append(category)
    if site:
        filters.append("files.site = ?")
        params.append(site)
    if query:
        filters.append("""
        (
            works.search_query LIKE ?
            OR works.title LIKE ?
            OR COALESCE(works.author, '') LIKE ?
            OR COALESCE(extractions.category, '') LIKE ?
        )
        """)
        like_query = f"%{query}%"
        params.extend([like_query, like_query, like_query, like_query])

    limit_clause = ""
    if limit is not None:
        limit_clause = "LIMIT ?"
        params.append(limit)

    cursor.execute(f"""
    SELECT
        extractions.id AS extraction_id,
        extractions.extractor,
        extractions.text_uri,
        extractions.text_sha256,
        extractions.char_count,
        extractions.category,
        downloads.id AS download_id,
        downloads.sha256 AS raw_sha256,
        files.id AS file_id,
        files.work_id,
        files.site,
        files.format,
        files.url,
        files.download_url,
        works.title,
        works.author,
        works.search_query
    FROM extractions
    JOIN downloads ON downloads.id = extractions.download_id
    JOIN files ON files.id = downloads.file_id
    JOIN works ON works.id = files.work_id
    WHERE {" AND ".join(filters)}
    ORDER BY works.title COLLATE NOCASE, works.author COLLATE NOCASE, extractions.text_sha256
    {limit_clause}
    """, params)

    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

def upsert_corpus_spec(name, selection_json, ordering_strategy, normalizer_version, substitutions_sha256, substitutions_json):
    """Creates or updates a named corpus recipe and returns its id."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO corpus_specs (
        name,
        selection_json,
        ordering_strategy,
        normalizer_version,
        substitutions_sha256,
        substitutions_json
    )
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(name) DO UPDATE SET
        selection_json = excluded.selection_json,
        ordering_strategy = excluded.ordering_strategy,
        normalizer_version = excluded.normalizer_version,
        substitutions_sha256 = excluded.substitutions_sha256,
        substitutions_json = excluded.substitutions_json
    """, (
        name,
        selection_json,
        ordering_strategy,
        normalizer_version,
        substitutions_sha256,
        substitutions_json,
    ))
    conn.commit()
    cursor.execute("SELECT id FROM corpus_specs WHERE name = ?", (name,))
    spec_id = cursor.fetchone()[0]
    conn.close()
    return spec_id

def add_corpus_build(spec_id, manifest_sha256, manifest_uri, corpus_uri, item_count, total_chars, items):
    """Persists an immutable corpus build and its ordered item manifest."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO corpus_builds (
        spec_id,
        manifest_sha256,
        manifest_uri,
        corpus_uri,
        item_count,
        total_chars
    )
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(manifest_sha256) DO UPDATE SET
        manifest_uri = excluded.manifest_uri,
        corpus_uri = excluded.corpus_uri,
        item_count = excluded.item_count,
        total_chars = excluded.total_chars
    """, (spec_id, manifest_sha256, manifest_uri, corpus_uri, item_count, total_chars))
    conn.commit()
    cursor.execute("SELECT id FROM corpus_builds WHERE manifest_sha256 = ?", (manifest_sha256,))
    build_id = cursor.fetchone()[0]

    cursor.execute("DELETE FROM corpus_items WHERE build_id = ?", (build_id,))
    cursor.executemany("""
    INSERT INTO corpus_items (
        build_id,
        item_index,
        extraction_id,
        work_id,
        title,
        author,
        text_uri,
        text_sha256,
        transformed_sha256,
        char_count
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (
            build_id,
            item["item_index"],
            item["extraction_id"],
            item["work_id"],
            item["title"],
            item.get("author"),
            item["text_uri"],
            item["text_sha256"],
            item["transformed_sha256"],
            item["char_count"],
        )
        for item in items
    ])
    conn.commit()
    conn.close()
    return build_id

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
