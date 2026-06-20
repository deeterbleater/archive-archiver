import requests
from bs4 import BeautifulSoup
import re
import urllib.parse
import time
import random
import xml.etree.ElementTree as ET

import file_selection


SLUM_ARCHIVE_MIRRORS = [
    {"name": "Anna's Archive GL", "group": "annas_archive", "url": "https://annas-archive.gl/"},
    {"name": "Anna's Archive VG", "group": "annas_archive", "url": "https://annas-archive.vg/"},
    {"name": "Anna's Archive PK", "group": "annas_archive", "url": "https://annas-archive.pk/"},
    {"name": "Anna's Archive GD", "group": "annas_archive", "url": "https://annas-archive.gd/"},
    {"name": "Libgen+ BZ", "group": "libgen_plus", "url": "https://libgen.bz/"},
    {"name": "Libgen+ LA", "group": "libgen_plus", "url": "https://libgen.la/"},
    {"name": "Libgen+ GL", "group": "libgen_plus", "url": "https://libgen.gl/"},
    {"name": "Libgen+ VG", "group": "libgen_plus", "url": "https://libgen.vg/"},
    {"name": "Z-Library SK", "group": "zlibrary", "url": "https://z-library.sk/"},
    {"name": "1lib SK", "group": "zlibrary", "url": "https://1lib.sk/"},
    {"name": "z-lib GL", "group": "zlibrary", "url": "https://z-lib.gl/"},
    {"name": "go-to-library.sk", "group": "zlibrary_info", "url": "https://go-to-library.sk/"},
    {"name": "library-access.sk", "group": "zlibrary_info", "url": "https://library-access.sk/"},
    {"name": "Liber3", "group": "other", "url": "https://liber3.eth.limo/"},
    {"name": "Memory of the World", "group": "other", "url": "https://library.memoryoftheworld.org/"},
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0"
]

ANNA_DOWNLOAD_PATH_RE = re.compile(r"^/(?:fast|slow)_download/[0-9a-f]{32}/", re.IGNORECASE)
ANNA_STUB_PATH_RE = re.compile(
    r"^/(?:md5|view|search|datasets|torrents|member_codes|fast_download_not_member)(?:/|$)",
    re.IGNORECASE,
)
LIBGEN_MIRROR_PRIORITY = {
    "libgen get": 0,
    "ipfs cloudflare": 1,
    "ipfs.io": 2,
    "pinata ipfs": 3,
    "tor": 4,
    "libgen.is 1000 torrent": 9,
    "pilimi torrent": 10,
}
EXTRACTABLE_LINK_FORMATS = {
    ".txt": "Text",
    ".text": "Text",
    ".muse": "Muse",
    ".md": "Text",
    ".html": "HTML",
    ".htm": "HTML",
    ".xml": "HTML",
    ".pdf": "PDF",
    ".epub": "EPUB",
    ".fb2": "FB2",
    ".mobi": "MOBI",
    ".azw3": "AZW3",
    ".djvu": "DJVU",
    ".gz": "Text",
}

def get_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1"
    }

def fetch_url(url, retries=3, delay=2):
    """Fetches HTML from a URL with retry logic and polite delays."""
    for attempt in range(retries):
        try:
            # Polite delay to avoid hammering sites
            time.sleep(delay + random.uniform(0.5, 1.5))
            response = requests.get(url, headers=get_headers(), timeout=15)
            if response.status_code == 200:
                return response.text
            elif response.status_code in (403, 503):
                # Cloudflare or rate limits
                print(f"[!] Warning: Got status code {response.status_code} for {url}. Attempt {attempt + 1} of {retries}.")
            else:
                print(f"[!] Warning: HTTP {response.status_code} for {url}")
        except Exception as e:
            print(f"[!] Request error for {url}: {e}")
            
    return None

def clean_html(html_content):
    """
    Cleans raw HTML by removing scripts, styles, and extracting meaningful text and links.
    This reduces token count dramatically for LLM analysis.
    """
    if not html_content:
        return ""
        
    soup = BeautifulSoup(html_content, "html.parser")
    
    # Remove script and style elements
    for script_or_style in soup(["script", "style", "meta", "noscript", "header", "footer"]):
        script_or_style.decompose()
        
    # Extract links and their text
    links_info = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        if text and href:
            links_info.append(f"Link: {text} -> {href}")
            
    # Get clean text
    text_content = soup.get_text(separator="\n", strip=True)
    
    # Combine links and text content
    cleaned = "=== PAGE TEXT ===\n"
    cleaned += "\n".join([line for line in text_content.splitlines() if line.strip()][:500]) # Limit length
    cleaned += "\n\n=== EXTRACTED LINKS ===\n"
    cleaned += "\n".join(links_info[:200]) # Limit to first 200 links
    
    return cleaned

def search_archive_org(query, max_results=10):
    """
    Searches Archive.org using their Advanced Search API.
    Returns a list of matching items with their metadata.
    """
    url = "https://archive.org/advancedsearch.php"
    params = {
        "q": f"{query} AND mediatype:texts",
        "fl[]": "identifier,title,creator",
        "rows": max_results,
        "page": 1,
        "output": "json"
    }
    try:
        response = requests.get(url, params=params, headers=get_headers(), timeout=10)
        if response.status_code == 200:
            data = response.json()
            docs = data.get("response", {}).get("docs", [])
            return docs
    except Exception as e:
        print(f"[!] Error searching Archive.org: {e}")
    return []

