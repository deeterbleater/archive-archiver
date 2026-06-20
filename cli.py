import argparse
import contextlib
import io
import sys
import os
import random
import time
import threading
import traceback
import urllib.parse
from dotenv import load_dotenv

# Ensure all print statements flush immediately (important for background logs)
import builtins
_THREAD_OUTPUT = threading.local()


class _ThreadTeeCapture(io.StringIO):
    def __init__(self, target=None):
        super().__init__()
        self.target = target

    def write(self, text):
        if self.target:
            self.target.write(text)
            self.target.flush()
        return super().write(text)

    def flush(self):
        if self.target:
            self.target.flush()
        return super().flush()


@contextlib.contextmanager
def capture_output(target=None):
    previous = getattr(_THREAD_OUTPUT, "stream", None)
    stream = _ThreadTeeCapture(target)
    _THREAD_OUTPUT.stream = stream
    try:
        yield stream
    finally:
        if previous is None:
            try:
                delattr(_THREAD_OUTPUT, "stream")
            except AttributeError:
                pass
        else:
            _THREAD_OUTPUT.stream = previous


def print(*args, **kwargs):
    kwargs.setdefault('flush', True)
    stream = getattr(_THREAD_OUTPUT, "stream", None)
    if stream is not None and "file" not in kwargs:
        kwargs["file"] = stream
    builtins.print(*args, **kwargs)

# Load dotenv before module-level CLI defaults read environment variables.
load_dotenv()

import db
import archive_plugins
import scrapers
import llm
import downloader
import processor
import corpus
import text_munger
import agent
import dashboard
import rss_ingest
import terminal_theme
import text_validator

PUBLIC_COLLECTOR_QUERIES = [
    "public domain philosophy",
    "public domain history",
    "public domain political economy",
    "public domain literature",
    "classical anarchism",
    "19th century philosophy",
    "enlightenment philosophy",
    "labor history public domain",
    "ancient history public domain",
    "ethics public domain",
]

AUTO_COLLECTION_QUERIES = [
    "public domain collected works",
    "public domain complete works",
    "public domain essays",
    "public domain letters",
    "public domain lectures",
    "public domain social theory",
    "public domain political theory",
    "public domain science fiction",
    "public domain philosophy primary texts",
    "public domain labor movement history",
    "public domain revolutionary history",
    "public domain literary criticism",
]

AUTO_CATEGORY_QUERIES = {
    "anarchism": [
        "classical anarchism primary texts",
        "anarchist pamphlets public domain",
        "libertarian socialist public domain",
    ],
    "philosophy": [
        "ethics public domain philosophy",
        "metaphysics public domain",
        "epistemology public domain",
    ],
    "political_economy": [
        "political economy public domain",
        "labor capital property public domain",
        "economics public domain theory",
    ],
    "history": [
        "historical accounts public domain",
        "revolution history public domain",
        "19th century history public domain",
    ],
    "literature": [
        "public domain novels",
        "public domain poetry",
        "public domain drama",
    ],
}

AUTO_FOCUS_TOPICS = [
    "golden age science fiction",
    "weird fiction and cosmic horror",
    "utopian and dystopian fiction",
    "detective fiction and mystery",
    "travel writing and exploration",
    "natural history and biology",
    "mathematics and logic",
    "astronomy and spaceflight",
    "labor history and union organizing",
    "urban history and city planning",
    "folk tales and mythology",
    "religion and comparative mythology",
    "early computing and cybernetics",
    "memoirs and autobiographies",
    "political theory and statecraft",
    "feminist history and theory",
    "public health and medicine",
    "agriculture and rural life",
    "maritime history and sea stories",
    "language learning and linguistics",
    "poetry and verse drama",
    "children's literature",
    "economic history",
    "military history",
    "art history and aesthetics",
    "education and pedagogy",
    "psychology and psychiatry",
    "engineering and practical mechanics",
    "cookery and domestic manuals",
    "occultism and esotericism",
]

DEFAULT_PUBLIC_SOURCES = (
    "archive_org",
    "anarchist_library",
    "arxiv",
    "substack",
    "annas_archive",
    "libgen",
    "archive_plugins",
)
ALL_SOURCES = (
    "archive_org",
    "anarchist_library",
    "arxiv",
    "substack",
    "annas_archive",
    "libgen",
    "slum_archives",
    "archive_plugins",
)

BANNER_LINES = [
    "░░      ░░░  ░░░░░░░░░      ░░░        ░░░░░░░",
    "▒  ▒▒▒▒  ▒▒  ▒▒▒▒▒▒▒▒  ▒▒▒▒▒▒▒▒  ▒▒▒▒▒▒▒▒▒▒▒▒▒",
    "▓  ▓▓▓▓  ▓▓  ▓▓▓▓▓▓▓▓  ▓▓▓   ▓▓      ▓▓▓▓▓▓▓▓▓",
    "█        ██  ████████  ████  ██  █████████████",
    "█  ████  ██        ███      ███        ███████",
]

def print_banner():
    terminal_theme.print_logo(BANNER_LINES)


def _add_best_file(work_id, files, base_url, site, default_source, trust_level="trusted"):
    if db.work_has_archive_activity(work_id):
        print("      [=] Work already has archive activity; skipping duplicate download candidate.")
        return None

    if scrapers.is_annas_archive_url(base_url) or scrapers.is_annas_archive_url(site):
        files = scrapers.filter_annas_download_files(files)
        trust_level = "untrusted"

    best = scrapers.select_best_file(files)
    if not best:
        return None

    f_url = urllib.parse.urljoin(base_url, best.get("url", ""))
    f_dl_url = urllib.parse.urljoin(base_url, best.get("download_url", ""))
    db.add_file(
        work_id=work_id,
        site=site,
        format=best.get("format", "Unknown"),
        url=f_url,
        file_size=best.get("file_size"),
        download_source=best.get("download_source", default_source),
        download_url=f_dl_url,
        trust_level=trust_level,
    )
    return best


def _stop_requested(should_stop):
    return bool(should_stop and should_stop())


