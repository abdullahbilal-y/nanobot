"""Voicemail task tool — read and manage the task list built from voice notes.

Gives the agent access to the same store the voicemail watcher writes to, so
the list can be queried and updated by chatting rather than only over SSH.
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool

# The store lives in a skill directory, which is not an importable package, so
# load it by path. Keeping one implementation matters more than import tidiness:
# the watcher and the agent must never drift into two views of the same list.
_STORE_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills" / "voicemail-tasks" / "scripts" / "voicemail_store.py"
)


def _load_store():
    if "voicemail_store" in sys.modules:
        return sys.modules["voicemail_store"]
    spec = importlib.util.spec_from_file_location("voicemail_store", _STORE_PATH)
    if not spec or not spec.loader:
        raise ImportError(f"cannot load voicemail store from {_STORE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["voicemail_store"] = module
    spec.loader.exec_module(module)
    return module


class VoicemailTasksTool(Tool):
    """Query and update the task list extracted from WhatsApp voice notes."""

    name = "voicemail_tasks"
    description = (
        "Read and manage the running task list built automatically from Ahmad Jasra's "
        "WhatsApp voice notes. Use whenever the user asks about their tasks, their "
        "to-do list, what Ahmad asked for, what voice notes came in, or wants to tick "
        "something off. "
        "Actions: 'list' (open tasks, or all with status='all'), 'summary' (digest of "
        "recent voice notes and their tasks), 'done' (mark task_id complete), "
        "'stats' (counts, including transcription failures), 'reset' (clear the task "
        "list; keeps the voice notes and backs up first)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "summary", "done", "stats", "reset"],
                "description": "What to do. Default 'list'.",
            },
            "status": {
                "type": "string",
                "enum": ["open", "done", "all"],
                "description": "For 'list': which tasks to show. Default 'open'.",
            },
            "days": {
                "type": "number",
                "description": "For 'summary'/'list': only cover this many days back. "
                               "Omit for everything.",
            },
            "task_id": {
                "type": "integer",
                "description": "For 'done': the task number to mark complete.",
            },
        },
        "required": [],
    }

    def __init__(self, db_path: str | None = None):
        self.db_path = Path(db_path) if db_path else None

    async def execute(self, action: str = "list", status: str = "open",
                      days: float = 0, task_id: int | None = None,
                      **kwargs: Any) -> str:
        try:
            store = _load_store()
        except Exception as e:
            return f"Error: voicemail store unavailable ({e})."

        db = self.db_path or store.DEFAULT_DB

        try:
            if action == "list":
                # 'all' means no status filter, not a literal status value.
                return store.list_tasks(db, "" if status == "all" else status, days or 0)

            if action == "summary":
                # 0 days would match nothing; default to a wide window instead.
                return store.summarize(db, days or 3650)

            if action == "done":
                if task_id is None:
                    return "Error: 'task_id' is required to mark a task done."
                return store.mark_done(db, int(task_id))

            if action == "stats":
                return store.stats(db)

            if action == "reset":
                return store.reset(db)

            return f"Error: unknown action '{action}'."
        except Exception as e:
            return f"Error running voicemail_tasks '{action}': {e}"