def get_archive_org_files(identifier):
    """
    Retrieves the files and formats for a specific Archive.org identifier.
    Uses the official metadata API.
    """
    url = f"https://archive.org/metadata/{identifier}"
    try:
        response = requests.get(url, headers=get_headers(), timeout=10)
        if response.status_code == 200:
            data = response.json()
            title = data.get("metadata", {}).get("title", identifier)
            creator = data.get("metadata", {}).get("creator", "Unknown")
            files = data.get("files", [])
            
            # Map files to versions/formats
            results = []
            for file in files:
                # We only want text/ebook files (pdf, epub, mobi, etc.)
                name = file.get("name")
                fmt = file.get("format")
                size = file.get("size")
                
                # Filter out system and structural files
                if not name or not fmt:
                    continue
                    
                # Standard formats we care about
                fmt_lower = fmt.lower()
                is_valid = any(ext in fmt_lower for ext in ["pdf", "epub", "mobi", "kindle", "text", "daisy", "torrent"])
                if is_valid:
                    download_url = f"https://archive.org/download/{identifier}/{name}"
                    detail_url = f"https://archive.org/details/{identifier}"
                    
                    # Convert size to readable format
                    size_str = "Unknown"
                    if size:
                        try:
                            size_bytes = int(size)
                            if size_bytes > 1024 * 1024:
                                size_str = f"{size_bytes / (1024 * 1024):.2f} MB"
                            else:
                                size_str = f"{size_bytes / 1024:.2f} KB"
                        except ValueError:
                            size_str = str(size)
                            
                    results.append({
                        "site": "archive.org",
                        "title": title,
                        "author": creator,
                        "format": fmt,
                        "url": detail_url,
                        "file_size": size_str,
                        "download_source": "Archive.org HTTP",
                        "download_url": download_url
                    })
            return results
    except Exception as e:
        print(f"[!] Error fetching Archive.org metadata for {identifier}: {e}")
    return []


def select_best_file(rows):
    return file_selection.select_best_file(rows)


def is_annas_archive_url(value):
    parsed = urllib.parse.urlparse(str(value or ""))
    host = parsed.netloc or str(value or "")
    return "annas-archive." in host


def is_annas_archive_stub_url(value):
    parsed = urllib.parse.urlparse(str(value or ""))
    if not is_annas_archive_url(value):
        return False
    return bool(ANNA_STUB_PATH_RE.match(parsed.path or ""))


def filter_annas_download_files(files):
    """Keeps concrete Anna download links and drops detail/member/navigation pages."""
    filtered = []
    for item in files or []:
        download_url = str(item.get("download_url") or item.get("url") or "")
        parsed = urllib.parse.urlparse(download_url)
        if is_annas_archive_url(download_url):
            if not ANNA_DOWNLOAD_PATH_RE.match(parsed.path or ""):
                continue
            if parsed.query:
                clean_url = urllib.parse.urlunparse(parsed._replace(query="", fragment=""))
                item = {**item, "download_url": clean_url}
        filtered.append(item)
    slow = [
        item for item in filtered
        if "/slow_download/" in str(item.get("download_url") or item.get("url") or "")
        and "waitlist" not in str(item.get("download_source") or "").lower()
    ]
    if slow:
        external = [
            item for item in filtered
            if not is_annas_archive_url(str(item.get("download_url") or item.get("url") or ""))
        ]
        return slow + external
    return filtered


def _annas_partner_rank(label, path):
    label = str(label or "").lower()
    if "waitlist" in label:
        return 100
    if "/slow_download/" not in path:
        return 50
    server_match = re.search(r"partner server\s*#?\s*(\d+)", label, re.IGNORECASE)
    if server_match and server_match.group(1) == "5":
        return 0
    if server_match:
        return 10 + abs(int(server_match.group(1)) - 5)
    return 20


def _meta_content(soup, *names):
    wanted = {name.lower() for name in names}
    for meta in soup.find_all("meta"):
        key = (meta.get("property") or meta.get("name") or "").lower()
        if key in wanted and meta.get("content"):
            return re.sub(r"\s+", " ", meta["content"]).strip()
    return None


def _document_title(soup, fallback=None):
    title = _meta_content(soup, "og:title", "twitter:title")
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(" ", strip=True)
    if not title and soup.title and soup.title.string:
        title = soup.title.string
    title = re.sub(r"\s+", " ", title or "").strip()
    title = re.sub(r"\s+[|—-]\s+The Anarchist Library$", "", title, flags=re.IGNORECASE)
    return title or fallback or "Untitled work"


def _document_author(soup):
    author = _meta_content(soup, "author", "article:author", "book:author")
    if author:
        return author
    for selector in (".author", ".authors", "[rel=author]", "h2"):
        node = soup.select_one(selector)
        if node:
            text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
            if text:
                return text[:240]
    return None


def _format_from_url(url):
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    path = parsed.path.lower()
    for suffix, fmt in sorted(EXTRACTABLE_LINK_FORMATS.items(), key=lambda item: -len(item[0])):
        if path.endswith(suffix):
            return fmt
    return None


