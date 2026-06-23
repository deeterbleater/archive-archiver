import json
import re
import time
import urllib.parse

import db
import downloader
import llm
import terminal_theme


DEFAULT_MODEL = "z-ai/glm-5.2"
DEFAULT_SLEEP_SECONDS = 30 * 60
ALLOWED_ACTIONS = {"retry", "replace_url", "disable", "defer"}
TERMINAL_ERROR_MARKERS = (
    "refusing bulk archive torrent",
    "refusing anna's archive page url",
    "anna's archive returned a page/gate",
    "anna's archive returned html",
    "fast_download_not_member",
    "HTTP 404",
)
RETRYABLE_ERROR_MARKERS = (
    "timed out",
    "timeout",
    "temporarily",
    "connection",
    "rate limit",
    "HTTP 429",
    "HTTP 500",
    "HTTP 502",
    "HTTP 503",
    "HTTP 504",
    "stale download attempt",
)


def _json_from_text(text):
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text or "", re.DOTALL)
    payload = match.group(1) if match else text
    return json.loads(payload)


def _host(url):
    try:
        return urllib.parse.urlparse(str(url or "")).netloc.lower()
    except ValueError:
        return ""


def _public_http_url(url):
    try:
        parsed = urllib.parse.urlparse(str(url or ""))
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _fallback_action(row):
    error = str(row.get("error") or "")
    attempts = int(row.get("attempts") or 0)
    if any(marker.lower() in error.lower() for marker in TERMINAL_ERROR_MARKERS):
        return {
            "file_id": row["file_id"],
            "action": "disable",
            "reason": f"terminal download error after {attempts} attempt(s): {error[:180]}",
        }
    if attempts <= 2 or any(marker.lower() in error.lower() for marker in RETRYABLE_ERROR_MARKERS):
        return {
            "file_id": row["file_id"],
            "action": "retry",
            "reason": f"retrying potentially transient failure after {attempts} attempt(s): {error[:180]}",
        }
    return {
        "file_id": row["file_id"],
        "action": "defer",
        "reason": f"needs alternate-source discovery; leaving failed row visible: {error[:180]}",
    }


def fallback_plan(rows):
    return {"actions": [_fallback_action(row) for row in rows]}


def _compact_rows(rows):
    compact = []
    for row in rows:
        compact.append({
            "download_id": row.get("id"),
            "file_id": row.get("file_id"),
            "work_id": row.get("work_id"),
            "title": row.get("title"),
            "author": row.get("author"),
            "site": row.get("site"),
            "format": row.get("format"),
            "source": row.get("download_source"),
            "url": row.get("url"),
            "download_url": row.get("download_url"),
            "host": _host(row.get("download_url") or row.get("url")),
            "http_status": row.get("http_status"),
            "attempts": row.get("attempts"),
            "error": row.get("error"),
            "updated_at": row.get("updated_at"),
        })
    return compact


def glm_plan(rows, model=DEFAULT_MODEL):
    messages = [
        {
            "role": "system",
            "content": (
                "You are ALGE's download unsticking agent. Decide conservative repair actions "
                "for failed archive downloads. Return JSON only. Allowed actions are retry, "
                "replace_url, disable, and defer. Use retry for transient network/status errors. "
                "Use disable only for terminal bad candidates such as bulk torrents, HTML gate pages, "
                "malware/quarantine, impossible member-only URLs, or permanent 404s. Use replace_url "
                "only when you can provide a concrete public http/https direct file URL. Never invent "
                "private credentials. Schema: {\"actions\":[{\"file_id\":123,\"action\":\"retry\","
                "\"download_url\":null,\"reason\":\"short reason\"}]}."
            ),
        },
        {
            "role": "user",
            "content": json.dumps({"stuck_downloads": _compact_rows(rows)}, ensure_ascii=True),
        },
    ]
    response = llm.chat_with_llm(messages, model=model, temperature=0.1)
    parsed = _json_from_text(response)
    actions = parsed.get("actions")
    if not isinstance(actions, list):
        raise ValueError("GLM plan did not include an actions list")
    return {"actions": actions}