def _parse_page_with_deterministic_fallback(
    html,
    url,
    model,
    source_name=None,
    trust_level="untrusted",
    allow_llm=True,
):
    parsed_data = scrapers.parse_known_archive_page(
        html,
        url,
        source_name=source_name,
        trust_level=trust_level,
    )
    if parsed_data:
        print("      [+] Parsed download links deterministically.")
        return parsed_data

    if not allow_llm:
        print("      [!] No deterministic download links found; skipping OpenRouter fallback.")
        return None

    cleaned = scrapers.clean_html(html)
    print("      [*] Analyzing page with OpenRouter LLM...")
    try:
        parsed_data = llm.parse_page_with_llm(cleaned, url, model=model)
    except ValueError as ve:
        print(f"      [!] LLM skipped: {ve}")
        return None
    print("      [+] OpenRouter analysis returned.")
    return parsed_data


def perform_crawl(query, model, max_results=3, sources=ALL_SOURCES, should_stop=None):
    sources = set(sources)
    print(f"[*] Searching archives for: '{query}'...")
    
    # 1. ARCHIVE.ORG SEARCH
    if "archive_org" in sources and not _stop_requested(should_stop):
        print("[*] Querying Archive.org Search API...")
        archive_docs = scrapers.search_archive_org(query, max_results=max_results)
        # Filter Archive.org files to only top max_results docs
        print(f"[+] Found {len(archive_docs)} matching documents on Archive.org. Processing top {max_results}...")
        for doc in archive_docs[:max_results]:
            if _stop_requested(should_stop):
                print("[!] Crawl halted by operator.")
                return
            identifier = doc.get("identifier")
            if not identifier:
                continue
            print(f"    - Processing Archive.org ID: {identifier}...")
            files = scrapers.get_archive_org_files(identifier)
            if files:
                work_id = db.add_work(
                    title=doc.get("title", identifier),
                    author=doc.get("creator", "Unknown"),
                    search_query=query
                )
                best = _add_best_file(
                    work_id,
                    files,
                    f"https://archive.org/details/{identifier}",
                    "archive.org",
                    "Archive.org HTTP",
                )
                if best:
                    print(f"      [+] Logged preferred version: [{best.get('format')}] {best.get('download_url')}")
            
    # 2. ARXIV SEARCH
    if "arxiv" in sources and not _stop_requested(should_stop):
        print("\n[*] Querying arXiv API...")
        arxiv_results = scrapers.search_arxiv(query, max_results=max_results)
        print(f"[+] Found {len(arxiv_results)} matching papers on arXiv. Processing top {max_results}...")
        for paper in arxiv_results[:max_results]:
            if _stop_requested(should_stop):
                print("[!] Crawl halted by operator.")
                return
            work_id = db.add_work(
                title=paper["title"],
                author=paper.get("author"),
                search_query=query,
            )
            if db.work_has_archive_activity(work_id):
                print(f"      [=] Skipping duplicate arXiv paper already in archive: '{paper['title']}'")
                continue
            db.add_file(
                work_id=work_id,
                site=paper["site"],
                format=paper["format"],
                url=paper["url"],
                file_size=paper["file_size"],
                download_source=paper["download_source"],
                download_url=paper["download_url"],
            )
            print(f"      [+] Logged arXiv paper: '{paper['title']}'")

    # 3. SUBSTACK SEARCH
    if "substack" in sources and not _stop_requested(should_stop):
        print("\n[*] Querying Substack search...")
        substack_results = scrapers.search_substack(query)
        print(f"[+] Found {len(substack_results)} matching Substack posts. Processing top {max_results}...")
        for post in substack_results[:max_results]:
            if _stop_requested(should_stop):
                print("[!] Crawl halted by operator.")
                return
            work_id = db.add_work(
                title=post["title"],
                author=post.get("author"),
                search_query=query,
            )
            if db.work_has_archive_activity(work_id):
                print(f"      [=] Skipping duplicate Substack post already in archive: '{post['title']}'")
                continue
            db.add_file(
                work_id=work_id,
                site=post["site"],
                format=post["format"],
                url=post["url"],
                file_size=post["file_size"],
                download_source=post["download_source"],
                download_url=post["download_url"],
            )
            print(f"      [+] Logged Substack post: '{post['title']}'")

    # 4. THE ANARCHIST LIBRARY SEARCH
    if "anarchist_library" in sources and not _stop_requested(should_stop):
        print("\n[*] Querying The Anarchist Library...")
        al_results = scrapers.search_anarchist_library(query)
        print(f"[+] Found {len(al_results)} search results on The Anarchist Library. Analyzing top {max_results}...")
        for res in al_results[:max_results]:
            if _stop_requested(should_stop):
                print("[!] Crawl halted by operator.")
                return
            url = res["url"]
            print(f"    - Scraping and analyzing: {url}...")
            html = scrapers.fetch_url(url)
            if not html:
                print("      [!] Failed to fetch content.")
                continue

            parsed_data = _parse_page_with_deterministic_fallback(
                html,
                url,
                model,
                source_name="The Anarchist Library",
                trust_level="trusted",
            )
            
            if parsed_data and parsed_data.get("title"):
                title = parsed_data["title"]
                author = parsed_data.get("author")
                work_id = db.add_work(title=title, author=author, search_query=query)
                
                files = parsed_data.get("files", [])
                best = _add_best_file(
                    work_id,
                    files,
                    url,
                    "theanarchistlibrary.org",
                    "Anarchist Library",
                )
                if best:
                    print(f"      [+] Logged work: '{title}' with preferred [{best.get('format')}] version.")
                else:
                    print(f"      [!] No downloadable version found for: '{title}'")
            else:
                print("      [!] No structured archive data extracted.")
            
    # 5. ANNA'S ARCHIVE SEARCH
    if "annas_archive" in sources and not _stop_requested(should_stop):
        print("\n[*] Querying Anna's Archive...")
        annas_results = scrapers.search_annas_archive(query, limit=max_results)
        print(f"[+] Found {len(annas_results)} search results on Anna's Archive. Analyzing top {max_results}...")
        for res in annas_results[:max_results]:
            if _stop_requested(should_stop):
                print("[!] Crawl halted by operator.")
                return
            url = res["url"]
            print(f"    - Scraping and analyzing: {url}...")
            html = scrapers.fetch_url(url)
            if not html:
                print("      [!] Failed to fetch content.")
                continue

            parsed_data = _parse_page_with_deterministic_fallback(
                html,
                url,
                model,
                source_name="Anna's Archive Mirror",
                trust_level="untrusted",
            )
            
            if parsed_data and parsed_data.get("title"):
                title = parsed_data["title"]
                author = parsed_data.get("author")
                work_id = db.add_work(title=title, author=author, search_query=query)
                
                files = parsed_data.get("files", [])
                best = _add_best_file(
                    work_id,
                    files,
                    url,
                    "annas-archive.org",
                    "Anna's Archive Mirror",
                    trust_level="untrusted",
                )
                if best:
                    print(f"      [+] Logged work: '{title}' with preferred [{best.get('format')}] version.")
                else:
                    print(f"      [!] No downloadable version found for: '{title}'")
            else:
                print("      [!] No structured archive data extracted.")

    # 6. LIBGEN MIRROR SEARCH
    if "libgen" in sources and not _stop_requested(should_stop):
        print("\n[*] Querying LibGen mirrors...")
        libgen_results = scrapers.search_libgen(query, limit=max_results)
        print(f"[+] Found {len(libgen_results)} candidate results across LibGen mirrors. Analyzing top {max_results}...")
        for res in libgen_results[:max_results]:
            if _stop_requested(should_stop):
                print("[!] Crawl halted by operator.")
                return
            url = res["url"]
            print(f"    - Scraping and analyzing LibGen source: {url}...")
            html = scrapers.fetch_url(url, retries=1, delay=0.2)
            if not html:
                print("      [!] Failed to fetch content.")
                continue

            parsed_data = _parse_page_with_deterministic_fallback(
                html,
                url,
                model,
                source_name="LibGen mirror",
                trust_level="untrusted",
            )

            if parsed_data and parsed_data.get("title"):
                title = parsed_data["title"]
                author = parsed_data.get("author")
                work_id = db.add_work(title=title, author=author, search_query=query)

                parsed_uri = urllib.parse.urlparse(url)
                site = parsed_uri.netloc or res.get("site") or "libgen"
                files = parsed_data.get("files", [])
                best = _add_best_file(
                    work_id,
                    files,
                    url,
                    site,
                    res.get("source_name", "LibGen mirror"),
                    trust_level="untrusted",
                )
                if best:
                    print(f"      [+] Logged untrusted LibGen work: '{title}' with preferred [{best.get('format')}] version.")
                else:
                    print(f"      [!] No downloadable version found for: '{title}'")
            else:
                print("      [!] No structured archive data extracted.")

    # 6. OPEN-SLUM MIRROR SET
    if "slum_archives" in sources and not _stop_requested(should_stop):
        print("\n[*] Querying Open SLUM mirror set...")
        slum_results = scrapers.search_slum_archives(query, limit=max_results)
        print(f"[+] Found {len(slum_results)} candidate results across SLUM mirrors. Analyzing top {max_results}...")
        for res in slum_results[:max_results]:
            if _stop_requested(should_stop):
                print("[!] Crawl halted by operator.")
                return
            url = res["url"]
            print(f"    - Scraping and analyzing untrusted source: {url}...")
            html = scrapers.fetch_url(url, retries=1, delay=0.2)
            if not html:
                print("      [!] Failed to fetch content.")
                continue

            parsed_data = _parse_page_with_deterministic_fallback(
                html,
                url,
                model,
                source_name=res.get("source_name", "Open SLUM mirror"),
                trust_level="untrusted",
            )

            if parsed_data and parsed_data.get("title"):
                title = parsed_data["title"]
                author = parsed_data.get("author")
                work_id = db.add_work(title=title, author=author, search_query=query)

                parsed_uri = urllib.parse.urlparse(url)
                site = parsed_uri.netloc or res.get("site") or "slum-archive"
                files = parsed_data.get("files", [])
                best = _add_best_file(
                    work_id,
                    files,
                    url,
                    site,
                    res.get("source_name", "Open SLUM mirror"),
                    trust_level="untrusted",
                )
                if best:
                    print(f"      [+] Logged untrusted work: '{title}' with preferred [{best.get('format')}] version.")
                else:
                    print(f"      [!] No downloadable version found for: '{title}'")
            else:
                print("      [!] No structured archive data extracted.")

    # 7. CONFIGURED ARCHIVE PLUGINS
    if "archive_plugins" in sources and not _stop_requested(should_stop):
        print("\n[*] Querying configured archive plugins...")
        plugin_results = archive_plugins.search_plugins(query)
        print(f"[+] Found {len(plugin_results)} candidate results across configured archive plugins. Analyzing top {max_results}...")
        for res in plugin_results[:max_results]:
            if _stop_requested(should_stop):
                print("[!] Crawl halted by operator.")
                return
            url = res["url"]
            print(f"    - Scraping and analyzing plugin source: {url}...")
            html = scrapers.fetch_url(url, retries=1, delay=0.2)
            if not html:
                print("      [!] Failed to fetch content.")
                continue

            parsed_data = _parse_page_with_deterministic_fallback(
                html,
                url,
                model,
                source_name=res.get("source_name", "Archive plugin"),
                trust_level=res.get("trust_level", "untrusted"),
                allow_llm=False,
            )

            if parsed_data and parsed_data.get("title"):
                title = parsed_data["title"]
                author = parsed_data.get("author")
                work_id = db.add_work(title=title, author=author, search_query=query)
                files = parsed_data.get("files", [])
                best = _add_best_file(
                    work_id,
                    files,
                    url,
                    res.get("site") or urllib.parse.urlparse(url).netloc or "archive-plugin",
                    res.get("source_name", "Archive plugin"),
                    trust_level=res.get("trust_level", "untrusted"),
                )
                if best:
                    print(f"      [+] Logged plugin work: '{title}' with preferred [{best.get('format')}] version.")
                else:
                    print(f"      [!] No downloadable version found for: '{title}'")
            else:
                print("      [!] No structured archive data extracted.")

    print(f"\n[+] Crawl complete for: '{query}'")

