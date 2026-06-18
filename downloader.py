import hashlib
import mimetypes
import os
from pathlib import Path
import random
import re
import time
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import db
import scanner
import scrapers


DEFAULT_RAW_BUCKET_DIR = os.getenv("ARCHIVE_RAW_BUCKET_DIR", "bucket/raw")
DEFAULT_QUARANTINE_BUCKET_DIR = os.getenv("ARCHIVE_QUARANTINE_BUCKET_DIR", "bucket/quarantine")


class DownloadQuarantined(ValueError):
    def __init__(self, message, scan_status, quarantine_uri, scan_engine=None, scan_signature=None):
        super().__init__(message)
        self.scan_status = scan_status
        self.quarantine_uri = quarantine_uri
        self.scan_engine = scan_engine
        self.scan_signature = scan_signature


def _safe_segment(value, fallback="unknown"):
    value = str(value or fallback).strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = value.strip(".-")
    return value[:96] or fallback


def _extension_from_response(url, content_type, fallback_format):
    parsed = urllib.parse.urlparse(url)
    suffix = Path(urllib.parse.unquote(parsed.path)).suffix
    if suffix and re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
        return suffix.lower()

    guessed = mimetypes.guess_extension((content_type or "").split(";")[0].strip())
    if guessed:
        return guessed

    fmt = str(fallback_format or "").lower()
    for ext in ("pdf", "epub", "mobi", "txt", "html", "htm", "xml", "json"):
        if ext in fmt:
            return f".{ext}"
    return ".bin"


def _object_key(file_row, sha256, extension):
    site = _safe_segment(file_row.get("site"))
    work_id = _safe_segment(file_row.get("work_id"), "work")
    file_id = _safe_segment(file_row.get("id"), "file")
    return f"{site}/{work_id}/{file_id}/{sha256[:16]}{extension}"


def download_domain(file_row):
    url = file_row.get("download_url") or file_row.get("url") or ""
    parsed = urllib.parse.urlparse(url)
    return parsed.netloc.lower() or _safe_segment(file_row.get("site"))


class HostRateLimiter:
    def __init__(self, requests_per_second=0.2, jitter_seconds=0.5):
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be greater than zero")
        self.min_interval = 1.0 / requests_per_second
        self.jitter_seconds = max(jitter_seconds, 0)
        self.last_seen = {}

    def wait(self, url):
        host = urllib.parse.urlparse(url).netloc or "default"
        now = time.monotonic()
        next_allowed = self.last_seen.get(host, 0) + self.min_interval
        delay = max(0, next_allowed - now)
        if self.jitter_seconds:
            delay += random.uniform(0, self.jitter_seconds)
        if delay:
            time.sleep(delay)
        self.last_seen[host] = time.monotonic()


def _stream_to_temp_file(response, temp_path, max_bytes):
    digest = hashlib.sha256()
    byte_count = 0

    with temp_path.open("wb") as out:
        for chunk in response.iter_content(chunk_size=1024 * 256):
            if not chunk:
                continue
            byte_count += len(chunk)
            if max_bytes is not None and byte_count > max_bytes:
                raise ValueError(f"download exceeded max byte limit ({max_bytes})")
            digest.update(chunk)
            out.write(chunk)

    return digest.hexdigest(), byte_count


def _is_untrusted(file_row):
    return str(file_row.get("trust_level") or "trusted").lower() != "trusted"


def _quarantine_key(file_row, sha256, extension):
    site = _safe_segment(file_row.get("site"))
    work_id = _safe_segment(file_row.get("work_id"), "work")
    file_id = _safe_segment(file_row.get("id"), "file")
    return f"{site}/{work_id}/{file_id}/{sha256[:16]}{extension}"


def download_file(
    file_row,
    bucket_dir=DEFAULT_RAW_BUCKET_DIR,
    limiter=None,
    max_bytes=None,
    quarantine_dir=DEFAULT_QUARANTINE_BUCKET_DIR,
):
    url = file_row.get("download_url") or file_row.get("url")
    if not url:
        raise ValueError("file row has no download_url or url")

    if limiter:
        limiter.wait(url)

    bucket_root = Path(bucket_dir)
    quarantine_root = Path(quarantine_dir)
    temp_dir = quarantine_root / ".tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"file-{file_row['id']}-{time.time_ns()}.part"

    try:
        with requests.get(
            url,
            headers=scrapers.get_headers(),
            timeout=(10, 90),
            stream=True,
            allow_redirects=True,
        ) as response:
            status_code = response.status_code
            if status_code >= 400:
                raise requests.HTTPError(f"HTTP {status_code}", response=response)

            content_type = response.headers.get("Content-Type")
            sha256, byte_count = _stream_to_temp_file(response, temp_path, max_bytes)
            extension = _extension_from_response(response.url, content_type, file_row.get("format"))
            storage_key = _object_key(file_row, sha256, extension)
            quarantine_key = _quarantine_key(file_row, sha256, extension)
            quarantine_path = quarantine_root / quarantine_key
            quarantine_path.parent.mkdir(parents=True, exist_ok=True)
            if quarantine_path.exists():
                temp_path.unlink(missing_ok=True)
            else:
                temp_path.replace(quarantine_path)

            required_scan = _is_untrusted(file_row)
            try:
                scan = scanner.scan_file(quarantine_path, required=required_scan)
            except scanner.MalwareDetected as exc:
                raise DownloadQuarantined(
                    f"quarantined malware signature={exc.signature}",
                    scan_status="infected",
                    scan_engine="clamscan",
                    scan_signature=exc.signature,
                    quarantine_uri=quarantine_path.resolve().as_uri(),
                ) from exc
            except scanner.ScannerUnavailable as exc:
                raise DownloadQuarantined(
                    f"quarantine scan unavailable: {exc}",
                    scan_status="unavailable",
                    scan_engine="none",
                    scan_signature=None,
                    quarantine_uri=quarantine_path.resolve().as_uri(),
                ) from exc

            final_path = bucket_root / storage_key
            final_path.parent.mkdir(parents=True, exist_ok=True)

            if final_path.exists():
                quarantine_path.unlink(missing_ok=True)
            else:
                quarantine_path.replace(final_path)

            return {
                "bucket_uri": final_path.resolve().as_uri(),
                "storage_key": storage_key,
                "sha256": sha256,
                "byte_count": byte_count,
                "content_type": content_type,
                "http_status": status_code,
                "final_url": response.url,
                "etag": response.headers.get("ETag"),
                "last_modified": response.headers.get("Last-Modified"),
                "scan_status": scan["status"],
                "scan_engine": scan["engine"],
                "scan_signature": scan["signature"],
                "quarantine_uri": None,
            }
    finally:
        temp_path.unlink(missing_ok=True)


