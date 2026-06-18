import json
import os
from pathlib import Path
import time

import requests

import llm


DEFAULT_MEMORY_PATH = os.getenv("ALGE_MEMORY_PATH", "logs/agent_memory.jsonl")
DEFAULT_MODEL_CACHE_PATH = os.getenv("ALGE_MODEL_CACHE_PATH", "logs/openrouter_models.json")
DEFAULT_CONTEXT_LENGTH = int(os.getenv("ALGE_DEFAULT_CONTEXT_LENGTH", "32768"))
DEFAULT_COMPACTION_RATIO = float(os.getenv("ALGE_COMPACTION_RATIO", "0.55"))
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json_dumps(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def estimate_tokens(text):
    return max(1, len(text) // 4)


class MemoryStore:
    def __init__(
        self,
        path=DEFAULT_MEMORY_PATH,
        model_cache_path=DEFAULT_MODEL_CACHE_PATH,
        default_context_length=DEFAULT_CONTEXT_LENGTH,
        compaction_ratio=DEFAULT_COMPACTION_RATIO,
    ):
        self.path = Path(path)
        self.model_cache_path = Path(model_cache_path)
        self.default_context_length = default_context_length
        self.compaction_ratio = compaction_ratio

    def append(self, kind, content, metadata=None):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": _now(),
            "kind": kind,
            "content": str(content),
            "metadata": metadata or {},
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(_json_dumps(entry) + "\n")
        return entry

    def entries(self):
        if not self.path.exists():
            return []
        rows = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    rows.append({
                        "ts": "unknown",
                        "kind": "corrupt",
                        "content": line,
                        "metadata": {},
                    })
        return rows

    def clear(self):
        self.path.unlink(missing_ok=True)

    def recent(self, limit=20, query=None):
        rows = self.entries()
        if query:
            needle = query.lower()
            rows = [
                row for row in rows
                if needle in row.get("content", "").lower()
                or needle in _json_dumps(row.get("metadata", {})).lower()
            ]
        return rows[-limit:]

    def token_count(self):
        return estimate_tokens("\n".join(_json_dumps(row) for row in self.entries()))

    def fetch_model_specs(self, force=False):
        if self.model_cache_path.exists() and not force:
            age_seconds = time.time() - self.model_cache_path.stat().st_mtime
            if age_seconds < 24 * 60 * 60:
                try:
                    return json.loads(self.model_cache_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    pass

        response = requests.get(OPENROUTER_MODELS_URL, timeout=20)
        response.raise_for_status()
        payload = response.json()
        self.model_cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.model_cache_path.write_text(_json_dumps(payload), encoding="utf-8")
        return payload

    def model_context_length(self, model=None, force_refresh=False):
        if not model:
            model = os.getenv("OPENROUTER_MODEL") or llm.DEFAULT_MODEL
        try:
            payload = self.fetch_model_specs(force=force_refresh)
            for item in payload.get("data", []):
                if item.get("id") == model or item.get("canonical_slug") == model:
                    top_provider = item.get("top_provider") or {}
                    return int(top_provider.get("context_length") or item.get("context_length") or self.default_context_length)
        except Exception:
            return self.default_context_length
        return self.default_context_length

    def should_compact(self, model=None):
        tokens = self.token_count()
        default_threshold = int(self.default_context_length * self.compaction_ratio)
        if tokens <= default_threshold:
            return False, self.default_context_length, default_threshold
        context_length = self.model_context_length(model=model)
        threshold = int(context_length * self.compaction_ratio)
        return tokens > threshold, context_length, threshold

    def compact(self, model=None, force=False):
        rows = self.entries()
        if not rows:
            return {"compacted": False, "reason": "memory is empty"}

        should_compact, context_length, threshold = self.should_compact(model=model)
        if not force and not should_compact:
            return {
                "compacted": False,
                "reason": "below threshold",
                "tokens": self.token_count(),
                "context_length": context_length,
                "threshold": threshold,
            }

        compactable = [row for row in rows if row.get("kind") != "summary"]
        if len(compactable) < 2 and not force:
            return {"compacted": False, "reason": "not enough entries"}

        summary = self._summarize(compactable, model=model)
        preserved = [row for row in rows if row.get("kind") == "summary"][-3:]
        preserved.append({
            "ts": _now(),
            "kind": "summary",
            "content": summary,
            "metadata": {
                "source_entries": len(compactable),
                "context_length": context_length,
                "threshold": threshold,
            },
        })
        preserved.extend(compactable[-10:])

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            for row in preserved:
                handle.write(_json_dumps(row) + "\n")

        return {
            "compacted": True,
            "source_entries": len(rows),
            "kept_entries": len(preserved),
            "tokens": self.token_count(),
            "context_length": context_length,
            "threshold": threshold,
        }

    def _summarize(self, rows, model=None):
        text = "\n".join(
            f"- {row.get('ts')} [{row.get('kind')}] {row.get('content')} {row.get('metadata', {})}"
            for row in rows
        )
        try:
            client = llm.get_openrouter_client()
            response = client.chat.completions.create(
                model=model or os.getenv("OPENROUTER_MODEL") or llm.DEFAULT_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": "Compress terminal agent memory into concise operational context. Preserve user goals, decisions, commands, failures, and next actions.",
                    },
                    {
                        "role": "user",
                        "content": text[-60000:],
                    },
                ],
                temperature=0.1,
            )
            return response.choices[0].message.content.strip()
        except Exception:
            recent = rows[-25:]
            return "\n".join(
                f"{row.get('ts')} [{row.get('kind')}] {row.get('content')}"
                for row in recent
            )
