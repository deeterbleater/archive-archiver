import hashlib
import json
import os
from pathlib import Path
import re
import unicodedata
import urllib.parse

import db
import llm
import terminal_theme


MUNGER_VERSION = "text-munger.v1"
DEFAULT_MUNGED_BUCKET_DIR = os.getenv("ARCHIVE_MUNGED_TEXT_BUCKET_DIR", "bucket/munged-text")
DEFAULT_MUNGER_MODEL = os.getenv("ALGE_MUNGER_MODEL", os.getenv("OPENROUTER_MODEL", llm.DEFAULT_MODEL))

LIGATURES = {
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
}
PUNCTUATION = {
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u2013": "-",
    "\u2014": "--",
    "\u00a0": " ",
}


class MungeError(ValueError):
    pass


def _sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _path_from_file_uri(uri):
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "file":
        raise MungeError("text munger currently reads file:// text bucket URIs only")
    return Path(urllib.parse.unquote(parsed.path))


def _safe_segment(value, fallback="unknown"):
    value = str(value or fallback).strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = value.strip(".-")
    return value[:96] or fallback


def _storage_key(row, text_sha256):
    site = _safe_segment(row.get("site"))
    work_id = _safe_segment(row.get("work_id"), "work")
    extraction_id = _safe_segment(row.get("extraction_id"), "extraction")
    return f"{site}/{work_id}/{extraction_id}/{text_sha256[:16]}.txt"


def normalize_unicode(text):
    for source, replacement in LIGATURES.items():
        text = text.replace(source, replacement)
    for source, replacement in PUNCTUATION.items():
        text = text.replace(source, replacement)
    text = unicodedata.normalize("NFKC", text)
    text = "".join(
        ch for ch in text
        if ch in "\n\t" or (unicodedata.category(ch)[0] != "C" and ch != "\ufffd")
    )
    return text


def strip_boilerplate(text):
    markers = [
        ("*** START OF THE PROJECT GUTENBERG", "*** END OF THE PROJECT GUTENBERG"),
        ("*** START OF THIS PROJECT GUTENBERG", "*** END OF THIS PROJECT GUTENBERG"),
    ]
    upper = text.upper()
    for start_marker, end_marker in markers:
        start = upper.find(start_marker)
        end = upper.find(end_marker)
        if start >= 0:
            line_end = text.find("\n", start)
            text = text[line_end + 1 if line_end >= 0 else start:]
            upper = text.upper()
        if end >= 0:
            text = text[:end]
            upper = text.upper()
    return text


def remove_repeated_running_lines(lines):
    counts = {}
    for line in lines:
        key = re.sub(r"\d+", "#", line.strip().lower())
        if 0 < len(key) <= 90:
            counts[key] = counts.get(key, 0) + 1
    repeated = {key for key, count in counts.items() if count >= 3}
    cleaned = []
    removed = 0
    for line in lines:
        stripped = line.strip()
        key = re.sub(r"\d+", "#", stripped.lower())
        if re.fullmatch(r"[-–— ]*\d+[-–— ]*", stripped):
            removed += 1
            continue
        if key in repeated and len(stripped) <= 90:
            removed += 1
            continue
        cleaned.append(line)
    return cleaned, removed


def dehyphenate(text):
    return re.sub(r"([A-Za-z]{2,})-\n([a-z]{2,})", r"\1\2", text)


def unwrap_paragraphs(text):
    paragraphs = re.split(r"\n{2,}", text)
    unwrapped = []
    for paragraph in paragraphs:
        lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
        if not lines:
            continue
        if len(lines) == 1:
            unwrapped.append(lines[0])
            continue
        if any(re.match(r"^(\s*[-*•]|\s*\d+[.)])\s+", line) for line in lines):
            unwrapped.append("\n".join(lines))
            continue
        if all(len(line) <= 72 for line in lines) and len(lines) <= 8:
            unwrapped.append("\n".join(lines))
            continue
        unwrapped.append(" ".join(lines))
    return "\n\n".join(unwrapped)


def deterministic_clean(text):
    original = text
    text = normalize_unicode(text)
    text = strip_boilerplate(text)
    text = dehyphenate(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    lines, removed_running_lines = remove_repeated_running_lines(text.splitlines())
    text = "\n".join(lines)
    text = unwrap_paragraphs(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text).strip()
    stats = {
        "original_chars": len(original),
        "cleaned_chars": len(text),
        "removed_running_lines": removed_running_lines,
        "replacement_chars_removed": original.count("\ufffd"),
    }
    return text, stats


def suspicious_lines(text, limit=60):
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        odd = (
            "\ufffd" in stripped
            or len(re.findall(r"[^A-Za-z0-9\s.,;:'\"!?()\[\]{}\-]", stripped)) >= 3
            or re.search(r"\b\w\s+\w\s+\w\s+\w\b", stripped)
            or re.search(r"[A-Za-z]{2,}\|[A-Za-z]{2,}", stripped)
        )
        if odd:
            lines.append(stripped[:240])
        if len(lines) >= limit:
            break
    return lines


def _extract_json_array(response):
    response = response.strip()
    decoder = json.JSONDecoder()
    if response.startswith("["):
        value, _end = decoder.raw_decode(response)
        return value
    match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", response, re.S)
    if match:
        value, _end = decoder.raw_decode(match.group(1))
        return value
    start = response.find("[")
    if start >= 0:
        value, _end = decoder.raw_decode(response[start:])
        return value
    raise MungeError("model did not return a JSON rule array")


def propose_model_rules(text, model=DEFAULT_MUNGER_MODEL):
    sample = suspicious_lines(text)
    if not sample:
        return []
    prompt = {
        "suspicious_lines": sample,
        "allowed_rule_schema": {
            "type": "literal or regex",
            "from": "exact string or conservative regex",
            "to": "replacement string",
            "reason": "short reason",
            "max_replacements": "optional integer, default 1000",
        },
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You are a corpus text-cleaning analyst. Return only JSON. "
                "Suggest surgical cleanup rules for OCR/extraction artifacts. "
                "Do not rewrite prose, modernize spelling, summarize, censor, or change meaning."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(prompt, ensure_ascii=False),
        },
    ]
    response = llm.chat_with_llm(messages, model=model, temperature=0.0)
    rules = _extract_json_array(response)
    return validate_rules(rules)


