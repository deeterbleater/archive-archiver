import argparse
import sys
import os
import time
import urllib.parse
from dotenv import load_dotenv

# Ensure all print statements flush immediately (important for background logs)
import builtins
def print(*args, **kwargs):
    kwargs.setdefault('flush', True)
    builtins.print(*args, **kwargs)

# Load dotenv before module-level CLI defaults read environment variables.
load_dotenv()

import db
import scrapers
import llm
import downloader
import processor
import corpus
import agent

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

DEFAULT_PUBLIC_SOURCES = ("archive_org", "anarchist_library")
ALL_SOURCES = ("archive_org", "anarchist_library", "annas_archive", "slum_archives")

def print_banner():
    print("=========================================")
    print("      Archive Crawler & LLM Analyzer     ")
    print("=========================================")

def perform_crawl(query, model, max_results=3, sources=ALL_SOURCES):
    sources = set(sources)
    print(f"[*] Searching archives for: '{query}'...")
    
    # 1. ARCHIVE.ORG SEARCH
    if "archive_org" in sources:
        print("[*] Querying Archive.org Search API...")
        archive_docs = scrapers.search_archive_org(query)
        # Filter Archive.org files to only top max_results docs
        print(f"[+] Found {len(archive_docs)} matching documents on Archive.org. Processing top {max_results}...")
        for doc in archive_docs[:max_results]:
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
                for f in files:
                    db.add_file(
                        work_id=work_id,
                        site=f["site"],
                        format=f["format"],
                        url=f["url"],
                        file_size=f["file_size"],
                        download_source=f["download_source"],
                        download_url=f["download_url"]
                    )
                print(f"      [+] Logged {len(files)} versions/files for this work.")
            
    # 2. THE ANARCHIST LIBRARY SEARCH
    if "anarchist_library" in sources:
        print("\n[*] Querying The Anarchist Library...")
        al_results = scrapers.search_anarchist_library(query)
        print(f"[+] Found {len(al_results)} search results on The Anarchist Library. Analyzing top {max_results}...")
        for res in al_results[:max_results]:
            url = res["url"]
            print(f"    - Scraping and analyzing: {url}...")
            html = scrapers.fetch_url(url)
            if not html:
                print("      [!] Failed to fetch content.")
                continue
                
            cleaned = scrapers.clean_html(html)
            print("      [*] Analyzing page with OpenRouter LLM...")
            try:
                parsed_data = llm.parse_page_with_llm(cleaned, url, model=model)
            except ValueError as ve:
                print(f"      [!] LLM skipped: {ve}")
                parsed_data = None
            
            if parsed_data and parsed_data.get("title"):
                title = parsed_data["title"]
                author = parsed_data.get("author")
                work_id = db.add_work(title=title, author=author, search_query=query)
                
                files = parsed_data.get("files", [])
                for f in files:
                    # Resolve relative url if any
                    f_url = urllib.parse.urljoin(url, f.get("url", ""))
                    f_dl_url = urllib.parse.urljoin(url, f.get("download_url", ""))
                    
                    db.add_file(
                        work_id=work_id,
                        site="theanarchistlibrary.org",
                        format=f.get("format", "Unknown"),
                        url=f_url,
                        file_size=f.get("file_size"),
                        download_source=f.get("download_source", "Anarchist Library"),
                        download_url=f_dl_url
                    )
                print(f"      [+] Logged work: '{title}' with {len(files)} download versions.")
            else:
                print("      [!] LLM failed to parse or extract structure.")
            
    # 3. ANNA'S ARCHIVE SEARCH
    if "annas_archive" in sources:
        print("\n[*] Querying Anna's Archive...")
        annas_results = scrapers.search_annas_archive(query)
        print(f"[+] Found {len(annas_results)} search results on Anna's Archive. Analyzing top {max_results}...")
        for res in annas_results[:max_results]:
            url = res["url"]
            print(f"    - Scraping and analyzing: {url}...")
            html = scrapers.fetch_url(url)
            if not html:
                print("      [!] Failed to fetch content.")
                continue
                
            cleaned = scrapers.clean_html(html)
            print("      [*] Analyzing page with OpenRouter LLM...")
            try:
                parsed_data = llm.parse_page_with_llm(cleaned, url, model=model)
            except ValueError as ve:
                print(f"      [!] LLM skipped: {ve}")
                parsed_data = None
            
            if parsed_data and parsed_data.get("title"):
                title = parsed_data["title"]
                author = parsed_data.get("author")
                work_id = db.add_work(title=title, author=author, search_query=query)
                
                files = parsed_data.get("files", [])
                for f in files:
                    f_url = urllib.parse.urljoin(url, f.get("url", ""))
                    f_dl_url = urllib.parse.urljoin(url, f.get("download_url", ""))
                    
                    db.add_file(
                        work_id=work_id,
                        site="annas-archive.org",
                        format=f.get("format", "Unknown"),
                        url=f_url,
                        file_size=f.get("file_size"),
                        download_source=f.get("download_source", "Anna's Archive Mirror"),
                        download_url=f_dl_url
                    )
                print(f"      [+] Logged work: '{title}' with {len(files)} download versions.")
            else:
                print("      [!] LLM failed to parse or extract structure.")

    # 4. OPEN-SLUM MIRROR SET
    if "slum_archives" in sources:
        print("\n[*] Querying Open SLUM mirror set...")
        slum_results = scrapers.search_slum_archives(query)
        print(f"[+] Found {len(slum_results)} candidate results across SLUM mirrors. Analyzing top {max_results}...")
        for res in slum_results[:max_results]:
            url = res["url"]
            print(f"    - Scraping and analyzing untrusted source: {url}...")
            html = scrapers.fetch_url(url, retries=1, delay=0.2)
            if not html:
                print("      [!] Failed to fetch content.")
                continue

            cleaned = scrapers.clean_html(html)
            print("      [*] Analyzing page with OpenRouter LLM...")
            try:
                parsed_data = llm.parse_page_with_llm(cleaned, url, model=model)
            except ValueError as ve:
                print(f"      [!] LLM skipped: {ve}")
                parsed_data = None

            if parsed_data and parsed_data.get("title"):
                title = parsed_data["title"]
                author = parsed_data.get("author")
                work_id = db.add_work(title=title, author=author, search_query=query)

                parsed_uri = urllib.parse.urlparse(url)
                site = parsed_uri.netloc or res.get("site") or "slum-archive"
                files = parsed_data.get("files", [])
                for f in files:
                    f_url = urllib.parse.urljoin(url, f.get("url", ""))
                    f_dl_url = urllib.parse.urljoin(url, f.get("download_url", ""))

                    db.add_file(
                        work_id=work_id,
                        site=site,
                        format=f.get("format", "Unknown"),
                        url=f_url,
                        file_size=f.get("file_size"),
                        download_source=f.get("download_source", res.get("source_name", "Open SLUM mirror")),
                        download_url=f_dl_url,
                        trust_level="untrusted",
                    )
                print(f"      [+] Logged untrusted work: '{title}' with {len(files)} download versions.")
            else:
                print("      [!] LLM failed to parse or extract structure.")

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
        
    cleaned = scrapers.clean_html(html)
    print("[*] Analyzing content with OpenRouter LLM...")
    try:
        parsed_data = llm.parse_page_with_llm(cleaned, args.url, model=args.model)
    except ValueError as ve:
        print(f"[!] LLM failed: {ve}")
        sys.exit(1)
    
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
    for f in files:
        f_url = urllib.parse.urljoin(args.url, f.get("url", ""))
        f_dl_url = urllib.parse.urljoin(args.url, f.get("download_url", ""))
        
        db.add_file(
            work_id=work_id,
            site=domain,
            format=f.get("format", "Unknown"),
            url=f_url,
            file_size=f.get("file_size"),
            download_source=f.get("download_source", "Direct URL Mirror"),
            download_url=f_dl_url
        )
        
    print(f"\n[+] Successfully logged work: '{title}' by {author}")
    print(f"[+] Added {len(files)} files/versions to database.")
    for f in files:
        print(f"    - [{f.get('format')}] Source: {f.get('download_source')} ({f.get('file_size')})")

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


