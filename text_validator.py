import json
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import db
import llm
import terminal_theme


DEFAULT_VALIDATOR_MODEL = "minimax/minimax-m3"
MAX_VALIDATION_SAMPLE_CHARS = 6000


def _path_from_file_uri(uri):
    parsed = urllib.parse.urlparse(uri or "")
    if parsed.scheme != "file":
        raise ValueError("text validation only supports local file:// text artifacts")
    return Path(urllib.parse.unquote(parsed.path))


def read_text_artifact(uri, max_chars=None):
    path = _path_from_file_uri(uri)
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[:max_chars] if max_chars else text


def _control_ratio(text):
    if not text:
        return 1.0
    allowed = {"\n", "\r", "\t", "\f"}
    controls = sum(1 for char in text if (ord(char) < 32 and char not in allowed) or 127 <= ord(char) <= 159)
    return controls / max(1, len(text))


def _replacement_ratio(text):
    if not text:
        return 1.0
    return text.count("\ufffd") / max(1, len(text))


def _wordish_count(text):
    return len(re.findall(r"[\w\u00c0-\uffff]{3,}", text, flags=re.UNICODE))


def heuristic_quality(text):
    sample = text[:MAX_VALIDATION_SAMPLE_CHARS]
    stripped = sample.strip()
    if not stripped:
        return {
            "status": "unusable",
            "score": 0.0,
            "reason": "empty text artifact",
            "needs_llm": False,
        }
    control_ratio = _control_ratio(sample)
    replacement_ratio = _replacement_ratio(sample)
    wordish = _wordish_count(sample)
    if sample.startswith("\x1f\ufffd") or sample.startswith("\x1f\x8b") or sample.startswith("\x8b") or "\x00" in sample[:200]:
        return {
            "status": "unusable",
            "score": 0.02,
            "reason": "artifact appears to contain compressed or binary bytes",
            "needs_llm": False,
        }
    if control_ratio > 0.03:
        return {
            "status": "unusable",
            "score": 0.05,
            "reason": f"too many control characters ({control_ratio:.1%})",
            "needs_llm": False,
        }
    if replacement_ratio > 0.02:
        return {
            "status": "unusable",
            "score": 0.10,
            "reason": f"too many replacement characters ({replacement_ratio:.1%})",
            "needs_llm": False,
        }
    if len(stripped) < 120 or wordish < 12:
        return {
            "status": "suspect",
            "score": 0.45,
            "reason": "very little readable prose detected",
            "needs_llm": True,
        }
    return {
        "status": "usable",
        "score": 0.75,
        "reason": "local checks found readable text",
        "needs_llm": True,
    }


def llm_quality(row, text, model=DEFAULT_VALIDATOR_MODEL):
    sample = text[:MAX_VALIDATION_SAMPLE_CHARS]
    prompt = f"""
Decide whether this extracted archive text is legible enough for a human to read.

Non-English natural language is usable. OCR noise is acceptable if the document is mostly readable.
Compressed bytes, encrypted text, binary data, mojibake garbage, repeated extraction artifacts, or mostly unreadable character soup are unusable.

Metadata:
Title: {row.get("title") or "unknown"}
Author: {row.get("author") or "unknown"}
Site: {row.get("site") or "unknown"}
Format: {row.get("format") or "unknown"}
Extractor warnings: {row.get("warnings") or "none"}

Text sample:
{sample}

Return only JSON:
{{
  "usable": true,
  "score": 0.0,
  "reason": "brief explanation"
}}
"""
    response = llm.chat_completion(
        [
            {"role": "system", "content": "You are a strict plaintext quality-control validator. Output only JSON."},
            {"role": "user", "content": prompt},
        ],
        model=model,
        temperature=0.0,
    )
    raw = response.choices[0].message.content.strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    payload = _parse_json_object(match.group(1) if match else raw)
    usable = bool(payload.get("usable"))
    score = float(payload.get("score", 1.0 if usable else 0.0))
    return {
        "status": "usable" if usable else "unusable",
        "score": max(0.0, min(1.0, score)),
        "reason": str(payload.get("reason") or ("legible" if usable else "not legible"))[:1000],
        "model": model,
    }


