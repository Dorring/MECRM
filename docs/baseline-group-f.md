# Group F Baseline -- Image Metrics

**Status:** PENDING -- Docker daemon unavailable on this host
**Date:** 2026-07-11
**Commit:** 0de8b71
**Branch:** codex/group-f-image-optimization

## Context

This baseline must be collected on a machine with Docker daemon running.
Re-run when Docker is available:

```bash
bash scripts/collect-image-metrics.sh
```

## Instructions

1. Ensure Docker daemon is running (Docker Desktop, Colima, or Linux docker).
2. From the repo root, run the script above.
3. The script will:
   - Build all four images with --no-cache (clean, reproducible metrics)
   - Record per-image: build duration, uncompressed size, layer count,
     docker history, build context size
   - Optionally measure cold-start / health-ready time
   - Write this file with populated tables

## Expected Output Tables

### Image Build Metrics

| Image | Build Duration (s) | Uncompressed Size (MB) | Layer Count | Image ID |
|---|---|---|---|---|
| gateway | -- | -- | -- | -- |
| frontend | -- | -- | -- | -- |
| agents | -- | -- | -- | -- |
| migrate | -- | -- | -- | -- |

### Build Context Sizes

| Service | Context Directory | Context Size (MB) |
|---|---|---|
| gateway | ./gateway | -- |
| frontend | ./frontend | -- |
| agents | ./agents | -- |
| migrate | . (repo root) | -- |

### Cold-Start / Health-Ready Time (optional)

| Service | Time to Healthy (s) |
|---|---|
| gateway | -- |
| frontend | -- |
| agents | -- |

### docker history

- gateway: (run script to populate)
- frontend: (run script to populate)
- agents: (run script to populate)
- migrate: (run script to populate)
