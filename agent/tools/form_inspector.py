"""Form inspection tools — two variants, one shared output shape.

inspect_form: the cloud path — visits an arbitrary URL via AgentCore
Browser and reads back its form structure. No pre-built map; the agent
discovers field labels, types, and selectors live from whatever HTML the
page actually returns.

inspect_provided_html: the Chrome-extension path — same discovery job, but
the HTML is handed to it directly (the extension's content script already
read the user's open tab) instead of fetched by driving a remote browser.
No AgentCore Browser dependency at all for this path.

Both are isolated Strands Agents, same pattern as document_parser's vision
extractor — callback_handler=None because tool-call chatter isn't
user-facing. Both are direct @tool functions (unlike form_filler) since
they're read-only — no approval boundary to protect.

Deliberate scope limits, stated rather than silently assumed:
  - Reads the FIRST <form> found on the page. Multi-form pages (e.g. a
    search box plus the actual RSVP form) may need a hint; not handled.
  - No login walls, no CAPTCHA solving, no JS-heavy SPA forms that render
    fields after complex interaction. Static/server-rendered forms only.
  - Neither fills or submits anything -- read-only.

Cost note: both inspectors run on Haiku, not the default Sonnet. Reading
labels/types/selectors out of HTML is structured extraction, not judgment
-- it doesn't need a frontier model, and inspect_form's browser-driving
loop in particular racks up many sequential calls (one per browser action),
each at Sonnet's 5x-pricier output-token rate otherwise. The orchestrator
itself (agent/orchestrator.py) stays on the default model -- that's where
matching a form against a real person's info under ambiguity actually
needs the stronger model.
"""

_HAIKU_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

import json
from typing import Any

from strands import Agent, tool

_FIELD_SCHEMA_INSTRUCTIONS = """\
For every field, determine:
  - label: the human-readable label (from an associated <label>, aria-label, \
    placeholder, or nearby text -- best guess if ambiguous). For grid questions, \
    include row label: e.g. "Food quality (Rate 1-5)" or "Availability [Monday]".
  - field_type: one of "text", "email", "tel", "number", "date", "time", \
    "textarea", "select", "radio", "checkbox", "file", "rating", "linear_scale", "grid_radio", "grid_checkbox"
    * text/email/tel/number/textarea = short answer / paragraph / typed inputs
    * select = dropdown (Google Forms: div[role=listbox] / div[role=option])
    * radio = multiple-choice single-select (Google Forms: div[role=radio][data-value])
    * checkbox = multiple-select (Google Forms: div[role=checkbox][data-value])
    * file = file upload (Google Forms: input[type=file] or "Add file" button inside listitem with data-params)
    * date = date picker (Google Forms: input[type=date] or 3 inputs + placeholder MM DD YYYY)
    * time = time picker (Google Forms: hour/minute inputs + AM/PM select, or input[type=time])
    * rating / linear_scale = linear scale 1-5 etc. (Google Forms: row of div[role=radio] with numeric data-value)
    * grid_radio / grid_checkbox = multiple-choice / checkbox grid (rows x columns, each cell is radio/checkbox)
  - selector: a CSS selector you could use to target this exact element -- \
    strongly prefer "#id" if the element has an id, otherwise a \
    name= attribute selector '[name="entry.XXXXXXX"]' for Google Forms, otherwise the most specific selector you can \
    construct. For grid, return the row's selector; the filler will select the column value.
  - options: for "select", "radio", "checkbox", "rating", "linear_scale", "grid_radio", "grid_checkbox" fields, the list of visible option text values (for grid: column headers); \
    null for everything else (text/date/time/file)
  - required: true if the element has a required attribute or is visually/\
    textually marked required (e.g. an asterisk, or aria-required=true on Google Forms listitem)

Also identify submit_selector: a CSS selector for the form's submit \
button (Google Forms: div[role=button][jsname] with text "Submit" / "Send").

Respond with ONLY a single JSON object, no prose, no markdown fence:
{"ok": true|false, "fields": [{"label": "...", "field_type": "...", \
"selector": "...", "options": null, "required": true|false}, ...], \
"submit_selector": "...", "notes": "<anything unusual: no form found, \
login wall, CAPTCHA, JS-rendered content that didn't load, etc>"}

If you cannot find a usable form, set "ok": false and explain why in \
notes -- do not invent fields that aren't really there.
"""

