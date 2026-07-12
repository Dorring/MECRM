# syntax=docker/dockerfile:1.7

# --- builder: compile native extensions ---
FROM python:3.11-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --user -r requirements.txt

# --- runner: runtime only, no build toolchain ---
FROM python:3.11-slim AS runner

WORKDIR /app

# Create non-root user (consistent with frontend non-root pattern)
RUN addgroup --system --gid 1001 app && \
    adduser --system --uid 1001 --gid 1001 app

# Copy pip-installed packages from builder.
# pip --user installs to /root/.local/lib/python*/site-packages and
# /root/.local/bin.  After COPY to /home/app/.local, Python auto-discovers
# the packages because the app user's home is /home/app and Python adds
# ~/.local/lib/python3.11/site-packages to sys.path at startup.
COPY --from=builder --chown=app:app /root/.local /home/app/.local

# Runtime source (selective -- no tests, scripts, or tooling)
COPY --chown=app:app src/ /app/src/

# sitecustomize.py adds /app/src (and conditionally core_services/src/) to
# sys.path.  Python loads it from CWD (/app) during site initialization.
COPY --chown=app:app sitecustomize.py /app/

# PATH so any pip-installed executables (uvicorn, etc.) are reachable.
# PYTHONPATH ensures orchestrator and intelligence modules are importable.
ENV HOME=/home/app
ENV PATH=/home/app/.local/bin:$PATH
ENV PYTHONPATH=/app/src

USER app

EXPOSE 5010

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD python -c "import httpx,sys; r=httpx.get('http://localhost:5010/health'); sys.exit(0 if r.is_success else 1)"

CMD ["python", "-m", "orchestrator.main"]
