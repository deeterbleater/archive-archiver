import os
import re
import shutil
import urllib.parse


FORMAT_PREFERENCE = {
    "text": 0,
    "plain text": 0,
    "txt": 0,
    "muse": 0,
    "html": 1,
    "htm": 1,
    "epub": 2,
    "fb2": 3,
    "rtf": 3,
    "docx": 4,
    "doc": 5,
    "pdf": 6,
    "mobi": 7,
    "kindle": 7,
    "azw": 8,
    "daisy": 9,
    "djvu": 10,
    "torrent": 99,
}


def _format_rank(value):
    text = str(value or "").lower()
    for key, rank in FORMAT_PREFERENCE.items():
        if key in text:
            return rank
    return 50


def _size_rank(value):
    text = str(value or "").strip().lower()
    if not text or text == "unknown":
        return 1_000_000_000_000
    match = re.search(r"([\d.]+)\s*(b|kb|mb|gb)?", text)
    if not match:
        return 1_000_000_000_000
    number = float(match.group(1))
    unit = match.group(2) or "b"
    multiplier = {
        "b": 1,
        "kb": 1024,
        "mb": 1024 * 1024,
        "gb": 1024 * 1024 * 1024,
    }.get(unit, 1)
    return int(number * multiplier)


def _is_onion_url(parsed):
    return (parsed.hostname or "").lower().endswith(".onion")


def _has_tor_proxy():
    return bool(os.getenv("ALGE_TOR_PROXY") or os.getenv("ARCHIVE_TOR_PROXY"))


def _has_torrent_client():
    configured = os.getenv("ALGE_TORRENT_CLIENT") or os.getenv("ARCHIVE_TORRENT_CLIENT")
    candidates = [configured] if configured else ["aria2c", "transmission-cli"]
    return any(candidate and (shutil.which(candidate) or os.path.exists(candidate)) for candidate in candidates)


def _looks_like_torrent(row, parsed):
    fmt = str(row.get("format") or "").lower()
    source = str(row.get("download_source") or "").lower()
    path = (parsed.path or "").lower()
    return "torrent" in fmt or "torrent" in source or path.endswith(".torrent")


def _is_bulk_torrent_url(url):
    text = str(url or "").lower()
    return any(
        marker in text
        for marker in (
            "/torrents/managed_by_aa/",
            "/torrents/external/",
            "pilimi-",
            "zlib2-",
            "libgen-",
            "libgen_rs_fic",
        )
    )


def _is_annas_stub_url(parsed):
    host = (parsed.hostname or "").lower()
    if "annas-archive." not in host:
        return False
    path = parsed.path or ""
    return path.startswith(("/md5/", "/view", "/search", "/datasets", "/torrents", "/member_codes", "/fast_download_not_member"))


def is_runnable_download_candidate(row):
    """Return false for URLs the local downloader cannot use without operator setup."""
    download_url = str(row.get("download_url") or row.get("url") or "")
    try:
        parsed = urllib.parse.urlparse(download_url)
    except ValueError:
        return False
    if parsed.scheme == "file":
        return bool(parsed.path)
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.netloc:
        return False
    if _is_annas_stub_url(parsed):
        return False
    if _is_onion_url(parsed) and not _has_tor_proxy():
        return False
    if _looks_like_torrent(row, parsed) and (_is_bulk_torrent_url(download_url) or not _has_torrent_client()):
        return False
    return True


def file_preference_key(row):
    download_url = str(row.get("download_url") or row.get("url") or "")
    try:
        parsed = urllib.parse.urlparse(download_url)
        available_rank = 0 if is_runnable_download_candidate(row) else 1
    except ValueError:
        available_rank = 2
    return (
        available_rank,
        _format_rank(row.get("format")),
        _size_rank(row.get("file_size")),
        int(row.get("mirror_rank") or 0),
        str(row.get("download_url") or row.get("url") or ""),
        str(row.get("id") or ""),
    )


def select_best_file(rows):
    candidates = [
        row for row in (rows or [])
        if (row.get("download_url") or row.get("url")) and is_runnable_download_candidate(row)
    ]
    if not candidates:
        return None
    return min(candidates, key=file_preference_key)