_INSPECTOR_PROMPT = f"""\
You are inspecting a web form, not filling it out. Navigate to the given \
URL (use init_session first, then navigate), then use get_html to read the \
page's HTML source. Find the first <form> on the page (or the most \
prominent set of input fields if there's no explicit <form> tag).
Every div[role=listitem] on Google Forms is one question — count them all, even if 15+.

Google Forms type mapping (same as HTML inspector):
- div[role=radio][data-value] → radio (if numeric 1-5 with scale/rate label → linear_scale/rating)
- div[role=checkbox][data-value] → checkbox
- div[role=listbox]+div[role=option] → select (dropdown)
- input[type=file] / "Add file" → file
- date: MM DD YYYY placeholder or input[type=date] → date
- time: HH : MM + AM/PM → time
- table with radio/checkbox cells → grid_radio / grid_checkbox (one field per row)

{_FIELD_SCHEMA_INSTRUCTIONS}
(For this cloud path, "no usable form" also covers: login required, \
CAPTCHA present, page didn't load.)
"""

_HTML_INSPECTOR_PROMPT = f"""\
You are inspecting HTML from a web form. This HTML was NOT fetched by \
you — it was read directly from the user's own open browser tab by a \
Chrome extension and handed to you as text. Do not attempt to navigate \
anywhere; just read the given HTML. Find the first <form> in it (or the \
most prominent set of input fields if there's no explicit <form> tag).
Every div[role=listitem] on Google Forms is one question — count them all, even if 15+.

Special handling for Google Forms (docs.google.com/forms): the real \
fields are inputs with name="entry.XXXXXXX" (often inside div[role=listitem]), \
and the visible textbox may be div[role=textbox] or textarea. Always \
return selector '[name="entry.XXXXXXX"]' for those (strongly preferred \
over a generated id), and map the visible question text as label.

Google Forms type mapping (detect via aria + structure, not input type alone):
- div[role=radio][data-value] inside listitem without grid table → field_type "radio", options = each data-value/text, selector = '[name="entry.XXXX"]' if hidden input exists else listitem selector. If options are numeric 1-5 and label contains "scale"/"rate"/"rating" → use "linear_scale" or "rating".
- div[role=checkbox][data-value] → "checkbox"
- div[role=listbox] + div[role=option] → "select" (dropdown)
- input[type=file] OR div containing "Add file" / "Upload" button + hidden input → "file", options=null
- Date: look for placeholder "MM DD YYYY" or 3 inputs + year/month/day labels, or input[type=date] → "date"
- Time: look for "HH : MM" placeholder or hour/minute inputs + AM/PM select, or input[type=time] → "time"
- Linear scale: single row with radio 1..N (often 1-5, 1-10) → "linear_scale" or "rating"
- Grid: table with rows as sub-questions and columns as headers, cells contain radio/checkbox → "grid_radio" (if radio) or "grid_checkbox" (if checkbox), options = column headers, label = overall grid title + row label for each row entry (emit one field per row).

{_FIELD_SCHEMA_INSTRUCTIONS}
"""


def _strip_thinking(text: str) -> str:
    """Remove <thinking>...</thinking> blocks some models emit before JSON."""
    import re

    return re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()


