#!/usr/bin/env python3
"""
Rerun Anna's Archive works that were previously archived as HTML stubs.

This script intentionally drives ALGE through bin/alge -c instead of calling the
crawler internals directly. It builds targeted query batches from the live DB,
then runs the normal /cycle command to discover, download, process, validate,
and optionally archive raw originals.
"""

import argparse
import difflib
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse


REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR))

import db
import downloader
import processor

DB_FILE = REPO_DIR / "archive_works.db"
ALGE = REPO_DIR / "bin" / "alge"
DEFAULT_SOURCES = ("annas_archive", "libgen", "archive_org")
TITLE_STOPWORDS = {
    "a",
    "an",
    "and",
    "at",
    "book",
    "by",
    "for",
    "from",
    "in",
    "of",
    "on",
    "the",
    "to",
    "vol",
    "volume",
    "with",
}


def _is_page_path(url):
    path = urllib.parse.urlparse(url or "").path or ""
    return path.startswith((
        "/md5/",
        "/view",
        "/search",
        "/datasets",
        "/torrents",
        "/member_codes",
        "/fast_download_not_member",
    ))


def _connect(db_file):
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    return conn


def _normalize_match_text(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _significant_title_tokens(value):
    return {
        token
        for token in _normalize_match_text(value).split()
        if len(token) > 2 and token not in TITLE_STOPWORDS
    }


def _authors_compatible(left, right):
    left_key = _normalize_match_text(left)
    right_key = _normalize_match_text(right)
    if not left_key or not right_key:
        return False
    return left_key in right_key or right_key in left_key


def _titles_compatible(left, right, authors_match=False):
    left_key = _normalize_match_text(left)
    right_key = _normalize_match_text(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    if len(left_key) >= 12 and len(right_key) >= 12 and (left_key in right_key or right_key in left_key):
        return True

    ratio = difflib.SequenceMatcher(None, left_key, right_key).ratio()
    if ratio >= 0.90:
        return True

    left_tokens = _significant_title_tokens(left)
    right_tokens = _significant_title_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    smaller = left_tokens if len(left_tokens) <= len(right_tokens) else right_tokens
    overlap = len(left_tokens & right_tokens)
    if authors_match and overlap >= 2 and overlap / len(smaller) >= 0.85:
        return True
    return overlap >= 3 and overlap / len(smaller) >= 1.0


def affected_works(db_file=DB_FILE, include_resolved=False, limit=None, start_after_work_id=None):
    conn = _connect(db_file)
    rows = conn.execute("""
    SELECT
        works.id AS work_id,
        works.title,
        works.author,
        files.id AS file_id,
        files.format,
        files.url,
        files.download_url,
        downloads.final_url,
        downloads.content_type,
        extractions.status AS extraction_status,
        extractions.quality_status
    FROM files
    JOIN works ON works.id = files.work_id
    JOIN downloads ON downloads.file_id = files.id
    LEFT JOIN extractions ON extractions.download_id = downloads.id
    WHERE (
        files.site LIKE '%anna%'
        OR files.url LIKE '%annas-archive%'
        OR files.download_url LIKE '%annas-archive%'
        OR downloads.final_url LIKE '%annas-archive%'
    )
      AND downloads.status = 'downloaded'
      AND COALESCE(downloads.content_type, '') LIKE '%text/html%'
    ORDER BY works.id, files.id
    """).fetchall()

    works = {}
    for row in rows:
        item = dict(row)
        if start_after_work_id is not None and item["work_id"] <= start_after_work_id:
            continue
        if not (
            _is_page_path(item.get("url"))
            or _is_page_path(item.get("download_url"))
            or _is_page_path(item.get("final_url"))
        ):
            continue
        if not include_resolved and work_has_usable_text(conn, item["work_id"]):
            continue
        if not include_resolved and matching_work_has_usable_text(conn, item):
            continue
        works.setdefault(item["work_id"], item)
        if limit and len(works) >= limit:
            break
    conn.close()
    return list(works.values())


def work_has_usable_text(conn, work_id):
    row = conn.execute("""
    SELECT 1
    FROM files
    JOIN downloads ON downloads.file_id = files.id
    JOIN extractions ON extractions.download_id = downloads.id
    WHERE files.work_id = ?
      AND extractions.status = 'processed'
      AND extractions.text_uri IS NOT NULL
      AND COALESCE(extractions.quality_status, 'usable') != 'unusable'
    LIMIT 1
    """, (work_id,)).fetchone()
    return row is not None


def matching_work_has_usable_text(conn, item):
    title_key = _normalize_match_text(item.get("title"))
    author_key = _normalize_match_text(item.get("author"))
    if len(title_key) < 8:
        return False

    rows = conn.execute("""
    SELECT works.id, works.title, works.author
    FROM works
    JOIN files ON files.work_id = works.id
    JOIN downloads ON downloads.file_id = files.id
    JOIN extractions ON extractions.download_id = downloads.id
    WHERE works.id != ?
      AND extractions.status = 'processed'
      AND extractions.text_uri IS NOT NULL
      AND COALESCE(extractions.quality_status, 'usable') != 'unusable'
    """, (item["work_id"],)).fetchall()

    for row in rows:
        authors_match = _authors_compatible(item.get("author"), row["author"])
        if not _titles_compatible(item.get("title"), row["title"], authors_match=authors_match):
            continue
        candidate_author = _normalize_match_text(row["author"])
        candidate_title = _normalize_match_text(row["title"])
        if not author_key or authors_match or (not candidate_author and title_key == candidate_title):
            return True
    return False


def query_for_work(row):
    title = " ".join(str(row.get("title") or "").split())
    author = " ".join(str(row.get("author") or "").split())
    if author:
        return f"{title} {author}"
    return title


def batched(items, size):
    for index in range(0, len(items), size):
        yield items[index:index + size]


def alge_command(args, queries_file, download_limit=None, process_limit=None):
    download_limit = args.download_limit if download_limit is None else download_limit
    process_limit = args.process_limit if process_limit is None else process_limit
    parts = [
        "/cycle",
        "--queries-file",
        str(queries_file),
        "--sources",
        *args.sources,
        "--max-results",
        str(args.max_results),
        "--download-limit",
        str(download_limit),
        "--process-limit",
        str(process_limit),
        "--rps",
        str(args.rps),
        "--max-mb",
        str(args.max_mb),
    ]
    if args.max_domains is not None:
        parts.extend(["--max-domains", str(args.max_domains)])
    if args.per_domain_limit is not None:
        parts.extend(["--per-domain-limit", str(args.per_domain_limit)])
    return " ".join(shlex.quote(part) for part in parts)


def run_alge(command, dry_run=False):
    env = os.environ.copy()
    env["ALGE_NO_TMUX"] = "1"
    env.setdefault("ALGE_NO_BANNER", "1")
    invocation = [str(ALGE), "--no-tmux", "-c", command]
    print("+ " + " ".join(shlex.quote(part) for part in invocation), flush=True)
    if dry_run:
        return 0
    completed = subprocess.run(invocation, cwd=REPO_DIR, env=env, check=False)
    return completed.returncode


def run_followup_commands(args):
    commands = []
    if args.validate:
        commands.append(
            "/validate-texts --limit "
            f"{args.validate_limit} --workers {args.validate_workers}"
        )
    if not args.no_archive_raw:
        commands.append(f"/archive-raw --limit {args.archive_raw_limit}")

    for command in commands:
        code = run_alge(command, dry_run=args.dry_run)
        if code:
            return code
    return 0


def write_queries(batch):
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        prefix="annas-rerun-",
        suffix=".txt",
        dir=REPO_DIR / "tmp" if (REPO_DIR / "tmp").is_dir() else None,
        delete=False,
    )
    with handle:
        for row in batch:
            query = query_for_work(row)
            if query:
                handle.write(query + "\n")
    return Path(handle.name)


def run_targeted_workflow(args, batch):
    queries = [query_for_work(row) for row in batch if query_for_work(row)]
    work_ids = set(db.get_work_ids_for_search_queries(queries))
    work_ids.update(row["work_id"] for row in batch)
    if not work_ids:
        print("targeted workflow: no matching works discovered", flush=True)
        return 0

    max_bytes = args.max_mb * 1024 * 1024 if args.max_mb else None
    download_results = downloader.download_work_ids_by_domain(
        sorted(work_ids),
        limit=args.download_limit,
        bucket_dir=downloader.DEFAULT_RAW_BUCKET_DIR,
        requests_per_second=args.rps,
        max_bytes=max_bytes,
        max_domains=args.max_domains,
        per_domain_limit=args.per_domain_limit,
        quarantine_dir=downloader.DEFAULT_QUARANTINE_BUCKET_DIR,
    )
    process_results = processor.process_pending_for_work_ids(
        sorted(work_ids),
        limit=args.process_limit,
        extractor=processor.EXTRACTOR_VERSION,
    )

    print("\n============= TARGETED REPAIR SUMMARY ===========")
    print(f"work_ids: {len(work_ids)}")
    print(f"downloads: {download_results}")
    print(f"processing: {process_results}")
    print("=================================================", flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Use ALGE to rerun Anna's Archive HTML-stub affected works.",
    )
    parser.add_argument("--db-file", default=str(DB_FILE))
    parser.add_argument("--limit", type=int, help="Maximum affected works to rerun.")
    parser.add_argument("--start-after-work-id", type=int, help="Resume after this affected work id.")
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--include-resolved", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sources", nargs="+", default=list(DEFAULT_SOURCES))
    parser.add_argument("--max-results", type=int, default=5)
    parser.add_argument("--download-limit", type=int, default=25)
    parser.add_argument("--process-limit", type=int, default=25)
    parser.add_argument("--rps", type=float, default=0.05)
    parser.add_argument("--max-mb", type=int, default=250)
    parser.add_argument("--max-domains", type=int, default=4)
    parser.add_argument("--per-domain-limit", type=int, default=3)
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--validate-limit", type=int, default=25)
    parser.add_argument("--validate-workers", type=int, default=4)
    parser.add_argument("--no-archive-raw", action="store_true")
    parser.add_argument("--archive-raw-limit", type=int, default=25)
    parser.add_argument("--keep-query-files", action="store_true")
    parser.add_argument("--global-queue", action="store_false", dest="targeted_workflow")
    parser.set_defaults(targeted_workflow=True)
    args = parser.parse_args()

    works = affected_works(
        db_file=args.db_file,
        include_resolved=args.include_resolved,
        limit=args.limit,
        start_after_work_id=args.start_after_work_id,
    )
    print(f"affected works selected: {len(works)}", flush=True)
    if not works:
        return 0

    failures = 0
    for batch_index, batch in enumerate(batched(works, max(args.batch_size, 1)), start=1):
        query_file = write_queries(batch)
        print(f"batch {batch_index}: {len(batch)} works -> {query_file}", flush=True)
        for row in batch:
            print(f"  - #{row['work_id']} {query_for_work(row)}", flush=True)

        try:
            cycle_download_limit = 0 if args.targeted_workflow else None
            cycle_process_limit = 0 if args.targeted_workflow else None
            code = run_alge(
                alge_command(
                    args,
                    query_file,
                    download_limit=cycle_download_limit,
                    process_limit=cycle_process_limit,
                ),
                dry_run=args.dry_run,
            )
            if code:
                failures += 1
                print(f"batch {batch_index} failed with exit code {code}", flush=True)
            if args.targeted_workflow and not args.dry_run:
                targeted_code = run_targeted_workflow(args, batch)
                if targeted_code:
                    failures += 1
                    print(f"batch {batch_index} targeted workflow failed with exit code {targeted_code}", flush=True)
            followup_code = run_followup_commands(args)
            if followup_code:
                failures += 1
                print(f"batch {batch_index} follow-up failed with exit code {followup_code}", flush=True)
        finally:
            if not args.keep_query_files:
                query_file.unlink(missing_ok=True)

    if failures:
        print(f"completed with {failures} failed command(s)", flush=True)
        return 1
    print("rerun complete", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
