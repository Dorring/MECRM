#!/usr/bin/env bash
# collect-image-metrics.sh -- Group F F1 baseline metrics collector
# ------------------------------------------------------------------
# Measures build duration, image size, layer count, docker history,
# and build context size for the four built images (gateway, frontend,
# agents, migrate) and writes a structured markdown report.
#
# Usage:
#   bash scripts/collect-image-metrics.sh            # write to docs/baseline-group-f.md
#   bash scripts/collect-image-metrics.sh --json      # also write baseline-group-f.json
#   DRY_RUN=1 bash scripts/collect-image-metrics.sh   # print what would run without building
#
# Prerequisites:
#   - Docker daemon running (Docker Desktop, Colima, or Linux docker)
#   - BuildKit enabled (set DOCKER_BUILDKIT=1 if not default)
#   - Run from the repo root
#   - Working directory clean recommended (to get reproducible context sizes)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

OUTPUT_MD="${REPO_ROOT}/docs/baseline-group-f.md"
OUTPUT_JSON="${REPO_ROOT}/docs/baseline-group-f.json"
WRITE_JSON=false
DRY_RUN="${DRY_RUN:-0}"

for arg in "$@"; do
  case "$arg" in
    --json) WRITE_JSON=true ;;
    *)       echo "Unknown arg: $arg"; exit 1 ;;
  esac
done

TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u '+%Y-%m-%dT%H:%M:%SZ')
COMMIT=$(git rev-parse HEAD)
BRANCH=$(git branch --show-current)

# ------------------------------------------------------------------
# Guard: Docker daemon available?
# ------------------------------------------------------------------
if ! docker info --format '{{.ServerVersion}}' &>/dev/null; then
  cat <<'DOCKERNA'
============================================================
DOCKER DAEMON UNAVAILABLE -- metrics collection SKIPPED
============================================================
This script requires a running Docker daemon.  Please run it
from a machine with Docker Desktop or Docker Engine available
(e.g. a CI runner with the docker CLI, or a developer laptop
with Docker Desktop).

The baseline report at docs/baseline-group-f.md has been
written with a 'pending' marker.  Re-run this script once
Docker is available to populate real numbers.

  bash scripts/collect-image-metrics.sh

============================================================
DOCKERNA

  # Write a pending baseline so the doc exists but is clearly
  # marked as not-yet-collected.
  cat > "$OUTPUT_MD" <<PENDING
# Group F Baseline -- Image Metrics

**Status:** PENDING -- Docker daemon unavailable

Date: $TIMESTAMP
Commit: $COMMIT
Branch: $BRANCH

## Important

No real baseline numbers have been collected yet. All tables below contain
placeholder values (--). F2 must not start until this baseline is populated
on a Docker-capable host or CI runner.

## How to Populate

Run this script from a machine with Docker daemon available:

```bash
bash scripts/collect-image-metrics.sh
```

The script will:
- Build all four images with `--no-cache` (clean, reproducible metrics)
- Record per-image: build duration, uncompressed size, layer count,
  docker history, build context size
- Overwrite this file with populated tables

## Image Build Metrics

| Image | Build Duration (s) | Uncompressed Size (MB) | Layer Count | Image ID |
|---|---|---|---|---|
| gateway | -- | -- | -- | -- |
| frontend | -- | -- | -- | -- |
| agents | -- | -- | -- | -- |
| migrate | -- | -- | -- | -- |

## Build Context Sizes

Context size is measured as raw tar archive size of the context directory.
This is an approximation; it does NOT apply `.dockerignore` filtering.
For accurate before/after comparison in F2, use the "transferring context"
line from `docker build` output.

| Service | Context Directory | Raw Tar Context Size (MB) |
|---|---|---|
| gateway | ./gateway | -- |
| frontend | ./frontend | -- |
| agents | ./agents | -- |
| migrate | . (repo root) | -- |

## Cold-Start / Health-Ready Time (optional, best-effort)

| Service | Time to Healthy (s) |
|---|---|
| gateway | -- |
| frontend | -- |
| agents | -- |

## docker history

