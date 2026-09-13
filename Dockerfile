# FormBuddy on AgentCore Runtime. AgentCore health-checks GET /ping and
# routes invocations to POST /invocations (both in api/main.py). The same
# image serves the local web UI at / for extension/cloud demoing.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    UV_NO_SYNC_OUTPUT=1

WORKDIR /app

COPY pyproject.toml uv.lock* README.md ./
COPY agent/ ./agent/
COPY api/ ./api/
COPY frontend/ ./frontend/
COPY demo/ ./demo/
COPY tests/ ./tests/

RUN pip install --no-cache-dir uv && \
    uv sync --frozen --no-dev 2>/dev/null || uv sync --no-dev

EXPOSE 8080

# Local default is file-backed stores; the Runtime sets dynamodb env vars.
CMD ["sh", "-c", ".venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8080"]
