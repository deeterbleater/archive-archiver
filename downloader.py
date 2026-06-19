import hashlib
import mimetypes
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import time
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import db
import file_selection
import scanner
import scrapers
import terminal_theme


DEFAULT_RAW_BUCKET_DIR = os.getenv("ARCHIVE_RAW_BUCKET_DIR", "bucket/raw")
DEFAULT_QUARANTINE_BUCKET_DIR = os.getenv("ARCHIVE_QUARANTINE_BUCKET_DIR", "bucket/quarantine")
DEFAULT_TOR_PROXY = os.getenv("ALGE_TOR_PROXY") or os.getenv("ARCHIVE_TOR_PROXY")
DEFAULT_TORRENT_CLIENT = os.getenv("ALGE_TORRENT_CLIENT") or os.getenv("ARCHIVE_TORRENT_CLIENT")
DEFAULT_TORRENT_TIMEOUT_SECONDS = int(os.getenv("ALGE_TORRENT_TIMEOUT", "300"))
DEFAULT_TORRENT_STALL_SECONDS = int(os.getenv("ALGE_TORRENT_STALL_TIMEOUT", "60"))
TORRENT_PAYLOAD_EXTENSIONS = {
    ".txt",
    ".text",
    ".md",
    ".html",
    ".htm",
    ".xml",
    ".pdf",
    ".epub",
    ".gz",
}


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


def _content_type_for_path(path):
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def _object_key(file_row, sha256, extension):
    site = _safe_segment(file_row.get("site"))
    work_id = _safe_segment(file_row.get("work_id"), "work")
    file_id = _safe_segment(file_row.get("id"), "file")
    return f"{site}/{work_id}/{file_id}/{sha256[:16]}{extension}"


def download_domain(file_row):
    url = file_row.get("download_url") or file_row.get("url") or ""
    parsed = urllib.parse.urlparse(url)
    return parsed.netloc.lower() or _safe_segment(file_row.get("site"))


def _is_onion_url(url):
    try:
        parsed = urllib.parse.urlparse(str(url or ""))
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return host.endswith(".onion")


def _looks_like_torrent(file_row, url):
    fmt = str(file_row.get("format") or "").lower()
    source = str(file_row.get("download_source") or "").lower()
    try:
        parsed = urllib.parse.urlparse(str(url or ""))
        path = parsed.path.lower()
    except ValueError:
        path = str(url or "").lower()
    return "torrent" in fmt or "torrent" in source or path.endswith(".torrent")


def _is_bulk_torrent_url(url):
    text = str(url or "").lower()
    bulk_markers = (
        "/torrents/managed_by_aa/",
        "/torrents/external/",
        "pilimi-",
        "zlib2-",
        "libgen-",
        "libgen_rs_fic",
    )
    return any(marker in text for marker in bulk_markers)


def _request_proxies_for_url(url, tor_proxy=None):
    if not _is_onion_url(url):
        return None
    proxy = DEFAULT_TOR_PROXY if tor_proxy is None else tor_proxy
    if not proxy:
        raise ValueError(
            "refusing .onion download without Tor proxy; set ALGE_TOR_PROXY=socks5h://127.0.0.1:9050"
        )
    return {"http": proxy, "https": proxy}


def _is_annas_archive_file(file_row, url):
    site = str(file_row.get("site") or "").lower()
    parsed = urllib.parse.urlparse(str(url or ""))
    return "annas-archive" in site or "annas-archive." in parsed.netloc


def _reject_annas_stub_response(file_row, request_url, final_url, content_type):
    if not _is_annas_archive_file(file_row, request_url) and not _is_annas_archive_file(file_row, final_url):
        return
    parsed_request = urllib.parse.urlparse(str(request_url or ""))
    parsed_final = urllib.parse.urlparse(str(final_url or ""))
    bad_paths = ("/md5/", "/view", "/search", "/datasets", "/torrents", "/member_codes", "/fast_download_not_member")
    if any((parsed_request.path or "").startswith(path) for path in bad_paths):
        raise ValueError(f"refusing Anna's Archive page URL as file download: {request_url}")
    if any((parsed_final.path or "").startswith(path) for path in bad_paths):
        raise ValueError(f"Anna's Archive returned a page/gate instead of a file: {final_url}")
    if "text/html" in str(content_type or "").lower():
        raise ValueError(f"Anna's Archive returned HTML instead of a book file: {final_url}")


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


