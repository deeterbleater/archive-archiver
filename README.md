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

Download pending file rows into the raw bucket:

```sh
python cli.py download --limit 10 --rps 0.2 --max-mb 250
```

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

## Local Buckets

By default, artifacts are written under `bucket/`, which is ignored by git:

- `bucket/raw`: raw downloaded files
- `bucket/text`: extracted plaintext
- `bucket/corpora`: corpus manifests and concatenated corpus text

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
