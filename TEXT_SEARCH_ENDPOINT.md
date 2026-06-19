# Text Search Endpoint Guide

This repo exposes a full-text search endpoint for the extracted text viewer:

```text
GET /texts/search
```

The endpoint is backend-only. It is intended for the frontend text browser, but
it can also be used directly by a Codex instance when checking archived text.

## Quick Start

Local service URL:

```text
http://127.0.0.1:8090/texts/search?q=thelema&limit=20
```

Public API URL, when routed through the deployed domain:

```text
https://api.ufotoken.app/texts/search?q=thelema&limit=20
```

Useful curl examples:

```sh
curl -s 'http://127.0.0.1:8090/texts/search?q=thelema&limit=5'
curl -s 'http://127.0.0.1:8090/texts/search?q=golden%20dawn&mode=phrase'
curl -s 'http://127.0.0.1:8090/texts/search?q=ritual%20magick&mode=all&category=occult'
curl -s 'http://127.0.0.1:8090/texts/search?q=mutual%20aid&mode=any&site=archive.org'
```

## Query Parameters

- `q`: required search string.
- `mode`: optional search parser mode. Defaults to `auto`.
- `site`: optional exact source/domain filter, for example `archive.org`.
- `category`: optional exact category filter, for example `philosophy`.
- `quality`: optional exact text quality filter, for example `usable` or
  `unvalidated`.
- `status`: optional extraction status filter. Defaults to `processed`.
- `limit`: result count, 1-500. Defaults to 50.
- `offset`: pagination offset. Defaults to 0.

## Search Modes

- `auto`: safe default. Quoted phrases are preserved, unquoted terms are joined
  with `AND`.
- `all`: all terms must match.
- `any`: at least one term must match.
- `phrase`: the entire query is searched as an exact phrase.
- `match`: raw SQLite FTS5 syntax. Use this only when you need advanced FTS5
  operators and can handle 400 responses for invalid syntax.

Examples:

```text
q=golden dawn&mode=phrase
q=thelema ritual&mode=all
q=thelema anarchism&mode=any
q=title:thelema OR body:ritual&mode=match
```

## Response Shape

The response is plain JSON:

```json
{
  "query": "thelema",
  "match_query": "thelema",
  "mode": "auto",
  "total": 14,
  "limit": 3,
  "offset": 0,
  "results": []
}
```

Each result includes the same metadata used by `/texts`, plus:

- `rank`: SQLite FTS5 BM25 score. Lower is better.
- `title_snippet`: title with `<mark>` highlights.
- `body_snippet`: matching body context with `<mark>` highlights.

The full text is not returned by search. Use the returned `extraction_id` with:

```text
GET /texts/{extraction_id}
```

## Indexing Behavior

The search index is SQLite FTS5 and lives in the same database:

- `text_search_index`: virtual FTS5 table.
- `text_search_meta`: indexed extraction metadata and text hash.

New works are indexed automatically when `db.mark_extraction_succeeded(...)`
runs. That is the path used by `processor.py` after plaintext extraction
finishes.

Unusable or rejected texts are removed from the index when:

- `db.mark_text_quality(extraction_id, "unusable", ...)` runs.
- `db.reject_text_extraction(...)` runs.
- an extraction is marked `failed` or `skipped`.

The search endpoint also performs an incremental sync before querying, so stale
or missing FTS rows are repaired on read.

## Maintenance Commands

Initialize or rebuild missing/stale index rows:

```sh
cd /root/archive-archiver
python3 - <<'PY'
import db
db.init_db()
print(db.sync_text_search_index())
PY
```

Smoke test the live API:

```sh
curl -s 'http://127.0.0.1:8090/texts/search?q=thelema&limit=3'
```

Relevant tests:

```sh
python3 -m unittest tests.test_api
python3 -m unittest discover
```