- gateway: (run script to populate)
- frontend: (run script to populate)
- agents: (run script to populate)
- migrate: (run script to populate)
PENDING

  exit 0
fi

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
now_epoch() {
  if command -v gdate &>/dev/null; then
    gdate +%s%N
  elif date +%s%N 2>/dev/null | grep -qv 'N'; then
    date +%s%N
  else
    # fallback: seconds only (macOS BSD date without %N)
    date +%s
  fi
}

elapsed_s() {
  local start_ns="$1" end_ns="$2"
  echo "scale=1; ($end_ns - $start_ns) / 1000000000" | bc 2>/dev/null || \
    echo "scale=1; ($end_ns - $start_ns) / 1000000000" | awk '{printf "%.1f", $1}' 2>/dev/null || \
    echo "N/A"
}

format_bytes_mb() {
  local bytes="$1"
  if command -v bc &>/dev/null; then
    echo "scale=1; $bytes / 1048576" | bc
  else
    awk -v b="$bytes" 'BEGIN { printf "%.1f", b / 1048576 }'
  fi
}

find_python() {
  # Return the first available Python interpreter (python3 or python).
  for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
      echo "$candidate"
      return 0
    fi
  done
  echo "python3"
}

PYTHON="$(find_python)"

measure_context_size_bytes() {
  # Raw tar approximation of the build context directory.
  # WARNING: This does NOT apply .dockerignore filtering.  For accurate
  # before/after comparison, capture the "transferring context" line from
  # `docker build` output (e.g. `docker build ... 2>&1 | grep "transferring"`).
  # This function is provided for a quick off-line estimate when Docker is
  # not running or when building without BuildKit.
  local ctx_dir="$1"
  local tarfile
  tarfile="$(mktemp -t ctx-metrics.XXXXXX.tar)"
  tar -cf "$tarfile" -C "$ctx_dir" . 2>/dev/null || true
  if [ -f "$tarfile" ]; then
    wc -c < "$tarfile" 2>/dev/null || stat -f%z "$tarfile" 2>/dev/null || stat -c%s "$tarfile" 2>/dev/null || echo "0"
    rm -f "$tarfile"
  else
    echo "0"
  fi
}

# ------------------------------------------------------------------
# Service definitions
#   Each entry: name:context_dir:dockerfile:compose_target
# ------------------------------------------------------------------
SERVICES=(
  "gateway:./gateway:gateway/Dockerfile:gateway"
  "frontend:./frontend:frontend/Dockerfile:frontend"
  "agents:./agents:agents/Dockerfile:agents"
  "migrate:.:database/Dockerfile.migrate:migrate"
)

HOSTNAME=$(hostname 2>/dev/null || echo "unknown")
DOCKER_VERSION=$(docker info --format '{{.ServerVersion}}' 2>/dev/null || echo "unknown")
BUILDKIT_VERSION=$(docker buildx version 2>/dev/null | head -1 || echo "unknown")

# ------------------------------------------------------------------
# Collect metrics
# ------------------------------------------------------------------
declare -A BUILD_DURATION
declare -A IMAGE_SIZE_BYTES
declare -A IMAGE_SIZE_MB
declare -A LAYER_COUNT
declare -A IMAGE_ID
declare -A CONTEXT_SIZE_BYTES
declare -A CONTEXT_SIZE_MB
declare -A HISTORY_FILE

echo "=== Group F F1: Image Metrics Collection ==="
echo "Timestamp: $TIMESTAMP"
echo "Commit:    $COMMIT"
echo "Branch:    $BRANCH"
echo "Host:      $HOSTNAME"
echo "Docker:    $DOCKER_VERSION"
echo "BuildKit:  $BUILDKIT_VERSION"
echo ""

TMPDIR="$(mktemp -d -t f1-metrics.XXXXXX)"
trap "rm -rf $TMPDIR" EXIT

