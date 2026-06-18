import json
import os
import re
import urllib.parse
from pathlib import Path

from bs4 import BeautifulSoup

import scrapers


DEFAULT_REGISTRY_PATH = os.getenv("ALGE_ARCHIVE_PLUGINS_PATH", "config/archive_plugins.json")


def slugify(value):
    slug = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    return slug or "archive"


def _registry_path(path=None):
    return Path(path or DEFAULT_REGISTRY_PATH)


def load_plugins(path=None):
    registry = _registry_path(path)
    if not registry.exists():
        return []
    with registry.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if isinstance(payload, dict):
        return list(payload.get("archives", []))
    if isinstance(payload, list):
        return payload
    return []


def save_plugins(plugins, path=None):
    registry = _registry_path(path)
    registry.parent.mkdir(parents=True, exist_ok=True)
    payload = {"archives": sorted(plugins, key=lambda row: row["slug"])}
    with registry.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")


def add_plugin(
    name,
    base_url,
    search_url_template=None,
    result_selector=None,
    link_selector=None,
    title_selector=None,
    trust_level="untrusted",
    enabled=True,
    path=None,
):
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("base_url must be an absolute http(s) URL")

    slug = slugify(name or parsed.netloc)
    base_url = base_url.rstrip("/")
    if not search_url_template:
        search_url_template = urllib.parse.urljoin(base_url + "/", "search?q={query}")

    plugin = {
        "slug": slug,
        "name": str(name or parsed.netloc),
        "base_url": base_url,
        "search_url_template": str(search_url_template),
        "result_selector": result_selector or "a[href]",
        "link_selector": link_selector or None,
        "title_selector": title_selector or None,
        "trust_level": trust_level or "untrusted",
        "enabled": bool(enabled),
        "strategy": "search-page-links",
    }

    plugins = [row for row in load_plugins(path) if row.get("slug") != slug]
    plugins.append(plugin)
    save_plugins(plugins, path)
    return plugin


def _format_search_url(template, query):
    encoded = urllib.parse.quote(query)
    return template.format(query=encoded, raw_query=query)


def _node_link(node, plugin, search_url):
    link_selector = plugin.get("link_selector")
    link_node = node.select_one(link_selector) if link_selector else None
    if link_node is None and getattr(node, "name", None) == "a":
        link_node = node
    if link_node is None:
        link_node = node.select_one("a[href]")
    if link_node is None:
        return None
    href = link_node.get("href")
    if not href:
        return None
    full_url = urllib.parse.urljoin(search_url, href)
    parsed = urllib.parse.urlparse(full_url)
    if parsed.scheme not in ("http", "https"):
        return None
    return full_url


def _node_title(node, plugin, fallback_url):
    title_selector = plugin.get("title_selector")
    title_node = node.select_one(title_selector) if title_selector else None
    title_source = title_node or node
    title = re.sub(r"\s+", " ", title_source.get_text(" ", strip=True)).strip()
    return title[:240] or fallback_url


def search_plugin(plugin, query, limit=10):
    if not plugin.get("enabled", True):
        return []
    search_url = _format_search_url(plugin["search_url_template"], query)
    html = scrapers.fetch_url(search_url, retries=1, delay=0.2)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    rows = []
    seen = set()
    for node in soup.select(plugin.get("result_selector") or "a[href]"):
        url = _node_link(node, plugin, search_url)
        if not url or url in seen:
            continue
        seen.add(url)
        rows.append({
            "title": _node_title(node, plugin, url),
            "url": url,
            "site": urllib.parse.urlparse(url).netloc,
            "source_name": plugin.get("name") or plugin.get("slug"),
            "plugin_slug": plugin.get("slug"),
            "trust_level": plugin.get("trust_level") or "untrusted",
        })
        if len(rows) >= limit:
            break
    return rows


def search_plugins(query, slugs=None, limit_per_archive=10, path=None):
    requested = set(slugs or [])
    rows = []
    for plugin in load_plugins(path):
        if requested and plugin.get("slug") not in requested:
            continue
        try:
            rows.extend(search_plugin(plugin, query, limit=limit_per_archive))
        except Exception as exc:
            rows.append({
                "title": f"{plugin.get('name') or plugin.get('slug')} search failed",
                "url": plugin.get("base_url"),
                "site": urllib.parse.urlparse(plugin.get("base_url", "")).netloc,
                "source_name": plugin.get("name") or plugin.get("slug"),
                "plugin_slug": plugin.get("slug"),
                "trust_level": plugin.get("trust_level") or "untrusted",
                "error": str(exc),
            })
    return [row for row in rows if not row.get("error")]
