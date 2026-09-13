"""propose_form_fill (agent-facing tool) + resume_after_approval
(externally-driven, NOT an agent tool) — the two halves of the human
approval session boundary. Supports two fill_modes:

  "cloud"     — resume_after_approval itself drives fill_and_submit_form
                (AgentCore Browser) synchronously and finalizes the result.
  "extension" — resume_after_approval marks the plan approved and returns;
                a Chrome extension executes the actual DOM fill locally
                (it already has the approved `fields` plan via the session
                GET endpoint) and later reports back through
                record_extension_fill_result, which finalizes the result
                the same way the cloud path does inline. This split exists
                because the backend cannot reach into the user's browser
                tab on its own — only the extension can act there.

propose_form_fill is the actual handoff point: calling it is the
orchestrator declaring "I'm done reasoning, here's exactly what I'd fill
in and submit, a human needs to decide." Nothing after this point runs
autonomously. Treat it as the consequential action it is, not a routine
tool call.

resume_after_approval and record_extension_fill_result are deliberately
NOT decorated with @tool — the agent does not call either on itself. Both
are invoked externally, only after an actual human/extension event.

Known gap, scoped out deliberately rather than silently: if a fill fails
(network blip, page changed, etc.), status lands on "submission_failed".
Use retry_failed_session() to re-queue it to "pending_approval" for one
more human-approved attempt — the UI exposes this as Retry, so users never
hang on a terminal failure.
"""

from typing import Any, Literal

from strands import tool

from agent.tools.audit_log import log_decision
from agent.tools.form_filler import fill_and_submit_form
from agent.tools.session_store import get_session, save_pending_proposal, update_status


def _missing_required_fields(fields: list[dict[str, Any]]) -> list[str]:
    return [f["label"] for f in fields if f.get("required") and f.get("value") in (None, "")]


@tool
def propose_form_fill(
    session_id: str,
    url: str,
    fields: list[dict[str, Any]],
    submit_selector: str | None,
    summary_for_human: str,
    fill_mode: Literal["cloud", "extension"] = "cloud",
) -> dict[str, Any]:
    """Save a proposed form submission and mark it awaiting human approval.

    Call this only after inspect_form/inspect_provided_html has told you
    what the form actually contains AND you have a value for every field
    you're confident about. This is the handoff: it durably records the
    exact fill plan (so it survives to a second, later invocation) and
    logs it to the audit trail with requires_human_approval=True. No
    submission happens here or as a result of calling this.

    Args:
        session_id: The session this proposal belongs to.
        url: The form's URL.
        fields: [{"label", "field_type", "selector", "required", "value"},
            ...] — one entry per discovered field. Leave "value" as
            null/empty for anything you don't have data for — do NOT
            invent a value. If any REQUIRED field has no value, this call
            is refused (see Raises) so you can ask the user instead of
            proposing an incomplete plan.
        submit_selector: CSS selector for the submit button.
        summary_for_human: Your plain-language explanation of what you're
            about to submit and why — this is what the human approver reads.
        fill_mode: "cloud" if you inspected via inspect_form (AgentCore
            Browser will also perform the submission on approval), or
            "extension" if you inspected via inspect_provided_html (a
            Chrome extension will perform the submission on approval —
            you have no submission tool either way).

    Returns:
        The saved session record: {session_id, status: "pending_approval",
        proposal, decision_note}.

    Raises:
        ValueError: one or more required fields have no value. The message
            lists which ones — go back and ask the user for them.
    """
    missing = _missing_required_fields(fields)
    if missing:
        raise ValueError(
            f"Refusing to propose: these required fields have no value yet: {missing}. "
            "Ask the user for them rather than guessing."
        )

    proposal = {
        "url": url,
        "fields": fields,
        "submit_selector": submit_selector,
        "summary_for_human": summary_for_human,
        "fill_mode": fill_mode,
    }
    record = save_pending_proposal(session_id, proposal)

    log_decision(
        session_id=session_id,
        actor="agent",
        action="form_fill_proposed",
        detail={"url": url, "summary": summary_for_human, "fill_mode": fill_mode},
        requires_human_approval=True,
    )

    return record