def download_pending(limit=10, bucket_dir=DEFAULT_RAW_BUCKET_DIR, requests_per_second=0.2, max_bytes=None, quarantine_dir=DEFAULT_QUARANTINE_BUCKET_DIR):
    limiter = HostRateLimiter(requests_per_second=requests_per_second)
    rows = db.get_pending_download_files(limit=limit)
    results = {"downloaded": 0, "failed": 0, "skipped": 0}

    for row in rows:
        file_id = row["id"]
        print(f"[*] Downloading file {file_id}: {row.get('title')} [{row.get('format')}]")
        db.mark_download_started(file_id)
        try:
            metadata = download_file(row, bucket_dir=bucket_dir, limiter=limiter, max_bytes=max_bytes, quarantine_dir=quarantine_dir)
            db.mark_download_succeeded(file_id=file_id, **metadata)
            results["downloaded"] += 1
            print(f"    [+] Stored {metadata['byte_count']} bytes at {metadata['storage_key']}")
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            db.mark_download_failed(
                file_id,
                exc,
                http_status=status,
                scan_status=getattr(exc, "scan_status", None),
                scan_engine=getattr(exc, "scan_engine", None),
                scan_signature=getattr(exc, "scan_signature", None),
                quarantine_uri=getattr(exc, "quarantine_uri", None),
            )
            results["failed"] += 1
            print(f"    [!] Download failed: {exc}")

    return results


def _download_domain_rows(domain, rows, bucket_dir, requests_per_second, max_bytes, quarantine_dir):
    limiter = HostRateLimiter(requests_per_second=requests_per_second)
    results = {"downloaded": 0, "failed": 0, "skipped": 0}

    for row in rows:
        file_id = row["id"]
        print(f"[*] [{domain}] Downloading file {file_id}: {row.get('title')} [{row.get('format')}]")
        db.mark_download_started(file_id)
        try:
            metadata = download_file(row, bucket_dir=bucket_dir, limiter=limiter, max_bytes=max_bytes, quarantine_dir=quarantine_dir)
            db.mark_download_succeeded(file_id=file_id, **metadata)
            results["downloaded"] += 1
            print(f"    [+] [{domain}] Stored {metadata['byte_count']} bytes at {metadata['storage_key']}")
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            db.mark_download_failed(
                file_id,
                exc,
                http_status=status,
                scan_status=getattr(exc, "scan_status", None),
                scan_engine=getattr(exc, "scan_engine", None),
                scan_signature=getattr(exc, "scan_signature", None),
                quarantine_uri=getattr(exc, "quarantine_uri", None),
            )
            results["failed"] += 1
            print(f"    [!] [{domain}] Download failed: {exc}")

    return results


def download_pending_by_domain(
    limit=50,
    bucket_dir=DEFAULT_RAW_BUCKET_DIR,
    requests_per_second=0.2,
    max_bytes=None,
    max_domains=None,
    per_domain_limit=None,
    quarantine_dir=DEFAULT_QUARANTINE_BUCKET_DIR,
):
    if per_domain_limit is not None and per_domain_limit <= 0:
        return {"downloaded": 0, "failed": 0, "skipped": 0}

    rows = db.get_pending_download_files(limit=limit)
    grouped = defaultdict(list)
    for row in rows:
        domain = download_domain(row)
        if per_domain_limit is not None and len(grouped[domain]) >= per_domain_limit:
            continue
        grouped[domain].append(row)

    domains = sorted(grouped)
    if max_domains is not None:
        domains = domains[:max_domains]

    results = {"downloaded": 0, "failed": 0, "skipped": 0}
    if not domains:
        return results

    print(f"[*] Starting {len(domains)} domain download workers.")
    with ThreadPoolExecutor(max_workers=len(domains)) as executor:
        futures = {
            executor.submit(
                _download_domain_rows,
                domain,
                grouped[domain],
                bucket_dir,
                requests_per_second,
                max_bytes,
                quarantine_dir,
            ): domain
            for domain in domains
        }
        for future in as_completed(futures):
            domain_results = future.result()
            for key, count in domain_results.items():
                results[key] += count

    return results