for entry in "${SERVICES[@]}"; do
  IFS=":" read -r name ctx dockerfile compose_target <<< "$entry"

  echo "---"
  echo "Service: $name"
  echo "  Context:    $ctx"
  echo "  Dockerfile: $dockerfile"

  # ----- Build context size (raw tar, no .dockerignore) -----
  echo -n "  Measuring raw context size (tar, no .dockerignore)... "
  ctx_bytes="$(measure_context_size_bytes "$ctx")"
  ctx_mb="$(format_bytes_mb "$ctx_bytes")"
  CONTEXT_SIZE_BYTES[$name]="$ctx_bytes"
  CONTEXT_SIZE_MB[$name]="$ctx_mb"
  echo "${ctx_mb} MB (${ctx_bytes} bytes)"

  # ----- Build image -----
  IMAGE_TAG="ecrm-${name}:f1-baseline"
  echo -n "  Building ${IMAGE_TAG} (no cache)... "

  if [ "$DRY_RUN" = "1" ]; then
    echo "DRY_RUN -- skipped"
    BUILD_DURATION[$name]="N/A"
    IMAGE_SIZE_BYTES[$name]="0"
    IMAGE_SIZE_MB[$name]="N/A"
    LAYER_COUNT[$name]="N/A"
    IMAGE_ID[$name]="N/A"
    HISTORY_FILE[$name]=""
    continue
  fi

  build_start="$(now_epoch)"
  docker build \
    --no-cache \
    --tag "$IMAGE_TAG" \
    --file "$dockerfile" \
    "$ctx" \
    > "$TMPDIR/${name}-build.log" 2>&1
  build_end="$(now_epoch)"
  build_dur="$(elapsed_s "$build_start" "$build_end")"
  BUILD_DURATION[$name]="$build_dur"
  echo "${build_dur}s"

  # ----- Image metrics -----
  echo -n "  Inspecting image... "
  inspect_json="$(docker image inspect "$IMAGE_TAG" 2>/dev/null || echo "{}")"

  img_size="$(echo "$inspect_json" | "$PYTHON" -c "
import sys,json
d=json.load(sys.stdin)
print(d[0]['Size'] if d else 0)
" 2>/dev/null || echo "0")"
  img_size_mb="$(format_bytes_mb "$img_size")"
  IMAGE_SIZE_BYTES[$name]="$img_size"
  IMAGE_SIZE_MB[$name]="$img_size_mb"

  layers="$(echo "$inspect_json" | "$PYTHON" -c "
import sys,json
d=json.load(sys.stdin)
print(len(d[0]['RootFS']['Layers']) if d else 0)
" 2>/dev/null || echo "0")"
  LAYER_COUNT[$name]="$layers"

  img_id="$(echo "$inspect_json" | "$PYTHON" -c "
import sys,json
d=json.load(sys.stdin)
print(d[0]['Id'].split(':')[1][:12] if d else 'N/A')
" 2>/dev/null || echo "N/A")"
  IMAGE_ID[$name]="$img_id"

  echo "${img_size_mb} MB (${layers} layers, ${img_id})"

  # ----- docker history -----
  hist_file="$TMPDIR/${name}-history.txt"
  docker history --no-trunc --human "$IMAGE_TAG" > "$hist_file" 2>/dev/null || true
  HISTORY_FILE[$name]="$hist_file"

  echo "  docker history saved (lines: $(wc -l < "$hist_file" 2>/dev/null || echo 0))"
done

echo ""
echo "=== Collection complete ==="

# ------------------------------------------------------------------
# Cold-start / health-ready time (optional, best-effort)
# ------------------------------------------------------------------
echo ""
echo "--- Cold-start / health-ready ---"
COLD_START_GATEWAY="N/A"
COLD_START_FRONTEND="N/A"
COLD_START_AGENTS="N/A"

if [ "$DRY_RUN" = "1" ]; then
  echo "DRY_RUN -- skipped cold-start measurement"
