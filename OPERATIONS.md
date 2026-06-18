# Archive Archiver Operations

This server runs Archive Archiver as an autonomous public-works ingestion node.
It discovers archive records, downloads source files into local buckets, extracts
plaintext, stores state in SQLite, and exposes a read-only API for dashboarding.

## Repository

- Path: `/root/archive-archiver`
- Python environment: `/root/archive-archiver/venv`
- Runtime config: `/root/archive-archiver/.env`
- Database: `/root/archive-archiver/archive_works.db`
- Logs: `/root/archive-archiver/logs/`

The `.env` file contains the OpenRouter API key and must stay uncommitted. The
current LLM parsing model is configured as:

```sh
OPENROUTER_MODEL=nvidia/nemotron-3-ultra-550b-a55b:free
```

## Data Flow

1. `cli.py collect` runs a repeated collection cycle.
2. Discovery queries Archive.org and The Anarchist Library.
3. Archive.org metadata is parsed directly.
4. The Anarchist Library pages are parsed with OpenRouter via `llm.py`.
5. File records are written to SQLite.
6. Downloader workers process pending files.
7. Downloads are grouped by domain: one sequential worker per domain.
8. Downloaded raw files are written under `bucket/raw`.
9. Text extraction writes plaintext under `bucket/text`.
10. `api.py` exposes read-only status and visualization endpoints.

## Local Buckets

```text
bucket/raw       raw downloaded files
bucket/text      extracted plaintext files
bucket/corpora   built corpus manifests and concatenated text
```

These are local filesystem-backed buckets. SQLite records the object URIs,
hashes, byte counts, extraction status, and corpus build metadata.

## Services

### Collector

Service name:

```sh
archive-collector.service
```

Installed unit:

```sh
/etc/systemd/system/archive-collector.service
```

Repo copy:

```sh
/root/archive-archiver/systemd/archive-collector.service
```

Current command:

```sh
/root/archive-archiver/venv/bin/python /root/archive-archiver/cli.py \
  --max-results 2 collect \
  --sleep-seconds 21600 \
  --download-limit 50 \
  --process-limit 50 \
  --max-domains 4 \
  --per-domain-limit 3 \
  --rps 0.05 \
  --sources archive_org anarchist_library
```

Important behavior:

- `--sleep-seconds 21600`: one cycle every 6 hours.
- `--max-domains 4`: up to four domain workers in a download phase.
- `--per-domain-limit 3`: at most three files per domain per cycle.
- `--rps 0.05`: one request every 20 seconds per domain.
- `--sources archive_org anarchist_library`: avoids Anna's Archive unless explicitly enabled.

Commands:

```sh
systemctl status archive-collector.service --no-pager
systemctl restart archive-collector.service
systemctl stop archive-collector.service
tail -f /root/archive-archiver/logs/collector.log
```

### Visualization API

Service name:

```sh
archive-api.service
```

Installed unit:

```sh
/etc/systemd/system/archive-api.service
```

Repo copy:

```sh
/root/archive-archiver/systemd/archive-api.service
```

The API listens on:

```text
http://127.0.0.1:8090
```

It is bound to `0.0.0.0:8090` in systemd, but local access should use
`127.0.0.1:8090` unless a reverse proxy is configured.

Commands:

```sh
systemctl status archive-api.service --no-pager
systemctl restart archive-api.service
tail -f /root/archive-archiver/logs/api.log
```

## API Endpoints

The visualization layer is a read-only FastAPI app in `api.py`. It connects to
SQLite in read-only mode and is intended as the backend for an external
dashboard. It does not mutate crawler state, download files, or trigger
collection work.

The API returns plain JSON and is organized around dashboard needs:

- summary counters for top-level KPI cards
- dimensions for filter dropdowns
- breakdowns for bar/pie/table visualizations
- time series for line charts
- recent activity feeds
- paginated drilldown tables for works and files

The service has permissive CORS by default:

```sh
ARCHIVE_API_CORS_ORIGINS=*
```

For production dashboard hosting, narrow this in
`systemd/archive-api.service` to the dashboard origin and reinstall/restart the
unit.

Health and summary:

