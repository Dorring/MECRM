#!/usr/bin/env bash
# ci-inspect-image.sh -- Group F F3 single-image metrics collector for CI
# ------------------------------------------------------------------
# Pulls a just-pushed image by digest, inspects it, and writes a
# structured JSON artifact suitable for upload via actions/upload-artifact.
#
# Usage:
#   bash scripts/ci-inspect-image.sh --image <ref@digest> --output <path.json> [--build-duration-seconds <seconds>]
#   bash scripts/ci-inspect-image.sh --image ghcr.io/org/repo/gateway@sha256:abc --output image-metrics-gateway.json --build-duration-seconds 42
#
# Output JSON schema (Group F F3):
#   image              string   full image reference
#   imageName          string   short name (gateway | frontend | agents)
#   digest             string   sha256:...
#   uncompressedSize   number   bytes
#   uncompressedSizeMB string   formatted MB
#   layerCount         number   len(RootFS.Layers)
#   created            string   ISO 8601 from image metadata
#   buildTimestamp     string   ISO 8601 when this script ran
#   buildDurationS     number   CI build duration in seconds, or null if unknown
#   dockerHistory      array    [{ID, Created, CreatedBy, Size, Comment}]
#
# Prerequisites:
#   - Docker daemon with access to the registry
#   - python3 (pre-installed on ubuntu-latest GitHub Actions runner)
#   - Run from the repo root

set -euo pipefail

IMAGE=""
OUTPUT=""
BUILD_DURATION_SECONDS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image) IMAGE="$2"; shift 2 ;;
    --output) OUTPUT="$2"; shift 2 ;;
    --build-duration-seconds) BUILD_DURATION_SECONDS="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$IMAGE" || -z "$OUTPUT" ]]; then
  echo "Usage: $0 --image <ref@digest> --output <path.json>" >&2
  exit 1
fi

echo "=== ci-inspect-image.sh ===" >&2
echo "Image:  $IMAGE" >&2
echo "Output: $OUTPUT" >&2

# ------------------------------------------------------------------
# Pull the exact image by digest
# ------------------------------------------------------------------
echo "Pulling image..." >&2
if ! docker pull "$IMAGE" >&2; then
  echo "FATAL: docker pull failed for $IMAGE" >&2
  exit 1
fi

TMPDIR="$(mktemp -d -t ci-metrics.XXXXXX)"
trap "rm -rf $TMPDIR" EXIT

# ------------------------------------------------------------------
# Collect docker inspect and docker history
# ------------------------------------------------------------------
echo "Inspecting image..." >&2
if ! docker image inspect "$IMAGE" > "$TMPDIR/inspect.json" 2>/dev/null; then
  echo "FATAL: docker image inspect failed for $IMAGE" >&2
  exit 1
fi

echo "Collecting docker history..." >&2
if ! docker history --format '{{json .}}' --no-trunc "$IMAGE" > "$TMPDIR/history.jsonl" 2>/dev/null; then
  echo "WARNING: docker history --format failed; falling back to plain text" >&2
  docker history --no-trunc "$IMAGE" > "$TMPDIR/history.txt" 2>/dev/null || true
  echo '{"ID":"N/A","Created":"N/A","CreatedBy":"docker history --format not supported","Size":"N/A","Comment":""}' > "$TMPDIR/history.jsonl"
fi

# ------------------------------------------------------------------
# Assemble metrics JSON with python3
# ------------------------------------------------------------------
echo "Assembling metrics JSON..." >&2

export CI_METRICS_IMAGE="$IMAGE"
export CI_METRICS_OUTPUT="$OUTPUT"
export CI_METRICS_TMPDIR="$TMPDIR"
export CI_METRICS_BUILD_DURATION_SECONDS="$BUILD_DURATION_SECONDS"

python3 <<'PYEOF'
import json, os
from datetime import datetime, timezone

tmpdir = os.environ['CI_METRICS_TMPDIR']
image_ref = os.environ['CI_METRICS_IMAGE']
output_path = os.environ['CI_METRICS_OUTPUT']
build_duration_raw = os.environ.get('CI_METRICS_BUILD_DURATION_SECONDS', '')

# Parse docker image inspect
with open(f'{tmpdir}/inspect.json') as f:
    inspect = json.load(f)[0]

# Parse docker history (JSONL)
history = []
history_path = f'{tmpdir}/history.jsonl'
if os.path.exists(history_path):
    with open(history_path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    history.append(json.loads(line))
                except json.JSONDecodeError:
                    history.append({'CreatedBy': line, 'Size': ''})

# Derive short image name
image_name = image_ref.split('/')[-1].split('@')[0].split(':')[0]

# Extract digest from RepoDigests or Id
digest = ''
for rd in inspect.get('RepoDigests', []):
    if '@' in rd:
        digest = rd.split('@')[1]
        break
if not digest:
    raw_id = inspect.get('Id', '')
    digest = raw_id if raw_id.startswith('sha256:') else f"sha256:{raw_id[:64]}"
elif not digest.startswith('sha256:'):
    digest = f'sha256:{digest}'

size = inspect.get('Size', 0)
size_mb = round(size / 1048576, 1)
layer_count = len(inspect.get('RootFS', {}).get('Layers', []))
try:
    build_duration = int(build_duration_raw) if build_duration_raw else None
except ValueError:
    build_duration = None

data = {
    'image': image_ref,
    'imageName': image_name,
    'digest': digest,
    'uncompressedSize': size,
    'uncompressedSizeMB': f'{size_mb}',
    'layerCount': layer_count,
    'created': inspect.get('Created', ''),
    'buildTimestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'buildDurationS': build_duration,
    'dockerHistory': history,
}

with open(output_path, 'w') as f:
    json.dump(data, f, indent=2)

print(f'Metrics written to {output_path}: size={size_mb}MB, layers={layer_count}')
PYEOF

echo "Done." >&2
