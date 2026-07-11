# Group F Baseline -- Image Metrics

**Status:** PENDING -- Docker daemon unavailable

Date: 2026-07-11
Commit: 0de8b71
Branch: codex/group-f-image-optimization

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
- Build all four images with --no-cache (clean, reproducible metrics)
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
This is an approximation; it does NOT apply .dockerignore filtering.
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