def _extractable_link_rows(soup, detail_url, title, author, source_name, trust_level="untrusted", include_html=False):
    files = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        try:
            full_url = urllib.parse.urljoin(detail_url, href)
            parsed = urllib.parse.urlparse(full_url)
        except ValueError:
            continue
        if parsed.scheme not in ("http", "https"):
            continue
        fmt = _format_from_url(full_url)
        if not fmt:
            continue
        if fmt == "HTML" and not include_html:
            continue
        clean_url = urllib.parse.urlunparse(parsed._replace(fragment=""))
        if clean_url in seen:
            continue
        seen.add(clean_url)
        label = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        files.append({
            "site": parsed.netloc,
            "title": title,
            "author": author,
            "format": fmt,
            "url": detail_url,
            "file_size": "Unknown",
            "download_source": label or source_name,
            "download_url": clean_url,
            "trust_level": trust_level,
        })
    return files


def _annas_detail_title(soup):
    if soup.title and soup.title.string:
        title = re.sub(r"\s*-\s*Anna(?:'|.)s Archive\s*$", "", soup.title.string)
        title = re.sub(r"\s+", " ", title).strip(" -")
        if title and not re.fullmatch(r"anna.?s archive", title.lower()):
            return title
    for div in soup.find_all("div"):
        text = re.sub(r"\s+", " ", div.get_text(" ", strip=True)).strip()
        if 8 <= len(text) <= 300 and "search" not in text.lower():
            return re.sub(r"[^\w\s:;,.!?()\\[\\]{}'\"-]+$", "", text).strip()
    return "Anna's Archive work"


def _annas_detail_author(soup):
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        first_line = re.sub(r"\s+", " ", meta["content"]).strip()
        if first_line:
            return first_line[:240]
    return None


def _annas_detail_format_size(soup):
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    match = re.search(
        r"(?:^|\s)·\s*([A-Za-z0-9]{2,8})\s*·\s*([0-9][0-9.,]*\s*(?:KB|MB|GB))\s*·",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).upper(), match.group(2).replace(" ", "")
    match = re.search(r"\b(PDF|EPUB|MOBI|AZW3|DJVU|TXT|RTF)\b", text, re.IGNORECASE)
    return (match.group(1).upper() if match else "Unknown"), "Unknown"


def parse_annas_detail_page(html, detail_url):
    """Extracts concrete Anna download candidates from an /md5 detail page."""
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    title = _annas_detail_title(soup)
    author = _annas_detail_author(soup)
    fmt, file_size = _annas_detail_format_size(soup)
    files = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full_url = urllib.parse.urljoin(detail_url, href)
        parsed = urllib.parse.urlparse(full_url)
        if not is_annas_archive_url(full_url):
            continue
        if not ANNA_DOWNLOAD_PATH_RE.match(parsed.path or ""):
            continue
        full_url = urllib.parse.urlunparse(parsed._replace(query="", fragment=""))
        if full_url in seen:
            continue
        seen.add(full_url)
        label = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip() or "Anna's Archive download"
        mirror_rank = _annas_partner_rank(label, parsed.path or "")
        files.append({
            "site": "annas-archive.org",
            "title": title,
            "author": author,
            "format": fmt,
            "url": detail_url,
            "file_size": file_size,
            "download_source": f"Anna's Archive {label}",
            "download_url": full_url,
            "mirror_rank": mirror_rank,
            "trust_level": "untrusted",
        })
    if not files:
        return None
    files = filter_annas_download_files(files)
    if not files:
        return None
    files.sort(key=lambda row: int(row.get("mirror_rank") or 0))
    return {"title": title, "author": author, "files": files}


def _libgen_label_value(soup, label):
    wanted = label.lower().rstrip(":")
    for strong in soup.find_all("strong"):
        text = re.sub(r"\s+", " ", strong.get_text(" ", strip=True)).strip()
        if text.lower().rstrip(":") != wanted:
            continue
        parent = strong.parent
        if not parent:
            continue
        value = re.sub(r"\s+", " ", parent.get_text(" ", strip=True)).strip()
        value = re.sub(rf"^{re.escape(text)}\s*", "", value, flags=re.IGNORECASE).strip()
        return value or None
    return None


def _libgen_title_from_document(soup):
    title = _libgen_label_value(soup, "Title")
    if title:
        return title
    if soup.title and soup.title.string:
        text = re.sub(r"\s+", " ", soup.title.string).strip()
        text = re.sub(r"^LG\+:\s*", "", text)
        text = re.sub(r"\{[0-9]+\}(?:\s+libgen\.[^. ]+\.[A-Za-z0-9]+)?$", "", text).strip()
        text = re.sub(r"\{[^{}]{1,240}\}\([^)]+\)$", "", text).strip()
        return text or None
    return None


def _libgen_author_from_document(soup):
    author = _libgen_label_value(soup, "Author(s)")
    if author:
        return author
    if soup.title and soup.title.string:
        match = re.search(r"\{([^{}]+)\}\([^)]+\)\{[0-9]+\}", soup.title.string)
        if match:
            return match.group(1).strip()
    return None


