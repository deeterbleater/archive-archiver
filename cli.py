import argparse
import sys
import os
import urllib.parse
from dotenv import load_dotenv

# Ensure all print statements flush immediately (important for background logs)
import builtins
def print(*args, **kwargs):
    kwargs.setdefault('flush', True)
    builtins.print(*args, **kwargs)

import db
import scrapers
import llm

# Load dotenv to read OPENROUTER_API_KEY
load_dotenv()

def print_banner():
    print("=========================================")
    print("      Archive Crawler & LLM Analyzer     ")
    print("=========================================")

def perform_crawl(query, model, max_results=3):
    print(f"[*] Searching archives for: '{query}'...")
    
    # 1. ARCHIVE.ORG SEARCH
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

    print(f"\n[+] Crawl complete for: '{query}'")

def handle_search(args):
    perform_crawl(args.query, args.model, args.max_results)

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
        perform_crawl(query, args.model, args.max_results)
        
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
    
    # Direct URL Command
    parser_url = subparsers.add_parser("url", help="Crawl and analyze a specific book detail page.")
    parser_url.add_argument("url", type=str, help="The direct URL of the archive detail page.")
    
    # Research Command
    parser_research = subparsers.add_parser("research", help="Run agentic topic research (generates terms, crawls, reports).")
    parser_research.add_argument("topic", type=str, help="The broad topic to research.")
    
    # Status Command
    subparsers.add_parser("status", help="Show database crawler statistics.")
    
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

if __name__ == "__main__":
    main()
