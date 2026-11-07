# PR & code-review audit - dashboard + CLI in one small image (Python standard library only).
#   docker build -t pr-audit .                       # dashboard + rule-based scoring (no LLM)
#   docker build -t pr-audit --build-arg WITH_LLM=1 .  # also the Anthropic API scorer
FROM python:3.12-slim

ARG WITH_LLM=0
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    AUDIT_DB=/data/audit.db HOST=0.0.0.0 PORT=8765

WORKDIR /app
COPY requirements-llm.txt ./
RUN if [ "$WITH_LLM" = "1" ]; then pip install --no-cache-dir -r requirements-llm.txt; fi

COPY audit.py github.py store.py scoring.py schema.sql STANDARDS.md ./
COPY dashboard/ ./dashboard/

RUN useradd --create-home --uid 10001 audit && mkdir -p /data && chown audit:audit /data
USER audit
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8765\")}/healthz',timeout=4)"

CMD ["python", "audit.py", "serve"]
