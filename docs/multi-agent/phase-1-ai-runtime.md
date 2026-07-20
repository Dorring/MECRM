# Phase 1: AI Runtime Mode 与 Provider 边界统一

> **分支:** `feat/ma-01-ai-runtime-mode`
> **状态:** 审核修订完成
> **依赖:** Phase 0（`docs/multi-agent/current-state-audit.md`、`docs/multi-agent/implementation-map.md`）

---

## 1. 交付摘要

Phase 1 建立了三种 AI 运行模式（`disabled`、`deterministic`、`live`），统一了全仓 Provider 创建边界，新增了确定性 Provider 用于 CI 和离线测试，并将 AI_MODE 门控扩展到 Voice/STT 路径和向量集合命名。

### 核心交付

| 交付 | 位置 |
|---|---|
| `AIMode` / `AIProvider` / `AgentOrchestrationMode` 枚举 + `AIConfigurationError` | `agents/src/orchestrator/ai_mode.py` |
| `DeterministicChatProvider` / `DeterministicEmbeddingsProvider` | `agents/src/intelligence/deterministic_provider.py` |
| `DisabledChatProvider` / `DisabledEmbeddingsProvider` | `agents/src/intelligence/providers.py` |
| `DisableWhisperSTT` / `DeterministicWhisperSTT` — Voice AI_MODE 门控 | `agents/src/intelligence/i18n/voice_ingest.py` |
| AI_MODE 门控（`create_chat_model` / `create_embeddings`） | `agents/src/intelligence/providers.py` |
| `provider_health_check()` + `/ready` 端点（HTTP 200/503） | `agents/src/intelligence/providers.py` + `orchestrator/main.py` |
| 向量集合隔离（`vector_collection_name`） | 接入 5 个向量写入/查询路径 |
| 配置模型 + `load_dotenv()` 顺序修复 | `agents/src/orchestrator/config.py`、`main.py`、`.env.example`、`docker-compose.yml` |
| Phase 1 测试（54 pass, 0 fail） | `agents/tests/unit/test_ai_mode.py` |

---

## 2. 三种 AI_MODE

| AI_MODE | 行为 | 网络访问 | Chat Model | Embeddings | Voice/STT |
|---|---|---|---|---|---|
| `disabled` | 不初始化模型；AI 调用抛出 `AIModeDisabledError` | ❌ 否 | `DisabledChatProvider` | `DisabledEmbeddingsProvider` | `DisabledWhisperSTT`（返回 unavailable） |
| **`deterministic`（默认）** | 本地确定性 Provider；同输入→同输出 | ❌ 否 | `DeterministicChatProvider` | `DeterministicEmbeddingsProvider` | `DeterministicWhisperSTT`（固定 Fixture） |
| `live` | 根据 `AI_PROVIDER` 调用真实模型 | ✅ 是 | `ChatOllama` 或 `ChatOpenAI` | `OllamaEmbeddings` 或 `OpenAIEmbeddings` | `WhisperSTT`（使用 `WHISPER_URL`） |

**默认值：`AI_MODE=deterministic`** — 默认启动不连接任何外部模型服务，CI 友好。

---

## 3. Provider 配置矩阵

```
AI_MODE=disabled
  → AI_PROVIDER 被忽略
  → 无模型初始化
  → /ready 返回 HTTP 200, status=ready, checks={chat_model: skipped, embedding_model: skipped}

AI_MODE=deterministic（默认）
  → AI_PROVIDER 被忽略
  → 本地确定性 Provider
  → /ready 返回 HTTP 200, status=ready
  → metadata 显示真实 deterministic 模型名：
    chat_model=deterministic-chat-v1
    embedding_model=deterministic-embed-v1

AI_MODE=live
  → 必须配置 AI_PROVIDER（不再有默认值）
  → AI_PROVIDER=ollama → ChatOllama + OllamaEmbeddings
  → AI_PROVIDER=nvidia_nim → ChatOpenAI + OpenAIEmbeddings（需 API Key）
  → 未配置 Provider → AIConfigurationError（Factory 层）
  → /ready 返回 HTTP 503, status=unavailable
  → Provider 不可用 → /ready 返回 HTTP 503, status=degraded
```

---

## 4. 非法配置 Fail Fast

| 场景 | 行为 |
|---|---|
| `AI_MODE` 未设置或空 | → `deterministic`（安全默认） |
| `AI_MODE` 为合法值 | → 正常 |
| `AI_MODE` 为非法非空值 | → `AIConfigurationError`（不再静默切换） |
| `AI_MODE=live` 且 `AI_PROVIDER` 未设置 | → `AIConfigurationError` |
| `AI_MODE=live` 且 `AI_PROVIDER` 为非法值 | → `AIConfigurationError` |
| `AI_MODE` 未设置但 `AI_PROVIDER` 已设置（旧配置） | → `deterministic` + 一次兼容警告日志 |