```text
GET /health
GET /summary
GET /dimensions
GET /activity/recent
```

Visualization aggregates:

```text
GET /viz/breakdowns/sites
GET /viz/breakdowns/formats
GET /viz/breakdowns/categories
GET /viz/status/downloads
GET /viz/status/extractions
GET /viz/timeseries/works?bucket=day
GET /viz/timeseries/downloads?bucket=day
GET /viz/timeseries/extractions?bucket=day
```

Drilldown:

```text
GET /works?limit=50&offset=0
GET /works/{work_id}
GET /files?site=archive.org&limit=50
GET /corpora
```

Dashboard integration notes:

- Use `/summary` for headline cards such as works, files, downloaded bytes,
  extracted chars, processed texts, and pending download files.
- Use `/dimensions` to populate filters for `site`, `format`, `category`, and
  `search_query`.
- Use `/viz/breakdowns/sites` for source/domain comparison charts.
- Use `/viz/breakdowns/formats` to see which file formats are being discovered,
  downloaded, processed, skipped, or failing.
- Use `/viz/breakdowns/categories` after extraction to chart content
  categories by text count and character volume.
- Use `/viz/status/downloads` and `/viz/status/extractions` for health/status
  charts.
- Use `/viz/timeseries/*?bucket=day` for daily charts, or `bucket=hour` for
  short-range operational charts.
- Use `/works` and `/files` for paginated tables and drilldown pages.
- Use `/works/{work_id}` when a dashboard needs all file/download/extraction
  rows for one work.

Common filters:

```text
GET /works?q=philosophy&site=archive.org&limit=50&offset=0
GET /files?site=archive.org&download_status=downloaded&limit=50
GET /files?extraction_status=processed&limit=50
GET /viz/timeseries/downloads?bucket=hour&limit=72
```

Example checks:

```sh
curl -s http://127.0.0.1:8090/summary
curl -s http://127.0.0.1:8090/viz/breakdowns/sites
```

## Manual CLI Operations

Activate the environment:

```sh
cd /root/archive-archiver
. venv/bin/activate
```

Check database state:

```sh
python cli.py status
```

Run one collection cycle manually:

```sh
python cli.py --max-results 2 collect --once \
  --download-limit 50 \
  --process-limit 50 \
  --max-domains 4 \
  --per-domain-limit 3 \
  --rps 0.05 \
  --sources archive_org anarchist_library
```

Run only downloads with domain workers:

```sh
python cli.py download --domain-workers \
  --limit 50 \
  --max-domains 4 \
  --per-domain-limit 3 \
  --rps 0.05
```

Run only text extraction:

```sh
python cli.py process --limit 50
```

Build a corpus:

```sh
python cli.py corpus public-v1 --ordering title --limit 100
```

## Testing

Run all tests:

```sh
cd /root/archive-archiver
venv/bin/python -m unittest discover
```

Compile-check core modules:

```sh
venv/bin/python -m py_compile api.py cli.py db.py downloader.py llm.py processor.py corpus.py
```

## Operational Notes

- The collector is intentionally conservative. Increase `--max-domains`,
  `--per-domain-limit`, or `--rps` only if the target domains can tolerate it.
- Archive.org records do not require LLM parsing.
- The Anarchist Library path depends on `OPENROUTER_MODEL`.
- If Anarchist Library parsing starts failing, check `logs/collector.log` for
  OpenRouter model availability or JSON parse failures.
- Failed torrent downloads from Archive.org are expected occasionally. HTTP 403
  on `Archive BitTorrent` rows does not imply the whole cycle failed.
- Port `8080` is already occupied by a docker proxy on this server, so the API
  uses port `8090`.
- Runtime logs are ignored by git via `logs/`.
- The local database and buckets are ignored by git via `archive_works.db` and
  `bucket/`.

## Current Known Good State

As of the last setup pass:

- `archive-collector.service`: active
- `archive-api.service`: active
- API: `http://127.0.0.1:8090`
- OpenRouter model: `nvidia/nemotron-3-ultra-550b-a55b:free`
- Public sources enabled by default: `archive_org`, `anarchist_library`
