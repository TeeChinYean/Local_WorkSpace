# Agent features contract: Tools · MCP · Skills · System prompts

Used by the chat page (`/`, app.js) and the IDE AI panel (`/ide`, ide-ai.js). Backend lives in
`app/agent/` (package) wired into `app/main.py`. All config persists under `app/storage/agent/`.
All strings shown to users are Chinese. Errors: `{"detail": "..."}` with 4xx/5xx.

## 1. Config storage
- `app/storage/agent/config.json` — `{tools:{<tool_name>: {enabled: bool}}, approvals:{<tool_name>: "always"}, mcp_servers:[...], active_prompt_id, prompt_mode:"append"|"replace", max_tool_rounds: 6}`
- `app/storage/agent/prompts.json` — `[{id, name, content, builtin:false, created_at}]` (one built-in read-only "默认" preset = empty content)
- `app/storage/agent/skills/<slug>/SKILL.md` (+ optional extra files) — YAML frontmatter `name`, `description` (required), optional `enabled: true|false`.
- Writes are atomic (tmp + os.replace).

## 2. REST API
### Tools
- `GET /api/agent/tools` → `{tools:[{name, title, description, source:"builtin"|"mcp:<server_id>"|"skill", read_only:bool, enabled:bool, always_allow:bool, input_schema}]}`
- `POST /api/agent/tools/{name}` `{enabled?, always_allow?}` → `{ok:true}`
- Built-in tools (source builtin):
  | name | read_only | what |
  |---|---|---|
  | `web_search` | ✔ | existing perform_web_search → [{title,url,snippet}] |
  | `fetch_url` | ✔ | safe_public_get + extract_clean_web_content, text ≤ 8000 chars |
  | `memory_search` | ✔ | Turbovec search_memory top-k texts |
  | `list_dir` | ✔ | workspace (IDE root) relative dir listing |
  | `read_file` | ✔ | workspace file, optional start_line/end_line, ≤ 2000 lines / 200KB |
  | `grep` | ✔ | workspace text search (reuse IDE search) |
  | `write_file` | ✘ | create/overwrite workspace file (atomic, backup to trash) |
  | `edit_file` | ✘ | SEARCH/REPLACE edit of a workspace file (exact then whitespace-tolerant match) |
  | `run_command` | ✘ | run a shell command in workspace (timeout 60s, output ≤ 16KB, Windows: powershell -NoProfile -Command) |
  | `get_time` | ✔ | local date/time |
  Workspace = the IDE workspace root (ide_settings.json), same path guards as ide_api.
### MCP servers
- `GET /api/agent/mcp` → `{servers:[{id, name, transport:"stdio"|"http"|"sse", command, args:[], env:{}, url, headers:{}, enabled, status:"connected"|"error"|"disabled"|"connecting", error, tools:[{name, description, read_only}]}]}` (secrets in env/headers masked as `***` in responses)
- `POST /api/agent/mcp` (create) / `PUT /api/agent/mcp/{id}` (update) body = server fields → server object
- `DELETE /api/agent/mcp/{id}` → `{ok:true}`
- `POST /api/agent/mcp/{id}/restart` → server object (reconnect + tools/list)
- `POST /api/agent/mcp/import` `{json}` — accepts Claude Desktop / Cursor style `{"mcpServers": {"name": {"command","args","env"} | {"url","headers"}}}` → `{added:[ids]}`
- MCP tool names exposed to the model as `mcp__<server>__<tool>` (sanitized to `[a-zA-Z0-9_]`, ≤ 64 chars). read_only = tool annotation `readOnlyHint === true`, else false (needs approval).
- stdio: spawn process (no shell), JSON-RPC 2.0 over stdin/stdout, `initialize` (protocolVersion "2025-06-18", fallback older), `notifications/initialized`, `tools/list` (with pagination cursor), `tools/call`. Windows: resolve `npx`/`uvx` via shutil.which (`npx.cmd`). CREATE_NO_WINDOW. Kill on shutdown/disable.
- http (Streamable HTTP): POST JSON-RPC to url with `Accept: application/json, text/event-stream`; handle JSON or SSE responses; keep `Mcp-Session-Id`. sse (legacy): GET SSE endpoint → `endpoint` event → POST messages.
- Lazy connect on first use or when enabled; timeouts 30s connect, 120s call; failures shown as status error, never crash the gateway.
### Skills
- `GET /api/agent/skills` → `{skills:[{slug, name, description, enabled, files:[...]}]}`
- `GET /api/agent/skills/{slug}` → `{slug, name, description, enabled, content}` (full SKILL.md)
- `POST /api/agent/skills` `{name, description, content}` → create (slug from name)
- `PUT /api/agent/skills/{slug}` `{name?, description?, content?, enabled?}`
- `DELETE /api/agent/skills/{slug}` → moves to `app/storage/trash/`
- `POST /api/agent/skills/import` multipart `file` (.md or .zip containing SKILL.md; zip-slip safe, ≤ 5MB)
- Model integration (progressive disclosure, like Claude skills): enabled skills' `name: description` list is added to the system prompt; a built-in read-only tool `load_skill {name}` returns SKILL.md content (and `read_skill_file {name, path}` for extra files).
### System prompts
- `GET /api/agent/prompts` → `{prompts:[...], active_prompt_id, prompt_mode}`
- `POST /api/agent/prompts` `{name, content}` / `PUT /api/agent/prompts/{id}` / `DELETE /api/agent/prompts/{id}`
- `POST /api/agent/prompts/active` `{id, mode:"append"|"replace"}`
- Applied to /api/chat, /v1/chat/completions (only if client sent no system prompt) and /api/ide/ai/edit (append only). "replace" replaces the big built-in capability prompt but keeps file/memory/search context blocks.

