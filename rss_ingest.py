import json
from pathlib import Path
import re
import urllib.parse
import xml.etree.ElementTree as ET

import requests

import db
import scrapers


DEFAULT_FEEDS_PATH = Path(__file__).resolve().parent / "config" / "rss_feeds.json"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_LIMIT_PER_FEED = 25


def _local_name(tag):
    return str(tag or "").rsplit("}", 1)[-1]


def _child(element, name):
    for child in list(element):
        if _local_name(child.tag) == name:
            return child
    return None


def _children(element, name):
    return [child for child in list(element) if _local_name(child.tag) == name]


def _text(element, name=None):
    node = _child(element, name) if name else element
    if node is None or node.text is None:
        return ""
    return re.sub(r"\s+", " ", node.text).strip()


def _atom_link(entry):
    for link in _children(entry, "link"):
        rel = (link.get("rel") or "alternate").lower()
        href = (link.get("href") or "").strip()
        if href and rel == "alternate":
            return href
    for link in _children(entry, "link"):
        href = (link.get("href") or "").strip()
        if href:
            return href
    return ""


def _rss_item_link(item):
    guid = _child(item, "guid")
    enclosure = _child(item, "enclosure")
    if enclosure is not None and enclosure.get("url"):
        return enclosure.get("url").strip()
    return _text(item, "link") or (_text(guid) if guid is not None and guid.get("isPermaLink") == "true" else "")


def _item_id(feed_url, title, link, guid=None):
    value = guid or link or title
    return f"{feed_url}#{value}"


def _site_for_url(url, fallback):
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return fallback
    return parsed.netloc or fallback


def _format_for_url(url):
    path = urllib.parse.urlparse(url).path.lower()
    if path.endswith(".pdf"):
        return "PDF"
    if path.endswith(".epub"):
        return "EPUB"
    if path.endswith((".txt", ".text")):
        return "Text"
    if path.endswith(".html") or path.endswith(".htm"):
        return "HTML"
    return "HTML"


def _parse_rss(root, feed_url, feed_name):
    channel = _child(root, "channel") or root
    rows = []
    for item in _children(channel, "item"):
        title = _text(item, "title")
        link = _rss_item_link(item)
        guid = _text(item, "guid")
        author = _text(item, "creator") or _text(item, "author")
        if not title or not link:
            continue
        rows.append({
            "id": _item_id(feed_url, title, link, guid=guid),
            "title": title,
            "author": author or feed_name,
            "url": link,
            "site": _site_for_url(link, feed_name),
            "format": _format_for_url(link),
        })
    return rows


def _parse_atom(root, feed_url, feed_name):
    rows = []
    for entry in _children(root, "entry"):
        title = _text(entry, "title")
        link = _atom_link(entry)
        entry_id = _text(entry, "id")
        author = ""
        author_node = _child(entry, "author")
        if author_node is not None:
            author = _text(author_node, "name")
        if not title or not link:
            continue
        rows.append({
            "id": _item_id(feed_url, title, link, guid=entry_id),
            "title": title,
            "author": author or feed_name,
            "url": link,
            "site": _site_for_url(link, feed_name),
            "format": _format_for_url(link),
        })
    return rows


def parse_feed(xml_text, feed_url="feed", feed_name="RSS Feed"):
    root = ET.fromstring(xml_text)
    root_name = _local_name(root.tag).lower()
    if root_name == "feed":
        return _parse_atom(root, feed_url, feed_name)
    return _parse_rss(root, feed_url, feed_name)


def load_feeds(path=DEFAULT_FEEDS_PATH):
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    feeds = payload.get("feeds", payload) if isinstance(payload, dict) else payload
    normalized = []
    for feed in feeds:
        if isinstance(feed, str):
            feed = {"url": feed}
        url = str(feed.get("url") or "").strip()
        if not url:
            continue
        normalized.append({
            "url": url,
            "name": str(feed.get("name") or _site_for_url(url, "RSS Feed")),
            "trust_level": str(feed.get("trust_level") or "trusted"),
            "enabled": bool(feed.get("enabled", True)),
        })
    return normalized


def fetch_feed(feed, timeout=DEFAULT_TIMEOUT_SECONDS):
    response = requests.get(feed["url"], headers=scrapers.get_headers(), timeout=timeout)
    response.raise_for_status()
    return response.text


def archive_feed_items(feed, items, limit=DEFAULT_LIMIT_PER_FEED, dry_run=False):
    results = {"seen": 0, "archived": 0, "skipped": 0, "failed": 0}
    for item in items[:limit]:
        item_id = item["id"]
        if db.rss_item_seen(feed["url"], item_id):
            results["seen"] += 1
            continue
        if dry_run:
            results["skipped"] += 1
            continue
        try:
            work_id = db.add_work(
                item["title"],
                author=item.get("author") or feed["name"],
                search_query=f"rss:{feed['url']}",
            )
            db.add_file(
                work_id=work_id,
                site=item.get("site") or feed["name"],
                format=item.get("format") or "HTML",
                url=item["url"],
                download_source=f"RSS: {feed['name']}",
                download_url=item["url"],
                trust_level=feed.get("trust_level") or "trusted",
            )
            db.mark_rss_item_archived(feed["url"], item_id, item_url=item["url"], work_id=work_id)
        except Exception as exc:
            print(f"[!] RSS item failed: {item.get('title')}: {exc}")
            results["failed"] += 1
        else:
            results["archived"] += 1
    return results


def ingest_feeds(path=DEFAULT_FEEDS_PATH, limit_per_feed=DEFAULT_LIMIT_PER_FEED, dry_run=False, timeout=DEFAULT_TIMEOUT_SECONDS):
    summary = {"feeds": 0, "items": 0, "seen": 0, "archived": 0, "skipped": 0, "failed": 0}
    feeds = [feed for feed in load_feeds(path) if feed.get("enabled")]
    for feed in feeds:
        summary["feeds"] += 1
        print(f"[*] RSS feed: {feed['name']} <{feed['url']}>")
        try:
            xml_text = fetch_feed(feed, timeout=timeout)
            items = parse_feed(xml_text, feed_url=feed["url"], feed_name=feed["name"])
            results = archive_feed_items(feed, items, limit=limit_per_feed, dry_run=dry_run)
        except Exception as exc:
            print(f"[!] RSS feed failed: {feed['url']}: {exc}")
            summary["failed"] += 1
            continue
        summary["items"] += len(items[:limit_per_feed])
        for key in ("seen", "archived", "skipped", "failed"):
            summary[key] += results[key]
        print(f"    {results}")
    return summary
