FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Set Python path
ENV PYTHONPATH=/app/src

# Expose port for health checks
EXPOSE 5010

# Health check
# P0-6: check the response status code, not just that the request completed.
# A bare httpx.get() returns even on HTTP 500; a failing endpoint must be unhealthy.
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD python -c "import httpx,sys; r=httpx.get('http://localhost:5010/health'); sys.exit(0 if r.is_success else 1)"

# Run the orchestrator
CMD ["python", "-m", "orchestrator.main"]