def handle_collect(args):
    queries = _load_queries(args)
    if not queries:
        print("[!] No collection queries provided.")
        sys.exit(1)

    cycle = 0
    while True:
        cycle += 1
        print("\n================ COLLECTION CYCLE ===============")
        print(f"Cycle: {cycle}")
        print(f"Queries: {len(queries)}")
        print(f"Sources: {', '.join(args.sources)}")
        print("=================================================")

        for query in queries:
            perform_crawl(query, args.model, args.max_results, sources=args.sources)

        max_bytes = args.max_mb * 1024 * 1024 if args.max_mb else None
        download_results = downloader.download_pending_by_domain(
            limit=args.download_limit,
            bucket_dir=args.raw_bucket_dir,
            requests_per_second=args.rps,
            max_bytes=max_bytes,
            max_domains=args.max_domains,
            per_domain_limit=args.per_domain_limit,
            quarantine_dir=args.quarantine_dir,
        )
        process_results = processor.process_pending(
            limit=args.process_limit,
            bucket_dir=args.text_bucket_dir,
            extractor=args.extractor,
        )

        print("\n================ CYCLE SUMMARY ==================")
        print(f"downloads: {download_results}")
        print(f"processing: {process_results}")
        print("=================================================")

        if args.once:
            break
        time.sleep(args.sleep_seconds)

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


