import hashlib
import json
import os
from pathlib import Path
import random
import re
import urllib.parse

import db


NORMALIZER_VERSION = "corpus-normalize.v1"
DEFAULT_CORPUS_BUCKET_DIR = os.getenv("ARCHIVE_CORPUS_BUCKET_DIR", "bucket/corpora")


class CorpusBuildError(ValueError):
    pass


def _safe_segment(value, fallback="corpus"):
    value = str(value or fallback).strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = value.strip(".-")
    return value[:96] or fallback


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _path_from_file_uri(uri):
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "file":
        raise CorpusBuildError("corpus builder currently reads file:// text bucket URIs only")
    return Path(urllib.parse.unquote(parsed.path))


def load_substitutions(path=None):
    if not path:
        substitutions = []
        return substitutions, _sha256_text(_canonical_json(substitutions))

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if isinstance(raw, dict):
        substitutions = [{"from": str(k), "to": str(v)} for k, v in raw.items()]
    elif isinstance(raw, list):
        substitutions = []
        for item in raw:
            if not isinstance(item, dict) or "from" not in item or "to" not in item:
                raise CorpusBuildError("substitution list entries must contain 'from' and 'to'")
            substitutions.append({"from": str(item["from"]), "to": str(item["to"])})
    else:
        raise CorpusBuildError("substitutions file must be a JSON object or list")

    return substitutions, _sha256_text(_canonical_json(substitutions))


def normalize_text(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def apply_substitutions(text, substitutions):
    for substitution in substitutions:
        text = text.replace(substitution["from"], substitution["to"])
    return text


def _sort_candidates(rows, ordering_strategy, seed):
    rows = list(rows)
    if ordering_strategy == "title":
        rows.sort(key=lambda row: (
            str(row.get("title") or "").lower(),
            str(row.get("author") or "").lower(),
            str(row.get("text_sha256") or ""),
        ))
    elif ordering_strategy == "hash":
        rows.sort(key=lambda row: str(row.get("text_sha256") or ""))
    elif ordering_strategy == "created":
        rows.sort(key=lambda row: int(row.get("extraction_id") or 0))
    elif ordering_strategy == "random":
        rows.sort(key=lambda row: str(row.get("text_sha256") or ""))
        random.Random(seed).shuffle(rows)
    else:
        raise CorpusBuildError(f"unknown ordering strategy: {ordering_strategy}")
    return rows


def _read_text(row):
    path = _path_from_file_uri(row["text_uri"])
    text = path.read_text(encoding="utf-8")
    actual_sha256 = _sha256_text(normalize_text(text))
    if row.get("text_sha256") and actual_sha256 != row["text_sha256"]:
        raise CorpusBuildError(
            f"text hash mismatch for extraction {row['extraction_id']}: "
            f"expected {row['text_sha256']} got {actual_sha256}"
        )
    return text


def _combined_text(items):
    parts = []
    for item in items:
        header = [
            f"--- BEGIN DOCUMENT {item['item_index']} ---",
            f"title: {item['title']}",
            f"author: {item.get('author') or ''}",
            f"source_text_sha256: {item['text_sha256']}",
            f"transformed_sha256: {item['transformed_sha256']}",
            "",
        ]
        parts.append("\n".join(header) + item["text"] + f"\n--- END DOCUMENT {item['item_index']} ---")
    return "\n\n".join(parts) + "\n"


def build_corpus(
    name,
    category=None,
    site=None,
    query=None,
    ordering_strategy="title",
    seed=0,
    limit=None,
    substitutions_path=None,
    output_dir=DEFAULT_CORPUS_BUCKET_DIR,
    use_munged=False,
):
    selection = {
        "category": category,
        "site": site,
        "query": query,
        "limit": limit,
        "use_munged": use_munged,
    }
    substitutions, substitutions_sha256 = load_substitutions(substitutions_path)
    candidates = db.get_processed_extractions(
        category=category,
        site=site,
        query=query,
        limit=limit,
        use_munged=use_munged,
    )
    candidates = _sort_candidates(candidates, ordering_strategy, seed)
    if not candidates:
        raise CorpusBuildError("no processed text extractions matched the requested corpus selection")

    items = []
    total_chars = 0
    for index, row in enumerate(candidates, start=1):
        source_text = _read_text(row)
        transformed = apply_substitutions(normalize_text(source_text), substitutions)
        transformed_sha256 = _sha256_text(transformed)
        total_chars += len(transformed)
        items.append({
            "item_index": index,
            "extraction_id": row["extraction_id"],
            "work_id": row["work_id"],
            "title": row["title"],
            "author": row.get("author"),
            "site": row.get("site"),
            "format": row.get("format"),
            "category": row.get("category"),
            "text_uri": row["text_uri"],
            "text_sha256": row["text_sha256"],
            "transformed_sha256": transformed_sha256,
            "char_count": len(transformed),
            "text": transformed,
        })

    manifest_items = [
        {key: value for key, value in item.items() if key != "text"}
        for item in items
    ]
    manifest = {
        "format": "archive-archiver.corpus.v1",
        "name": name,
        "selection": selection,
        "ordering_strategy": ordering_strategy,
        "seed": seed,
        "normalizer_version": NORMALIZER_VERSION,
        "substitutions_sha256": substitutions_sha256,
        "substitutions": substitutions,
        "item_count": len(items),
        "total_chars": total_chars,
        "items": manifest_items,
    }
    manifest_json = _canonical_json(manifest)
    manifest_sha256 = _sha256_text(manifest_json)

    build_dir = Path(output_dir) / _safe_segment(name) / manifest_sha256[:16]
    build_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = build_dir / "manifest.json"
    corpus_path = build_dir / "corpus.txt"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    corpus_path.write_text(_combined_text(items), encoding="utf-8")

    spec_id = db.upsert_corpus_spec(
        name=name,
        selection_json=_canonical_json(selection),
        ordering_strategy=ordering_strategy,
        normalizer_version=NORMALIZER_VERSION,
        substitutions_sha256=substitutions_sha256,
        substitutions_json=_canonical_json(substitutions),
    )
    build_id = db.add_corpus_build(
        spec_id=spec_id,
        manifest_sha256=manifest_sha256,
        manifest_uri=manifest_path.resolve().as_uri(),
        corpus_uri=corpus_path.resolve().as_uri(),
        item_count=len(items),
        total_chars=total_chars,
        items=manifest_items,
    )

    return {
        "build_id": build_id,
        "spec_id": spec_id,
        "manifest_sha256": manifest_sha256,
        "manifest_path": str(manifest_path.resolve()),
        "corpus_path": str(corpus_path.resolve()),
        "item_count": len(items),
        "total_chars": total_chars,
    }
