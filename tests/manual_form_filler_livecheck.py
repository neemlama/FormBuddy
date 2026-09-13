"""Manual live check of the full generic pipeline against real AgentCore
Browser — NOT part of the pytest suite (not named test_*.py, never
auto-collected; the rest of the suite stays zero-AWS-dependency).

Run directly:
    uv run python tests/manual_form_filler_livecheck.py

Exercises inspect_form -> propose_form_fill -> resume_after_approval ->
fill_and_submit_form against the real mock RSVP form on S3.
"""

import json
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from agent.tools.form_inspector import inspect_form  # noqa: E402
from agent.tools.proposal import propose_form_fill, resume_after_approval  # noqa: E402

# Demo bucket (upload demo/mock-rsvp/ there).
RSVP_URL = "https://formbuddy-demo-rsvp.s3.amazonaws.com/index.html"

USER_INFO = {
    "Full Name": "Alex Rai",
    "Email Address": "alex.rai@example.com",
    "Phone Number": "9800000000",
    "Number of Guests (including yourself)": "2",
    "T-Shirt Size": "L",
    "Dietary Restrictions": "Vegetarian",
}


def _draft_value(label: str) -> str | bool | None:
    if label in USER_INFO:
        return USER_INFO[label]
    if "plus-one" in label.lower():
        return True
    return None


if __name__ == "__main__":
    print("--- Step 1: inspect_form ---")
    inspection = inspect_form(RSVP_URL)
    print(json.dumps(inspection, indent=2, ensure_ascii=False))
    assert inspection["ok"] is True, f"inspection failed: {inspection['notes']}"
    assert len(inspection["fields"]) >= 5, "expected the RSVP form's several fields to be discovered"

    print("\n--- Step 2: draft field values from discovered fields ---")
    fields = []
    for f in inspection["fields"]:
        value = _draft_value(f["label"])
        fields.append({**f, "value": value})
    print(json.dumps(fields, indent=2, ensure_ascii=False))

    missing_required = [f["label"] for f in fields if f.get("required") and not f.get("value")]
    if missing_required:
        raise SystemExit(f"Manual check's USER_INFO doesn't cover required fields: {missing_required}")

    print("\n--- Step 3: propose_form_fill ---")
    session_id = "manual-generic-livecheck"
    record = propose_form_fill(
        session_id=session_id,
        url=RSVP_URL,
        fields=fields,
        submit_selector=inspection["submit_selector"],
        summary_for_human="RSVPing Alex Rai to the meetup with 2 guests, size L, vegetarian.",
    )
    print(json.dumps(record, indent=2, ensure_ascii=False))
    assert record["status"] == "pending_approval"

    print("\n--- Step 4: resume_after_approval(approved) -- real submission ---")
    message = resume_after_approval(session_id, decision="approved", note="manual livecheck")
    print(message)
    assert "Confirmation:" in message and "RSVP-" in message, f"unexpected result message: {message}"

    print("\nPASS: full generic pipeline completed a real submission against the mock RSVP form.")