def handle_search(args):
    perform_crawl(args.query, args.model, args.max_results, sources=args.sources)

def handle_research(args):
    import re
    print(f"[*] Starting agentic research coordinator for topic: '{args.topic}'")
    
    # 1. Generate search queries
    print(f"[*] Querying GLM-5.2 to generate specific search queries...")
    try:
        queries = llm.generate_search_queries(args.topic, model=args.model or "z-ai/glm-5.2")
    except ValueError as ve:
        print(f"[!] LLM failed to generate queries: {ve}")
        sys.exit(1)
        
    print(f"[+] Research queries generated by GLM-5.2: {queries}")
    
    # 2. Programmatically crawl each query
    for idx, query in enumerate(queries):
        print(f"\n=========================================")
        print(f"  Researching query {idx+1}/{len(queries)}: '{query}'")
        print("=========================================")
        perform_crawl(query, args.model, args.max_results, sources=args.sources)
        
    # 3. Compile database entries
    print("\n[*] Compiling crawled works from database...")
    works_data = db.get_works_by_queries(queries)
    if not works_data:
        print("[!] No works were found or logged during the research crawl.")
        sys.exit(1)
        
    print(f"[+] Found {len(works_data)} unique works in database. Synthesizing research report...")
    
    # 4. Generate report with GLM-5.2
    try:
        report = llm.generate_research_report(args.topic, works_data, model=args.model or "z-ai/glm-5.2")
    except ValueError as ve:
        print(f"[!] LLM failed to generate report: {ve}")
        sys.exit(1)
    else:
        print("[+] Research report synthesis returned.")
        
    if not report:
        print("[!] Failed to generate report.")
        sys.exit(1)
        
    # 5. Write to markdown file
    clean_topic = re.sub(r'[^a-zA-Z0-9_\-]+', '_', args.topic.lower().strip().replace(" ", "_"))
    report_filename = f"research_report_{clean_topic}.md"
    
    with open(report_filename, "w", encoding="utf-8") as f:
        f.write(report)
        
    print("\n=========================================")
    print("        RESEARCH REPORT SYNTHESIS        ")
    print("=========================================")
    print(report)
    print("=========================================")
    print(f"[+] Research report successfully saved to: {os.path.abspath(report_filename)}")

