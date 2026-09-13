"""Minimal AgentCore Browser connectivity diagnostic — isolates "can we even
start a session and navigate" from the complexity of the full multi-step
form_filler task. Not part of pytest. Prints progress at every step,
unbuffered, so a hang shows exactly where it's stuck.

Run: uv run python -u tests/manual_browser_diagnostic.py
"""

import sys
import time

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


log("importing strands_tools.browser...")
from strands_tools.browser import AgentCoreBrowser  # noqa: E402

log("constructing AgentCoreBrowser(region=us-east-1)...")
browser_tool = AgentCoreBrowser(region="us-east-1")
log("constructed OK")

log("calling browser() init_session directly (no LLM involved)...")
result = browser_tool.browser(
    browser_input={
        "action": {
            "type": "init_session",
            "description": "diagnostic session",
            "session_name": "diag-session-001",
        }
    }
)
log(f"init_session result: {result}")

log("calling browser() navigate...")
result = browser_tool.browser(
    browser_input={
        "action": {
            "type": "navigate",
            "session_name": "diag-session-001",
            # HTTPS S3 REST endpoint -- confirmed live that AgentCore Browser
            # blocks plain HTTP (net::ERR_BLOCKED_BY_CLIENT) and the
            # HTTP-only "website hosting" endpoint style along with it.
            # Point at the demo bucket (upload demo/mock-rsvp/ there).
            "url": "https://formbuddy-demo-rsvp.s3.amazonaws.com/index.html",
        }
    }
)
log(f"navigate result: {result}")

log("calling browser() get_text on <title>...")
result = browser_tool.browser(
    browser_input={
        "action": {
            "type": "get_text",
            "session_name": "diag-session-001",
            "selector": "h1",
        }
    }
)
log(f"get_text result: {result}")

log("calling browser() close...")
result = browser_tool.browser(browser_input={"action": {"type": "close", "session_name": "diag-session-001"}})
log(f"close result: {result}")

log("DIAGNOSTIC COMPLETE")
