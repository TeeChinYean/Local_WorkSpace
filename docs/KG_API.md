# Knowledge Graph memory (Graph RAG) contract

Used by the chat page (`/`, app.js → Settings → 知识图谱, plus a one-line status under 长期记忆 in the sidebar).
Backend lives in `app/kg/` (package, never imports `main`) wired into `app/main.py` through `KGDeps`.
All data persists under `app/storage/kg/`. User-facing strings are Chinese. Errors: `{"detail": "..."}` with 4xx.

Idea: while the user is **not** talking to the assistant, the local LLM reads new memory entries
(chat turns, file chunks, web results) and extracts `entities` + `relations`. At answer time the
entities mentioned in the question (plus name-embedding neighbours) are expanded 1–2 hops and the
relations are added to the per-turn context as a compact `【知识图谱】` block.

## 1. Storage
- `app/storage/kg/graph.db` — stdlib `sqlite3`, WAL, `foreign_keys=ON`, one shared connection behind an RLock (thread-safe).
  | table | columns |
  |---|---|
  | `entities` | `id, name, norm_name UNIQUE, type, description (≤40), mention_count, created_at, updated_at, name_vec BLOB NULL` |
  | `aliases` | `norm_name PK, entity_id → entities ON DELETE CASCADE` (names merged into another entity) |
  | `relations` | `id, head_id, relation (≤24, lowercased), tail_id, weight, created_at, UNIQUE(head_id, relation, tail_id)` — FK cascade |
  | `evidence` | `id, relation_id, source_kind 'chat'\|'file'\|'web', source_ref, snippet (≤300), created_at, UNIQUE(relation_id, source_kind, source_ref)` |
  | `queue` | `id, kind, ref, text, hash UNIQUE (sha1 of text), status 'pending'\|'running'\|'done'\|'error', attempts, error, created_at, updated_at` |
  | `meta` | `key, value` (`last_error`, `last_error_at`, `last_run_at`, `vec_dim`) |
- Name normalisation: NFKC → lowercase → drop whitespace, punctuation (Unicode `P*`/`Z*`) and control chars. CJK and symbols such as `+` are kept (`「项目 A」` → `项目a`, `ＧＰＴ－４` → `gpt4`).
- Upsert by `norm_name` (or alias): `mention_count += 1`, an `other` type / empty description gets filled in by later mentions.
- Relations dedupe on `(head, relation, tail)`; `weight += 1` only when a *new* evidence source is added (the same memory entry / file never counts twice). Self loops are dropped.
- `source_ref`: memory_history index for chat/web entries, file name for file chunks (`【文件切片 - name (i/n)】`, `【文件全局…- name】`; `🌐 …` virtual web files count as `web`).
- `app/storage/kg/settings.json` — `{enabled: true, paused: false, idle_seconds: 20}` (atomic tmp + fsync + os.replace; corrupt file → defaults). `idle_seconds` ∈ [5, 3600].

## 2. Pipeline
### Enqueue
- `main.add_memory_entries` → `kg_service.enqueue_memory(entries, start_index)` after the vectors/texts are appended (turn records, file chunks, web results). Skipped when KG is disabled, for entries < 40 chars, and for duplicates (text hash). Failures never affect the memory write.
- `POST /api/kg/rebuild {mode:"missing"}` enqueues every existing `memory_history` entry that is not queued yet; `mode:"all"` clears the graph (entities, relations, evidence, aliases, queue) and re-enqueues everything.

### Idle extractor (`KGWorker`, asyncio task started in `lifespan`, stopped on shutdown)
- Checks every 2 s (1 s between consecutive jobs; no busy loop). A job runs only when **all** hold:
  enabled · not paused · no user LLM request in flight and the last one ended ≥ `idle_seconds` ago ·
  `state._switching_model` false · llama healthy (`state._live_n_ctx` known) · `n_ctx ≥ 4096` · a pending job exists.
  `GET /status` reports the failing condition as `waiting`: `disabled | paused | busy | switching | llm_unavailable | ctx_too_small | queue_empty | ready`.
- One job at a time. Text capped at 1500 chars. Request to llama `/v1/chat/completions`:
  `stream: true, temperature: 0, max_tokens: 512, cache_prompt: false, chat_template_kwargs.enable_thinking: false,
  response_format: {type: "json_object"}` + a bilingual system prompt asking for STRICT JSON
  `{"entities":[{"name","type":"person|org|place|concept|file|code|product|event|other","description"≤40}],"relations":[{"head","relation","tail"}]}`
  and `/no_think` in the user message. If llama rejects `response_format` (400/422/500) the request is retried once without it and JSON mode stays off for the process.
