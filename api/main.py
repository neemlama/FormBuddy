"""FastAPI backend for FormBuddy.

Thin HTTP wrapper around the existing agent/tools code — no agent logic is
duplicated here, every endpoint just calls the same functions the CLI uses
(agent/orchestrator.py, agent/tools/proposal.py). Serves frontend/ as
static files from the same origin so the browser never needs CORS.

Run:
    uv run uvicorn api.main:app --reload

One deliberate simplification for the MVP: per-session conversation state
(the Strands Agent object, which holds turn-by-turn memory) is kept in a
plain in-process dict, not a persistence layer. Fine for a single-server
demo; a multi-instance deployment would swap this for AgentCore Memory or
similar without changing any endpoint's logic.
"""

import json
import sys
from pathlib import Path
from typing import Any, Literal

# Same fix as agent/orchestrator.py's CLI path, applied here at server
# startup instead of inside a __main__ guard: Strands streams tool-call/
# response chatter straight to stdout via its default callback handler,
# and Windows consoles default to a legacy codepage that can't encode
# emoji/Devanagari -- confirmed live, this crashed every /api/chat request
# with UnicodeEncodeError until fixed. Kept as visible terminal streaming
# (not silenced) since watching it live alongside the browser is a decent
# demo device.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from strands import Agent

from agent.orchestrator import build_agent
from agent.tools.audit_log import read_local_entries
from agent.tools.file_store import delete_file, get_file_bytes, get_file_path, list_files, save_file
from agent.tools.profile_store import load_profile, profile_as_text, save_profile
from agent.tools.proposal import (
    begin_cloud_approval,
    finish_cloud_fill,
    record_extension_fill_result,
    resume_after_approval,
    retry_failed_session,
)
from agent.tools.session_store import get_session

app = FastAPI(title="FormBuddy API")

# The web frontend is served from this same origin (no CORS needed there),
# but the Chrome extension's side panel runs on a chrome-extension://
# origin -- a genuinely different origin, so its fetch() calls here need
# CORS enabled. Permissive for hackathon-demo purposes; a real deployment
# would scope allow_origins to the extension's specific ID instead of "*".
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_agents: dict[str, Agent] = {}


def _get_agent(session_id: str) -> Agent:
    if session_id not in _agents:
        _agents[session_id] = build_agent()
    return _agents[session_id]


class ChatRequest(BaseModel):
    session_id: str
    message: str
    page_html: str | None = None  # set by the Chrome extension; triggers extension-mode inspection
    page_url: str | None = None


class ChatResponse(BaseModel):
    reply: str


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    agent = _get_agent(req.session_id)
    # Inject cross-session saved profile so future forms auto-fill without re-asking
    saved = load_profile("default")
    profile_block = f"\n\nSAVED_PROFILE:\n{profile_as_text(saved)}" if saved else ""
    # Inject vault file list so orchestrator can auto-match file uploads without asking
    vault = list_files()
    vault_block = ""
    if vault:
        vault_lines = [f"{m['stored_as']} ({m['mime']}, {m['size']} bytes)" for m in vault[:20]]
        vault_block = "\n\nFILE_VAULT:\n" + "\n".join(vault_lines) + "\n(Use stored_as as value for file fields when label matches; e.g. 'Citizenship Photo' -> citizenship.jpg if present)"
    prompt = f"session_id: {req.session_id}{profile_block}{vault_block}\n\n{req.message}"
    # Ground the agent in the real approval state so a chat "yes" can never
    # be mistaken for (or hallucinated into) a submission. Live 2026-09-13:
    # agent forged human approval + submitted messages while the real fill
    # was still running, and the UI still showed the Approve button.
    _state = get_session(req.session_id)
    if _state is not None:
        _st = _state.get("status", "none")
        _ground = {
            "pending_approval": "SESSION_STATE: status=pending_approval. A proposal awaits the Approve & Submit button click. A chat yes/approve changes nothing — direct the user to the button.",
            "approved": "SESSION_STATE: status=approved. The user already clicked Approve; cloud fill is running in background. Tell them to wait and watch Agent Activity. Never claim submitted.",
            "submitted": "SESSION_STATE: status=submitted. The fill already completed. Report that plainly; offer further help.",
            "submission_failed": "SESSION_STATE: status=submission_failed. Direct the user to the Retry submission button or New Conversation. Never claim submitted.",
            "rejected": "SESSION_STATE: status=rejected. No submission was made. Offer New Conversation.",
        }.get(_st)
        if _ground:
            prompt += f"\n\n{_ground}"
    if req.page_html:
        # Marker string the orchestrator's system prompt is instructed to
        # look for -- selects inspect_provided_html + fill_mode="extension"
        # instead of inspect_form + fill_mode="cloud".
        prompt += f"\n\nPAGE_HTML_PROVIDED: (url: {req.page_url or 'unknown'})\n{req.page_html}"
    result = agent(prompt)
    return ChatResponse(reply=str(result))


@app.get("/ping")
def ping() -> dict[str, str]:
    """AgentCore Runtime health check. No AWS, no agent — instant."""
    return {"status": "Healthy"}


