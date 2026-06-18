import hashlib
import os
from pathlib import Path
import re
import urllib.parse

from bs4 import BeautifulSoup

import db
import s3_storage
import terminal_theme


EXTRACTOR_VERSION = "plaintext.v2"
DEFAULT_TEXT_BUCKET_DIR = os.getenv("ARCHIVE_TEXT_BUCKET_DIR", "bucket/text")
ARCHIVE_RAW_TO_S3 = os.getenv("ARCHIVE_RAW_TO_S3", "0").lower() in ("1", "true", "yes")
STOPWORDS = {
    "about", "after", "again", "against", "also", "among", "because", "before",
    "being", "between", "could", "every", "first", "from", "have", "into",
    "more", "most", "other", "over", "shall", "should", "such", "than",
    "that", "their", "there", "these", "they", "this", "those", "through",
    "under", "upon", "were", "what", "when", "where", "which", "while",
    "with", "without", "would", "your", "the", "and", "for", "not", "are",
    "but", "you", "was", "his", "her", "its", "has", "had", "who", "one",
    "all", "can", "our", "out", "may", "will", "been", "them", "then",
    "some", "only", "many", "much", "very", "chapter", "page", "book",
    "archive", "library", "text", "work", "author",
}


class UnsupportedFormat(ValueError):
    pass


def _safe_segment(value, fallback="unknown"):
    value = str(value or fallback).strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = value.strip(".-")
    return value[:96] or fallback


def _path_from_file_uri(uri):
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "file":
        raise UnsupportedFormat(f"only file:// bucket URIs are supported by {EXTRACTOR_VERSION}")
    return Path(urllib.parse.unquote(parsed.path))