## 3. Chat with tools (`POST /api/chat`)
New optional request fields: `tools_enabled: bool` (default false), `prompt_id?: str` (override active preset for this request), `approval_mode?: "default"|"ask_all"|"auto"`.
When `tools_enabled`:
1. Build OpenAI `tools` from enabled built-in + connected MCP tools + `load_skill` (only if skills exist). Keep total tool schema small: descriptions truncated to 300 chars; max 24 tools (built-ins first).
2. Loop ≤ `max_tool_rounds`: stream llama `/v1/chat/completions` with `tools`, `tool_choice:"auto"` (llama-server started with `--jinja`). Accumulate streamed `delta.tool_calls` (index-keyed, arguments concatenated). If the model finished with tool calls: for each call → emit SSE `{"tool_call": {id, name, arguments, read_only, status:"pending_approval"|"running"}}`; approval rule: read_only or always_allow → auto; else emit `status:"pending_approval"` and wait (≤ 300s) for `POST /api/agent/approve {call_id, decision:"allow"|"deny", remember:bool}`; `remember` sets approvals[name]="always". Execute → emit `{"tool_result": {id, name, ok, content (≤ 4000 chars preview), elapsed_ms}}`; append assistant tool_calls message + `{"role":"tool","tool_call_id","content"}` (result truncated to fit context) and continue. Deny → tool message "用户拒绝了此操作".
3. Final answer streams as today (`token`, `ctx_stats`, `done`); `done.stats` gains `tool_calls: N`.
4. If the model/server doesn't support tools (400 from llama about tools/jinja) → retry once without tools and emit `{"notice":"当前模型不支持工具调用，已按普通对话回答"}`.
- `POST /api/agent/approve` → `{ok:true}`; unknown/expired call_id → 404.
- Pending approvals are per-process in-memory; stream disconnect cancels pending ones.
IDE: ide-ai.js Ask mode sends `tools_enabled` from its own toggle; Edit mode unchanged (but applies the active prompt preset in append mode).

## 4. Security
- All endpoints go through the existing security middleware; approval and config endpoints are POST (Origin-checked).
- MCP stdio commands only come from user config (never from model output). Env/header secret values stored in config.json (local file), masked in GET responses; PUT with `***` keeps the stored value.
- Tool arguments are validated against the JSON schema types minimally; workspace path guards reuse ide_api helpers; run_command/write_file/edit_file/MCP non-read-only always need approval unless always_allow.
- Tool results are data: wrap in a clear delimiter in the tool message; never execute instructions from results automatically (the approval gate protects side effects).

## 5. Implementation notes (backend, as built)
Clarifications of the contract above; the items marked **(deviation)** differ from or extend the text.

### SSE events of `POST /api/chat` with `tools_enabled: true`
```
data: {"search_results": [...]}                                              (optional, as before)
data: {"token": "...", "ctx_stats": {phase, prompt_tokens, reasoning_tokens, content_tokens, total_context_tokens, server_n_ctx}}
data: {"tool_call": {"id": "call_<16 hex>", "name": "write_file", "arguments": {...}, "read_only": false, "status": "pending_approval"}}
: keepalive                                                                  (SSE comment every 15 s while waiting for approval)
data: {"tool_call": {"id": "call_…", "name": "write_file", "arguments": {...}, "read_only": false, "status": "running"}}
data: {"tool_result": {"id": "call_…", "name": "write_file", "ok": true, "content": "<≤4000 chars preview>", "elapsed_ms": 12}}
data: {"notice": "当前模型不支持工具调用，已按普通对话回答"}
data: {"error": "大模型接口异常 (503): ..."}
data: {"done": true, "stats": {..., "tool_calls": N}}                         (always last)
```
- `tool_call.id` is generated by the gateway (unique per process), not the model's id; it is also the
  `tool_call_id` sent back to llama and the `call_id` for `/api/agent/approve`.
