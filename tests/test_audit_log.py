"""Tests for log_decision — runs fully local via AUDIT_LOG_LOCAL_PATH override."""

import json

import pytest

from agent.tools.audit_log import log_decision, log_system_decision, read_local_entries


@pytest.fixture(autouse=True)
def _isolated_log(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_LOG_LOCAL_PATH", str(tmp_path / "audit-log.jsonl"))
    yield


def test_normal_entry_round_trips_with_generated_fields():
    entry = log_decision(
        session_id="s1",
        actor="agent",
        action="eligibility_matched",
        detail={"matched_ids": ["example-rsvp-form"]},
    )
    assert entry["session_id"] == "s1"
    assert entry["entry_id"]  # generated, non-empty
    assert entry["timestamp"] > 0
    assert entry["requires_human_approval"] is False

    logged = read_local_entries("s1")
    assert len(logged) == 1
    assert logged[0]["entry_id"] == entry["entry_id"]


def test_multiple_entries_append_not_overwrite():
    log_decision(session_id="s1", actor="agent", action="a1", detail={})
    log_system_decision(session_id="s1", actor="human", action="a2", detail={}, requires_human_approval=True)
    log_decision(session_id="s2", actor="agent", action="a3", detail={})

    assert len(read_local_entries("s1")) == 2
    assert len(read_local_entries()) == 3  # unfiltered returns all sessions


def test_agent_tool_refuses_human_actor():
    # Live 2026-09-13: orchestrator forged human approval entries on chat
    # "yes" while the real fill was still running. The agent tool must
    # refuse actor="human" and write nothing.
    result = log_decision(session_id="s1", actor="human", action="submission_approved", detail={})
    assert result["ok"] is False
    assert read_local_entries("s1") == []


def test_requires_human_approval_flag_is_recorded_not_enforced():
    # This tool only records intent to gate — it does not itself block
    # anything. Documented explicitly so nobody mistakes logging for
    # enforcement later.
    entry = log_decision(
        session_id="s1",
        actor="agent",
        action="submission_prepared",
        detail={"url": "example-rsvp-form"},
        requires_human_approval=True,
    )
    assert entry["requires_human_approval"] is True


def test_entries_are_valid_jsonl_on_disk(tmp_path, monkeypatch):
    log_path = tmp_path / "custom.jsonl"
    monkeypatch.setenv("AUDIT_LOG_LOCAL_PATH", str(log_path))

    log_decision(session_id="s1", actor="agent", action="a1", detail={"x": 1})
    log_decision(session_id="s1", actor="agent", action="a2", detail={"y": 2})

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        json.loads(line)  # raises if not valid JSON
