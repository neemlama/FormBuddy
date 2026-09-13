"""fill_and_submit_form — drives AgentCore Browser to fill and submit an
arbitrary web form, given a URL and a set of field values already agreed
during the propose step.

Only ever called from resume_after_approval(), after a human has already
approved the proposal — never autonomously, never before approval, and
never called by the orchestrator's main agent on itself.

Runs its own isolated Strands Agent with the AgentCore Browser tool
registered, same isolation pattern as document_parser/form_inspector.

Safety property (the one this module keeps end to end): never invents a
value for a field it wasn't given. The pre-flight check in proposal.py
(matching field_values against inspect_form's discovered required fields)
is what prevents an incomplete plan from ever reaching here; this module
just fills exactly what it's handed and reports honestly if a field on the
live page doesn't match anything it has data for.

Cost note: runs on Haiku, not the default Sonnet -- see
agent/tools/form_inspector.py's module docstring for the full reasoning.
This is the single most expensive path in the system (one browser action
== one full model call, every field/click/screenshot), so it's also where
model tiering matters most. Live-tested after switching, not assumed --
see tests/manual_form_filler_livecheck.py.
"""

import json
from typing import Any

_HAIKU_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def _clean_selector(selector: str | None) -> str | None:
    """Undo double-escaped selectors from inspector JSON.

    Live failure 2026-09-13: Google Forms submit arrived as
    `[role=\\"button\\"][jsname=\\"M2UYVd\\"]` which is invalid CSS.
    """
    if not selector:
        return selector
    cleaned = selector.replace('\\\\"', '"').replace('\\"', '"')
    return cleaned.strip() or None


def _is_local_url(url: str) -> bool:
    lowered = (url or "").lower()
    return any(
        token in lowered
        for token in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", ".local", "file://", "192.168.", "10.")
    )