def _finalize_fill_result(session_id: str, url: str, result: dict[str, Any], note: str) -> str:
    """Shared by the cloud path (called inline from resume_after_approval)
    and the extension path (called later from record_extension_fill_result)
    — same logging/status-update regardless of who actually performed the
    DOM fill."""
    if result["ok"]:
        log_decision(
            session_id=session_id,
            actor="agent",
            action="submission_completed",
            detail={"url": url, "confirmation_text": result["confirmation_text"]},
            requires_human_approval=False,
        )
        update_status(session_id, status="submitted", decision_note=note)
        return f"Approved and submitted for session {session_id}. Confirmation: {result['confirmation_text']}"

    # Failed -- land on a distinct terminal-ish status (not "approved", not
    # "pending_approval") so it's visibly not silently retried by a repeat
    # call, and not confused with an undecided session. No automatic retry
    # built yet -- see module docstring.
    log_decision(
        session_id=session_id,
        actor="agent",
        action="submission_failed",
        detail={"url": url, "notes": result["notes"]},
        requires_human_approval=False,
    )
    update_status(session_id, status="submission_failed", decision_note=note)
    return f"Approved, but submission failed for session {session_id}: {result['notes']}"


def resume_after_approval(
    session_id: str,
    decision: Literal["approved", "rejected"],
    note: str = "",
) -> str:
    """Second half of the approval flow. Called externally after a human has
    actually reviewed the proposal saved by propose_form_fill — never by
    the agent on itself.

    Raises:
        KeyError: no proposal exists for session_id.
        ValueError: the session was already decided (idempotency guard —
            prevents a double-click or repeated call from processing the
            same approval/rejection twice, which matters a lot once
            "approved" triggers a real submission).
    """
    session = get_session(session_id)
    if session is None:
        raise KeyError(f"No pending proposal found for session_id={session_id!r}")
    if session["status"] != "pending_approval":
        raise ValueError(
            f"Session {session_id} was already resolved (status={session['status']!r}); "
            "refusing to process the same decision twice."
        )

    proposal = session["proposal"]
    url = proposal["url"]
    fill_mode = proposal.get("fill_mode", "cloud")

    log_decision(
        session_id=session_id,
        actor="human",
        action=f"submission_{decision}",
        detail={"note": note, "url": url},
        requires_human_approval=False,
    )
    update_status(session_id, status=decision, decision_note=note)

    if decision == "rejected":
        return f"Rejected. No further action taken for session {session_id}."

    if fill_mode == "extension":
        # Cannot reach into the user's browser tab from here -- the
        # extension already has this approved plan via the session GET
        # endpoint and will execute it, then call
        # record_extension_fill_result. Status stays "approved" (not
        # "submitted") until that happens.
        return (
            f"Approved for session {session_id}. Waiting for the browser extension "
            "to fill and submit the form in your tab."
        )

    # fill_mode == "cloud" -- drive the actual submission now. This is
    # the one place in the whole system that takes an irreversible external
    # action, and it can only be reached after the guard above confirms a
    # human decision was just recorded for this exact session.
    result = fill_and_submit_form(
        session_id=session_id, url=url, fields=proposal["fields"], submit_selector=proposal["submit_selector"]
    )
    return _finalize_fill_result(session_id, url, result, note)


def retry_failed_session(session_id: str) -> dict[str, Any]:
    """Re-queue a failed submission for one more attempt.

    Only valid from status "submission_failed" -> resets to
    "pending_approval" with the same proposal intact. The next Approve
    runs the fill again. Logs a human retry_requested entry so the audit
    shows the loop explicitly.

    Raises:
        KeyError: no session found.
        ValueError: session is not in submission_failed state.
    """
    session = get_session(session_id)
    if session is None:
        raise KeyError(f"No session found for session_id={session_id!r}")
    if session["status"] != "submission_failed":
        raise ValueError(
            f"Session {session_id} is not failed (status={session['status']!r}); "
            "only submission_failed can be retried."
        )
    log_decision(
        session_id=session_id,
        actor="human",
        action="retry_requested",
        detail={"url": session["proposal"]["url"]},
        requires_human_approval=False,
    )
    return update_status(session_id, status="pending_approval", decision_note=session.get("decision_note", ""))


def record_extension_fill_result(session_id: str, result: dict[str, Any], note: str = "") -> str:
    """Called by the API layer when a Chrome extension reports back after
    executing an approved extension-mode fill plan locally. NOT an agent
    tool -- the agent never calls this on itself, same reasoning as
    resume_after_approval.

    Args:
        result: {"ok": bool, "confirmation_text": str|None, "notes": str}
            as reported by the extension.

    Raises:
        KeyError: no session found.
        ValueError: session isn't in "approved" state (idempotency guard --
            prevents a replayed report from double-finalizing, and prevents
            calling this before resume_after_approval ever ran).
    """
    session = get_session(session_id)
    if session is None:
        raise KeyError(f"No session found for session_id={session_id!r}")
    if session["status"] != "approved":
        raise ValueError(
            f"Session {session_id} is not awaiting an extension fill result (status={session['status']!r})."
        )

    url = session["proposal"]["url"]
    return _finalize_fill_result(session_id, url, result, note)