def _row_by_file_id(rows):
    return {int(row["file_id"]): row for row in rows}


def apply_plan(rows, plan):
    by_file_id = _row_by_file_id(rows)
    results = {"retry": 0, "replace_url": 0, "disable": 0, "defer": 0, "invalid": 0}
    requeued_file_ids = []
    for action in plan.get("actions") or []:
        try:
            file_id = int(action.get("file_id"))
        except (TypeError, ValueError):
            results["invalid"] += 1
            continue
        if file_id not in by_file_id:
            results["invalid"] += 1
            continue
        verb = str(action.get("action") or "").lower()
        reason = str(action.get("reason") or "download unsticker action")
        if verb not in ALLOWED_ACTIONS:
            results["invalid"] += 1
            continue
        if verb == "retry":
            if db.reset_failed_download(file_id, reason=reason):
                requeued_file_ids.append(file_id)
            results["retry"] += 1
        elif verb == "replace_url":
            url = action.get("download_url")
            if not _public_http_url(url):
                results["invalid"] += 1
                continue
            if db.replace_file_download_url(file_id, url, reason=reason):
                requeued_file_ids.append(file_id)
            results["replace_url"] += 1
        elif verb == "disable":
            db.disable_download_file(file_id, reason)
            results["disable"] += 1
        else:
            results["defer"] += 1
    return results, requeued_file_ids


def run_once(
    limit=25,
    model=DEFAULT_MODEL,
    download_limit=25,
    rps=0.05,
    max_domains=4,
    per_domain_limit=3,
    max_mb=250,
    use_glm=True,
):
    rows = db.get_stuck_downloads(limit=limit)
    if not rows:
        terminal_theme.print_pip("success", "download unsticker found no stuck downloads")
        return {
            "stuck": 0,
            "plan_source": "none",
            "actions": {"retry": 0, "replace_url": 0, "disable": 0, "defer": 0, "invalid": 0},
            "download": {"downloaded": 0, "failed": 0, "skipped": 0},
        }

    plan_source = "glm"
    try:
        plan = glm_plan(rows, model=model) if use_glm else fallback_plan(rows)
        if not use_glm:
            plan_source = "fallback"
    except Exception as exc:
        plan_source = "fallback"
        terminal_theme.print_pip("warning", f"GLM download unsticker failed; using fallback: {exc}")
        plan = fallback_plan(rows)

    actions, requeued_file_ids = apply_plan(rows, plan)
    terminal_theme.print_pip(
        "pending",
        (
            f"download unsticker {plan_source}: "
            f"retry={actions['retry']} replace={actions['replace_url']} "
            f"disable={actions['disable']} defer={actions['defer']} invalid={actions['invalid']}"
        ),
    )

    download_results = {"downloaded": 0, "failed": 0, "skipped": 0}
    if requeued_file_ids and download_limit:
        max_bytes = max_mb * 1024 * 1024 if max_mb else None
        pending_rows = db.get_pending_download_files_for_file_ids(
            requeued_file_ids,
            limit=min(int(download_limit), len(requeued_file_ids)),
        )
        download_results = downloader.download_rows_by_domain(
            pending_rows,
            requests_per_second=rps,
            max_bytes=max_bytes,
            max_domains=max_domains,
            per_domain_limit=per_domain_limit,
        )

    return {
        "stuck": len(rows),
        "plan_source": plan_source,
        "actions": actions,
        "requeued": len(requeued_file_ids),
        "download": download_results,
    }


def run_service(
    sleep_seconds=DEFAULT_SLEEP_SECONDS,
    once=False,
    idle_exit=True,
    **kwargs,
):
    while True:
        result = run_once(**kwargs)
        terminal_theme.print_pip("success", f"download unsticker cycle: {result}")
        remaining = db.get_backlog_counts().get("failed_downloads", 0)
        if once or (idle_exit and not remaining):
            return result
        time.sleep(max(1, int(sleep_seconds or DEFAULT_SLEEP_SECONDS)))
