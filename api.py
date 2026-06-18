import os
import sqlite3
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

import db


DEFAULT_LIMIT = 50
MAX_LIMIT = 500

app = FastAPI(
    title="Archive Archiver API",
    description="Read-only API for visualizing archive ingestion state.",
    version="0.1.0",
)

allowed_origins = [
    origin.strip()
    for origin in os.getenv("ARCHIVE_API_CORS_ORIGINS", "*").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _connect():
    conn = sqlite3.connect(f"file:{db.DB_FILE}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(sql, params=()):
    with _connect() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _one(sql, params=()):
    with _connect() as conn:
        row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _limit_offset(limit, offset):
    return min(limit, MAX_LIMIT), max(offset, 0)


@app.get("/health")
def health():
    return {
        "ok": True,
        "database": db.DB_FILE,
        "database_exists": os.path.exists(db.DB_FILE),
    }


@app.get("/summary")
def summary():
    base = db.get_stats()
    extra = _one("""
        SELECT
            COALESCE(SUM(downloads.bytes), 0) AS downloaded_bytes,
            COALESCE(SUM(extractions.char_count), 0) AS extracted_chars,
            COUNT(DISTINCT CASE WHEN downloads.status = 'downloaded' THEN downloads.file_id END) AS downloaded_files,
            COUNT(DISTINCT CASE WHEN downloads.status = 'failed' THEN downloads.file_id END) AS failed_download_files,
            COUNT(DISTINCT CASE WHEN extractions.status = 'processed' THEN extractions.id END) AS processed_texts,
            COUNT(DISTINCT CASE WHEN files.trust_level = 'untrusted' THEN files.id END) AS untrusted_files,
            COUNT(DISTINCT CASE WHEN downloads.scan_status = 'clean' THEN downloads.id END) AS clean_scans,
            COUNT(DISTINCT CASE WHEN downloads.scan_status = 'infected' THEN downloads.id END) AS infected_scans,
            COUNT(DISTINCT CASE WHEN downloads.scan_status = 'unavailable' THEN downloads.id END) AS unavailable_scans,
            COUNT(DISTINCT CASE WHEN downloads.quarantine_uri IS NOT NULL THEN downloads.id END) AS quarantined_files,
            COUNT(DISTINCT CASE WHEN downloads.raw_archive_status = 'archived' THEN downloads.id END) AS archived_raw_files,
            COUNT(DISTINCT CASE WHEN downloads.local_raw_deleted_at IS NOT NULL THEN downloads.id END) AS deleted_local_raw_files
        FROM files
        LEFT JOIN downloads ON downloads.file_id = files.id
        LEFT JOIN extractions ON extractions.download_id = downloads.id
    """)
    pending = _one("""
        SELECT COUNT(DISTINCT files.work_id) AS pending_download_files
        FROM files
        LEFT JOIN downloads ON downloads.file_id = files.id
        WHERE COALESCE(files.download_url, files.url, '') <> ''
          AND downloads.id IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM files sibling_files
              JOIN downloads sibling_downloads
                ON sibling_downloads.file_id = sibling_files.id
              WHERE sibling_files.work_id = files.work_id
          )
    """)
    return {**base, **extra, **pending}


@app.get("/viz/breakdowns/sites")
def site_breakdown():
    return _rows("""
        SELECT
            files.site,
            COUNT(DISTINCT works.id) AS works,
            COUNT(files.id) AS files,
            COUNT(CASE WHEN downloads.status = 'downloaded' THEN 1 END) AS downloaded,
            COUNT(CASE WHEN downloads.status = 'failed' THEN 1 END) AS failed_downloads,
            COUNT(CASE WHEN files.trust_level = 'untrusted' THEN 1 END) AS untrusted,
            COUNT(CASE WHEN downloads.scan_status = 'clean' THEN 1 END) AS clean_scans,
            COUNT(CASE WHEN downloads.scan_status = 'infected' THEN 1 END) AS infected_scans,
            COUNT(CASE WHEN downloads.scan_status = 'unavailable' THEN 1 END) AS unavailable_scans,
            COUNT(CASE WHEN extractions.status = 'processed' THEN 1 END) AS processed,
            COALESCE(SUM(downloads.bytes), 0) AS bytes,
            COALESCE(SUM(extractions.char_count), 0) AS chars
        FROM files
        JOIN works ON works.id = files.work_id
        LEFT JOIN downloads ON downloads.file_id = files.id
        LEFT JOIN extractions ON extractions.download_id = downloads.id
        GROUP BY files.site
        ORDER BY files DESC, works DESC
    """)


@app.get("/viz/breakdowns/formats")
def format_breakdown():
    return _rows("""
        SELECT
            files.format,
            COUNT(*) AS files,
            COUNT(CASE WHEN downloads.status = 'downloaded' THEN 1 END) AS downloaded,
            COUNT(CASE WHEN extractions.status = 'processed' THEN 1 END) AS processed,
            COUNT(CASE WHEN extractions.status = 'skipped' THEN 1 END) AS skipped,
            COUNT(CASE WHEN extractions.status = 'failed' THEN 1 END) AS failed
        FROM files
        LEFT JOIN downloads ON downloads.file_id = files.id
        LEFT JOIN extractions ON extractions.download_id = downloads.id
        GROUP BY files.format
        ORDER BY files DESC
    """)


@app.get("/viz/breakdowns/categories")
def category_breakdown():
    return _rows("""
        SELECT
            COALESCE(extractions.category, 'unprocessed') AS category,
            COUNT(*) AS texts,
            COALESCE(SUM(extractions.char_count), 0) AS chars
        FROM downloads
        LEFT JOIN extractions ON extractions.download_id = downloads.id
        WHERE downloads.status = 'downloaded'
        GROUP BY COALESCE(extractions.category, 'unprocessed')
        ORDER BY texts DESC, chars DESC
    """)


@app.get("/viz/breakdowns/trust")
def trust_breakdown():
    return _rows("""
        SELECT
            files.trust_level,
            COUNT(*) AS files,
            COUNT(CASE WHEN downloads.status = 'downloaded' THEN 1 END) AS downloaded,
            COUNT(CASE WHEN downloads.status = 'failed' THEN 1 END) AS failed_downloads,
            COUNT(CASE WHEN downloads.scan_status = 'clean' THEN 1 END) AS clean_scans,
            COUNT(CASE WHEN downloads.scan_status = 'infected' THEN 1 END) AS infected_scans,
            COUNT(CASE WHEN downloads.scan_status = 'unavailable' THEN 1 END) AS unavailable_scans,
            COUNT(CASE WHEN downloads.quarantine_uri IS NOT NULL THEN 1 END) AS quarantined
        FROM files
        LEFT JOIN downloads ON downloads.file_id = files.id
        GROUP BY files.trust_level
        ORDER BY files DESC
    """)


@app.get("/viz/status/downloads")
def download_status():
    return _rows("""
        SELECT status, COUNT(*) AS count
        FROM downloads
        GROUP BY status
        ORDER BY count DESC
    """)


@app.get("/viz/status/extractions")
def extraction_status():
    return _rows("""
        SELECT status, COUNT(*) AS count
        FROM extractions
        GROUP BY status
        ORDER BY count DESC
    """)


@app.get("/viz/status/scans")
def scan_status():
    return _rows("""
        SELECT COALESCE(scan_status, 'unscanned') AS status, COUNT(*) AS count
        FROM downloads
        GROUP BY COALESCE(scan_status, 'unscanned')
        ORDER BY count DESC
    """)


@app.get("/viz/status/raw-archives")
def raw_archive_status():
    return _rows("""
        SELECT COALESCE(raw_archive_status, 'local') AS status, COUNT(*) AS count
        FROM downloads
        GROUP BY COALESCE(raw_archive_status, 'local')
        ORDER BY count DESC
    """)


@app.get("/viz/timeseries/works")
def works_timeseries(
    bucket: str = Query("day", pattern="^(day|hour)$"),
    limit: int = Query(90, ge=1, le=MAX_LIMIT),
):
    expression = "strftime('%Y-%m-%d %H:00:00', created_at)" if bucket == "hour" else "date(created_at)"
    return _rows(f"""
        SELECT {expression} AS bucket, COUNT(*) AS works
        FROM works
        GROUP BY {expression}
        ORDER BY bucket DESC
        LIMIT ?
    """, (limit,))


@app.get("/viz/timeseries/downloads")
def downloads_timeseries(
    bucket: str = Query("day", pattern="^(day|hour)$"),
    limit: int = Query(90, ge=1, le=MAX_LIMIT),
):
    expression = "strftime('%Y-%m-%d %H:00:00', downloaded_at)" if bucket == "hour" else "date(downloaded_at)"
    return _rows(f"""
        SELECT {expression} AS bucket, COUNT(*) AS downloads, COALESCE(SUM(bytes), 0) AS bytes
        FROM downloads
        WHERE downloaded_at IS NOT NULL
        GROUP BY {expression}
        ORDER BY bucket DESC
        LIMIT ?
    """, (limit,))


@app.get("/viz/timeseries/extractions")
def extractions_timeseries(
    bucket: str = Query("day", pattern="^(day|hour)$"),
    limit: int = Query(90, ge=1, le=MAX_LIMIT),
):
    expression = "strftime('%Y-%m-%d %H:00:00', processed_at)" if bucket == "hour" else "date(processed_at)"
    return _rows(f"""
        SELECT {expression} AS bucket, COUNT(*) AS extractions, COALESCE(SUM(char_count), 0) AS chars
        FROM extractions
        WHERE processed_at IS NOT NULL
        GROUP BY {expression}
        ORDER BY bucket DESC
        LIMIT ?
    """, (limit,))


@app.get("/activity/recent")
def recent_activity(limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)):
    return {
        "works": _rows("""
            SELECT id, title, author, search_query, created_at
            FROM works
            ORDER BY created_at DESC, id DESC
            LIMIT ?
        """, (limit,)),
        "downloads": _rows("""
            SELECT
                downloads.id,
                downloads.status,
                downloads.bytes,
                downloads.updated_at,
                downloads.downloaded_at,
                downloads.scan_status,
                downloads.scan_engine,
                downloads.scan_signature,
                downloads.quarantine_uri,
                downloads.raw_archive_status,
                downloads.raw_archive_uri,
                downloads.local_raw_deleted_at,
                files.site,
                files.format,
                files.trust_level,
                works.title,
                works.author
            FROM downloads
            JOIN files ON files.id = downloads.file_id
            JOIN works ON works.id = files.work_id
            ORDER BY downloads.updated_at DESC, downloads.id DESC
            LIMIT ?
        """, (limit,)),
        "extractions": _rows("""
            SELECT
                extractions.id,
                extractions.status,
                extractions.category,
                extractions.char_count,
                extractions.updated_at,
                extractions.processed_at,
                files.site,
                works.title,
                works.author
            FROM extractions
            JOIN downloads ON downloads.id = extractions.download_id
            JOIN files ON files.id = downloads.file_id
            JOIN works ON works.id = files.work_id
            ORDER BY extractions.updated_at DESC, extractions.id DESC
            LIMIT ?
        """, (limit,)),
    }


@app.get("/works")
def list_works(
    q: Optional[str] = None,
    site: Optional[str] = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
):
    limit, offset = _limit_offset(limit, offset)
    filters = []
    params = []
    if q:
        filters.append("(works.title LIKE ? OR COALESCE(works.author, '') LIKE ? OR COALESCE(works.search_query, '') LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    if site:
        filters.append("EXISTS (SELECT 1 FROM files WHERE files.work_id = works.id AND files.site = ?)")
        params.append(site)
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    params.extend([limit, offset])
    return _rows(f"""
        SELECT
            works.id,
            works.title,
            works.author,
            works.search_query,
            works.created_at,
            COUNT(files.id) AS file_count,
            COUNT(CASE WHEN downloads.status = 'downloaded' THEN 1 END) AS downloaded_count,
            COUNT(CASE WHEN extractions.status = 'processed' THEN 1 END) AS processed_count
        FROM works
        LEFT JOIN files ON files.work_id = works.id
        LEFT JOIN downloads ON downloads.file_id = files.id
        LEFT JOIN extractions ON extractions.download_id = downloads.id
        {where}
        GROUP BY works.id
        ORDER BY works.created_at DESC, works.id DESC
        LIMIT ? OFFSET ?
    """, params)


@app.get("/works/{work_id}")
def get_work(work_id: int):
    work = _one("SELECT * FROM works WHERE id = ?", (work_id,))
    if not work:
        raise HTTPException(status_code=404, detail="work not found")
    work["files"] = _rows("""
        SELECT
            files.*,
            downloads.id AS download_id,
            downloads.status AS download_status,
            downloads.bytes,
            downloads.bucket_uri,
            downloads.storage_key,
            downloads.http_status,
            COALESCE(downloads.scan_status, 'unscanned') AS scan_status,
            downloads.scan_engine,
            downloads.scan_signature,
            downloads.quarantine_uri,
            downloads.raw_archive_status,
            downloads.raw_archive_uri,
            downloads.local_raw_deleted_at,
            extractions.id AS extraction_id,
            extractions.status AS extraction_status,
            extractions.category,
            extractions.char_count,
            extractions.text_uri
        FROM files
        LEFT JOIN downloads ON downloads.file_id = files.id
        LEFT JOIN extractions ON extractions.download_id = downloads.id
        WHERE files.work_id = ?
        ORDER BY files.id
    """, (work_id,))
    return work


@app.get("/files")
def list_files(
    site: Optional[str] = None,
    format: Optional[str] = None,
    download_status: Optional[str] = None,
    extraction_status: Optional[str] = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
):
    limit, offset = _limit_offset(limit, offset)
    filters = []
    params = []
    if site:
        filters.append("files.site = ?")
        params.append(site)
    if format:
        filters.append("files.format = ?")
        params.append(format)
    if download_status:
        filters.append("COALESCE(downloads.status, 'pending') = ?")
        params.append(download_status)
    if extraction_status:
        filters.append("COALESCE(extractions.status, 'pending') = ?")
        params.append(extraction_status)
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    params.extend([limit, offset])
    return _rows(f"""
        SELECT
            files.id,
            files.work_id,
            files.site,
            files.format,
            files.file_size,
            files.url,
            files.download_url,
            files.trust_level,
            works.title,
            works.author,
            COALESCE(downloads.status, 'pending') AS download_status,
            downloads.bytes,
            downloads.http_status,
            COALESCE(downloads.scan_status, 'unscanned') AS scan_status,
            downloads.scan_engine,
            downloads.scan_signature,
            downloads.quarantine_uri,
            downloads.raw_archive_status,
            downloads.raw_archive_uri,
            downloads.local_raw_deleted_at,
            COALESCE(extractions.status, 'pending') AS extraction_status,
            extractions.category,
            extractions.char_count
        FROM files
        JOIN works ON works.id = files.work_id
        LEFT JOIN downloads ON downloads.file_id = files.id
        LEFT JOIN extractions ON extractions.download_id = downloads.id
        {where}
        ORDER BY files.created_at DESC, files.id DESC
        LIMIT ? OFFSET ?
    """, params)


@app.get("/corpora")
def list_corpora(limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT), offset: int = Query(0, ge=0)):
    limit, offset = _limit_offset(limit, offset)
    return _rows("""
        SELECT
            corpus_builds.id,
            corpus_specs.name,
            corpus_specs.selection_json,
            corpus_specs.ordering_strategy,
            corpus_builds.manifest_sha256,
            corpus_builds.item_count,
            corpus_builds.total_chars,
            corpus_builds.manifest_uri,
            corpus_builds.corpus_uri,
            corpus_builds.created_at
        FROM corpus_builds
        JOIN corpus_specs ON corpus_specs.id = corpus_builds.spec_id
        ORDER BY corpus_builds.created_at DESC, corpus_builds.id DESC
        LIMIT ? OFFSET ?
    """, (limit, offset))


@app.get("/dimensions")
def dimensions():
    return {
        "sites": _rows("SELECT site, COUNT(*) AS count FROM files GROUP BY site ORDER BY count DESC"),
        "formats": _rows("SELECT format, COUNT(*) AS count FROM files GROUP BY format ORDER BY count DESC"),
        "trust_levels": _rows("SELECT trust_level, COUNT(*) AS count FROM files GROUP BY trust_level ORDER BY count DESC"),
        "scan_statuses": _rows("SELECT COALESCE(scan_status, 'unscanned') AS status, COUNT(*) AS count FROM downloads GROUP BY COALESCE(scan_status, 'unscanned') ORDER BY count DESC"),
        "raw_archive_statuses": _rows("SELECT COALESCE(raw_archive_status, 'local') AS status, COUNT(*) AS count FROM downloads GROUP BY COALESCE(raw_archive_status, 'local') ORDER BY count DESC"),
        "categories": _rows("""
            SELECT category, COUNT(*) AS count
            FROM extractions
            WHERE category IS NOT NULL
            GROUP BY category
            ORDER BY count DESC
        """),
        "search_queries": _rows("""
            SELECT search_query, COUNT(*) AS count
            FROM works
            WHERE search_query IS NOT NULL
            GROUP BY search_query
            ORDER BY count DESC
        """),
    }