def validate_rules(rules):
    if not isinstance(rules, list):
        raise MungeError("model rules must be a list")
    validated = []
    for rule in rules[:50]:
        if not isinstance(rule, dict):
            continue
        rule_type = rule.get("type")
        source = str(rule.get("from") or "")
        replacement = str(rule.get("to") or "")
        if rule_type not in ("literal", "regex") or not source or len(source) > 200:
            continue
        if rule_type == "regex":
            try:
                re.compile(source)
            except re.error:
                continue
        validated.append({
            "type": rule_type,
            "from": source,
            "to": replacement,
            "reason": str(rule.get("reason") or "")[:200],
            "max_replacements": max(1, min(int(rule.get("max_replacements") or 1000), 10000)),
        })
    return validated


def apply_rules(text, rules):
    applied = []
    for rule in rules:
        before = text
        if rule["type"] == "literal":
            count = before.count(rule["from"])
            if count:
                text = before.replace(rule["from"], rule["to"], rule["max_replacements"])
                applied_count = min(count, rule["max_replacements"])
            else:
                applied_count = 0
        else:
            text, applied_count = re.subn(rule["from"], rule["to"], before, count=rule["max_replacements"])
        if applied_count:
            applied.append({**rule, "applied": applied_count})
    return text, applied


def munge_text(text, use_llm=False, model=DEFAULT_MUNGER_MODEL):
    text, stats = deterministic_clean(text)
    rules = []
    if use_llm:
        rules = propose_model_rules(text, model=model)
        text, applied = apply_rules(text, rules)
        text, more_stats = deterministic_clean(text)
        stats["model_rules_returned"] = len(rules)
        stats["model_rules_applied"] = len(applied)
        rules = applied
    stats["final_chars"] = len(text)
    return text, stats, rules


def munge_row(row, bucket_dir=DEFAULT_MUNGED_BUCKET_DIR, use_llm=False, model=DEFAULT_MUNGER_MODEL, dry_run=False):
    source_path = _path_from_file_uri(row["text_uri"])
    source = source_path.read_text(encoding="utf-8", errors="replace")
    source_sha256 = _sha256_text(source.strip())
    munged, stats, rules = munge_text(source, use_llm=use_llm, model=model)
    if not munged:
        raise MungeError("munger produced empty text")
    munged_sha256 = _sha256_text(munged)
    storage_key = _storage_key(row, munged_sha256)
    final_path = Path(bucket_dir) / storage_key
    if not dry_run:
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_text(munged + "\n", encoding="utf-8")
        db.mark_text_munge_succeeded(
            extraction_id=row["extraction_id"],
            munger_version=MUNGER_VERSION,
            source_text_sha256=source_sha256,
            munged_text_uri=final_path.resolve().as_uri(),
            munged_text_sha256=munged_sha256,
            char_count=len(munged),
            rules_json=json.dumps(rules, ensure_ascii=False, sort_keys=True),
            stats_json=json.dumps(stats, ensure_ascii=False, sort_keys=True),
            model=model if use_llm else None,
        )
    return {
        "extraction_id": row["extraction_id"],
        "title": row.get("title"),
        "source_chars": len(source),
        "munged_chars": len(munged),
        "munged_text_uri": final_path.resolve().as_uri(),
        "stats": stats,
        "rules": rules,
    }


def munge_pending(limit=10, bucket_dir=DEFAULT_MUNGED_BUCKET_DIR, use_llm=False, model=DEFAULT_MUNGER_MODEL, include_munged=False, dry_run=False):
    rows = db.get_munge_candidates(limit=limit, munger_version=MUNGER_VERSION, include_munged=include_munged)
    results = {"processed": 0, "failed": 0, "skipped": 0}
    for row in rows:
        terminal_theme.print_pip("pending", f"munge extraction {row['extraction_id']}: {row.get('title')}")
        try:
            result = munge_row(row, bucket_dir=bucket_dir, use_llm=use_llm, model=model, dry_run=dry_run)
            results["processed"] += 1
            terminal_theme.print_pip("success", f"munged {result['source_chars']} -> {result['munged_chars']} chars")
        except Exception as exc:
            results["failed"] += 1
            if not dry_run:
                db.mark_text_munge_failed(row["extraction_id"], MUNGER_VERSION, exc, model=model if use_llm else None)
            terminal_theme.print_pip("failed", f"munge failed: {exc}")
    return results