def _libgen_file_size_from_text(text):
    match = re.search(
        r"\bSize:\s*([0-9][0-9.,]*\s*(?:B|KB|MB|GB)(?:\s*\([^)]+\))?)",
        text,
        re.IGNORECASE,
    )
    if not match:
        match = re.search(r"\bFilesize:\s*([0-9][0-9.,]*\s*(?:B|KB|MB|GB)(?:\s*\([^)]+\))?)", text, re.IGNORECASE)
    return match.group(1).strip() if match else "Unknown"


def _libgen_format_from_text(text, url=None):
    match = re.search(r"\b(?:Ext\.|Extension):\s*([A-Za-z0-9]{2,8})", text, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    path = urllib.parse.urlparse(str(url or "")).path
    suffix = path.rsplit(".", 1)[-1] if "." in path else ""
    return suffix.upper() if suffix else "Unknown"


def _libgen_link_priority(link):
    title = str(link.get("title") or "").lower()
    href = str(link.get("href") or "").lower()
    parsed = urllib.parse.urlparse(href)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if path.endswith(".torrent") or "/torrents/" in path:
        return 10 if "pilimi" in href else 9
    if path.endswith("ads.php"):
        return 0
    if host.endswith(".onion"):
        return 4
    for key, rank in LIBGEN_MIRROR_PRIORITY.items():
        if key == "tor":
            continue
        if key in title or key in href:
            return rank
    return 99


def _libgen_usable_download_links(container, base_url):
    links = []
    seen = set()
    for a in container.find_all("a", href=True):
        href = a["href"]
        try:
            full_url = urllib.parse.urljoin(base_url, href)
            parsed = urllib.parse.urlparse(full_url)
        except ValueError:
            continue
        if parsed.scheme not in ("http", "https"):
            continue
        host = (parsed.hostname or "").lower()
        path = parsed.path.lower()
        title = str(a.get("title") or "").lower()
        if "annas-archive." in host or "anna's archive" in title:
            continue
        if host in ("localhost", "127.0.0.1"):
            continue
        if path.endswith("/edition.php") or path.endswith("/file.php"):
            continue
        if "/covers/" in path:
            continue
        if path.endswith("/ads.php"):
            source = "LibGen GET"
        elif "torrent" in title or path.endswith(".torrent") or "/torrents/" in path:
            source = str(a.get("title") or a.get_text(" ", strip=True) or "LibGen torrent").strip()
        elif "ipfs" in host or "/ipfs/" in path:
            source = str(a.get("title") or a.get_text(" ", strip=True) or "LibGen IPFS").strip()
        elif host.endswith(".onion"):
            source = "LibGen Tor"
        else:
            continue
        if full_url in seen:
            continue
        seen.add(full_url)
        links.append((full_url, source, _libgen_link_priority(a)))
    return sorted(links, key=lambda item: item[2])


def _libgen_file_rows_from_edition(soup, detail_url, title, author):
    files = []
    table = soup.find("table", id="tablelibgen")
    if not table:
        return files
    for tr in table.find_all("tr"):
        row_text = re.sub(r"\s+", " ", tr.get_text(" ", strip=True)).strip()
        if "Size:" not in row_text and "Extension:" not in row_text:
            continue
        fmt = _libgen_format_from_text(row_text)
        size = _libgen_file_size_from_text(row_text)
        for download_url, source, _priority in _libgen_usable_download_links(tr, detail_url):
            files.append({
                "site": urllib.parse.urlparse(download_url).netloc or "libgen",
                "title": title,
                "author": author,
                "format": fmt,
                "url": detail_url,
                "file_size": size,
                "download_source": source,
                "download_url": download_url,
                "mirror_rank": _priority,
                "trust_level": "untrusted",
            })
    return files


def _libgen_file_rows_from_file_page(soup, detail_url, title, author):
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
    fmt = _libgen_format_from_text(text, detail_url)
    size = _libgen_file_size_from_text(text)
    files = []
    for download_url, source, _priority in _libgen_usable_download_links(soup, detail_url):
        files.append({
            "site": urllib.parse.urlparse(download_url).netloc or "libgen",
            "title": title,
            "author": author,
            "format": fmt,
            "url": detail_url,
            "file_size": size,
            "download_source": source,
            "download_url": download_url,
            "mirror_rank": _priority,
            "trust_level": "untrusted",
        })
    return files


def parse_libgen_page(html, detail_url):
    """Extract title, author, and concrete LibGen file mirrors without LLM use."""
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    title = _libgen_title_from_document(soup)
    if not title:
        return None
    author = _libgen_author_from_document(soup)
    parsed = urllib.parse.urlparse(detail_url)
    if parsed.path.endswith("/edition.php"):
        files = _libgen_file_rows_from_edition(soup, detail_url, title, author)
    else:
        files = _libgen_file_rows_from_file_page(soup, detail_url, title, author)
    if not files:
        return None
    return {"title": title, "author": author, "files": files}


def _anarchist_library_export_url(detail_url, suffix):
    parsed = urllib.parse.urlparse(detail_url)
    path = parsed.path.rstrip("/")
    if not path.startswith("/library/"):
        return None
    if "." in path.rsplit("/", 1)[-1]:
        path = path.rsplit(".", 1)[0]
    return urllib.parse.urlunparse(parsed._replace(path=f"{path}{suffix}", query="", fragment=""))


def parse_anarchist_library_page(html, detail_url):
    """Extract The Anarchist Library work exports without LLM use."""
    if not html:
        return None
    try:
        parsed_url = urllib.parse.urlparse(detail_url)
    except ValueError:
        return None
    if "theanarchistlibrary.org" not in parsed_url.netloc.lower():
        return None

    soup = BeautifulSoup(html, "html.parser")
    title = _document_title(soup, fallback="Anarchist Library work")
    author = _document_author(soup)
    files = _extractable_link_rows(
        soup,
        detail_url,
        title,
        author,
        "The Anarchist Library export",
        trust_level="trusted",
        include_html=True,
    )

    current_format = _format_from_url(detail_url)
    if current_format:
        files.append({
            "site": parsed_url.netloc,
            "title": title,
            "author": author,
            "format": current_format,
            "url": detail_url,
            "file_size": "Unknown",
            "download_source": "The Anarchist Library direct file",
            "download_url": detail_url,
            "trust_level": "trusted",
        })
    else:
        files.append({
            "site": parsed_url.netloc,
            "title": title,
            "author": author,
            "format": "HTML",
            "url": detail_url,
            "file_size": "Unknown",
            "download_source": "The Anarchist Library HTML",
            "download_url": detail_url,
            "trust_level": "trusted",
        })

    seen = {row["download_url"] for row in files}
    for suffix, fmt in ((".muse", "Muse"), (".epub", "EPUB"), (".pdf", "PDF")):
        export_url = _anarchist_library_export_url(detail_url, suffix)
        if not export_url or export_url in seen:
            continue
        seen.add(export_url)
        files.append({
            "site": parsed_url.netloc,
            "title": title,
            "author": author,
            "format": fmt,
            "url": detail_url,
            "file_size": "Unknown",
            "download_source": f"The Anarchist Library {fmt}",
            "download_url": export_url,
            "trust_level": "trusted",
        })

    return {"title": title, "author": author, "files": files}


def parse_generic_download_page(html, detail_url, source_name="Download link", trust_level="untrusted"):
    """Extract obvious direct ebook/text links from a page without inferring page semantics."""
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    title = _document_title(soup, fallback=detail_url)
    author = _document_author(soup)
    files = _extractable_link_rows(soup, detail_url, title, author, source_name, trust_level=trust_level)
    if not files:
        return None
    return {"title": title, "author": author, "files": files}


def parse_known_archive_page(html, detail_url, source_name=None, trust_level="untrusted"):
    """Route known archive detail pages through deterministic parsers before LLM fallback."""
    try:
        parsed = urllib.parse.urlparse(detail_url)
    except ValueError:
        return None
    host = parsed.netloc.lower()
    path = parsed.path.lower()

    if is_annas_archive_url(detail_url):
        return parse_annas_detail_page(html, detail_url)
    if "libgen." in host or path.endswith("/edition.php") or path.endswith("/file.php"):
        return parse_libgen_page(html, detail_url)
    if host.endswith("theanarchistlibrary.org"):
        return parse_anarchist_library_page(html, detail_url)
    return parse_generic_download_page(
        html,
        detail_url,
        source_name=source_name or "Direct download link",
        trust_level=trust_level,
    )


def search_arxiv(query, max_results=10):
    """
    Searches arXiv using its public Atom API and returns paper metadata with PDF links.
    """
    url = "https://export.arxiv.org/api/query"
    params = {
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": max_results,
        "sortBy": "relevance",
        "sortOrder": "descending",
    }
    try:
        response = requests.get(url, params=params, headers=get_headers(), timeout=20)
        response.raise_for_status()
    except Exception as exc:
        print(f"[!] Error searching arXiv: {exc}")
        return []

    return parse_arxiv_feed(response.text)


def parse_arxiv_feed(xml_text):
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        print(f"[!] Error parsing arXiv feed: {exc}")
        return []

    results = []
    for entry in root.findall("atom:entry", ns):
        entry_id = (entry.findtext("atom:id", default="", namespaces=ns) or "").strip()
        title = re.sub(r"\s+", " ", entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
        summary = re.sub(r"\s+", " ", entry.findtext("atom:summary", default="", namespaces=ns) or "").strip()
        authors = [
            re.sub(r"\s+", " ", author.findtext("atom:name", default="", namespaces=ns) or "").strip()
            for author in entry.findall("atom:author", ns)
        ]
        authors = [author for author in authors if author]
        pdf_url = None
        detail_url = entry_id
        for link in entry.findall("atom:link", ns):
            href = link.attrib.get("href")
            if not href:
                continue
            if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                pdf_url = href
            elif link.attrib.get("rel") == "alternate":
                detail_url = href
        if not pdf_url and "/abs/" in entry_id:
            pdf_url = entry_id.replace("/abs/", "/pdf/")
        if not title or not pdf_url:
            continue
        results.append({
            "title": title,
            "author": ", ".join(authors) if authors else "Unknown",
            "summary": summary,
            "site": "arxiv.org",
            "format": "PDF",
            "url": detail_url,
            "file_size": "Unknown",
            "download_source": "arXiv PDF",
            "download_url": pdf_url,
        })
    return results

def search_anarchist_library(query):
    """
    Searches The Anarchist Library and returns a list of title/url dicts.
    """
    search_url = f"https://theanarchistlibrary.org/search?query={urllib.parse.quote(query)}"
    html = fetch_url(search_url)
    if not html:
        return []
        
    soup = BeautifulSoup(html, "html.parser")
    results = []
    
    # In Anarchist Library, search results are links within list tags
    # typically matching the pattern href="/library/..."
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/library/" in href and not href.endswith("/library/"):
            # Ensure it is an absolute URL
            full_url = urllib.parse.urljoin("https://theanarchistlibrary.org", href)
            # Avoid duplicate URLs
            if full_url not in [r["url"] for r in results]:
                title = a.get_text(strip=True)
                results.append({
                    "title": title,
                    "url": full_url
                })
                
    return results[:10] # limit to top 10

def _search_annas_archive_mirror(query, mirror, limit=10):
    """
    Searches one Anna's Archive mirror for a query.
    Returns detail page URLs found on the search result page.
    """
    mirror = mirror.rstrip("/")
    search_url = f"{mirror}/search?q={urllib.parse.quote(query)}"
    html = fetch_url(search_url)
    if not html:
        return []
        
    soup = BeautifulSoup(html, "html.parser")
    results = []
    
    # Anna's Archive links usually point to "/md5/..." or contains MD5 hashes
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/md5/" in href:
            full_url = urllib.parse.urljoin(mirror, href)
            if full_url not in [r["url"] for r in results]:
                # Try to extract title
                title_div = a.find("h3")
                title = title_div.get_text(strip=True) if title_div else a.get_text(strip=True)
                # Cleanup title text (often has formatting or icons)
                title = re.sub(r'\s+', ' ', title).strip()
                results.append({
                    "title": title,
                    "url": full_url
                })
                
    return results[:limit]


def search_annas_archive(query, mirrors=None, limit=10):
    """
    Searches Anna's Archive mirrors for a query. Mirrors are isolated so a down
    host does not fail the whole source.
    """
    if mirrors is None:
        mirrors = [
            mirror["url"] for mirror in SLUM_ARCHIVE_MIRRORS
            if mirror.get("group") == "annas_archive"
        ]
        mirrors = list(mirrors) + ["https://annas-archive.li"]
    else:
        mirrors = list(mirrors)
    rows = []
    seen = set()
    for mirror in mirrors:
        try:
            mirror_rows = _search_annas_archive_mirror(query, mirror, limit=limit)
        except Exception:
            continue
        for row in mirror_rows:
            if row["url"] in seen:
                continue
            seen.add(row["url"])
            rows.append(row)
        if len(rows) >= limit:
            break
    return rows[:limit]


def search_substack(query):
    """
    Searches Substack and returns post URLs.
    Substack markup changes frequently, so this intentionally extracts only
    stable public post URLs and lets the downstream HTML processor handle text.
    If the query contains a Substack publication URL, the publication RSS feed
    is used because it is stable and does not depend on Substack's JS app.
    """
    direct_url = _substack_direct_item_url(query)
    if direct_url:
        return [substack_row_from_url(direct_url)]

    publication_url = _substack_publication_url(query)
    if publication_url:
        return search_substack_publication(publication_url)

    api_url = "https://substack.com/api/v1/post/search"
    try:
        response = requests.get(
            api_url,
            params={"query": query, "limit": 10},
            headers={**get_headers(), "Accept": "application/json"},
            timeout=15,
        )
        if response.status_code == 200:
            rows = parse_substack_json(response.json(), query=query)
            if rows:
                return rows
    except Exception as exc:
        print(f"[!] Error searching Substack JSON endpoint: {exc}")

    search_url = f"https://substack.com/search/{urllib.parse.quote(query)}"
    html = fetch_url(search_url, retries=2, delay=0.5)
    if html:
        rows = parse_substack_search(html, search_url, query=query)
        if rows:
            return rows

    return search_substack_web(query)


def _decode_duckduckgo_href(href):
    parsed = urllib.parse.urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com"):
        query = urllib.parse.parse_qs(parsed.query)
        return (query.get("uddg") or [href])[0]
    return href


def _substack_web_queries(query):
    query = str(query or "").strip()
    terms = [f"site:substack.com {query}"]
    handle = query.lstrip("@")
    if re.fullmatch(r"[A-Za-z0-9_-]{2,40}", handle):
        terms.insert(0, f"site:substack.com/@{handle} {handle}")
    return _dedupe_preserve_order(terms)


def _dedupe_preserve_order(items):
    seen = set()
    result = []
    for item in items:
        key = str(item).casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def search_substack_web(query, limit=10):
    """Fallback to public SERP links when Substack's own search APIs return empty."""
    rows = []
    seen = set()
    for search_query in _substack_web_queries(query):
        rows.extend(_search_substack_bing_rss(search_query, seen=seen, limit=limit - len(rows)))
        if len(rows) >= limit:
            return rows[:limit]

        try:
            response = requests.post(
                "https://html.duckduckgo.com/html/",
                data={"q": search_query},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
            response.raise_for_status()
        except Exception as exc:
            print(f"[!] Error searching Substack via web fallback: {exc}")
            continue

        soup = BeautifulSoup(response.text, "html.parser")
        for a in soup.select("a.result__a[href]"):
            href = _decode_duckduckgo_href(a.get("href") or "")
            try:
                parsed = urllib.parse.urlparse(href)
            except ValueError:
                continue
            host = parsed.netloc.lower()
            path = parsed.path
            is_item = (
                host.endswith(".substack.com") and ("/p/" in path or "/note/" in path)
            ) or (
                host == "substack.com" and re.search(r"/@[^/]+/(?:p|note)/", path)
            )
            if not is_item:
                continue
            clean_url = urllib.parse.urlunparse(parsed._replace(fragment=""))
            if clean_url in seen:
                continue
            seen.add(clean_url)
            title = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip() or clean_url
            rows.append(substack_row_from_url(clean_url, title=title))
            if len(rows) >= limit:
                return rows
    return rows


def _search_substack_bing_rss(search_query, seen=None, limit=10):
    seen = seen if seen is not None else set()
    try:
        response = requests.get(
            "https://www.bing.com/search",
            params={"format": "rss", "q": search_query},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        response.raise_for_status()
        root = ET.fromstring(response.text)
    except Exception as exc:
        print(f"[!] Error searching Substack via Bing RSS fallback: {exc}")
        return []

    rows = []
    channel = root.find("channel")
    if channel is None:
        return rows
    for item in channel.findall("item"):
        link = (item.findtext("link", default="") or "").strip()
        title = re.sub(r"\s+", " ", item.findtext("title", default="")).strip()
        try:
            parsed = urllib.parse.urlparse(link)
        except ValueError:
            continue
        host = parsed.netloc.lower()
        path = parsed.path
        is_item = (
            host.endswith(".substack.com") and ("/p/" in path or "/note/" in path)
        ) or (
            host == "substack.com" and re.search(r"/@[^/]+/(?:p|note)/", path)
        )
        if not is_item:
            continue
        clean_url = urllib.parse.urlunparse(parsed._replace(fragment=""))
        if clean_url in seen:
            continue
        seen.add(clean_url)
        rows.append(substack_row_from_url(clean_url, title=title or clean_url))
        if len(rows) >= limit:
            break
    return rows


def _substack_direct_item_url(query):
    query = str(query or "").strip()
    match = re.search(r"https?://[A-Za-z0-9.-]*substack\.com/[^\s]+", query)
    if not match:
        return None
    parsed = urllib.parse.urlparse(match.group(0))
    host = parsed.netloc.lower()
    path = parsed.path
    is_item = (
        host.endswith(".substack.com") and ("/p/" in path or "/note/" in path)
    ) or (
        host == "substack.com" and re.search(r"/@[^/]+/(?:p|note)/", path)
    )
    if not is_item:
        return None
    return urllib.parse.urlunparse(parsed._replace(fragment=""))


def _substack_publication_url(query):
    query = str(query or "").strip()
    if query.startswith("substack:"):
        query = query.split(":", 1)[1].strip()
    match = re.search(r"https?://[A-Za-z0-9.-]*substack\.com(?:/[^\s]*)?", query)
    if not match:
        return None
    parsed = urllib.parse.urlparse(match.group(0))
    if not parsed.netloc.lower().endswith("substack.com"):
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def substack_row_from_url(url, title=None, author=None):
    parsed = urllib.parse.urlparse(url)
    title = title or url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ") or url
    return {
        "title": title,
        "author": author,
        "site": parsed.netloc.lower(),
        "format": "HTML",
        "url": url,
        "file_size": "Unknown",
        "download_source": "Substack HTML",
        "download_url": url,
    }


def parse_substack_json(payload, query=None):
    rows = []
    seen = set()
    candidates = []
    for key in ("focused", "results", "resultsWithTrackingParams"):
        value = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(value, list):
            candidates.extend(value)

    for item in candidates:
        post = item.get("post") if isinstance(item, dict) else None
        post = post or item
        if not isinstance(post, dict):
            continue
        url = post.get("canonical_url") or post.get("url") or post.get("web_url")
        if not url:
            publication = post.get("publication") or {}
            subdomain = publication.get("subdomain")
            slug = post.get("slug")
            if subdomain and slug:
                url = f"https://{subdomain}.substack.com/p/{slug}"
        if not url or url in seen:
            continue
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or "substack.com" not in parsed.netloc:
            continue
        seen.add(url)
        rows.append(substack_row_from_url(
            url,
            title=post.get("title") or post.get("subtitle") or url,
            author=((post.get("publishedBylines") or [{}])[0].get("name") if isinstance(post.get("publishedBylines"), list) else None),
        ))
    return rows


def search_substack_publication(publication_url):
    feed_url = urllib.parse.urljoin(publication_url.rstrip("/") + "/", "feed")
    try:
        response = requests.get(feed_url, headers=get_headers(), timeout=15)
        response.raise_for_status()
    except Exception as exc:
        print(f"[!] Error fetching Substack feed {feed_url}: {exc}")
        return []
    return parse_substack_feed(response.text, publication_url)


def parse_substack_feed(xml_text, publication_url):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        print(f"[!] Error parsing Substack feed: {exc}")
        return []

    channel = root.find("channel")
    if channel is None:
        return []
    site = urllib.parse.urlparse(publication_url).netloc
    rows = []
    for item in channel.findall("item"):
        title = re.sub(r"\s+", " ", item.findtext("title", default="")).strip()
        link = (item.findtext("link", default="") or "").strip()
        author = (item.findtext("{http://purl.org/dc/elements/1.1/}creator", default="") or "").strip()
        if not title or not link:
            continue
        row = substack_row_from_url(link, title=title, author=author or None)
        row["site"] = site
        row["download_source"] = "Substack RSS HTML"
        rows.append(row)
        if len(rows) >= 10:
            break
    return rows


def parse_substack_search(html, base_url="https://substack.com/search", query=None):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen = set()
    terms = [term.lower() for term in re.findall(r"[A-Za-z0-9]{4,}", query or "")[:5]]

    for a in soup.find_all("a"):
        href = a.get("href") or a.get("data-href")
        if not href:
            continue
        full_url = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(full_url)
        if parsed.scheme not in ("http", "https"):
            continue

        host = parsed.netloc.lower()
        path = parsed.path
        is_post = (
            host.endswith(".substack.com") and ("/p/" in path or "/note/" in path)
        ) or (
            host == "substack.com" and re.search(r"/@[^/]+/(?:p|note)/", path)
        )
        if not is_post or full_url in seen:
            continue

        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        if terms and text and not any(term in text.lower() or term in full_url.lower() for term in terms):
            continue

        seen.add(full_url)
        results.append(substack_row_from_url(full_url, title=text or full_url))
        if len(results) >= 10:
            break

    return results


def _candidate_search_urls(mirror, query):
    base = mirror["url"].rstrip("/")
    encoded = urllib.parse.quote(query)
    if mirror["group"] == "annas_archive":
        return [f"{base}/search?q={encoded}"]
    if mirror["group"] == "libgen_plus":
        return [f"{base}/index.php?req={encoded}"]
    if mirror["group"] == "zlibrary":
        return [
            f"{base}/s/{encoded}",
            f"{base}/search/{encoded}",
            f"{base}/?q={encoded}",
        ]
    return [
        f"{base}/?q={encoded}",
        f"{base}/search?q={encoded}",
    ]


def _detail_result_key(row, mirror=None):
    try:
        parsed = urllib.parse.urlparse(row.get("url") or "")
    except ValueError:
        return row.get("url")
    path = parsed.path.lower()
    query = urllib.parse.parse_qs(parsed.query)
    group = (mirror or {}).get("group")
    if group == "annas_archive" and "/md5/" in path:
        return f"anna:{path.rsplit('/md5/', 1)[-1].strip('/')}"
    if group == "libgen_plus" and path.endswith(("/edition.php", "/file.php")):
        ids = query.get("id")
        if ids:
            page = path.rsplit("/", 1)[-1]
            return f"libgen:{page}:{ids[0]}"
    return urllib.parse.urlunparse(parsed._replace(scheme="", netloc="", fragment=""))


def _extract_detail_links(html, base_url, query, mirror, limit=5):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen = set()
    terms = [term.lower() for term in re.findall(r"[A-Za-z0-9]{4,}", query)[:5]]
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        try:
            full_url = urllib.parse.urljoin(base_url, href)
            parsed = urllib.parse.urlparse(full_url)
        except ValueError:
            continue
        if parsed.scheme not in ("http", "https"):
            continue
        if mirror["group"] == "annas_archive" and "/md5/" not in parsed.path:
            continue
        if mirror["group"] == "libgen_plus":
            if not parsed.path.endswith("/edition.php") or not urllib.parse.parse_qs(parsed.query).get("id"):
                continue
            if not text or text.isdigit() or len(text) < 6:
                continue
        elif mirror["group"] != "annas_archive" and terms and not any(term in text.lower() or term in full_url.lower() for term in terms):
            continue
        row = {
            "title": text or mirror["name"],
            "url": full_url,
            "site": parsed.netloc,
            "source_name": mirror["name"],
            "trust_level": "untrusted",
        }
        key = _detail_result_key(row, mirror)
        if key in seen:
            continue
        seen.add(key)
        results.append(row)
        if len(results) >= limit:
            break
    return results


def search_slum_archives(query, mirrors=None, limit=10):
    """
    Searches the less-trusted mirrors listed by open-slum.org.
    Each mirror is isolated: outage, timeout, or unexpected markup returns no
    results for that mirror without failing the whole source.
    """
    mirrors = mirrors or SLUM_ARCHIVE_MIRRORS
    all_results = []
    seen = set()
    for mirror in mirrors:
        for search_url in _candidate_search_urls(mirror, query):
            html = fetch_url(search_url, retries=1, delay=0.2)
            if not html:
                continue
            results = _extract_detail_links(html, search_url, query, mirror, limit=limit)
            if results:
                for row in results:
                    key = _detail_result_key(row, mirror)
                    if key in seen:
                        continue
                    seen.add(key)
                    all_results.append(row)
                    if len(all_results) >= limit:
                        return all_results
                break
    return all_results


def search_libgen(query, mirrors=None, limit=10):
    """Search only the LibGen mirror subset from the SLUM mirror catalog."""
    mirrors = mirrors or [
        mirror for mirror in SLUM_ARCHIVE_MIRRORS
        if mirror.get("group") == "libgen_plus"
    ]
    return search_slum_archives(query, mirrors=mirrors, limit=limit)