def _parse_json_object(raw):
    decoder = json.JSONDecoder()
    text = str(raw or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise
        payload, _end = decoder.raw_decode(text[start:])
        return payload


def validate_text(row, model=DEFAULT_VALIDATOR_MODEL, use_llm=True):
    text = read_text_artifact(row["text_uri"])
    heuristic = heuristic_quality(text)
    if not use_llm or not heuristic["needs_llm"]:
        return {
            "status": "usable" if heuristic["status"] == "suspect" else heuristic["status"],
            "score": heuristic["score"],
            "reason": heuristic["reason"],
            "model": "local-heuristic",
        }
    try:
        return llm_quality(row, text, model=model)
    except Exception as exc:
        if heuristic["status"] == "suspect":
            return {
                "status": "error",
                "score": heuristic["score"],
                "reason": f"LLM validation failed after suspect local checks: {type(exc).__name__}: {exc}",
                "model": model,
            }
        return {
            "status": heuristic["status"],
            "score": heuristic["score"],
            "reason": f"{heuristic['reason']}; LLM validation failed: {type(exc).__name__}: {exc}",
            "model": model,
        }


def _validate_candidate(row, model, use_llm):
    try:
        result = validate_text(row, model=model, use_llm=use_llm)
    except Exception as exc:
        result = {
            "status": "error",
            "score": 0.0,
            "reason": f"{type(exc).__name__}: {exc}",
            "model": model,
        }
    db.mark_text_quality(
        row["extraction_id"],
        result["status"],
        score=result.get("score"),
        reason=result.get("reason"),
        model=result.get("model") or model,
    )
    if result["status"] == "unusable":
        _remove_unusable_text(row, result.get("reason"))
    return row, result


def _remove_unusable_text(row, reason=None):
    removed = db.reject_text_extraction(row["extraction_id"], reason=reason)
    uri = (removed or {}).get("text_uri") or row.get("text_uri")
    parsed = urllib.parse.urlparse(uri or "")
    if parsed.scheme == "file":
        path = Path(urllib.parse.unquote(parsed.path))
        path.unlink(missing_ok=True)


def validate_pending(
    limit=10,
    model=DEFAULT_VALIDATOR_MODEL,
    include_validated=False,
    use_llm=True,
    verbose=False,
    workers=1,
):
    rows = db.get_text_quality_candidates(limit=limit, include_validated=include_validated)
    results = {"usable": 0, "unusable": 0, "error": 0}
    workers = max(1, int(workers or 1))

    if verbose:
        terminal_theme.print_pip("pending", f"validating {len(rows)} text row(s) with {workers} worker(s)")

    def record(index, row, result):
        if verbose:
            status = "success" if result["status"] == "usable" else "failed" if result["status"] == "unusable" else "warning"
            terminal_theme.print_pip(
                status,
                f"text {index}/{len(rows)} #{row['extraction_id']} {result['status']}: {result.get('reason')}",
            )
        results[result["status"]] = results.get(result["status"], 0) + 1

    if workers == 1:
        for index, row in enumerate(rows, start=1):
            completed_row, result = _validate_candidate(row, model, use_llm)
            record(index, completed_row, result)
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(_validate_candidate, row, model, use_llm): row
                for row in rows
            }
            for index, future in enumerate(as_completed(future_map), start=1):
                row, result = future.result()
                record(index, row, result)
    results["checked"] = len(rows)
    return results


def remove_unusable(limit=None, verbose=False):
    rows = db.get_unusable_text_extractions(limit=limit)
    removed = 0
    for row in rows:
        _remove_unusable_text(row, row.get("quality_reason") or "rejected by text quality validation")
        removed += 1
        if verbose:
            terminal_theme.print_pip(
                "failed",
                f"removed unusable text #{row['extraction_id']}: {row.get('title')} [{row.get('site')}]",
            )
    return {"removed": removed}
