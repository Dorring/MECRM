# NVIDIA NIM Provider Setup

Use NVIDIA NIM only from the server-side `agents` service. The browser and the
API gateway must never receive an NVIDIA API key.

## Model roles

Configure one chat model and one embedding model. They are different services
with different outputs and cannot be substituted for each other.

| Setting | Role | Used by |
|---|---|---|
| `NVIDIA_CHAT_MODEL` | Generate text, classify intents, summarize, translate, call tools | Agent workflows |
| `NVIDIA_EMBED_MODEL` | Convert content and queries into vectors | Weaviate semantic search, memory, audit search |

Choose currently available model IDs from the [NVIDIA API Catalog](https://build.nvidia.com/models?filters=nimType%3Anim_type_preview). NVIDIA exposes OpenAI-compatible chat and embedding endpoints, but catalog availability can change; use the exact ID shown for your account rather than committing a transient model ID.

## Local configuration

1. Create an NVIDIA Developer/API Catalog key; keep it private.
2. In the untracked project-root `.env`, set:

```dotenv
AI_PROVIDER=nvidia_nim
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1
NVIDIA_API_KEY=replace-with-your-local-key
NVIDIA_CHAT_MODEL=replace-with-a-catalog-chat-model-id
NVIDIA_EMBED_MODEL=replace-with-a-catalog-embedding-model-id
AI_REQUEST_TIMEOUT_SECONDS=30
AI_MAX_RETRIES=2
```

3. Rebuild only the agents image and restart it:

```powershell
docker compose up -d --build agents
docker compose logs -f agents
```

Do not put the key in `NEXT_PUBLIC_*` variables, Docker build arguments, source
code, screenshots, or GitHub Actions logs.

## Embedding migration rule

Changing `NVIDIA_EMBED_MODEL`, or changing from Ollama embeddings to NVIDIA
embeddings, changes vector dimensions and similarity geometry. Existing
Weaviate vectors must be treated as incompatible. Before enabling semantic
search with a new embedding model, remove and rebuild the application-owned
Weaviate collections using the same model for both document indexing and query
embedding. Structured Postgres/RLS retrieval remains available independently.

## Failure behavior

- A missing key or model ID fails configuration before a remote request.
- Requests use a bounded timeout and retry policy.
- A provider failure must surface as a degraded/error outcome; it never grants
  approval, bypasses OPA, or weakens PostgreSQL RLS.
- CI does not use this key. Cloud model quality is evaluated separately from
  the deterministic structured-retrieval baseline.