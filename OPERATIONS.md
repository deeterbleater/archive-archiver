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
5. Optional less-trusted discovery queries the Open SLUM mirror set.
6. File records are written to SQLite.
7. Downloader workers process pending files.
8. Downloads are grouped by domain: one sequential worker per domain.
9. Incoming bytes are written under `bucket/quarantine` first.
10. Clean scanned files are promoted into `bucket/raw`.
11. Text extraction writes plaintext under `bucket/text`.
12. `api.py` exposes read-only status and visualization endpoints.

## Local Buckets

```text
bucket/raw       raw downloaded files
bucket/quarantine downloaded files before scan/promotion
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

The API listens locally on:

```text
http://127.0.0.1:8090
```

It is bound to `0.0.0.0:8090` in systemd, but direct local checks should use
`127.0.0.1:8090`.

The public HTTPS route on this server is:

```text
https://api.ufotoken.app
```

Nginx owns TLS for that domain and proxies requests to
`http://127.0.0.1:8090`. This domain previously routed to another completed
project on port `8080`; the route is now available for Archive Archiver's
read-only visualization API.

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
curl -s https://api.ufotoken.app/summary
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

Open the terminal agent harness:

```sh
python cli.py agent
```

Or use the repo-local launcher:

```sh
/root/archive-archiver/bin/alge
```

Install a global `alge` command on this server:

```sh
ln -sf /root/archive-archiver/bin/alge /usr/local/bin/alge
```

Useful harness input:

```text
what should I collect next for an egoist corpus?
/status
/config
/model
/model qwen/qwen3.7-plus
/set max-results 2
/set sources archive_org anarchist_library
/search "public domain political economy"
/download --limit 50 --domain-workers --max-domains 4 --per-domain-limit 3 --rps 0.05
/process --limit 50
/cycle --query "public domain philosophy" --download-limit 50 --process-limit 50
/corpus public-v1 --ordering title --limit 100
/remember focus next cycle on OCR Search Text
/memory --limit 20
/context --refresh
/compact --force
/exit
```

Run one harness command non-interactively:

```sh
alge -c "/status"
```

Lines without a leading slash are sent directly to the active OpenRouter model
and saved into the continuing context log with assistant replies. Slash commands
remain operational controls. `/model` fetches the live OpenRouter model catalog,
shows 20 entries per page, and accepts arrow-key page navigation plus numeric
selection; `/model MODEL_ID` sets a model directly.

The chat model can call app tools for status, backlog inspection, search, direct
URL ingestion, research, downloads, plaintext processing, raw-object archival,
and corpus builds. For requests such as `download all backlogged works and
process them`, it should call the continuous backlog loop and keep iterating
until the backlog is complete, progress stalls, or a tool reports a concrete
error.

Harness memory:

- Saved context log: `logs/agent_memory.jsonl`
- OpenRouter model metadata cache: `logs/openrouter_models.json`
- Default fallback context window: `32768` estimated tokens
- Default automatic compaction threshold: `55%` of the context window

Useful memory commands:

```text
/model
/model qwen/qwen3.7-plus
/remember TEXT
/memory --search TEXT
/context
/context --refresh
/compact --force
/memory --clear
/set compaction-ratio 0.50
/set memory-path logs/agent_memory.jsonl
```

The harness checks OpenRouter's public model metadata for `context_length` once
memory grows beyond the conservative fallback threshold. If the metadata call
fails, compaction still works with the fallback window and a local deterministic
summary.

Run only downloads with domain workers:

```sh
python cli.py download --domain-workers \
  --limit 50 \
  --max-domains 4 \
  --per-domain-limit 3 \
  --rps 0.05
```

Run discovery against less-trusted Open SLUM mirrors:

```sh
python cli.py search "political economy" --sources slum_archives --max-results 2
```

The `slum_archives` source uses the mirror list published by
`https://open-slum.org/`: Anna's Archive mirrors, Libgen+ mirrors,
Z-Library/info mirrors, Liber3, and Memory of the World. Each mirror is isolated
with short retries; a down host returns no candidates for that host without
failing the run.

Quarantine and scanning:

- Quarantine bucket: `bucket/quarantine`
- Raw bucket: `bucket/raw`
- Scanner command: `clamscan` from `PATH`, or `ARCHIVE_CLAMSCAN_BIN`
- Trusted sources may continue if the scanner is unavailable.
- `slum_archives` files are marked `untrusted` and fail closed if scanning is
  unavailable.
- Detected malware remains in quarantine and is not promoted into `bucket/raw`.

Run only text extraction:

```sh
python cli.py process --limit 50
```

Archive already-processed raw originals to S3-compatible object storage:

```sh
python cli.py archive-raw --limit 50
```

Raw object archival:

- Enable automatic archival with `ARCHIVE_RAW_TO_S3=1`.
- Credentials/config are read from `ARCHIVE_S3_CONFIG`, defaulting to
  `/root/.s3`.
- Destination bucket is `ARCHIVE_S3_BUCKET` or `ARCHIVE_RAW_OBJECT_BUCKET`.
- Destination prefix defaults to `raw-originals`.
- A raw file is uploaded only after plaintext extraction has succeeded.
- The local raw file is deleted only after the upload succeeds.
- SQLite records `raw_archive_uri`, `raw_archive_status`, `raw_archived_at`,
  and `local_raw_deleted_at`.

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
- Failed downloads are retained as metrics and review records, but they fall
  out of the automatic pending queue. Requeue only after intentional review by
  clearing or updating the corresponding `downloads` row.
- Port `8080` is already occupied by a docker proxy on this server, so the API
  uses port `8090`. Nginx routes `https://api.ufotoken.app` to port `8090`.
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