def _parse_result_json(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Confirmed live (form_filler, pre-pivot): models sometimes prefix JSON
    # with a sentence of prose despite explicit "ONLY JSON" instructions.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise json.JSONDecodeError("no JSON object found in model output", text, 0)


def _build_task_prompt(url: str, fields: list[dict[str, Any]], submit_selector: str | None) -> str:
    submit_selector = _clean_selector(submit_selector)
    lines = [
        f"Use the browser tool. First call init_session, then navigate to {url}.",
        "",
        "Fill in the following fields, in order. Be precise about field_type:",
        "  - text/email/tel/number/textarea: use the type action on the given selector. For Google Forms div[role=textbox], type into it or use evaluate to set textContent and dispatch input+change.",
        "  - date: if input[type=date] exists, type YYYY-MM-DD; if Google Forms shows 3 inputs (Month/Day/Year), type month into first, day into second, year into third (parse value like 2026-09-03 or 9/3/2026 accordingly).",
        "  - time: if input[type=time] exists, type HH:MM (24h, e.g. 11am -> 11:00, 3:30 pm -> 15:30); if Google Forms shows hour/minute + AM/PM dropdown, type hour into first input, minute into second, then click the AM/PM listbox option.",
        "  - select/radio/rating/linear_scale: click the matching option where data-value or visible text equals the value (case-insensitive). For Google Forms, search inside the same [role=listitem] for [role=radio][data-value] or [role=option]. For linear_scale/rating numeric (e.g. 4), click the radio whose data-value is that number.",
        "  - checkbox/grid_checkbox: for each value (comma-separated if multiple), click the matching [role=checkbox][data-value] so aria-checked becomes true. For grid, each field is one row; value is the column header to select in that row.",
        "    Google Forms checkbox safety: prefer a single click via the click action on the [role=checkbox] element inside the same [role=listitem]. Do NOT double-click. If aria-checked is already true, leave it. If a click closes the browser context, stop, re-init a fresh session with init_session, navigate again, verify which fields are already filled via get_text/get_html, and continue with the remaining fields only.",
        "  - grid_radio: one radio per row; value is column to select; find row container then click its matching radio.",
        "  - file: file uploads cannot be set to a local path via automation due to browser security. Click the 'Add file' / upload button to open the picker, then note in your final JSON that file upload requires manual user action - do not claim success if no file was attached.",
        "  - For any Google Forms field, the selector may be [name=\"entry.XXXXXXX\"] pointing to a hidden input - fall back to finding the visible widget inside the same [role=listitem] via data-params containing the entry number.",
        "  - If a selector contains :nth-child or looks stale, fall back to label text: find [role=listitem] containing the label, then act inside it.",
        "",
        "If a selector is not found, search by the label text inside [role=listitem] as fallback before reporting failure. Never repeat a click that closed the browser — re-init and continue instead.",
        "",
    ]
    for f in fields:
        opts = f" options={f.get('options')!r}" if f.get("options") else ""
        lines.append(f"  - {f['field_type']} field, selector \"{f['selector']}\" (label: {f['label']!r}{opts}) = {f['value']!r}")

    submit_line = (
        f'After all fields are filled, click the submit button (selector: "{submit_selector}") to submit the form.'
        if submit_selector
        else "After all fields are filled, find and click the form's submit button."
    )
    lines += [
        "",
        submit_line,
        "",
        "After submitting, capture whatever confirmation the page shows (a "
        "success message, a confirmation code, a thank-you page -- use "
        "get_text on the page body if you're not sure of an exact selector) "
        "and take a screenshot for the record.",
        "",
        "Respond with ONLY a single JSON object, no prose, no markdown fence:",
        '{"ok": true|false, "confirmation_text": "<string or null>", "notes": '
        '"<what happened, especially any error, unexpected page state, or '
        'field you could not fill>"}',
    ]
    return "\n".join(lines)


def fill_and_submit_form(
    session_id: str,
    url: str,
    fields: list[dict[str, Any]],
    submit_selector: str | None,
    region: str = "us-east-1",
) -> dict[str, Any]:
    """Fill and submit a web form using AgentCore Browser.

    NOT a Strands @tool — deliberately not callable by the orchestrator's
    main agent. Only resume_after_approval() calls this, after a human
    decision is already recorded.

    Args:
        session_id: For browser session naming only.
        url: The form's URL.
        fields: [{"label", "field_type", "selector", "value"}, ...] — the
            exact plan already shown to and approved by a human. Every
            entry here gets filled; nothing is added or invented.
        submit_selector: CSS selector for the submit button, if known from
            inspect_form.

    Returns:
        {"ok": bool, "confirmation_text": str | None, "notes": str}
    """
    submit_selector = _clean_selector(submit_selector)
    if _is_local_url(url):
        return {
            "ok": False,
            "confirmation_text": None,
            "notes": (
                f"Cloud browser cannot reach local URL {url!r}. "
                "Host demo/mock-rsvp on S3 for cloud mode, or use extension mode "
                "(Analyze This Page) for localhost — it fills your own tab for free."
            ),
        }

    from strands import Agent
    from strands_tools.browser import AgentCoreBrowser  # local import: keeps this dep off tools that don't need it

    task_prompt = _build_task_prompt(url, fields, submit_selector)
    last_error: Exception | None = None

    for attempt in (1, 2):
        browser_tool = AgentCoreBrowser(region=region)
        filler_agent = Agent(
            system_prompt=(
                "You are a form-filling agent. Follow the given instructions exactly, "
                "step by step, using the browser tool. Do not skip fields. Do not invent "
                "values not given to you. If a step fails or the page doesn't look as "
                "expected, note it in your final JSON response rather than guessing."
            ),
            tools=[browser_tool.browser],
            callback_handler=None,  # raw tool chatter isn't user-facing; see module docstring
            model=_HAIKU_MODEL_ID,
        )

        try:
            response = filler_agent(task_prompt)
        except Exception as e:
            last_error = e
            try:
                browser_tool._cleanup()
            except Exception:
                pass
            # Live 2026-09-13: Google Forms checkbox click closed the
            # context -> "Playwright not initialized" on re-init. Retry once
            # with a completely fresh browser_tool instead of failing stuck.
            msg = str(e).lower()
            if attempt == 1 and any(t in msg for t in ("playwright", "closed", "init_session", "session")):
                continue
            return {"ok": False, "confirmation_text": None, "notes": f"Browser agent failed: {e}"}
        else:
            try:
                browser_tool._cleanup()
            except Exception:
                pass  # best-effort -- AWS's idle timeout is still a backstop
            # Confirmed live: without this, the remote AgentCore Browser
            # session was only ever cleaned up by Python's __del__ at an
            # unpredictable time (or AWS's own idle timeout, up to
            # session_timeout_seconds=3600 by default) -- neither is
            # deterministic for a billable cloud resource. _cleanup() closes
            # every session this browser_tool opened, whatever the LLM named
            # it (the public `close` action needs a session_name to match,
            # which we don't reliably know -- the model picks it). Private
            # method, used deliberately: it's strands_tools.browser's own
            # teardown path (the public `close` action calls this same
            # method), not a workaround.
            break
    else:
        return {"ok": False, "confirmation_text": None, "notes": f"Browser agent failed after retry: {last_error}"}

    try:
        result = _parse_result_json(str(response))
    except json.JSONDecodeError:
        return {
            "ok": False,
            "confirmation_text": None,
            "notes": f"Browser agent did not return valid JSON: {str(response)[:500]!r}",
        }

    return {
        "ok": bool(result.get("ok", False)),
        "confirmation_text": result.get("confirmation_text"),
        "notes": result.get("notes", ""),
    }