else
  if command -v docker-compose &>/dev/null || docker compose version &>/dev/null; then
    echo "Attempting cold-start measurement (best-effort)..."
    COMPOSE_CMD="docker compose"
    docker compose version &>/dev/null || COMPOSE_CMD="docker-compose"

    $COMPOSE_CMD up -d --wait postgres 2>/dev/null || true

    gate_start="$(now_epoch)"
    $COMPOSE_CMD up -d --wait gateway 2>/dev/null && \
      COLD_START_GATEWAY="$(elapsed_s "$gate_start" "$(now_epoch)")" || \
      COLD_START_GATEWAY="FAILED"
    echo "  gateway healthy after: ${COLD_START_GATEWAY}s"

    fe_start="$(now_epoch)"
    $COMPOSE_CMD up -d --wait frontend 2>/dev/null && \
      COLD_START_FRONTEND="$(elapsed_s "$fe_start" "$(now_epoch)")" || \
      COLD_START_FRONTEND="FAILED"
    echo "  frontend healthy after: ${COLD_START_FRONTEND}s"

    ag_start="$(now_epoch)"
    $COMPOSE_CMD up -d --wait agents 2>/dev/null && \
      COLD_START_AGENTS="$(elapsed_s "$ag_start" "$(now_epoch)")" || \
      COLD_START_AGENTS="FAILED"
    echo "  agents healthy after: ${COLD_START_AGENTS}s"

    $COMPOSE_CMD down --remove-orphans 2>/dev/null || true
  else
    echo "docker compose not available -- skipped cold-start measurement"
  fi
fi

# ------------------------------------------------------------------
# Write markdown report
# ------------------------------------------------------------------
echo ""
echo "--- Writing report ---"

cat > "$OUTPUT_MD" <<MDHEAD
# Group F Baseline -- Image Metrics

**Status:** COLLECTED
**Date:** $TIMESTAMP
**Commit:** $COMMIT
**Branch:** $BRANCH
**Host:** $HOSTNAME
**Docker:** $DOCKER_VERSION
**BuildKit:** $BUILDKIT_VERSION

## Reproducibility

To reproduce these metrics on another machine:

\`\`\`bash
git checkout $COMMIT
bash scripts/collect-image-metrics.sh
\`\`\`

All images were built with \`--no-cache\` to ensure clean, reproducible builds.

## Image Build Metrics

| Image | Build Duration (s) | Uncompressed Size (MB) | Layer Count | Image ID |
|---|---|---|---|---|
| gateway | ${BUILD_DURATION[gateway]} | ${IMAGE_SIZE_MB[gateway]} | ${LAYER_COUNT[gateway]} | ${IMAGE_ID[gateway]} |
| frontend | ${BUILD_DURATION[frontend]} | ${IMAGE_SIZE_MB[frontend]} | ${LAYER_COUNT[frontend]} | ${IMAGE_ID[frontend]} |
| agents | ${BUILD_DURATION[agents]} | ${IMAGE_SIZE_MB[agents]} | ${LAYER_COUNT[agents]} | ${IMAGE_ID[agents]} |
| migrate | ${BUILD_DURATION[migrate]} | ${IMAGE_SIZE_MB[migrate]} | ${LAYER_COUNT[migrate]} | ${IMAGE_ID[migrate]} |

## Build Context Sizes

Context size is measured as raw tar archive size of the context directory.
This is an approximation; it does NOT apply \`.dockerignore\` filtering.
For accurate before/after comparison, use the "transferring context" line
from \`docker build\` output.

| Service | Context Directory | Raw Tar Context Size (MB) | Raw Tar Context Size (bytes) |
|---|---|---|---|
| gateway | ./gateway | ${CONTEXT_SIZE_MB[gateway]} | ${CONTEXT_SIZE_BYTES[gateway]} |
| frontend | ./frontend | ${CONTEXT_SIZE_MB[frontend]} | ${CONTEXT_SIZE_BYTES[frontend]} |
| agents | ./agents | ${CONTEXT_SIZE_MB[agents]} | ${CONTEXT_SIZE_BYTES[agents]} |
| migrate | . (repo root) | ${CONTEXT_SIZE_MB[migrate]} | ${CONTEXT_SIZE_BYTES[migrate]} |

## Cold-Start / Health-Ready Time (best-effort)

Measured from \`docker compose up -d --wait <service>\` invocation to healthy state.

| Service | Time to Healthy (s) |
|---|---|
| gateway | $COLD_START_GATEWAY |
| frontend | $COLD_START_FRONTEND |
| agents | $COLD_START_AGENTS |

## docker history

MDHEAD

# Append docker history for each image
for entry in "${SERVICES[@]}"; do
  IFS=":" read -r name ctx dockerfile compose_target <<< "$entry"

  hist="${HISTORY_FILE[$name]}"
  if [ -n "$hist" ] && [ -f "$hist" ]; then
    {
      echo ""
      echo "### $name"
      echo ""
      echo '```text'
      cat "$hist"
      echo '```'
      echo ""
    } >> "$OUTPUT_MD"
  else
    {
      echo ""
      echo "### $name"
      echo ""
      echo "(Not collected -- DRY_RUN or build skipped)"
      echo ""
    } >> "$OUTPUT_MD"
  fi
