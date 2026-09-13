"""session_store — durable state for the human-approval session boundary.

This is a stand-in for AgentCore Memory: same job (persist state between the
"propose" agent invocation and the "resume after human approval" invocation
— see the Phase 3 architecture decision to make approval a session boundary
rather than an in-process wait), but a plain dual-backend key/value store we
fully control and can test today, instead of an unfamiliar SDK adopted
under deadline pressure. Swapping the backend to real AgentCore Memory
later is a drop-in replacement behind this same interface if there's time;
it does not change propose_application/resume_after_approval's logic.

Same local-JSON/DynamoDB dual-backend pattern as catalog.py and
audit_log.py.
"""

import json
import os
from pathlib import Path
from typing import Any

_DEFAULT_LOCAL_DIR = Path(__file__).resolve().parents[2] / "infra" / "seed-data" / "sessions.local"


def _local_dir() -> Path:
    return Path(os.environ.get("SESSION_STORE_LOCAL_DIR", _DEFAULT_LOCAL_DIR))


def _safe_filename(session_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)


def _local_path(session_id: str) -> Path:
    return _local_dir() / f"{_safe_filename(session_id)}.json"


def _use_dynamodb() -> bool:
    return os.environ.get("SESSION_STORE_SOURCE", "local") == "dynamodb"


def _table():
    import boto3  # local import: keeps boto3 off the hot path for local/test runs

    table_name = os.environ.get("SESSION_TABLE_NAME", "formbuddy-sessions")
    return boto3.resource("dynamodb").Table(table_name)


def _to_dynamo(value: Any) -> Any:
    """Recursively convert floats to Decimal — DynamoDB rejects floats."""
    from decimal import Decimal

    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_dynamo(v) for v in value]
    return value


def _write(record: dict[str, Any]) -> None:
    if _use_dynamodb():
        _table().put_item(Item=_to_dynamo(record))
        return
    path = _local_path(record["session_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def get_session(session_id: str) -> dict[str, Any] | None:
    if _use_dynamodb():
        resp = _table().get_item(Key={"session_id": session_id})
        return resp.get("Item")
    path = _local_path(session_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_pending_proposal(session_id: str, proposal: dict[str, Any]) -> dict[str, Any]:
    """Create (or revise, while still undecided) a pending proposal.

    Refuses to overwrite a session that's already been decided (approved or
    rejected) — that history is meant to be immutable. An agent that wants
    to propose something new after a rejection must use a new session_id.
    """
    existing = get_session(session_id)
    if existing is not None and existing["status"] not in ("pending_approval", "chatting", "none"):
        raise ValueError(
            f"Session {session_id} was already resolved (status={existing['status']!r}); "
            "cannot overwrite a decided session. Use a new session_id for a new proposal."
        )

    record = {"session_id": session_id, "status": "pending_approval", "proposal": proposal, "decision_note": ""}
    # Preserve multi-turn context across the chatting -> pending_approval transition
    if existing is not None:
        if isinstance(existing.get("history"), list):
            record["history"] = existing["history"][-10:]
        if existing.get("last_form_url"):
            record["last_form_url"] = existing["last_form_url"]
    _write(record)
    return record


def update_status(session_id: str, status: str, decision_note: str = "") -> dict[str, Any]:
    record = get_session(session_id)
    if record is None:
        raise KeyError(f"No session found: {session_id}")
    record["status"] = status
    record["decision_note"] = decision_note
    _write(record)
    return record


def append_turn(session_id: str, role: str, content: str, last_form_url: str | None = None) -> dict[str, Any]:
    """Persist multi-turn chat context so AgentCore Runtime stays stateless-safe.

    _agents dict in api/main.py is in-process only — Runtime containers freeze,
    scale, or restart between /invocations, wiping Strands Agent memory. This
    stores the last 10 turns + last seen form URL in the same DynamoDB/local
    record so chat() can re-inject context every turn.
    """
    record = get_session(session_id)
    if record is None:
        record = {"session_id": session_id, "status": "chatting", "proposal": None, "decision_note": ""}
    history = record.get("history", [])
    if not isinstance(history, list):
        history = []
    history.append({"role": role, "content": content[:4000]})
    record["history"] = history[-10:]
    if last_form_url:
        record["last_form_url"] = last_form_url
    _write(record)
    return record
