import requests
from bs4 import BeautifulSoup
import re
import urllib.parse
import time
import random

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

def search_annas_archive(query, mirror="https://annas-archive.li"):
    """
    Searches Anna's Archive mirror for a query.
    Returns detail page URLs found on the search result page.
    """
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
