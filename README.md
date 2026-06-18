# Archive Archiver

Archive Archiver is a small CLI for assembling reproducible document corpora from public archive metadata. It has four stages:

1. Discover works and candidate files from archive search/detail pages.
2. Download selected files into a raw object bucket with rate limiting and hashes.
3. Extract plaintext into a text bucket with extractor/version provenance.
4. Build immutable corpus manifests and concatenated text files for model-training experiments.

The intended invariant is that corpus experiments are described by manifests, not by an informal folder of files. Ordering, substitutions, extractor versions, and source text hashes are recorded so runs can be compared cleanly.

## Setup

```sh
python3 -m venv venv
. venv/bin/activate
python -m pip install -r requirements.txt
cp .env.template .env
```

Set `OPENROUTER_API_KEY` in `.env` if you want LLM-assisted extraction from archive detail pages.

The direct dependencies are intentionally small and canonical:

- `requests`
- `beautifulsoup4`
- `openai`
- `python-dotenv`
- `pypdf`
- `EbookLib`

`pypdf` and `EbookLib` are used by the plaintext processor for PDF and EPUB files. If they are missing, those files are skipped with an explicit reason rather than silently misprocessed.

## Commands

Search archive sources and log discovered works/files:

```sh
python cli.py search "max stirner" --max-results 3
```

Run the broader research coordinator:

```sh
python cli.py research "19th century egoist philosophy" --max-results 2
```

Query the less-trusted Open SLUM mirror set:

```sh
python cli.py search "political economy" --sources slum_archives --max-results 2
```

`slum_archives` covers the mirrors listed by `https://open-slum.org/`,
including Anna's Archive mirrors, Libgen+ mirrors, Z-Library/info mirrors,
Liber3, and Memory of the World. Each mirror is queried independently so an
outage or unexpected page shape logs a warning and does not fail the whole run.

Download pending file rows into the raw bucket:

```sh
python cli.py download --limit 10 --rps 0.2 --max-mb 250
```

Failed downloads are recorded under `downloads.status = 'failed'` and reported
as failure metrics, but they are removed from the automatic pending queue.
Requeue a failed file by clearing or updating its download row intentionally
after review.

Downloads are written to quarantine first, scanned, and only then promoted to
the raw bucket. Files discovered through `slum_archives` are marked
`untrusted`; if `clamscan` is unavailable for an untrusted file, the download is
left in quarantine and marked failed instead of entering `bucket/raw`.

Archive processed raw originals to S3-compatible object storage:

```sh
python cli.py archive-raw --limit 50
```

Set `ARCHIVE_RAW_TO_S3=1` to run this automatically after successful plaintext
extraction. S3 credentials are read from `~/.s3` by default, and
`ARCHIVE_S3_BUCKET` selects the destination bucket.

Process downloaded raw objects into plaintext:

```sh
python cli.py process --limit 10
```

Build a deterministic corpus from processed plaintext:

```sh
python cli.py corpus egoism-v1 --query egoist --ordering title --limit 100
```

Build with deterministic random order and text substitutions:

```sh
python cli.py corpus egoism-randomized \
  --query egoist \
  --ordering random \
  --seed 42 \
  --substitutions-file substitutions.json
```

`substitutions.json` may be either:

```json
{
  "old phrase": "new phrase"
}
```

or:

```json
[
  {"from": "old phrase", "to": "new phrase"}
]
```

Show pipeline status:

```sh
python cli.py status
```

Open the terminal agent harness:

```sh
python cli.py agent
```

Or use the repo-local launcher:

```sh
bin/alge
```

For a global `alge` command on this server:

```sh
ln -sf /root/archive-archiver/bin/alge /usr/local/bin/alge
```

The harness gives you an `alge>` prompt for directing the pipeline without
remembering every full command. Anything typed without a leading slash is sent
to the active OpenRouter model as part of the ongoing conversation. The model
can call app tools for status, discovery, direct URL ingestion, research,
downloads, plaintext processing, raw-object archival, corpus builds, and backlog
draining. Natural-language requests can run continuous task loops:

```text
download all backlogged works and process them
drain the backlog, archive raw originals after text extraction, and tell me what stalled
search archive.org for public domain labor history and add the results
build a corpus called egoism-v1 from processed egoism texts
```

Common slash commands include:

```text
/status
/config
/model
/model qwen/qwen3.7-plus
/set sources archive_org anarchist_library
/search "max stirner" --max-results 3
/download --limit 10 --domain-workers
/process --limit 10
/cycle --query "public domain philosophy" --download-limit 20 --process-limit 20
/corpus egoism-v1 --query egoist --ordering title
/remember prioritize archive.org text formats
/memory --search archive.org
/context --refresh
/compact --force
/exit
```

Use `/model` to fetch OpenRouter's live model catalog and choose from a
paginated terminal picker. It shows 20 models at a time; use the arrow keys to
move between pages and enter a number to select. Use `/model MODEL_ID` when
running non-interactively.

Run one harness command without entering the prompt:

```sh
alge -c "/status"
```

## Harness Memory

The `alge` harness saves command context and operator notes to
`logs/agent_memory.jsonl`. Use slash commands to work with that memory:

```text
/model
/model qwen/qwen3.7-plus
/remember TEXT
/memory --limit 20
/memory --search TEXT
/context
/compact --force
/memory --clear
```

The harness estimates saved-context size and compacts automatically when memory
passes a configurable share of the active model context window. It uses
OpenRouter's public model metadata endpoint to read `context_length` when the
local estimate is large enough to need compaction, and falls back to a 32k-token
window if metadata is unavailable. You can tune this per session:

```text
/set model qwen/qwen3.7-plus
/set compaction-ratio 0.55
/set memory-path logs/agent_memory.jsonl
```

## Local Buckets

By default, artifacts are written under `bucket/`, which is ignored by git:

- `bucket/raw`: raw downloaded files
- `bucket/quarantine`: downloaded files awaiting or failing scan
- `bucket/text`: extracted plaintext
- `bucket/corpora`: corpus manifests and concatenated corpus text

When raw-object S3 archival is enabled, local raw originals are uploaded after
plaintext extraction and then deleted from `bucket/raw`; plaintext and corpus
artifacts remain filesystem-backed.

These are filesystem-backed buckets. The DB records object URIs and hashes so an S3, GCS, or R2 backend can replace the filesystem adapter later without changing the corpus manifest contract.

## Corpus Manifest Contract

Each corpus build writes:

- `manifest.json`: canonical recipe and ordered item list
- `corpus.txt`: concatenated text with document separators and source hashes

The manifest records:

- selection filters
- ordering strategy and seed
- normalizer version
- substitutions and substitutions hash
- source text SHA-256 values
- transformed text SHA-256 values
- item order

If a text file no longer matches the SHA-256 stored at extraction time, the corpus builder refuses to build.

## Tests

```sh
python -m unittest discover
```

The tests cover the SQLite state machine and deterministic corpus builds without network access.