@app.post("/invocations")
async def invocations(request: Request) -> dict[str, Any]:
    """AgentCore Runtime entrypoint.

    Accepts the raw body because AgentCore's proxy envelope is not plain
    JSON (seen live: first body byte 0xb1). Tries JSON, then {"input": ...}
    envelope, then raw text as the message.
    """
    raw = await request.body()
    payload: dict[str, Any] = {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
        payload = parsed.get("input", parsed) if isinstance(parsed, dict) else {}
    except Exception:
        print(f"WARN /invocations non-JSON body len={len(raw)} head={raw[:64]!r} tail={raw[-64:]!r}", flush=True)
        try:
            payload = {"message": raw.decode("utf-8", errors="replace")}
        except Exception:
            payload = {}
    req = ChatRequest(
        session_id=str(payload.get("session_id", "runtime-default")),
        message=str(payload.get("message", "")),
        page_html=payload.get("page_html"),
        page_url=payload.get("page_url"),
    )
    reply = chat(req).reply
    if isinstance(reply, bytes):  # defensive: surfaced live 2026-09-13 as 500 in encoders
        print(f"WARN /invocations reply was bytes ({len(reply)}B), decoding with replace", flush=True)
        reply = reply.decode("utf-8", errors="replace")
    return {"reply": str(reply)}


# --- profile endpoints (cross-session memory, zero AWS cost local) ---
class ProfileRequest(BaseModel):
    profile: dict[str, Any]


@app.get("/api/profile")
def get_profile() -> dict[str, Any]:
    return {"profile": load_profile("default")}


@app.post("/api/profile")
def set_profile(req: ProfileRequest) -> dict[str, Any]:
    save_profile(req.profile, "default")
    return {"profile": load_profile("default")}


@app.get("/api/session/{session_id}")
def session_status(session_id: str) -> dict[str, Any]:
    session = get_session(session_id)
    if session is None:
        return {"session_id": session_id, "status": "none", "proposal": None, "decision_note": ""}
    return session


class DecisionRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    note: str = ""


@app.post("/api/session/{session_id}/decide")
def decide(session_id: str, req: DecisionRequest) -> dict[str, Any]:
    import threading

    # Fast path: cloud approvals run the 1-2 min browser fill in a
    # background thread so the request returns instantly and the UI can
    # poll progress instead of hanging on an open HTTP call.
    if req.decision == "approved":
        session = get_session(session_id)
        if session is not None and session["status"] == "pending_approval" and session.get("proposal", {}).get("fill_mode", "cloud") == "cloud":
            try:
                begin_cloud_approval(session_id, note=req.note)
            except (KeyError, ValueError) as e:
                code = 404 if isinstance(e, KeyError) else 409
                raise HTTPException(status_code=code, detail=str(e)) from e
            thread = threading.Thread(target=finish_cloud_fill, args=(session_id,), daemon=True)
            thread.start()
            return {"message": "Approved — filling in background. Watch Agent Activity for progress.", "status": "approved"}

    try:
        message = resume_after_approval(session_id, decision=req.decision, note=req.note)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    session = get_session(session_id)
    return {"message": message, "status": session["status"] if session else "unknown"}


@app.post("/api/session/{session_id}/retry")
def retry(session_id: str) -> dict[str, Any]:
    """Re-queue a submission_failed session to pending_approval.

    UI calls this from the Retry button so users are never stuck on the
    terminal failure message — next Approve re-runs the fill.
    """
    try:
        record = retry_failed_session(session_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"message": f"Session {session_id} re-queued — review and Approve again.", "status": record["status"]}


# --- file vault (auto-upload support) ---
@app.post("/api/files/upload")
async def upload_file(file: UploadFile = File(...)) -> dict[str, Any]:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large (max 10MB)")
    meta = save_file(data, file.filename or "upload.bin", file.content_type)
    return meta

@app.get("/api/files")
def list_vault_files() -> list[dict[str, Any]]:
    return list_files()

@app.get("/api/files/{stored_as}")
def download_file(stored_as: str):
    result = get_file_bytes(stored_as)
    if result is None:
        raise HTTPException(status_code=404, detail="File not found")
    data, mime = result
    # Prefer FileResponse for local disk (efficient), fallback to streaming for S3 bytes
    path = get_file_path(stored_as)
    if path is not None:
        return FileResponse(path, media_type=mime, filename=stored_as)
    from fastapi.responses import Response
    return Response(content=data, media_type=mime, headers={"Content-Disposition": f'inline; filename="{stored_as}"'})

@app.delete("/api/files/{stored_as}")
def remove_file(stored_as: str) -> dict[str, Any]:
    ok = delete_file(stored_as)
    if not ok:
        raise HTTPException(status_code=404, detail="File not found")
    return {"deleted": stored_as}


@app.get("/api/session/{session_id}/audit")
def audit(session_id: str) -> list[dict[str, Any]]:
    return read_local_entries(session_id)


class ExtensionResultRequest(BaseModel):
    ok: bool
    confirmation_text: str | None = None
    notes: str = ""
    note: str = ""  # human-facing decision note, separate from the extension's own notes


@app.post("/api/session/{session_id}/extension-result")
def extension_result(session_id: str, req: ExtensionResultRequest) -> dict[str, Any]:
    """Called by the Chrome extension after it executes an approved
    extension-mode fill plan locally and either succeeds or fails. Never
    called by the agent, never called for cloud-mode sessions (those
    finalize synchronously inside /decide instead)."""
    try:
        message = record_extension_fill_result(
            session_id,
            result={"ok": req.ok, "confirmation_text": req.confirmation_text, "notes": req.notes},
            note=req.note,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    session = get_session(session_id)
    return {"message": message, "status": session["status"] if session else "unknown"}


FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
