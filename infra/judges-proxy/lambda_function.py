"""Public proxy for judges: POST {action, session_id, ...} -> AgentCore Runtime.

Keeps the Runtime ARN server-side. No auth (judges-only link, throttled
at the Gateway stage); tear down after results. Payloads are tiny.
"""

import json
import os

import boto3

RUNTIME_ARN = os.environ["RUNTIME_ARN"]
REGION = os.environ.get("AWS_REGION", "us-east-1")

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


def handler(event, context):
    if event.get("httpMethod") == "OPTIONS" or event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return {"statusCode": 204, "headers": _CORS, "body": ""}
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {"message": str(body)}
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