def _torrent_client():
    configured = DEFAULT_TORRENT_CLIENT
    if configured:
        resolved = shutil.which(configured) or configured
        if Path(resolved).exists() or shutil.which(resolved):
            return resolved
        raise ValueError(f"configured torrent client not found: {configured}")
    for candidate in ("aria2c", "transmission-cli"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise ValueError("torrent download requested but no torrent client is installed; install aria2c or transmission-cli")


def _torrent_command(client, url, staging_dir):
    name = Path(client).name
    if name == "aria2c":
        return [
            client,
            "--dir", str(staging_dir),
            "--follow-torrent=mem",
            "--seed-time=0",
            "--summary-interval=0",
            "--console-log-level=warn",
            "--bt-stop-timeout", str(DEFAULT_TORRENT_STALL_SECONDS),
            "--lowest-speed-limit", "1K",
            "--allow-overwrite=true",
            "--auto-file-renaming=false",
            url,
        ]
    if name == "transmission-cli":
        return [client, "-w", str(staging_dir), url]
    raise ValueError(f"unsupported torrent client: {client}")


def _payload_format_for_path(path):
    suffixes = [suffix.lower() for suffix in path.suffixes]
    if len(suffixes) >= 2 and suffixes[-1] == ".gz":
        return "".join(suffixes[-2:])
    return path.suffix.lower().lstrip(".") or "unknown"


def _select_torrent_payload(staging_dir, max_bytes=None):
    candidates = []
    for path in staging_dir.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.lower()
        if name.endswith((".torrent", ".aria2", ".part", ".resume")):
            continue
        if not path.suffix.lower() in TORRENT_PAYLOAD_EXTENSIONS:
            continue
        size = path.stat().st_size
        if size <= 0:
            continue
        if max_bytes is not None and size > max_bytes:
            continue
        candidates.append({
            "path": path,
            "format": _payload_format_for_path(path),
            "file_size": str(size),
            "download_url": path.resolve().as_uri(),
        })

    best = file_selection.select_best_file(candidates)
    if not best:
        raise ValueError("torrent completed but no supported plaintext-extractable payload was found")
    return best["path"]


def _hash_file(path):
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            byte_count += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), byte_count


def _promote_quarantined_payload(
    file_row,
    payload_path,
    content_type,
    final_url,
    http_status=None,
    bucket_dir=DEFAULT_RAW_BUCKET_DIR,
    quarantine_dir=DEFAULT_QUARANTINE_BUCKET_DIR,
    extension=None,
):
    bucket_root = Path(bucket_dir)
    quarantine_root = Path(quarantine_dir)
    sha256, byte_count = _hash_file(payload_path)
    extension = extension or payload_path.suffix.lower() or _extension_from_response(str(payload_path), content_type, file_row.get("format"))
    storage_key = _object_key(file_row, sha256, extension)
    quarantine_key = _quarantine_key(file_row, sha256, extension)
    quarantine_path = quarantine_root / quarantine_key
    quarantine_path.parent.mkdir(parents=True, exist_ok=True)
    if quarantine_path.exists():
        payload_path.unlink(missing_ok=True)
    else:
        payload_path.replace(quarantine_path)

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
        "http_status": http_status,
        "final_url": final_url,
        "etag": None,
        "last_modified": None,
        "scan_status": scan["status"],
        "scan_engine": scan["engine"],
        "scan_signature": scan["signature"],
        "quarantine_uri": None,
    }


def _download_torrent_payload(file_row, url, bucket_dir, quarantine_dir, max_bytes=None):
    client = _torrent_client()
    quarantine_root = Path(quarantine_dir)
    staging_dir = quarantine_root / f".torrent-{file_row['id']}-{time.time_ns()}"
    staging_dir.mkdir(parents=True, exist_ok=True)
    try:
        command = _torrent_command(client, url, staging_dir)
        subprocess.run(
            command,
            cwd=staging_dir,
            check=True,
            timeout=DEFAULT_TORRENT_TIMEOUT_SECONDS,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        payload = _select_torrent_payload(staging_dir, max_bytes=max_bytes)
        content_type = _content_type_for_path(payload)
        return _promote_quarantined_payload(
            file_row,
            payload,
            content_type,
            final_url=url,
            http_status=200,
            bucket_dir=bucket_dir,
            quarantine_dir=quarantine_dir,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"torrent download timed out after {DEFAULT_TORRENT_TIMEOUT_SECONDS}s") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise ValueError(f"torrent client failed: {detail[:500] or exc}") from exc
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def download_file(
    file_row,
    bucket_dir=DEFAULT_RAW_BUCKET_DIR,
    limiter=None,
    max_bytes=None,
    quarantine_dir=DEFAULT_QUARANTINE_BUCKET_DIR,
    tor_proxy=None,
):
    url = file_row.get("download_url") or file_row.get("url")
    if not url:
        raise ValueError("file row has no download_url or url")
    if _looks_like_torrent(file_row, url):
        if _is_bulk_torrent_url(url):
            raise ValueError(f"refusing bulk archive torrent as single-work download: {url}")
        return _download_torrent_payload(
            file_row,
            url,
            bucket_dir=bucket_dir,
            quarantine_dir=quarantine_dir,
            max_bytes=max_bytes,
        )
    proxies = _request_proxies_for_url(url, tor_proxy=tor_proxy)

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
            proxies=proxies,
        ) as response:
            status_code = response.status_code
            if status_code >= 400:
                raise requests.HTTPError(f"HTTP {status_code}", response=response)

            content_type = response.headers.get("Content-Type")
            _reject_annas_stub_response(file_row, url, response.url, content_type)
            extension = _extension_from_response(response.url, content_type, file_row.get("format"))
            _stream_to_temp_file(response, temp_path, max_bytes)
            metadata = _promote_quarantined_payload(
                file_row,
                temp_path,
                content_type,
                final_url=response.url,
                http_status=status_code,
                bucket_dir=bucket_dir,
                quarantine_dir=quarantine_dir,
                extension=extension,
            )
            metadata["etag"] = response.headers.get("ETag")
            metadata["last_modified"] = response.headers.get("Last-Modified")
            return metadata
    finally:
        temp_path.unlink(missing_ok=True)


