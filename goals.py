import json
import os
from pathlib import Path
import time
import uuid


DEFAULT_GOAL_PATH = os.getenv("ALGE_GOAL_PATH", "logs/agent_goals.json")


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def parse_duration(value):
    if value is None:
        return None
    value = str(value).strip().lower()
    if not value:
        return None
    unit = value[-1]
    amount = value[:-1] if unit.isalpha() else value
    try:
        amount = float(amount)
    except ValueError as exc:
        raise ValueError(f"invalid duration: {value}") from exc
    if unit == "s" or unit.isdigit():
        seconds = amount
    elif unit == "m":
        seconds = amount * 60
    elif unit == "h":
        seconds = amount * 60 * 60
    elif unit == "d":
        seconds = amount * 60 * 60 * 24
    else:
        raise ValueError(f"invalid duration unit: {unit}")
    return int(seconds)


def timestamp_after(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + int(seconds)))


class GoalStore:
    def __init__(self, path=DEFAULT_GOAL_PATH):
        self.path = Path(path)

    def _load(self):
        if not self.path.exists():
            return {"goals": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {"goals": []}
        data.setdefault("goals", [])
        return data

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    def list(self):
        return self._load()["goals"]

    def get(self, goal_id):
        for goal in self.list():
            if goal["id"] == goal_id:
                return goal
        return None

    def active(self):
        active = [goal for goal in self.list() if goal.get("status") in ("active", "running")]
        return active[-1] if active else None

    def active_goals(self):
        return [goal for goal in self.list() if goal.get("status") in ("active", "running")]

    def supersede_active(self, replacement_id=None, reason="superseded by another goal"):
        data = self._load()
        changed = []
        timestamp = now()
        for goal in data["goals"]:
            if goal.get("status") not in ("active", "running"):
                continue
            if replacement_id and goal.get("id") == replacement_id:
                continue
            goal["status"] = "superseded"
            goal["updated_at"] = timestamp
            goal.setdefault("events", []).append({
                "ts": timestamp,
                "kind": "superseded",
                "content": reason,
                "metadata": {"replacement_id": replacement_id},
            })
            changed.append(goal)
        if changed:
            self._save(data)
        return changed

    def create(self, objective, metadata=None):
        data = self._load()
        goal = {
            "id": uuid.uuid4().hex[:12],
            "objective": objective,
            "status": "active",
            "created_at": now(),
            "updated_at": now(),
            "estimated_completion_at": None,
            "cycles": 0,
            "events": [],
            "metadata": metadata or {},
        }
        data["goals"].append(goal)
        self._save(data)
        return goal

    def update(self, goal_id, **fields):
        data = self._load()
        for goal in data["goals"]:
            if goal["id"] == goal_id:
                goal.update(fields)
                goal["updated_at"] = now()
                self._save(data)
                return goal
        raise KeyError(f"unknown goal: {goal_id}")

    def append_event(self, goal_id, kind, content, metadata=None):
        data = self._load()
        for goal in data["goals"]:
            if goal["id"] == goal_id:
                event = {
                    "ts": now(),
                    "kind": kind,
                    "content": str(content),
                    "metadata": metadata or {},
                }
                goal.setdefault("events", []).append(event)
                goal["updated_at"] = now()
                self._save(data)
                return event
        raise KeyError(f"unknown goal: {goal_id}")