def _decode_bytes(raw):
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _normalize_text(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def _extract_html(raw):
    soup = BeautifulSoup(raw, "html.parser")
    for node in soup(["script", "style", "meta", "noscript"]):
        node.decompose()
    return _normalize_text(soup.get_text(separator="\n", strip=True))

def _extract_pdf(path):
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise UnsupportedFormat("PDF extraction requires the canonical 'pypdf' package") from exc

    reader = PdfReader(str(path))
    pages = []
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            pages.append(page.extract_text() or "")
        except Exception as exc:
            pages.append(f"\n[page {page_number} extraction failed: {exc}]\n")
    return _normalize_text("\n\n".join(pages))

def _extract_epub(path):
    try:
        import ebooklib
        from ebooklib import epub
    except ImportError as exc:
        raise UnsupportedFormat("EPUB extraction requires the canonical 'EbookLib' package") from exc

    book = epub.read_epub(str(path))
    chapters = []
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        chapters.append(_extract_html(item.get_content()))
    return _normalize_text("\n\n".join(chapters))


def extract_plaintext(path, content_type=None, format_hint=None):
    suffix = path.suffix.lower()
    hint = f"{content_type or ''} {format_hint or ''}".lower()
    raw = path.read_bytes()

    if suffix in (".html", ".htm", ".xml") or "html" in hint:
        return _extract_html(raw), "html"

    if suffix in (".txt", ".text", ".md", ".json", ".csv") or "text" in hint:
        return _normalize_text(_decode_bytes(raw)), "text"

    if suffix == ".pdf" or "pdf" in hint:
        return _extract_pdf(path), "pdf"

    if suffix == ".epub" or "epub" in hint:
        return _extract_epub(path), "epub"

    raise UnsupportedFormat(f"unsupported plaintext extractor format: {suffix or format_hint or content_type}")


def categorize_text(row, text):
    haystack = " ".join([
        str(row.get("title") or ""),
        str(row.get("author") or ""),
        str(row.get("site") or ""),
        text[:5000],
    ]).lower()

    for category in db.get_categories():
        needles = category.get("keywords") or [category["name"].replace("_", " ")]
        if any(needle in haystack for needle in needles):
            return category["name"]
    return create_dynamic_category(row, text)


def create_dynamic_category(row, text):
    source = " ".join([
        str(row.get("title") or ""),
        text[:20000],
    ]).lower()
    counts = {}
    for token in re.findall(r"[a-z][a-z0-9_'-]{3,}", source):
        token = token.strip("'_-")
        if len(token) < 4 or token in STOPWORDS or token.isdigit():
            continue
        counts[token] = counts.get(token, 0) + 1

    if not counts:
        return db.ensure_category(
            "uncategorized",
            description="Auto-created fallback for works without enough category signals.",
            keywords=[],
            dynamic=True,
        )

    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    keywords = [token for token, _count in ranked[:8]]
    return db.ensure_category(
        keywords[0],
        description="Auto-created during extraction because no existing category matched.",
        keywords=keywords,
        dynamic=True,
    )


def _text_storage_key(row, text_sha256):
    site = _safe_segment(row.get("site"))
    work_id = _safe_segment(row.get("work_id"), "work")
    download_id = _safe_segment(row.get("id"), "download")
    return f"{site}/{work_id}/{download_id}/{text_sha256[:16]}.txt"


def process_download(row, bucket_dir=DEFAULT_TEXT_BUCKET_DIR, extractor=EXTRACTOR_VERSION):
    raw_path = _path_from_file_uri(row["bucket_uri"])
    text, mode = extract_plaintext(
        raw_path,
        content_type=row.get("content_type"),
        format_hint=row.get("format"),
    )
    if not text:
        raise UnsupportedFormat("extractor produced empty text")

    text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    storage_key = _text_storage_key(row, text_sha256)
    final_path = Path(bucket_dir) / storage_key
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_text(text + "\n", encoding="utf-8")

    return {
        "text_uri": final_path.resolve().as_uri(),
        "text_sha256": text_sha256,
        "char_count": len(text),
        "category": categorize_text(row, text),
        "warnings": f"extractor_mode={mode}",
    }


def archive_raw_after_extraction(row, delete_local=True):
    raw_path = _path_from_file_uri(row["bucket_uri"])
    key = s3_storage.object_key(row.get("storage_key") or raw_path.name)
    client = s3_storage.S3Client()
    result = client.put_file(
        raw_path,
        key,
        content_type=row.get("content_type"),
        metadata={
            "download-id": row.get("id"),
            "file-id": row.get("file_id"),
            "raw-sha256": row.get("sha256"),
            "source-site": row.get("site"),
        },
    )
    if delete_local:
        raw_path.unlink(missing_ok=True)
    db.mark_raw_archive_succeeded(row["id"], result["uri"], delete_local=delete_local)
    return result


def process_pending(limit=10, bucket_dir=DEFAULT_TEXT_BUCKET_DIR, extractor=EXTRACTOR_VERSION):
    rows = db.get_pending_extractions(limit=limit, extractor=extractor)
    results = {"processed": 0, "failed": 0, "skipped": 0}

    for row in rows:
        download_id = row["id"]
        terminal_theme.print_pip("pending", f"process download {download_id}: {row.get('title')} [{row.get('format')}]")
        db.mark_extraction_started(download_id, extractor)
        try:
            metadata = process_download(row, bucket_dir=bucket_dir, extractor=extractor)
            db.mark_extraction_succeeded(download_id=download_id, extractor=extractor, **metadata)
            if ARCHIVE_RAW_TO_S3 and not row.get("raw_archive_uri"):
                try:
                    terminal_theme.print_pip("pending", f"archive raw after extraction {download_id}")
                    archive = archive_raw_after_extraction(row, delete_local=True)
                    terminal_theme.print_pip("success", f"archived raw object to {archive['uri']} and removed local copy")
                except Exception as archive_exc:
                    db.mark_raw_archive_failed(download_id, archive_exc)
                    terminal_theme.print_pip("failed", f"raw archive failed: {archive_exc}")
            results["processed"] += 1
            terminal_theme.print_pip("success", f"extracted {metadata['char_count']} chars as {metadata['category']}")
        except UnsupportedFormat as exc:
            db.mark_extraction_skipped(download_id, extractor, exc)
            results["skipped"] += 1
            terminal_theme.print_pip("skipped", f"skipped: {exc}")
        except Exception as exc:
            db.mark_extraction_failed(download_id, extractor, exc)
            results["failed"] += 1
            terminal_theme.print_pip("failed", f"extraction failed: {exc}")

    return results


def archive_processed_raws(limit=10, delete_local=True):
    rows = db.get_raw_archive_candidates(limit=limit)
    results = {"archived": 0, "failed": 0, "skipped": 0}
    for row in rows:
        terminal_theme.print_pip("pending", f"archive raw download {row['id']}: {row.get('title')} [{row.get('format')}]")
        try:
            archive = archive_raw_after_extraction(row, delete_local=delete_local)
            results["archived"] += 1
            terminal_theme.print_pip("success", f"archived to {archive['uri']}")
        except FileNotFoundError as exc:
            db.mark_raw_archive_failed(row["id"], exc)
            results["skipped"] += 1
            terminal_theme.print_pip("skipped", f"local raw missing: {exc}")
        except Exception as exc:
            db.mark_raw_archive_failed(row["id"], exc)
            results["failed"] += 1
            terminal_theme.print_pip("failed", f"raw archive failed: {exc}")
    return results