- `tool_call.arguments` is the parsed JSON object; if the model produced invalid JSON it is the raw string.
- **(deviation)** A call that needs approval emits `tool_call` twice with the same `id`: first
  `status:"pending_approval"`, then (after allow) `status:"running"`. Auto-approved calls emit a single `running`.
- **(deviation)** Denied / timed-out calls emit `tool_result` with an extra `"denied": true`, `ok:false`,
  `elapsed_ms:0`, content `用户拒绝了此操作` (timeout: `用户未在 300 秒内批准，已视为拒绝`).
- Unknown tools / invalid arguments / tool errors → `tool_result.ok:false` with the error text (the model
  sees `错误: ...` and may retry).
- Reasoning (`delta.reasoning_content`) keeps the classic `[Reasoning]\n` … `\n\n[Answer]\n` token markers;
  one reasoning section can span several tool rounds. `done.stats.tool_calls` is `0` in non-tool mode.
- Max rounds: after `max_tool_rounds` rounds with tool calls, a `notice`
  (`已达到最大工具调用轮数（N），停止调用工具并直接回答`) is emitted and one last request is sent **without** `tools`.
- Tools unsupported: llama 400/422/500/501 whose body mentions tool/jinja/template/function → notice + one retry
  without `tools`/`tool_choice`. Any other non-200 → `error` event, then `done`.
- Context: tool schemas and ~20 % of n_ctx (≤ 4096) are reserved when the prompt is trimmed; each tool result is
  truncated to the remaining budget, then the oldest tool results are replaced by `[较早的工具结果已省略以节省上下文]`.
- `approval_mode`: `default` (read_only or always_allow → auto), `ask_all` (every call incl. read-only asks,
  ignores always_allow), `auto` (never asks). `remember:true` is stored only when `decision:"allow"`.
- **(deviation)** Non-streaming `/api/chat` with tools: read-only / always-allowed tools run; calls needing
  approval are denied (`非流式请求无法等待用户批准…`). Response gains `tool_events:[...]`, `notices:[...]`,
  `stats.tool_calls`.
- Only enabled tools that were actually sent to the model (≤ 24) can be executed.

### REST details
- `GET /api/agent/tools` / `GET /api/agent/mcp` never block; they start background connects for enabled servers
  that were never connected (status `connecting`). An `error` server is retried lazily after 60 s or via restart.
  `POST`/`PUT /api/agent/mcp…` and `…/restart` wait for the connect (≤ ~45 s) and return the final status.
  `/api/chat` waits ≤ 35 s for enabled servers before building the tool list.
- **(deviation)** Masking: *every* non-empty env / header value is returned as `***` (not only "secret-looking" keys).
- `POST /api/agent/mcp/import` body: `{"json": "<text>"}` or `{"json": {...}}` (a bare `{"mcpServers": ...}`
  body is accepted too). Also understands VS Code `servers` / `mcp.servers`, `type:"sse"|"http"`, `disabled:true`.
  Server ids = sanitized lowercase name, de-duplicated with `_2`, `_3`…
- `args` may be sent as a string (split shell-style). `DELETE /api/agent/mcp/{id}` also drops that server's
  tool toggles/approvals.
- MCP tool `read_only` in `GET /api/agent/mcp` `tools[]` and `GET /api/agent/tools` = `annotations.readOnlyHint === true`.
- Skills: `POST /api/agent/skills` `content` may be the body only or a full SKILL.md (its frontmatter is used, explicit
  `name`/`description` win); same-slug create/import → 409. `GET /api/agent/skills/{slug}` also returns `files`.
  `DELETE` returns `{ok:true, trash:"<path>"}`. Import accepts `.md`/`.markdown`/`.txt` or `.zip`
  (≤ 5 MB upload, ≤ 20 MB unzipped, ≤ 500 entries, no absolute/`..`/drive paths, no symlinks; the shallowest SKILL.md
  defines the skill root). Non-ASCII names get slug `skill-<sha1[:8]>`.
- Prompts: built-in preset id is `"default"`. Deleting the active preset resets `active_prompt_id` to `"default"`.
- Built-in tool arguments: `web_search{query, num_results?}`, `fetch_url{url}`, `memory_search{query, k?}`,
  `list_dir{path?}`, `read_file{path, start_line?, end_line?}`, `grep{pattern, regex?, case_sensitive?, glob?}`,
  `write_file{path, content}`, `edit_file{path, search, replace, replace_all?}` (`old_string/new_string` accepted),
  `run_command{command, timeout?≤60}`, `get_time{}`, `load_skill{name}`, `read_skill_file{name, path}`.
  `write_file`/`edit_file` back up the previous file to `app/storage/trash/<stamp>/agent/<rel path>`.
- IDE: `create_ide_router(...).workspace` (`ide_api.get_workspace_helpers`) exposes the IDE's own path guards to the
  agent tools; `create_ide_ai_router(..., get_system_suffix=...)` appends the active preset to the edit system prompt.