def _trim_html(html: str, limit: int = 120_000) -> str:
    """Strip scripts/styles and truncate huge pages keeping form content.

    Live 2026-09-13: full Google Forms HTML choked the inspector into
    prose instead of JSON. Scripts/styles never contain fields.
    """
    import re

    cleaned = re.sub(r"<script.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<style.*?</style>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    if len(cleaned) <= limit:
        return cleaned
    # Keep head start (form usually early) + tail (submit button often late)
    head = limit * 3 // 4
    return cleaned[:head] + "\n<!-- trimmed -->\n" + cleaned[-(limit - head) :]


def _parse_json(raw_text: str) -> dict[str, Any]:
    text = _strip_thinking(raw_text.strip())
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find the largest {...} span; models sometimes wrap JSON in prose
    # on both sides despite "ONLY JSON" instructions.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # One last attempt: drop trailing garbage after balanced braces
            depth, stop = 0, None
            for i, ch in enumerate(candidate):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        stop = i + 1
                        break
            if stop:
                return json.loads(candidate[:stop])
            raise
    raise json.JSONDecodeError("no JSON object found in model output", text, 0)


def _inspection_result_from_response(response: Any) -> dict[str, Any]:
    """Shared shape-normalization for both inspect_form and
    inspect_provided_html — same parsing, same fallback on bad output."""
    try:
        result = _parse_json(str(response))
    except json.JSONDecodeError:
        return {
            "ok": False,
            "fields": [],
            "submit_selector": None,
            "notes": f"Inspector did not return valid JSON: {str(response)[:500]!r}",
        }

    return {
        "ok": bool(result.get("ok", False)),
        "fields": result.get("fields", []),
        "submit_selector": result.get("submit_selector"),
        "notes": result.get("notes", ""),
    }


@tool
def inspect_form(url: str, region: str = "us-east-1") -> dict[str, Any]:
    """Visit url (cloud path, via AgentCore Browser) and return its
    discovered form structure. Use this when you only have a URL and no
    extension has handed you the page's HTML directly — see
    inspect_provided_html for that case.

    Unlike fill_and_submit_form, this IS a direct agent tool — it's
    read-only (never fills or submits anything), so there's no approval
    boundary to protect here, same reasoning as document_parser being a
    direct tool while form_filler is not.

    Returns:
        {"ok": bool, "fields": [...], "submit_selector": str|None,
         "notes": str}
        ok=False (e.g. login wall, CAPTCHA, page didn't load, unparseable
        model output) always comes back structured, never raises.
    """
    from strands_tools.browser import AgentCoreBrowser  # local: heavy dep, see form_filler.py

    browser_tool = AgentCoreBrowser(region=region)
    inspector = Agent(
        system_prompt=_INSPECTOR_PROMPT, tools=[browser_tool.browser], callback_handler=None, model=_HAIKU_MODEL_ID
    )
    try:
        response = inspector(f"Inspect the form at this URL: {url}")
    except Exception as e:
        return {"ok": False, "fields": [], "submit_selector": None, "notes": f"Inspection failed: {e}"}
    finally:
        # Same fix as form_filler.py's fill_and_submit_form -- see that
        # comment for why this matters and why the private _cleanup() is
        # the right call here, not the public `close` action.
        try:
            browser_tool._cleanup()
        except Exception:
            pass
    result = _inspection_result_from_response(response)
    if not result["ok"] and "valid JSON" in result.get("notes", ""):
        # One re-ask: model produced prose instead of JSON (seen live on
        # heavy Google Forms pages). Cheap, no browser involved.
        try:
            retry = inspector("Your last reply was not valid JSON. Reply again with ONLY the JSON object, no prose.")
            return _inspection_result_from_response(retry)
        except Exception:
            pass
    return result


@tool
def inspect_provided_html(html: str, url: str = "") -> dict[str, Any]:
    """Parse HTML that a Chrome extension already read from the user's own
    open browser tab (extension path — no AgentCore Browser, no network
    fetch of any kind here) and return its discovered form structure. Use
    this instead of inspect_form whenever the conversation already
    contains the page's raw HTML.

    Args:
        html: The page's (or relevant form's) HTML, as provided.
        url: The page's URL, for context/logging only — not fetched.

    Returns:
        Same shape as inspect_form: {"ok": bool, "fields": [...],
        "submit_selector": str|None, "notes": str}.
    """
    inspector = Agent(system_prompt=_HTML_INSPECTOR_PROMPT, callback_handler=None, model=_HAIKU_MODEL_ID)
    html = _trim_html(html)
    response = inspector(f"URL (context only, do not fetch): {url or '(not given)'}\n\nPage HTML:\n{html}")
    result = _inspection_result_from_response(response)
    if not result["ok"] and "valid JSON" in result.get("notes", ""):
        try:
            retry = inspector("Your last reply was not valid JSON. Reply again with ONLY the JSON object, no prose.")
            return _inspection_result_from_response(retry)
        except Exception:
            pass
    return result
