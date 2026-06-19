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

def search_archive_org(query):
    """
    Searches Archive.org using their Advanced Search API.
    Returns a list of matching items with their metadata.
    """
    url = "https://archive.org/advancedsearch.php"
    params = {
        "q": f"{query} AND mediatype:texts",
        "fl[]": "identifier,title,creator",
        "rows": 10,
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

def _search_annas_archive_mirror(query, mirror):
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
                
    return results[:10]


def search_annas_archive(query, mirrors=None):
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
            mirror_rows = _search_annas_archive_mirror(query, mirror)
        except Exception:
            continue
        for row in mirror_rows:
            if row["url"] in seen:
                continue
            seen.add(row["url"])
            rows.append(row)
        if len(rows) >= 10:
            break
    return rows[:10]


def search_substack(query):
    """
    Searches Substack and returns post URLs.
    Substack markup changes frequently, so this intentionally extracts only
    stable public post URLs and lets the downstream HTML processor handle text.
    If the query contains a Substack publication URL, the publication RSS feed
    is used because it is stable and does not depend on Substack's JS app.
    """
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
    if not html:
        return []
    return parse_substack_search(html, search_url, query=query)


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
        rows.append({
            "title": post.get("title") or post.get("subtitle") or url,
            "author": ((post.get("publishedBylines") or [{}])[0].get("name") if isinstance(post.get("publishedBylines"), list) else None),
            "site": parsed.netloc,
            "format": "HTML",
            "url": url,
            "file_size": "Unknown",
            "download_source": "Substack HTML",
            "download_url": url,
        })
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
        rows.append({
            "title": title,
            "author": author or None,
            "site": site,
            "format": "HTML",
            "url": link,
            "file_size": "Unknown",
            "download_source": "Substack RSS HTML",
            "download_url": link,
        })
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
            host.endswith(".substack.com") and "/p/" in path
        ) or (
            host == "substack.com" and re.search(r"/@[^/]+/p/", path)
        )
        if not is_post or full_url in seen:
            continue

        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        if terms and text and not any(term in text.lower() or term in full_url.lower() for term in terms):
            continue

        seen.add(full_url)
        results.append({
            "title": text or full_url,
            "author": None,
            "site": host,
            "format": "HTML",
            "url": full_url,
            "file_size": "Unknown",
            "download_source": "Substack HTML",
            "download_url": full_url,
        })
        if len(results) >= 10:
            break

    return results


def _candidate_search_urls(mirror, query):
    base = mirror["url"].rstrip("/")
    encoded = urllib.parse.quote(query)
    if mirror["group"] == "annas_archive":
        return [f"{base}/search?q={encoded}"]
    if mirror["group"] == "libgen_plus":
        return [
            f"{base}/index.php?req={encoded}",
            f"{base}/search.php?req={encoded}",
        ]
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


def _extract_detail_links(html, base_url, query, mirror):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    terms = [term.lower() for term in re.findall(r"[A-Za-z0-9]{4,}", query)[:5]]
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        full_url = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(full_url)
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
        if full_url not in [row["url"] for row in results]:
            results.append({
                "title": text or mirror["name"],
                "url": full_url,
                "site": parsed.netloc,
                "source_name": mirror["name"],
                "trust_level": "untrusted",
            })
        if len(results) >= 5:
            break
    return results


def search_slum_archives(query, mirrors=None):
    """
    Searches the less-trusted mirrors listed by open-slum.org.
    Each mirror is isolated: outage, timeout, or unexpected markup returns no
    results for that mirror without failing the whole source.
    """
    mirrors = mirrors or SLUM_ARCHIVE_MIRRORS
    all_results = []
    for mirror in mirrors:
        for search_url in _candidate_search_urls(mirror, query):
            html = fetch_url(search_url, retries=1, delay=0.2)
            if not html:
                continue
            results = _extract_detail_links(html, search_url, query, mirror)
            if results:
                all_results.extend(results)
                break
    return all_results


def search_libgen(query, mirrors=None):
    """Search only the LibGen mirror subset from the SLUM mirror catalog."""
    mirrors = mirrors or [
        mirror for mirror in SLUM_ARCHIVE_MIRRORS
        if mirror.get("group") == "libgen_plus"
    ]
    return search_slum_archives(query, mirrors=mirrors)
