"""Public proxy for judges: POST {action, session_id, ...} -> AgentCore Runtime.

Keeps the Runtime ARN server-side. No auth (judges-only link, throttled
at the Gateway stage); tear down after results. Payloads are tiny.
"""

import json
import os

import boto3

RUNTIME_ARN = os.environ["RUNTIME_ARN"]
REGION = os.environ.get("AWS_REGION", "us-east-1")
SESSION_TABLE = os.environ.get("SESSION_TABLE_NAME", "formbuddy-sessions")
AUDIT_TABLE = os.environ.get("AUDIT_LOG_TABLE_NAME", "formbuddy-audit-log")

_client = None


def _client_lazy():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-agentcore", region_name=REGION)
    return _client


_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def _dynamo(table_name):
    import boto3

    return boto3.resource("dynamodb", region_name=REGION).Table(table_name)


def _norm(v):
    from decimal import Decimal

    if isinstance(v, Decimal):
        return int(v) if v % 1 == 0 else float(v)
    if isinstance(v, dict):
        return {k: _norm(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    return v


def _fast_session(session_id):
    try:
        resp = _dynamo(SESSION_TABLE).get_item(Key={"session_id": session_id})
        item = resp.get("Item")
        if item is None:
            return {"session_id": session_id, "status": "none"}
        return _norm(item)
    except Exception as e:
        return None, str(e)[:200]


def _fast_audit(session_id):
    try:
        table = _dynamo(AUDIT_TABLE)
        items = []
        kwargs = {}
        # Tiny tables (one row per decision) — scan+filter is fine, paginated.
        while True:
            resp = table.scan(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
            if len(items) > 500:
                break
        entries = [_norm(e) for e in items]
        entries = [e for e in entries if e.get("session_id") == session_id]
        entries.sort(key=lambda e: e.get("timestamp", 0))
        return {"entries": entries}
    except Exception as e:
        print(f"fast_audit failed: {type(e).__name__}: {e}", flush=True)
        return None, str(e)[:200]


def handler(event, context):
    if event.get("httpMethod") == "OPTIONS" or event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return {"statusCode": 204, "headers": _CORS, "body": ""}
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {"message": str(body)}
    # Cheap polls must NOT wake the Runtime (each Runtime invoke costs
    # container time; chat/decide are the only actions needing Bedrock).
    # Serve session/audit straight from DynamoDB — verified 2026-09-14
    # after 283 proxy invokes -> 915 Bedrock calls on Sep 13.
    action = str(body.get("action", "chat"))
    session_id = str(body.get("session_id", "runtime-default"))
    if action == "session":
        out = _fast_session(session_id)
        if isinstance(out, tuple):
            # Dynamo unavailable — fall through to Runtime so judges still work.
            pass
        else:
            return {"statusCode": 200, "headers": {**_CORS, "Content-Type": "application/json"}, "body": json.dumps(out)}
    if action == "audit":
        out = _fast_audit(session_id)
        if isinstance(out, tuple):
            pass
        else:
            return {"statusCode": 200, "headers": {**_CORS, "Content-Type": "application/json"}, "body": json.dumps(out)}
    # Small guardrails: cap message size so one request can't burn budget.
    if isinstance(body.get("message"), str) and len(body["message"]) > 20000:
        body["message"] = body["message"][:20000]
    if isinstance(body.get("page_html"), str) and len(body["page_html"]) > 120000:
        return {"statusCode": 413, "headers": _CORS, "body": json.dumps({"error": "page_html too large (120KB max)"})}
    try:
        resp = _client_lazy().invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN, payload=json.dumps(body).encode()
        )
        out = resp["response"].read().decode()
    except Exception as e:
        return {"statusCode": 502, "headers": _CORS, "body": json.dumps({"error": str(e)[:500]})}
    return {
        "statusCode": 200,
        "headers": {**_CORS, "Content-Type": "application/json"},
        "body": out,
    }