done

echo "Report written: $OUTPUT_MD"

# ------------------------------------------------------------------
# Optional JSON output
# ------------------------------------------------------------------
if [ "$WRITE_JSON" = true ]; then
  "$PYTHON" -c "
import json, sys
data = {
  'status': 'collected',
  'timestamp': '$TIMESTAMP',
  'commit': '$COMMIT',
  'branch': '$BRANCH',
  'host': '$HOSTNAME',
  'dockerVersion': '$DOCKER_VERSION',
  'buildkitVersion': '$BUILDKIT_VERSION',
  'images': {
    'gateway': {
      'buildDurationS': '${BUILD_DURATION[gateway]}',
      'uncompressedSizeMB': '${IMAGE_SIZE_MB[gateway]}',
      'uncompressedSizeBytes': ${IMAGE_SIZE_BYTES[gateway]},
      'layerCount': ${LAYER_COUNT[gateway]},
      'imageId': '${IMAGE_ID[gateway]}',
      'contextSizeMB': '${CONTEXT_SIZE_MB[gateway]}',
      'contextSizeBytes': ${CONTEXT_SIZE_BYTES[gateway]},
    },
    'frontend': {
      'buildDurationS': '${BUILD_DURATION[frontend]}',
      'uncompressedSizeMB': '${IMAGE_SIZE_MB[frontend]}',
      'uncompressedSizeBytes': ${IMAGE_SIZE_BYTES[frontend]},
      'layerCount': ${LAYER_COUNT[frontend]},
      'imageId': '${IMAGE_ID[frontend]}',
      'contextSizeMB': '${CONTEXT_SIZE_MB[frontend]}',
      'contextSizeBytes': ${CONTEXT_SIZE_BYTES[frontend]},
    },
    'agents': {
      'buildDurationS': '${BUILD_DURATION[agents]}',
      'uncompressedSizeMB': '${IMAGE_SIZE_MB[agents]}',
      'uncompressedSizeBytes': ${IMAGE_SIZE_BYTES[agents]},
      'layerCount': ${LAYER_COUNT[agents]},
      'imageId': '${IMAGE_ID[agents]}',
      'contextSizeMB': '${CONTEXT_SIZE_MB[agents]}',
      'contextSizeBytes': ${CONTEXT_SIZE_BYTES[agents]},
    },
    'migrate': {
      'buildDurationS': '${BUILD_DURATION[migrate]}',
      'uncompressedSizeMB': '${IMAGE_SIZE_MB[migrate]}',
      'uncompressedSizeBytes': ${IMAGE_SIZE_BYTES[migrate]},
      'layerCount': ${LAYER_COUNT[migrate]},
      'imageId': '${IMAGE_ID[migrate]}',
      'contextSizeMB': '${CONTEXT_SIZE_MB[migrate]}',
      'contextSizeBytes': ${CONTEXT_SIZE_BYTES[migrate]},
    },
  },
  'coldStart': {
    'gateway': '$COLD_START_GATEWAY',
    'frontend': '$COLD_START_FRONTEND',
    'agents': '$COLD_START_AGENTS',
  },
}
with open('$OUTPUT_JSON', 'w') as f:
    json.dump(data, f, indent=2)
print(f'JSON written: $OUTPUT_JSON')
" 2>/dev/null || echo "Warning: could not write JSON ($PYTHON not available?)"
fi

echo ""
echo "Done. Output files:"
echo "  $OUTPUT_MD"
[ "$WRITE_JSON" = true ] && echo "  $OUTPUT_JSON"