- Parsing (`parse_extraction`): strips `<think>…</think>`, prefers ```json fences, takes the first balanced `{…}`, tolerates trailing commas / smart quotes / surrounding prose, accepts `[head, rel, tail]` lists and `source/target` keys; caps 16 entities / 16 relations. Invalid output → job `error` (message stored in `last_error`).
- After a successful job, entity name vectors are computed with the existing ONNX embedder in batches of 16 (≤ 64 per job) and cached in `entities.name_vec`.
- **Pre-emption**: an ASGI middleware (`ActivityMiddleware`) wraps `POST /api/chat` (incl. the agent tool loop), `/v1/chat/completions`, `/v1/completions`, `/api/ide/ai/*` for the whole response (streaming included). At request start it increments the activity counter and synchronously calls `worker.preempt()`, which cancels the running extraction task → the httpx stream is closed → llama-server stops generating (single slot, `-np 1`) before the user's request is forwarded. The job goes back to `pending` with `attempts + 1`; after 3 attempts it becomes `error`. The stream loop also checks the counter on every line as a second guard.
- On startup `running` jobs are reset to `pending`.

### Retrieval (Graph RAG) in `/api/chat`
1. Only when KG is enabled and has ≥ 1 relation.
2. Seeds: substring matches of normalised entity names/aliases (≥ 2 chars) in the normalised question, longest first, names inside an already matched longer name skipped, max 8; plus the top-3 entities by cosine of name vectors vs. the question vector with cos ≥ 0.6.
3. Relations touching the seeds (1 hop); if fewer than 4, the neighbours' relations are added (2 hops).
4. Rank: `weight + 0.5·evidence_count` + 3 (both ends are seeds) / 1.5 (one end) / 0.5 (touches a 1-hop neighbour).
5. Format `【知识图谱】\n- A —关系→ B` lines until `count_text_tokens(block) ≤ 500` (`main.KG_BLOCK_MAX_TOKENS`), max 40 lines.
6. The block goes into `per_turn` (the **last user message**, before the Turbovec memory slices) so the system prompt stays a byte-identical, KV-cacheable prefix. Trim order when over budget: file bodies → outlines → **KG block** → memory slices → history turns.

## 3. REST API (`/api/kg`)
Router dependency calls `check_request_security` (same as the agent router); POST/DELETE are additionally Origin-guarded by the global middleware (cross-site → 403).
- `GET /api/kg/status` → `{enabled, paused, idle_seconds, running, waiting, entities, relations, queue:{pending, running, done, error}, last_error, last_error_at, last_run_at, idle_for}`
- `POST /api/kg/settings` `{enabled?, paused?, idle_seconds?}` → status (400 when idle_seconds ∉ [5, 3600]). Disabling / pausing aborts a running extraction.
- `GET /api/kg/entities?q=&limit=50&offset=0` → `{items:[{id, name, type, description, mention_count, relation_count, created_at, updated_at}], total, limit, offset}` — `q` matches name / normalised name / description / aliases; sorted by relation_count, mention_count, name. `limit` ≤ 200.
- `GET /api/kg/entities/{id}` → `{entity:{…, aliases:[norm names]}, relations:[{id, relation, weight, direction:"out"|"in", head:{id,name,type}, tail:{id,name,type}, evidence_count, evidence:[{source_kind, source_ref, snippet}] (latest 5)}]}` · 404
- `DELETE /api/kg/relations/{id}` → `{ok:true}` · 404
- `DELETE /api/kg/entities/{id}` → `{ok:true, deleted_relations:n}` (relations, evidence, aliases cascade) · 404
- `POST /api/kg/entities/merge` `{keep_id, merge_ids:[…]}` → `{ok:true, entity}` — relations are re-pointed to `keep_id` (duplicates combined: evidence moved, weights added; self loops dropped); merged names become aliases of `keep_id`. 400 empty `merge_ids`, 404 unknown id.
- `POST /api/kg/rebuild` `{mode:"missing"|"all"}` → `{ok:true, mode, enqueued}` · 400 other modes
- `POST /api/kg/queue/clear_errors` → `{ok:true, cleared}` (deletes `error` jobs; a later `rebuild missing` retries them)
- `GET /api/status` additionally returns `kg: {enabled, paused, running, entities, relations, pending}` for the sidebar line.

## 4. UI (Settings → 知识图谱)
- Help text: 助手会在你不对话时，把记忆整理成「人物/项目/概念」之间的关系，回答时一起参考。
- Status card: dot + 空闲中 / 正在后台整理 / 已暂停 / 已关闭, `实体 N · 关系 M · 待处理 K (· 失败 E)`, a plain-language hint from `waiting`, collapsible last error.
- Switches 启用 / 暂停, select 空闲多久后开始整理 (10 s / 20 s / 1 min / 5 min) — saved instantly.
- Buttons 整理已有记忆 (rebuild missing), 重新整理全部 (confirm dialog, rebuild all), 清除失败任务 (only when errors exist).
- Entity browser: search (debounced) → list (name, type chip, relation/mention counts, 加载更多) → detail view (← 返回列表, relations `A —rel→ B` with 删除 and collapsible evidence labelled 对话/文件/网页, clickable other end, 合并到… with a searchable target select, 删除实体 with confirm).
- `/api/kg/status` is polled every 3 s only while the tab is visible; polling stops on tab switch / modal close.
- All model-derived text is written with `textContent` (`el()`), never `innerHTML`. No horizontal scroll at 390 px.

## 5. Resource notes
- sqlite only (no networkx); retrieval caches the name list / vector matrix per store version.
- Embeddings reuse `state.embedder` (ONNX) in batches of 16.
- `cache_prompt:false` avoids polluting llama's prompt cache with extraction prompts, but with a single slot the
  next chat turn after an extraction still re-processes its prompt once (llama.cpp has no per-request slot save/restore without `--slot-save-path`).