def handle_url(args):
    print(f"[*] Crawling direct URL: {args.url}")
    html = scrapers.fetch_url(args.url)
    if not html:
        print("[!] Failed to fetch page content.")
        sys.exit(1)
        
    parsed_data = _parse_page_with_deterministic_fallback(
        html,
        args.url,
        args.model,
        source_name="Direct URL Mirror",
    )
    
    if not parsed_data or not parsed_data.get("title"):
        print("[!] Failed to parse or extract structured data from page.")
        sys.exit(1)
        
    title = parsed_data["title"]
    author = parsed_data.get("author")
    work_id = db.add_work(title=title, author=author, search_query="direct_url")
    
    # Parse domain name for site field
    parsed_uri = urllib.parse.urlparse(args.url)
    domain = parsed_uri.netloc or "direct-url"
    
    files = parsed_data.get("files", [])
    best = _add_best_file(
        work_id,
        files,
        args.url,
        domain,
        "Direct URL Mirror",
    )
        
    print(f"\n[+] Successfully logged work: '{title}' by {author}")
    if best:
        print("[+] Added preferred file/version to database.")
        print(f"    - [{best.get('format')}] Source: {best.get('download_source')} ({best.get('file_size')})")
    else:
        print("[!] No downloadable file/version was added.")

def handle_status(args):
    stats = db.get_stats()
    print("\n================ DATABASE STATUS ================")
    print(f"Total Unique Works Logged: {stats['total_works']}")
    print(f"Total Download Files Logged: {stats['total_files']}")
    print("\nFiles Logged per Site/Domain:")
    for site, count in stats["files_by_site"].items():
        print(f"  - {site}: {count} files")
    print("\nDownload Jobs by Status:")
    if stats["downloads_by_status"]:
        for status, count in stats["downloads_by_status"].items():
            print(f"  - {status}: {count}")
    else:
        print("  - none")
    print("\nPlaintext Extractions by Status:")
    if stats["extractions_by_status"]:
        for status, count in stats["extractions_by_status"].items():
            print(f"  - {status}: {count}")
    else:
        print("  - none")
    print("\nQuarantine Scans by Status:")
    if stats.get("scans_by_status"):
        for status, count in stats["scans_by_status"].items():
            print(f"  - {status}: {count}")
    else:
        print("  - none")
    print("\nRaw Original Archives by Status:")
    if stats.get("raw_archives_by_status"):
        for status, count in stats["raw_archives_by_status"].items():
            print(f"  - {status}: {count}")
    else:
        print("  - none")
    print(f"\nCorpus Builds: {stats['total_corpus_builds']}")
    print("=================================================")

def handle_download(args):
    max_bytes = args.max_mb * 1024 * 1024 if args.max_mb else None
    if args.domain_workers:
        results = downloader.download_pending_by_domain(
            limit=args.limit,
            bucket_dir=args.bucket_dir,
            requests_per_second=args.rps,
            max_bytes=max_bytes,
            max_domains=args.max_domains,
            per_domain_limit=args.per_domain_limit,
            quarantine_dir=args.quarantine_dir,
        )
    else:
        results = downloader.download_pending(
            limit=args.limit,
            bucket_dir=args.bucket_dir,
            requests_per_second=args.rps,
            max_bytes=max_bytes,
            quarantine_dir=args.quarantine_dir,
        )
    print("\n================ DOWNLOAD SUMMARY ===============")
    for status, count in results.items():
        print(f"{status}: {count}")
    print("=================================================")


def _load_queries(args):
    if args.queries_file:
        with open(args.queries_file, "r", encoding="utf-8") as f:
            queries = [line.strip() for line in f if line.strip() and not line.lstrip().startswith("#")]
    else:
        queries = list(PUBLIC_COLLECTOR_QUERIES)
    if args.query:
        queries.extend(args.query)
    return queries


def _dedupe_queries(queries):
    seen = set()
    deduped = []
    for query in queries:
        normalized = " ".join(str(query or "").split())
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)
    return deduped


def _rotated(items, offset):
    if not items:
        return []
    offset = offset % len(items)
    return list(items[offset:]) + list(items[:offset])


def random_auto_focus(rng=None):
    chooser = rng or random
    return chooser.choice(AUTO_FOCUS_TOPICS)