def handle_archive_raw(args):
    results = processor.archive_processed_raws(
        limit=args.limit,
        delete_local=not args.keep_local,
    )
    print("\n=============== RAW ARCHIVE SUMMARY =============")
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

    # Archive Raw Command
    parser_archive_raw = subparsers.add_parser("archive-raw", help="Upload processed raw originals to S3-compatible object storage.")
    parser_archive_raw.add_argument("--limit", type=int, default=10, help="Maximum processed raw downloads to archive.")
    parser_archive_raw.add_argument("--keep-local", action="store_true", help="Keep local raw files after successful upload.")

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
    parser_collect.add_argument("--download-limit", type=int, default=100, help="Maximum files to download per cycle.")
    parser_collect.add_argument("--process-limit", type=int, default=100, help="Maximum downloads to process per cycle.")
    parser_collect.add_argument("--raw-bucket-dir", default=downloader.DEFAULT_RAW_BUCKET_DIR, help="Filesystem-backed raw bucket directory.")
    parser_collect.add_argument("--quarantine-dir", default=downloader.DEFAULT_QUARANTINE_BUCKET_DIR, help="Filesystem-backed quarantine bucket directory.")
    parser_collect.add_argument("--text-bucket-dir", default=processor.DEFAULT_TEXT_BUCKET_DIR, help="Filesystem-backed text bucket directory.")
    parser_collect.add_argument("--rps", type=float, default=0.2, help="Per-domain download requests per second.")
    parser_collect.add_argument("--max-mb", type=int, default=250, help="Maximum size per file in MB. Use 0 for no limit.")
    parser_collect.add_argument("--max-domains", type=int, help="Maximum domain workers per download phase.")
    parser_collect.add_argument("--per-domain-limit", type=int, help="Maximum files assigned to each domain worker per cycle.")
    parser_collect.add_argument("--extractor", default=processor.EXTRACTOR_VERSION, help="Extractor version label for idempotent processing.")

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

    # Interactive Agent Harness
    parser_agent = subparsers.add_parser("agent", help="Open an interactive terminal harness for directing the archiver.")
    parser_agent.add_argument(
        "--command",
        "-c",
        dest="agent_command",
        help="Run one agent-harness command and exit, e.g. -c 'status' or -c 'download --limit 5'.",
    )
    
    args = parser.parse_args()
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
    elif args.command == "archive-raw":
        handle_archive_raw(args)
    elif args.command == "collect":
        handle_collect(args)
    elif args.command == "corpus":
        handle_corpus(args)
    elif args.command == "agent":
        agent.run_agent(sys.modules[__name__], command=args.agent_command)

if __name__ == "__main__":
    main()
