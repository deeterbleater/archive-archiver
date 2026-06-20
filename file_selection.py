import re
import urllib.parse


FORMAT_PREFERENCE = {
    "text": 0,
    "plain text": 0,
    "txt": 0,
    "html": 1,
    "htm": 1,
    "epub": 2,
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


def file_preference_key(row):
    download_url = str(row.get("download_url") or row.get("url") or "")
    try:
        parsed = urllib.parse.urlparse(download_url)
        available_rank = 0 if parsed.scheme in ("http", "https") and parsed.netloc else 1
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
        if row.get("download_url") or row.get("url")
    ]
    if not candidates:
        return None
    return min(candidates, key=file_preference_key)