def build_auto_queries(limit=12, cycle=1, extra_queries=None, auto_focus=None):
    """Build a rotating, corpus-aware batch of autonomous collection queries."""
    limit = max(1, int(limit or 1))
    queries = []
    if auto_focus:
        queries.extend([
            f"public domain {auto_focus}",
            f"{auto_focus} complete works",
            f"{auto_focus} essays",
        ])

    try:
        categories = db.get_categories(include_counts=True)
    except Exception as exc:
        print(f"[!] Could not inspect category coverage for auto mode: {exc}")
        categories = []

    sparse_categories = sorted(
        [
            category
            for category in categories
            if category.get("name") in AUTO_CATEGORY_QUERIES
        ],
        key=lambda category: (
            int(category.get("count") or 0),
            int(category.get("chars") or 0),
            str(category.get("name")),
        ),
    )
    for category in sparse_categories:
        category_queries = AUTO_CATEGORY_QUERIES.get(category["name"], [])
        queries.extend(_rotated(category_queries, cycle - 1)[:2])

    queries.extend(_rotated(AUTO_COLLECTION_QUERIES, cycle - 1))
    queries.extend(_rotated(PUBLIC_COLLECTOR_QUERIES, cycle - 1))
    if extra_queries:
        queries.extend(extra_queries)

    return _dedupe_queries(queries)[:limit]