def download_pending(limit=10, bucket_dir=DEFAULT_RAW_BUCKET_DIR, requests_per_second=0.2, max_bytes=None, quarantine_dir=DEFAULT_QUARANTINE_BUCKET_DIR):
    limiter = HostRateLimiter(requests_per_second=requests_per_second)
    rows = db.get_pending_download_files(limit=limit)
    results = {"downloaded": 0, "failed": 0, "skipped": 0}

    for row in rows:
        file_id = row["id"]
        terminal_theme.print_pip("pending", f"download file {file_id}: {row.get('title')} [{row.get('format')}]")
        db.mark_download_started(file_id)
        try:
            metadata = download_file(row, bucket_dir=bucket_dir, limiter=limiter, max_bytes=max_bytes, quarantine_dir=quarantine_dir)
            db.mark_download_succeeded(file_id=file_id, **metadata)
            results["downloaded"] += 1
            status = "success" if metadata.get("http_status") == 200 else "failed"
            terminal_theme.print_pip(status, f"HTTP {metadata.get('http_status')} stored {metadata['byte_count']} bytes at {metadata['storage_key']}")
            terminal_theme.print_pip("success" if metadata.get("scan_status") in ("clean", "unavailable") else "failed", f"scan {metadata.get('scan_status')} via {metadata.get('scan_engine')}")
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
            terminal_theme.print_pip("failed", f"download failed: {exc}")

    return results


def _download_domain_rows(domain, rows, bucket_dir, requests_per_second, max_bytes, quarantine_dir):
    limiter = HostRateLimiter(requests_per_second=requests_per_second)
    results = {"downloaded": 0, "failed": 0, "skipped": 0}

    for row in rows:
        file_id = row["id"]
        terminal_theme.print_pip("pending", f"[{domain}] download file {file_id}: {row.get('title')} [{row.get('format')}]")
        db.mark_download_started(file_id)
        try:
            metadata = download_file(row, bucket_dir=bucket_dir, limiter=limiter, max_bytes=max_bytes, quarantine_dir=quarantine_dir)
            db.mark_download_succeeded(file_id=file_id, **metadata)
            results["downloaded"] += 1
            status = "success" if metadata.get("http_status") == 200 else "failed"
            terminal_theme.print_pip(status, f"[{domain}] HTTP {metadata.get('http_status')} stored {metadata['byte_count']} bytes at {metadata['storage_key']}")
            terminal_theme.print_pip("success" if metadata.get("scan_status") in ("clean", "unavailable") else "failed", f"[{domain}] scan {metadata.get('scan_status')} via {metadata.get('scan_engine')}")
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
            terminal_theme.print_pip("failed", f"[{domain}] download failed: {exc}")

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
    return download_rows_by_domain(
        rows,
        bucket_dir=bucket_dir,
        requests_per_second=requests_per_second,
        max_bytes=max_bytes,
        max_domains=max_domains,
        per_domain_limit=per_domain_limit,
        quarantine_dir=quarantine_dir,
    )


def download_work_ids_by_domain(
    work_ids,
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
    rows = db.get_pending_download_files_for_work_ids(work_ids, limit=limit)
    return download_rows_by_domain(
        rows,
        bucket_dir=bucket_dir,
        requests_per_second=requests_per_second,
        max_bytes=max_bytes,
        max_domains=max_domains,
        per_domain_limit=per_domain_limit,
        quarantine_dir=quarantine_dir,
    )


def download_rows_by_domain(
    rows,
    bucket_dir=DEFAULT_RAW_BUCKET_DIR,
    requests_per_second=0.2,
    max_bytes=None,
    max_domains=None,
    per_domain_limit=None,
    quarantine_dir=DEFAULT_QUARANTINE_BUCKET_DIR,
):
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