**不再有隐式 Ollama 连接。** `AI_PROVIDER` 默认值为空字符串——必须显式设置。

---

## 5. Ollama 启动方式

Ollama **不随默认 Compose 启动**。

```bash
# 默认启动（无 Ollama，无模型网络访问）
docker compose up -d

# 需要 Ollama 时显式启用 local-llm profile
docker compose --profile local-llm up -d ollama
```

启动 Ollama 后，需同时设置 `AI_MODE=live` 和 `AI_PROVIDER=ollama` 才能连接。

---

## 6. Deterministic Provider 限制

`DeterministicChatProvider` 和 `DeterministicEmbeddingsProvider`：

- ✅ 同输入→同输出（SHA-256 哈希派生）
- ✅ 固定 Embedding 维度（768）
- ✅ 支持多种输入格式：`str`、LangChain Message、`list[Message]`、`dict(content=...)`、`dict(messages=[...])`
- ✅ 支持 Fixture 注入（`register_fixture`）
- ✅ 支持故障注入（`error/timeout`、`error/empty`、`error/malformed`、`error/low_confidence`、`error/provider_error`）
- ✅ Provider Metadata 稳定：`provider=deterministic`、`model=deterministic-chat-v1`
- ❌ 输出不代表真实语义质量
- ❌ 不应用于声称真实语义检索效果
- ❌ 不替代 PostgreSQL、RLS、OPA、Kafka、Weaviate、审批、审计、业务工具

---

## 7. Health / Readiness 语义

| 端点 | HTTP | 用途 | Ollama 不可用时 |
|---|---|---|---|
| `GET /health` | 200 | 进程存活 + HTTP 响应 | ✅ 正常（不检查模型） |
| `GET /ready` | 200/503 | 服务就绪（含 AI Provider 健康） | 503 |

`/ready` 返回示例（deterministic 模式，HTTP 200）：

```json
{
  "ai_mode": "deterministic",
  "provider": "none",
  "chat_model": "deterministic-chat-v1",
  "embedding_model": "deterministic-embed-v1",
  "remote": false,
  "status": "ready",
  "checks": {
    "chat_model": "available",
    "embedding_model": "available",
    "embedding_dimension": 768
  }
}
```

`/ready` 返回示例（live 模式未配置 Provider，HTTP 503）：

```json
{
  "ai_mode": "live",
  "provider": "none",
  "status": "unavailable",
  "error": "AI_MODE=live requires AI_PROVIDER to be set to 'ollama' or 'nvidia_nim'"
}
```

- Chat Model 和 Embedding Model **分别**检查 Ollama（不同模型！）
- NIM 401/403 → `degraded`；404 → `degraded`；5xx → `degraded`
- 不泄露 API Key、Authorization Header 或完整内部异常

---

## 8. 向量集合命名和重新索引策略

### 命名规则

集合名格式：`{BaseCollection}_{mode_or_provider}_{model_fingerprint}`

- `disabled` 模式：`{BaseCollection}_disabled`
- `deterministic` 模式：`{BaseCollection}_deterministic_embed_v1`
- `live` + ollama：`{BaseCollection}_ollama_{model_hash_8chars}`
- `live` + nvidia_nim：`{BaseCollection}_nvidia_nim_{model_hash_8chars}`

### 接入路径

| 模块 | 集合 | 位置 |
|---|---|---|
| WeaviateChatMemory | `ChatMemory_*` | `chat/memory.py` |
| AuditIndexer | `AuditEmbedding_*` | `compliance/audit_indexer.py` |
| KnowledgePublisher | `KnowledgeBase_*` | `knowledge/publisher.py` |
| VectorSearch | `CrmEntity_*` / `KnowledgeBase_*` | `chat/tools/vector_search.py` |
| HybridRetriever | `CrmEntity_*` / `KnowledgeBase_*` | `search/retriever.py` |

### 重新索引要求

- **deterministic 向量不会与 live 向量混在同一集合中。**
- **不同 Embedding Model 不会共享同一集合。**
- **切换 `AI_MODE`、`AI_PROVIDER` 或 Embedding Model 后，必须重新索引所有 Weaviate 数据。**
- 如需迁移现有向量，使用 `agents/src/intelligence/knowledge/reindex.py`。

---

## 9. 被统一的 Provider 调用点

