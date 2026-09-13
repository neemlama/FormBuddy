# Deploy — AgentCore Runtime (Good Neighbor track) — Ephemeral 30-min plan to protect $181 credits

> See `COST.md` for the full guard. **Default to local.** Only go to AgentCore for the video, then tear down. This still earns the “AgentCore deployed” bonus — judges accept on-demand deployments.

## Recommended: Ephemeral deploy (create → record → destroy, ~$1–2 total)

```bash
# 0. Stay local for dev
uv run pytest tests/ -q   # 48 tests, $0
uv run uvicorn api.main:app --port 8000   # local demo at http://localhost:8000

# 1. Only on video day, create DynamoDB tables (one-time, free tier, PAY_PER_REQUEST)
aws dynamodb create-table --table-name formbuddy-sessions --attribute-definitions AttributeName=session_id,AttributeType=S --key-schema AttributeName=session_id,KeyType=HASH --billing-mode PAY_PER_REQUEST --region us-east-1
aws dynamodb create-table --table-name formbuddy-audit-log --attribute-definitions AttributeName=entry_id,AttributeType=S --key-schema AttributeName=entry_id,KeyType=HASH --billing-mode PAY_PER_REQUEST --region us-east-1

# 2. Deploy to AgentCore Runtime (console or CLI) — point entry to api.main:app
# Env on Runtime:
#   SESSION_STORE_SOURCE=dynamodb
#   AUDIT_LOG_SOURCE=dynamodb
#   SESSION_TABLE_NAME=formbuddy-sessions
#   AUDIT_LOG_TABLE_NAME=formbuddy-audit-log
#   + Bedrock access for Haiku (inspector/filler/parser) + Sonnet (orchestrator)
# Frontend is served from same origin; set extension:
#   extension/sidepanel.js:6  const BACKEND_URL = "https://<runtime-id>.agentcore.aws"

# 3. Record video in ONE take (<30 min Runtime up):
#   a) Extension mode (free): open demo/mock-rsvp/index.html → side panel Analyze → Authorize & Fill → you click Submit
#   b) Cloud mode (billed): paste URL + profile → propose → Approve → AgentCore Browser fills + submits (1 Browser session)
#   c) Show audit trail: GET /api/session/{id}/audit → all steps logged

# 4. Teardown immediately after recording (do not leave Runtime running to Sep 14)
#   AgentCore console → Stop/Delete Runtime
aws dynamodb delete-table --table-name formbuddy-sessions --region us-east-1
aws dynamodb delete-table --table-name formbuddy-audit-log --region us-east-1
# Revert extension: extension/sidepanel.js:6  BACKEND_URL = "http://localhost:8000"
```

> Note for judges in README: “Deployed on demand to AgentCore Runtime for the live Browser demo (video timestamp); tore down to stay within hackathon credits — re-deploy is `uv run agentcore deploy` per this guide.” This is accepted and saves ~$10+ idle cost vs leaving it up 11 days.

`agent/tools/session_store.py:7` and `agent/tools/audit_log.py:22` already abstract `local`↔`dynamodb` behind env — no code change. Swapping to **AgentCore Memory** later is a drop-in.

## Fallback: Local-only demo (if you skip AgentCore entirely)

Host `demo/mock-rsvp/` on S3 for a stable URL the Browser can reach, record locally:

```bash
uv sync && uv run uvicorn api.main:app --port 8000
# http://localhost:8000 + S3 demo URL
```

You still score on Strands/Haiku tiering + audit, but the “AgentCore deployed” bonus is lost.

## CORS

`api/main.py:51` currently `allow_origins=["*"]` for extension debugging. For production scope to `["chrome-extension://<your-id>"]`.

## Observability (optional, only when Runtime is up)

Enable AgentCore Observability/CloudWatch briefly for the video to show traces of `inspect_form`/`fill_and_submit_form` — then disable with the Runtime.
