"""log_decision — durable record of every decision the agent (or a human)
makes. This is what turns "autonomous until uncertain" into something
auditable instead of a black box.

Note what this tool is NOT: it doesn't enforce the approval gate — it's a
record of what happened. The actual gate is structural (the orchestrator
stops and returns control to the human before any submission tool runs; see
docs/program-catalog-schema.md and the Phase 3 architecture notes). Logging
happens on both sides of that gate so the trail is complete either way.
"""

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from strands import tool

_DEFAULT_LOCAL_PATH = Path(__file__).resolve().parents[2] / "infra" / "seed-data" / "audit-log.local.jsonl"


def _local_path() -> Path:
    return Path(os.environ.get("AUDIT_LOG_LOCAL_PATH", _DEFAULT_LOCAL_PATH))


def _write_local(entry: dict[str, Any]) -> None:
    path = _local_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _write_dynamodb(entry: dict[str, Any]) -> None:
    import boto3  # local import: keeps boto3 off the hot path for local/test runs

    table_name = os.environ.get("AUDIT_LOG_TABLE_NAME", "formbuddy-audit-log")
    boto3.resource("dynamodb").Table(table_name).put_item(Item=entry)


def read_local_entries(session_id: str | None = None) -> list[dict[str, Any]]:
    """Test/debug helper — reads back the local JSONL log, optionally filtered."""
    path = _local_path()
    if not path.exists():
        return []
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if session_id is not None:
        entries = [e for e in entries if e["session_id"] == session_id]
    return entries


@tool
def log_decision(
    session_id: str,
    actor: Literal["agent", "human"],
    action: str,
    detail: dict[str, Any],
    requires_human_approval: bool = False,
) -> dict[str, Any]:
    """Record a decision/action in the durable audit trail.

    Every autonomous decision the agent makes (a program was matched, a field
    was filled, an application was queued) and every human decision (approved,
    rejected, edited) must be logged here — it's what a human reviewer or a
    post-incident review would read.

    Args:
        session_id: Identifies the applicant session this decision belongs to.
        actor: Who made this decision — "agent" or "human".
        action: Short machine-readable action name, e.g. "eligibility_matched",
            "form_filled", "submission_approved", "submission_rejected".
        detail: Free-form structured detail relevant to the action (matched
            program ids, filled field values, rejection reason, etc).
        requires_human_approval: True if the action this entry describes
            cannot proceed to submission without an explicit human approval
            logged afterward.

    Returns:
        The full logged entry, including its generated entry_id and timestamp.
    """
    if actor == "human":
        # Live 2026-09-13: the orchestrator forged human approval +
        # submission_completed entries when the user typed "yes" in chat,
        # while the real fill was still running. Agents must never log as
        # human — real human actions are recorded by proposal.py via
        # log_system_decision() when the Approve button is clicked.
        return {
            "ok": False,
            "error": "Agents cannot log human actions. Reply directing the user "
            "to click the Approve & Submit button — that click is the approval.",
        }
    return log_system_decision(
        session_id=session_id,
        actor=actor,
        action=action,
        detail=detail,
        requires_human_approval=requires_human_approval,
    )


def log_system_decision(
    session_id: str,
    actor: str,
    action: str,
    detail: dict[str, Any],
    requires_human_approval: bool = False,
) -> dict[str, Any]:
    """Non-tool audit writer for backend code (proposal.py).

    Same record shape as log_decision but callable for genuine human events
    (Approve button clicks, extension reports) that the agent tool above
    deliberately refuses.
    """
    entry = {
        "entry_id": str(uuid.uuid4()),
        "session_id": session_id,
        "actor": actor,
        "action": action,
        "detail": detail,
        "requires_human_approval": requires_human_approval,
        "timestamp": time.time(),
    }

    if os.environ.get("AUDIT_LOG_SOURCE", "local") == "dynamodb":
        _write_dynamodb(entry)
    else:
        _write_local(entry)

    return entry