替换前有 **6 处** `os.getenv("OLLAMA_*")` 或 `_env("OLLAMA_*")` 绕过 `Settings`。

| 文件 | 修复方式 |
|---|---|
| `support.py` | `os.getenv(...)` → `settings.OLLAMA_EMBED_MODEL`；移除 `import os` |
| `chat_agent.py` | `_env(...)` → `settings.OLLAMA_EMBED_MODEL`；移除 `_env` 函数 |
| `knowledge_agent.py` | 同上 |
| `search_agent.py` | 同上 |
| `publisher.py` | 同上 |
| `audit_indexer.py` | 同上 |
| `compliance_agent.py` | 同上 |
| `main.py` | 同上；移除 `_env` 函数 |

**核心运行路径已无直接 OLLAMA 环境变量读取。** 所有 Chat/Embedding 创建均通过 `create_chat_model()` / `create_embeddings()` 统一工厂。

**Voice 路径已接入 AI_MODE：** `WhisperSTT` 不再默认回退到 `OLLAMA_URL`，`get_stt()` 根据 AI_MODE 返回对应的 STT Provider。

---

## 10. 尚未统一的历史 Provider 耦合点

| 文件 | 耦合方式 | 状态 |
|---|---|---|
| `evaluate_retrieval.py:133-134` | 直接使用 `settings.OLLAMA_URL` / `settings.OLLAMA_EMBED_MODEL` | **不迁移**：评测脚本，允许直接访问 Settings |
| `config.py:63-65` | `OLLAMA_URL` / `OLLAMA_MODEL` / `OLLAMA_EMBED_MODEL` 定义 | **保留**：Settings 是单点定义，合法 |

---

## 11. 兼容策略

| 旧配置 | 行为 |
|---|---|
| `AI_PROVIDER=ollama`（无 `AI_MODE`） | 默认 `AI_MODE=deterministic`，输出一次兼容警告日志（进程生命周期内仅一次） |
| `AI_PROVIDER=nvidia_nim`（无 `AI_MODE`） | 同上 |
| `create_embeddings(ollama_url=..., embedding_model=...)` | 旧参数名保留为 deprecated alias，新代码建议用 `base_url` / `model` |
| `WhisperSTT` 默认 `OLLAMA_URL` | **已移除**：必须设置 `WHISPER_URL` 或传入 `whisper_url` 参数 |

---

## 12. 测试覆盖

| 测试类别 | 数量 | 状态 |
|---|---|---|
| AI_MODE 配置解析（含 fail-fast） | 10 | ✅ |
| Provider Factory 工厂 | 8 | ✅ |
| 网络隔离（Mock HTTP，跨平台） | 3 | ✅ |
| Voice/STT 隔离（Mock HTTP，跨平台） | 2 | ✅ |
| Deterministic Chat 输入解析（str/dict/list） | 8 | ✅ |
| Deterministic Chat 故障注入 | 5 | ✅ |
| Deterministic Embeddings | 5 | ✅ |
| Readiness / Health（含 HTTP 503、NIM 401） | 7 | ✅ |
| 向量集合隔离 | 3 | ✅ |
| Disabled Provider 行为 | 3 | ✅ |
| **总计** | **54** | **54 pass / 0 fail / 0 skip** |

**所有 54 项测试无需 Ollama、无 NVIDIA API Key，跨平台运行。**

### 实际测试命令与结果

```bash
# Phase 1 单元测试
pytest tests/unit/test_ai_mode.py -v          # 54 passed

# 现有回归测试（排除需要 PostgreSQL 的）
pytest tests/ -v --ignore=tests/chaos --ignore=tests/integration --ignore=tests/unit
                                               # 144 passed, 29 skipped, 3 errors (DB only)
```

### 未运行的测试及原因

| 测试 | 原因 |
|---|---|
| `tests/chaos/` | 需要完整 Docker Compose 环境（Kafka、Redis、Postgres） |
| `tests/integration/` | 同上 |
| `tests/test_schema_contract.py` | 需要 PostgreSQL（本地未启动） |

---

## 13. 本阶段禁止事项验证

- [x] 不实现 Supervisor
- [x] 不实现 AgentRegistry
- [x] 不实现 Planner
- [x] 不实现 Specialist
- [x] 不修改 Kafka Topic 路由
- [x] 不修改 OPA 权限语义
- [x] 不修改 RLS
- [x] 不修改审批状态机
- [x] 不新增业务数据库表
- [x] 不默认启动 Ollama
- [x] 不保存 Chain-of-thought
- [x] 不大规模重构与 Provider 无关的代码