def _collect_phase(name, fn):
    try:
        return fn(), None
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        print(f"[!] Collection phase '{name}' failed: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return None, exc


def _run_collect_cycle(args, queries, cycle):
    print("\n================ COLLECTION CYCLE ===============")
    print(f"Cycle: {cycle}")
    print(f"Queries: {len(queries)}")
    print(f"Sources: {', '.join(args.sources)}")
    print("=================================================")

    errors = []
    search_results = {"searched": 0, "failed": 0}
    for query in queries:
        if _stop_requested(getattr(args, "should_stop", None)):
            print("[!] Collection cycle stop requested before next query.")
            break

        def search_one(query=query):
            perform_crawl(
                query,
                args.model,
                args.max_results,
                sources=args.sources,
                should_stop=getattr(args, "should_stop", None),
            )
            return True

        _result, exc = _collect_phase(f"search:{query}", search_one)
        if exc:
            search_results["failed"] += 1
            errors.append(exc)
        else:
            search_results["searched"] += 1

    max_bytes = args.max_mb * 1024 * 1024 if args.max_mb else None
    download_results, exc = _collect_phase(
        "download",
        lambda: downloader.download_pending_by_domain(
            limit=args.download_limit,
            bucket_dir=args.raw_bucket_dir,
            requests_per_second=args.rps,
            max_bytes=max_bytes,
            max_domains=args.max_domains,
            per_domain_limit=args.per_domain_limit,
            quarantine_dir=args.quarantine_dir,
        ),
    )
    if exc:
        errors.append(exc)
        download_results = {"downloaded": 0, "failed": 0, "skipped": 0, "phase_error": 1}

    process_results, exc = _collect_phase(
        "process",
        lambda: processor.process_pending(
            limit=args.process_limit,
            bucket_dir=args.text_bucket_dir,
            extractor=args.extractor,
        ),
    )
    if exc:
        errors.append(exc)
        process_results = {"processed": 0, "failed": 0, "skipped": 0, "phase_error": 1}

    raw_archive_results = None
    if args.archive_raw_limit:
        raw_archive_results, exc = _collect_phase(
            "archive-raw",
            lambda: processor.archive_processed_raws(limit=args.archive_raw_limit, delete_local=True),
        )
        if exc:
            errors.append(exc)
            raw_archive_results = {"archived": 0, "failed": 0, "skipped": 0, "phase_error": 1}

    print("\n================ CYCLE SUMMARY ==================")
    print(f"search: {search_results}")
    print(f"downloads: {download_results}")
    print(f"processing: {process_results}")
    if raw_archive_results is not None:
        print(f"raw_archive: {raw_archive_results}")
    print(f"errors: {len(errors)}")
    print("=================================================")
    return errors


def _sleep_interruptibly(seconds, should_stop=None):
    deadline = time.time() + max(0, seconds)
    while time.time() < deadline:
        if _stop_requested(should_stop):
            return
        time.sleep(min(5, deadline - time.time()))


def handle_collect(args):
    queries = _load_queries(args)
    if not queries:
        print("[!] No collection queries provided.")
        sys.exit(1)

    cycle = 0
    consecutive_error_cycles = 0
    while True:
        if _stop_requested(getattr(args, "should_stop", None)):
            print("[!] Collection loop stopped by operator.")
            break
        cycle += 1
        errors = _run_collect_cycle(args, queries, cycle)
        if errors:
            consecutive_error_cycles += 1
        else:
            consecutive_error_cycles = 0

        if args.once:
            break
        sleep_seconds = args.sleep_seconds
        if errors:
            sleep_seconds = min(
                args.max_error_sleep_seconds,
                args.error_sleep_seconds * max(1, consecutive_error_cycles),
            )
            print(f"[!] Collection cycle had {len(errors)} error(s); backing off for {sleep_seconds}s.")
        _sleep_interruptibly(sleep_seconds, getattr(args, "should_stop", None))


def handle_auto(args):
    cycle = 0
    consecutive_error_cycles = 0
    extra_queries = _load_queries(args) if args.queries_file or args.query else []
    auto_focus = getattr(args, "auto_focus", None) or random_auto_focus()
    print(f"[*] Auto collection focus: {auto_focus}")
    while True:
        if _stop_requested(getattr(args, "should_stop", None)):
            print("[!] Auto loop stopped by operator.")
            break
        cycle += 1
        queries = build_auto_queries(
            limit=args.query_limit,
            cycle=cycle,
            extra_queries=extra_queries,
            auto_focus=auto_focus,
        )
        errors = _run_collect_cycle(args, queries, cycle)
        if errors:
            consecutive_error_cycles += 1
        else:
            consecutive_error_cycles = 0

        if args.once:
            break
        sleep_seconds = args.sleep_seconds
        if errors:
            sleep_seconds = min(
                args.max_error_sleep_seconds,
                args.error_sleep_seconds * max(1, consecutive_error_cycles),
            )
            print(f"[!] Auto cycle had {len(errors)} error(s); backing off for {sleep_seconds}s.")
        _sleep_interruptibly(sleep_seconds, getattr(args, "should_stop", None))

def handle_process(args):
    results = processor.process_pending(
        limit=args.limit,
        bucket_dir=args.bucket_dir,
        extractor=args.extractor,
    )
    print("\n=============== PROCESSING SUMMARY ==============")
    for status, count in results.items():
        print(f"{status}: {count}")
    print("=================================================")


def handle_validate_texts(args):
    if args.remove_unusable:
        results = text_validator.remove_unusable(limit=args.limit, verbose=True)
        print("\n=============== TEXT REJECTION CLEANUP ==========")
        for status, count in results.items():
            print(f"{status}: {count}")
        print("=================================================")
        return
    results = text_validator.validate_pending(
        limit=args.limit,
        model=args.validator_model,
        include_validated=args.recheck,
        use_llm=not args.no_llm,
        verbose=True,
        workers=args.workers,
    )
    print("\n=============== TEXT VALIDATION SUMMARY =========")
    for status, count in results.items():
        print(f"{status}: {count}")
    print("=================================================")


def handle_munge_texts(args):
    results = text_munger.munge_pending(
        limit=args.limit,
        bucket_dir=args.bucket_dir,
        use_llm=args.use_llm,
        model=args.munger_model,
        include_munged=args.recheck,
        dry_run=args.dry_run,
    )
    print("\n=============== TEXT MUNGING SUMMARY ============")
    for status, count in results.items():
        print(f"{status}: {count}")
    print("=================================================")


def handle_archive_raw(args):
    results = processor.archive_processed_raws(
        limit=args.limit,
        delete_local=not args.keep_local,
    )
    print("\n=============== RAW ARCHIVE SUMMARY =============")
    for status, count in results.items():
        print(f"{status}: {count}")
    print("=================================================")


def handle_rss_ingest(args):
    results = rss_ingest.ingest_feeds(
        path=args.feeds_file,
        limit_per_feed=args.limit_per_feed,
        dry_run=args.dry_run,
        timeout=args.timeout,
    )
    print("\n================ RSS INGEST SUMMARY =============")
    for status, count in results.items():
        print(f"{status}: {count}")
    print("=================================================")

def handle_corpus(args):
    try:
        result = corpus.build_corpus(
            name=args.name,
            category=args.category,
            site=args.site,
            query=args.query,
            ordering_strategy=args.ordering,
            seed=args.seed,
            limit=args.limit,
            substitutions_path=args.substitutions_file,
            output_dir=args.bucket_dir,
            use_munged=args.munged,
        )
    except corpus.CorpusBuildError as exc:
        print(f"[!] Corpus build failed: {exc}")
        sys.exit(1)

    print("\n================ CORPUS BUILD ===================")
    print(f"Build ID: {result['build_id']}")
    print(f"Manifest SHA-256: {result['manifest_sha256']}")
    print(f"Items: {result['item_count']}")
    print(f"Total chars: {result['total_chars']}")
    print(f"Manifest: {result['manifest_path']}")
    print(f"Corpus text: {result['corpus_path']}")
    print("=================================================")


def handle_dashboard(args):
    dashboard.run_dashboard(
        BANNER_LINES,
        watch=args.watch,
        interval=args.interval,
    )


def handle_agent_status(args):
    row = db.add_agent_status(
        " ".join(args.message),
        session_id=args.session_id,
        loop_kind=args.loop_kind,
        phase=args.phase,
        model=args.status_model,
        goal_id=args.goal_id,
    )
    print(f"[+] agent status #{row['id']} {row['created_at']}: {row['message']}")

def main():
    # Initialize DB first
    db.init_db()
    
    parser = argparse.ArgumentParser(description="Archive crawler leveraging OpenRouter LLM analysis.")
    parser.add_argument("--model", type=str, help="Override default OpenRouter model to use.")
    parser.add_argument("--max-results", type=int, default=2, help="Maximum search results to parse per query (default: 2)")
    
    subparsers = parser.add_subparsers(dest="command", required=True, help="Command to run")
    
    # Search Command
    parser_search = subparsers.add_parser("search", help="Search and crawl archive sites for a term.")
    parser_search.add_argument("query", type=str, help="The search query (title, author, keywords).")
    parser_search.add_argument(
        "--sources",
        nargs="+",
        choices=ALL_SOURCES,
        default=ALL_SOURCES,
        help="Archive sources to query.",
    )
    
    # Direct URL Command
    parser_url = subparsers.add_parser("url", help="Crawl and analyze a specific book detail page.")
    parser_url.add_argument("url", type=str, help="The direct URL of the archive detail page.")
    
    # Research Command
    parser_research = subparsers.add_parser("research", help="Run agentic topic research (generates terms, crawls, reports).")
    parser_research.add_argument("topic", type=str, help="The broad topic to research.")
    parser_research.add_argument(
        "--sources",
        nargs="+",
        choices=ALL_SOURCES,
        default=ALL_SOURCES,
        help="Archive sources to query.",
    )
    
    # Status Command
    subparsers.add_parser("status", help="Show database crawler statistics.")

    # Download Command
    parser_download = subparsers.add_parser("download", help="Download logged files into the raw object bucket.")
    parser_download.add_argument("--limit", type=int, default=10, help="Maximum files to download in this run.")
    parser_download.add_argument("--bucket-dir", default=downloader.DEFAULT_RAW_BUCKET_DIR, help="Filesystem-backed raw bucket directory.")
    parser_download.add_argument("--quarantine-dir", default=downloader.DEFAULT_QUARANTINE_BUCKET_DIR, help="Filesystem-backed quarantine bucket directory.")
    parser_download.add_argument("--rps", type=float, default=0.2, help="Per-host requests per second. Default is 0.2, or one request every five seconds.")
    parser_download.add_argument("--max-mb", type=int, default=250, help="Maximum size per file in MB. Use 0 for no limit.")
    parser_download.add_argument("--domain-workers", action="store_true", help="Run one sequential worker per download domain.")
    parser_download.add_argument("--max-domains", type=int, help="Maximum domain workers to run in this command.")
    parser_download.add_argument("--per-domain-limit", type=int, help="Maximum files assigned to each domain worker.")

    # Process Command
    parser_process = subparsers.add_parser("process", help="Extract plaintext from downloaded raw objects.")
    parser_process.add_argument("--limit", type=int, default=10, help="Maximum downloads to process in this run.")
    parser_process.add_argument("--bucket-dir", default=processor.DEFAULT_TEXT_BUCKET_DIR, help="Filesystem-backed text bucket directory.")
    parser_process.add_argument("--extractor", default=processor.EXTRACTOR_VERSION, help="Extractor version label for idempotent processing.")

    # Text Validation Command
    parser_validate = subparsers.add_parser("validate-texts", help="Validate extracted plaintext legibility.")
    parser_validate.add_argument("--limit", type=int, default=25, help="Maximum extracted texts to validate in this run.")
    parser_validate.add_argument("--validator-model", default=text_validator.DEFAULT_VALIDATOR_MODEL, help="OpenRouter model for ambiguous legibility checks.")
    parser_validate.add_argument("--workers", type=int, default=4, help="Concurrent validator workers for OpenRouter calls.")
    parser_validate.add_argument("--recheck", action="store_true", help="Recheck already validated text rows.")
    parser_validate.add_argument("--no-llm", action="store_true", help="Only use local byte/prose heuristics.")
    parser_validate.add_argument("--remove-unusable", action="store_true", help="Remove text artifacts already marked unusable and mark their extraction skipped.")

    # Text Munging Command
    parser_munge = subparsers.add_parser("munge-texts", help="Clean processed plaintext into training-ready derived artifacts.")
    parser_munge.add_argument("--limit", type=int, default=25, help="Maximum processed texts to munge in this run.")
    parser_munge.add_argument("--bucket-dir", default=text_munger.DEFAULT_MUNGED_BUCKET_DIR, help="Filesystem-backed munged text bucket directory.")
    parser_munge.add_argument("--use-llm", action="store_true", help="Ask the model for validated surgical cleanup rules.")
    parser_munge.add_argument("--munger-model", default=text_munger.DEFAULT_MUNGER_MODEL, help="OpenRouter model for optional cleanup rule proposals.")
    parser_munge.add_argument("--recheck", action="store_true", help="Regenerate already munged rows.")
    parser_munge.add_argument("--dry-run", action="store_true", help="Run munging without writing artifacts or DB rows.")

    # Archive Raw Command
    parser_archive_raw = subparsers.add_parser("archive-raw", help="Upload processed raw originals to S3-compatible object storage.")
    parser_archive_raw.add_argument("--limit", type=int, default=10, help="Maximum processed raw downloads to archive.")
    parser_archive_raw.add_argument("--keep-local", action="store_true", help="Keep local raw files after successful upload.")

    # RSS Ingest Command
    parser_rss = subparsers.add_parser("rss-ingest", help="Archive configured RSS/Atom feed items into the download backlog.")
    parser_rss.add_argument("--feeds-file", default=str(rss_ingest.DEFAULT_FEEDS_PATH), help="JSON feed list to ingest.")
    parser_rss.add_argument("--limit-per-feed", type=int, default=rss_ingest.DEFAULT_LIMIT_PER_FEED, help="Maximum items to ingest per feed.")
    parser_rss.add_argument("--timeout", type=int, default=rss_ingest.DEFAULT_TIMEOUT_SECONDS, help="HTTP timeout per feed.")
    parser_rss.add_argument("--dry-run", action="store_true", help="Parse feeds and report items without writing DB rows.")

    # Autonomous Collection Command
    parser_collect = subparsers.add_parser("collect", help="Autonomously discover, download, and process public works.")
    parser_collect.add_argument("--query", action="append", help="Additional query to include. Can be repeated.")
    parser_collect.add_argument("--queries-file", help="Newline-delimited queries file. Blank lines and # comments are ignored.")
    parser_collect.add_argument(
        "--sources",
        nargs="+",
        choices=ALL_SOURCES,
        default=DEFAULT_PUBLIC_SOURCES,
        help="Sources to query. Defaults to public-source collection only.",
    )
    parser_collect.add_argument("--once", action="store_true", help="Run one collection cycle and exit.")
    parser_collect.add_argument("--sleep-seconds", type=int, default=3600, help="Delay between collection cycles.")
    parser_collect.add_argument("--error-sleep-seconds", type=int, default=300, help="Delay after a collection cycle with errors.")
    parser_collect.add_argument("--max-error-sleep-seconds", type=int, default=3600, help="Maximum delay after repeated error cycles.")
    parser_collect.add_argument("--download-limit", type=int, default=100, help="Maximum files to download per cycle.")
    parser_collect.add_argument("--process-limit", type=int, default=100, help="Maximum downloads to process per cycle.")
    parser_collect.add_argument("--archive-raw-limit", type=int, default=25, help="Retry this many pending raw S3 archives per cycle. Use 0 to disable.")
    parser_collect.add_argument("--raw-bucket-dir", default=downloader.DEFAULT_RAW_BUCKET_DIR, help="Filesystem-backed raw bucket directory.")
    parser_collect.add_argument("--quarantine-dir", default=downloader.DEFAULT_QUARANTINE_BUCKET_DIR, help="Filesystem-backed quarantine bucket directory.")
    parser_collect.add_argument("--text-bucket-dir", default=processor.DEFAULT_TEXT_BUCKET_DIR, help="Filesystem-backed text bucket directory.")
    parser_collect.add_argument("--rps", type=float, default=0.2, help="Per-domain download requests per second.")
    parser_collect.add_argument("--max-mb", type=int, default=250, help="Maximum size per file in MB. Use 0 for no limit.")
    parser_collect.add_argument("--max-domains", type=int, help="Maximum domain workers per download phase.")
    parser_collect.add_argument("--per-domain-limit", type=int, help="Maximum files assigned to each domain worker per cycle.")
    parser_collect.add_argument("--extractor", default=processor.EXTRACTOR_VERSION, help="Extractor version label for idempotent processing.")

    # Autonomous Agent-Led Collection Command
    parser_auto = subparsers.add_parser("auto", help="Continuously expand the data lake using rotating, corpus-aware public-work queries.")
    parser_auto.add_argument("--query", action="append", help="Additional standing query to include. Can be repeated.")
    parser_auto.add_argument("--queries-file", help="Newline-delimited standing queries. Blank lines and # comments are ignored.")
    parser_auto.add_argument(
        "--sources",
        nargs="+",
        choices=ALL_SOURCES,
        default=DEFAULT_PUBLIC_SOURCES,
        help="Sources to query. Defaults to public-source collection only.",
    )
    parser_auto.add_argument("--once", action="store_true", help="Run one autonomous cycle and exit.")
    parser_auto.add_argument("--query-limit", type=int, default=12, help="Maximum auto-selected queries per cycle.")
    parser_auto.add_argument("--auto-focus", help="Override the random fiction/non-fiction focus for this auto run.")
    parser_auto.add_argument("--sleep-seconds", type=int, default=1800, help="Delay between autonomous cycles.")
    parser_auto.add_argument("--error-sleep-seconds", type=int, default=300, help="Delay after a cycle with errors.")
    parser_auto.add_argument("--max-error-sleep-seconds", type=int, default=3600, help="Maximum delay after repeated error cycles.")
    parser_auto.add_argument("--download-limit", type=int, default=100, help="Maximum files to download per cycle.")
    parser_auto.add_argument("--process-limit", type=int, default=100, help="Maximum downloads to process per cycle.")
    parser_auto.add_argument("--archive-raw-limit", type=int, default=50, help="Retry this many pending raw S3 archives per cycle. Use 0 to disable.")
    parser_auto.add_argument("--raw-bucket-dir", default=downloader.DEFAULT_RAW_BUCKET_DIR, help="Filesystem-backed raw bucket directory.")
    parser_auto.add_argument("--quarantine-dir", default=downloader.DEFAULT_QUARANTINE_BUCKET_DIR, help="Filesystem-backed quarantine bucket directory.")
    parser_auto.add_argument("--text-bucket-dir", default=processor.DEFAULT_TEXT_BUCKET_DIR, help="Filesystem-backed text bucket directory.")
    parser_auto.add_argument("--rps", type=float, default=0.2, help="Per-domain download requests per second.")
    parser_auto.add_argument("--max-mb", type=int, default=250, help="Maximum size per file in MB. Use 0 for no limit.")
    parser_auto.add_argument("--max-domains", type=int, help="Maximum domain workers per download phase.")
    parser_auto.add_argument("--per-domain-limit", type=int, help="Maximum files assigned to each domain worker per cycle.")
    parser_auto.add_argument("--extractor", default=processor.EXTRACTOR_VERSION, help="Extractor version label for idempotent processing.")

    # Corpus Command
    parser_corpus = subparsers.add_parser("corpus", help="Build an immutable corpus manifest from processed plaintext.")
    parser_corpus.add_argument("name", help="Name for this corpus recipe.")
    parser_corpus.add_argument("--category", help="Only include processed texts with this category.")
    parser_corpus.add_argument("--site", help="Only include processed texts from this source site.")
    parser_corpus.add_argument("--query", help="Match search query, title, author, or category text.")
    parser_corpus.add_argument("--ordering", choices=["title", "hash", "created", "random"], default="title", help="Deterministic ordering strategy.")
    parser_corpus.add_argument("--seed", type=int, default=0, help="Seed used by --ordering random.")
    parser_corpus.add_argument("--limit", type=int, help="Maximum processed texts to include.")
    parser_corpus.add_argument("--substitutions-file", help="JSON object or list of {'from','to'} replacements.")
    parser_corpus.add_argument("--bucket-dir", default=corpus.DEFAULT_CORPUS_BUCKET_DIR, help="Filesystem-backed corpus artifact directory.")
    parser_corpus.add_argument("--munged", action="store_true", help="Build from munged training-text artifacts instead of raw extracted plaintext.")

    # Sticky tmux Dashboard
    parser_dashboard = subparsers.add_parser("dashboard", help="Render a compact live dashboard for the archiver.")
    parser_dashboard.add_argument("--watch", action="store_true", help="Continuously refresh the dashboard.")
    parser_dashboard.add_argument("--interval", type=float, default=2.0, help="Refresh interval in seconds for --watch.")

    # Agent status log command
    parser_agent_status = subparsers.add_parser("agent-status", help="Write a short agent status update for live dashboards.")
    parser_agent_status.add_argument("message", nargs="+", help="One or two sentence status update.")
    parser_agent_status.add_argument("--session-id", help="Agent session identifier.")
    parser_agent_status.add_argument("--loop-kind", default="manual", help="Loop type, e.g. chat, goal, or manual.")
    parser_agent_status.add_argument("--phase", default="update", help="Status phase, e.g. start, end, halted, or error.")
    parser_agent_status.add_argument("--model", dest="status_model", help="Model associated with this status.")
    parser_agent_status.add_argument("--goal-id", help="Goal identifier associated with this status.")

    # Interactive Agent Harness
    parser_agent = subparsers.add_parser("agent", help="Open an interactive terminal harness for directing the archiver.")
    parser_agent.add_argument(
        "--command",
        "-c",
        dest="agent_command",
        help="Run one agent-harness command and exit, e.g. -c 'status' or -c 'download --limit 5'.",
    )
    
    args = parser.parse_args()
    if not os.getenv("ALGE_NO_BANNER") and args.command not in ("dashboard", "agent-status"):
        print_banner()
    
    if args.command == "search":
        handle_search(args)
    elif args.command == "research":
        handle_research(args)
    elif args.command == "url":
        handle_url(args)
    elif args.command == "status":
        handle_status(args)
    elif args.command == "download":
        handle_download(args)
    elif args.command == "process":
        handle_process(args)
    elif args.command == "validate-texts":
        handle_validate_texts(args)
    elif args.command == "munge-texts":
        handle_munge_texts(args)
    elif args.command == "archive-raw":
        handle_archive_raw(args)
    elif args.command == "rss-ingest":
        handle_rss_ingest(args)
    elif args.command == "collect":
        handle_collect(args)
    elif args.command == "auto":
        handle_auto(args)
    elif args.command == "corpus":
        handle_corpus(args)
    elif args.command == "dashboard":
        handle_dashboard(args)
    elif args.command == "agent-status":
        handle_agent_status(args)
    elif args.command == "agent":
        agent.run_agent(sys.modules[__name__], command=args.agent_command)

if __name__ == "__main__":
    main()
