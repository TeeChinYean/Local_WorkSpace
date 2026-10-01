# 功能说明

### 上下文自动加长至显存红线 (Auto Context Expansion)
- 说明：llama-server 启动时预分配全部 KV，运行中显存固定；因此在启动时把 `-c` 设到“总显存 − 204MB”红线以内的最大值，显存还有余量就自动加长。
- 涉及文件/模块：`llm_models.json`、`app/llm_launcher.py`、`run_local_llm.ps1`、`app/main.py`（`restart_native_llama_server`、`/api/model`）
- 实现要点：真实 KV = `kv_layers × kv_heads × head_dim × (K+V 字节)`（q8_0=34/32、q4_0=18/32 字节）；启动后 nvidia-smi 实测，余量 ≥2k tokens 则加长重启一次、超红线则缩小、启动失败减半重试；实测非 KV 开销（需 ≥ 模型文件 50% 才可信）写入 `app/storage/ctx_calibration.json`，下次直接命中；KV 类型保持 q8_0 / q4_0。
- 相关测试：`app/tests/test_llm_launcher.py`
- 状态：Done

### 安全防护 (Security Guard)
- 说明：默认只监听本机；局域网访问需令牌；防 CSRF / DNS rebinding / SSRF / 路径穿越；llama-server 仅本机 + API Key。
- 涉及文件/模块：`app/main.py`（`check_request_security`、`_is_allowed_path`、`_is_allowed_write_path`、`safe_filename`、`storage_path`、`assert_public_url`）、`app/llm_launcher.py`（`get_api_key`）
- 实现要点：环境变量 `TV_HOST` / `TV_API_TOKEN` / `TV_ALLOWED_ORIGINS` / `TV_ALLOWED_HOSTS` / `ALLOWED_LOCAL_DIRS` / `TV_ALLOW_ALL_DRIVES`；路径用 realpath + normcase + commonpath；写回禁止服务自身代码、`.git`、根目录启动脚本与 `llm_models.json`。
- 相关测试：`app/tests/test_security.py`、`app/tests/e2e/test_ui_smoke.py`
- 状态：Done

### 后台运行时监控 (Runtime Monitor)
- 说明：所有阻塞探测（nvidia-smi、psutil、推理引擎健康、n_ctx、当前模型）放到后台线程，每 3s 刷新；请求路径只读缓存，事件循环不再被卡住。
- 涉及文件/模块：`app/main.py`（`refresh_runtime_state`、`start_runtime_monitor`、`get_*` 缓存读取函数）
- 实现要点：n_ctx 探测失败不缓存 fallback；模型切换期间监控暂停。
- 相关测试：`app/tests/test_storage_runtime.py::test_status_does_not_block_event_loop`
- 状态：Done

### 记忆库一致性 (Memory Store Consistency)
- 说明：保证向量索引第 i 条永远对应 `memory_history[i]`，断电/关窗口不损坏。
- 涉及文件/模块：`app/main.py`（`_memory_lock`、`add_memory_entries`、`search_memory`、`save_storage`、`rebuild_index_from_history`、`save_turn_to_memory`）
- 实现要点：RLock 包住 add/search/持久化；临时文件 + `os.replace` 原子写；启动时条数不一致自动重建索引；空回答不入库。
- 相关测试：`app/tests/test_storage_runtime.py`
- 状态：Done

### [Chat] Prompt 共享预算与优先级裁剪
- 说明：多个工作区文件共享一个字符预算；超限时依次裁剪 文件正文 → 符号大纲 → 记忆切片 → 历史轮次，系统协议、检索结果与用户问题始终保留。
- 涉及文件/模块：`app/main.py`（`build_file_context_blocks`、`compose_messages`、`resolve_range_target`）
- 实现要点：预算只按真实 n_ctx 计算（KV 已预分配，与“已用显存”无关）；行号切片只作用于被点名的文件并受预算约束；行号识别需带“行/line”单位或整句只有范围。
- 相关测试：`app/tests/test_chat_logic.py`
- 状态：Done

### [Home] 前端安全渲染与流式体验
- 说明：所有服务端/网络数据转义后再插入 DOM；流式渲染每帧最多一次；Stop/Enter 行为修正；窄屏汉堡菜单；去掉高 GPU 开销的模糊与阴影动画；离线系统字体；无障碍焦点与标签；模型标签由 `/api/status` 动态生成。
- 涉及文件/模块：`app/static/app.js`、`app/static/index.html`、`app/static/style.css`
- 实现要点：`escapeHtml()` / `safeUrl()`；requestAnimationFrame 合批；距底部 80px 内才自动滚动；`data-filename` 去重 chip。
- 相关测试：`app/tests/e2e/test_ui_smoke.py`
- 状态：Done

### [IDE] Web IDE 页面 (/ide，仿 Cursor 布局)
- 说明：浏览器内轻量 IDE：左侧活动栏（资源管理器 / 搜索 / 源代码管理 / AI），中间多标签编辑器，底部终端，右侧 AI 对话；聊天首页右上角「⌨ IDE」进入。
- 涉及文件/模块：`app/static/ide.html`、`ide.css`、`ide.js`、`app/static/vendor/`（CodeMirror 6、xterm.js，离线本地化）、`app/main.py`（`/ide` 路由与 router 挂载）
- 实现要点：单个 EditorView 复用 + 每标签 EditorState；最多 12 标签（LRU 关闭未修改的）；目录树懒加载；Ctrl+S 保存带 mtime 冲突检测（409 → 覆盖/重新加载）；Ctrl+P 快速打开；git 状态刷新最多 15s 一次且仅页面可见时；无模糊/无限动画；实测打开 5 个文件 + 1 个终端 JS 堆约 6–10MB。
- 相关测试：`app/tests/e2e/test_ide_smoke.py`、`app/tests/e2e/test_ide_integration.py`
- 状态：Done

### [IDE] 资源管理器与文件操作
- 说明：懒加载目录树、git 状态着色、新建/重命名/删除（删除进入 `app/storage/trash/`，可找回）、全文搜索、快速打开。
- 涉及文件/模块：`app/ide_api.py`（`/api/ide/tree|files|file|fs|search`）
- 实现要点：所有路径相对工作区根，realpath+commonpath 防穿越；>2MB / 二进制不加载；保存原子写并保留原编码与换行符（CRLF/GBK）。
- 相关测试：`app/tests/test_ide_api.py`
- 状态：Done

### [IDE] 终端 (PTY)
- 说明：xterm.js + WebSocket `/api/ide/term`；Windows 用 pywinpty 真终端（PowerShell/pwsh），缺失时自动降级为 pipe 模式。
- 涉及文件/模块：`app/ide_api.py`、`app/static/ide.js`
- 实现要点：最多 3 个终端；输出 16ms 合批、每会话缓冲上限 256K 字符；关闭标签即杀整个进程树；WS 自行校验本机 Origin/Host（HTTP 中间件不覆盖 WS）。
- 相关测试：`app/tests/test_ide_api.py`（echo、resize、第 4 个拒绝、坏 Origin 4403、进程清理）、`test_ide_integration.py`
- 状态：Done

### [IDE] Git 版本管理
- 说明：状态/差异/暂存/取消暂存/丢弃/提交、历史与提交详情、回滚提交、恢复单文件到某版本、分支创建/切换、push/pull、初始化仓库。
- 涉及文件/模块：`app/ide_api.py`（`/api/ide/git/*`）、`app/static/ide.js`（源代码管理面板、diff 标签页）
- 实现要点：只调用 git CLI（参数列表、无 shell、`GIT_TERMINAL_PROMPT=0`、超时）；ref/分支名正则 + `git check-ref-format` 校验，禁止 `-` 开头；提交信息走 stdin；丢弃未跟踪文件进回收站；push 无上游时自动 `-u origin <branch>`。
- 相关测试：`app/tests/test_ide_api.py`（含本地 bare remote 的 push/pull）
- 状态：Done

### [IDE] AI 编辑文件 (Edit 模式) 与模型切换
- 说明：AI 面板底栏新增「问答 | 编辑」模式切换与模型下拉框。编辑模式下 AI 以 SEARCH/REPLACE 块给出修改，每个文件一张「修改建议」卡片（diff 预览 + 接受 / 接受并保存 / 拒绝 / 全部接受），支持多文件与新建文件；接受后可 Ctrl+Z 撤销。
- 涉及文件/模块：`app/ide_ai.py`（`POST /api/ide/ai/edit`，精简 prompt + 按 n_ctx 裁剪文件）、`app/static/ide-ai.js`、`app/main.py`（`ide_llm_stream`）
- 实现要点：容错解析（代码围栏、CRLF、5–9 个标记符、路径写法多样）；匹配顺序 精确 → 忽略行尾空白 → 忽略缩进 → 忽略空行 → 模糊(≥0.85，带「模糊匹配」标记)；未匹配显示原块可复制；temperature 0.2；模型切换失败自动回滚选择。
- 相关测试：`app/tests/test_ide_ai.py`、`app/tests/e2e/test_ide_ai_e2e.py`
- 状态：Done

### [IDE] 终端 (仿 VS Code / Cursor)
- 说明：Shell 配置下拉（pwsh / Windows PowerShell / cmd / Git Bash / WSL），ConPTY 优先；装好 pywinpty 后新开终端即生效，无需重启服务；标签重命名、最大化、退出后按 Enter 重启；Ctrl+C 有选区时复制、Ctrl+V/右键粘贴；Ctrl+点击链接；资源管理器「在集成终端中打开」。
- 涉及文件/模块：`app/ide_api.py`（`GET /api/ide/term/profiles`、`profile=` 参数）、`app/static/ide.js`、`vendor/addon-web-links.js`
- 实现要点：每次新建终端重新尝试 `import winpty`；子进程环境 TERM=xterm-256color / COLORTERM / TERM_PROGRAM=vscode；cmd 自动 `chcp 65001`；pipe 模式显示提示条。
- 相关测试：`app/tests/test_ide_api.py`、`app/tests/e2e/test_ide_smoke.py`
- 状态：Done

### llama.cpp --fit 上下文自动适配
- 说明：默认 `ctx_mode: "fit"`，由 llama-server 自己按设备可用显存（保留 204MB）选择最大上下文，避免估算误差导致的 Vulkan OOM。
- 涉及文件/模块：`app/llm_launcher.py`、`llm_models.json`
- 相关测试：`app/tests/test_llm_launcher.py`
- 状态：Done

### Embedding ONNX 后端
- 说明：Embedding 默认改用 ONNX Runtime fp32（bge-small-zh-v1.5），网关进程不再导入 PyTorch。基准（CPU，同一语料）：常驻内存 893 MB → 170 MB，单条查询 p50 6.1 ms → 2.1 ms，与 torch 向量 cos 1.000000（检索 top-5 完全一致），已有 Turbovec 索引无需重建。`EMBEDDING_BACKEND=torch` 可回到旧行为；`onnx-int8` 可选（需先导出，向量不兼容，切换时自动重建索引）。
- 涉及文件/模块：`app/embedding_backend.py`（`OnnxBgeEmbedder`、`load_embedder`、`export_onnx`、CLI `--export` / `--check`）、`app/main.py`（`get_embeddings`、`_worker_init_embedder_and_anchors`、`sync_embedding_backend_record`、`lifespan`）、`app/requirements.txt`、`app/storage/embedding_backend.txt`
- 实现要点：ONNX 目录 = `<model_dir>-onnx`；缺少 `model_fp32.onnx` 时用 `python -m embedding_backend --export` 在子进程中一次性导出（超时 300s，torch 内存随子进程释放），复制分词器/配置/池化文件并与 torch 逐条校验 cos ≥ 0.9999；缺 onnxruntime 或导出失败回退 torch 并打印中文修复命令，均不可用时报错。分词按 `do_lower_case` 加 Lowercase 归一化、截断 512；ORT `intra_op=物理核心数`、`inter_op=1`、`ORT_ENABLE_ALL`、关闭 CPU 内存池；按长度排序分批（结果按原顺序写回）、CLS 池化 + L2 归一化；ONNX 下 batch 上限 16（峰值内存 668 MB vs batch 64 的 3.9 GB）。启动日志打印后端 / 线程 / RSS；后端记录在 `storage/embedding_backend.txt`，fp32(torch/onnx) ↔ int8 切换时调用 `rebuild_index_from_history`。维度优先 `get_embedding_dimension`（消除 sentence-transformers 的 FutureWarning）。
- 相关测试：`app/tests/test_embedding_backend.py`（真实 ONNX：维度/归一化/确定性/批次顺序/大小写/截断/与 torch 参考 cos ≥ 0.9999/新进程 RSS < 400 MB；回退路径；后端切换重建逻辑；`TV_TEST_ONNX_EXPORT=1` 时跑真实导出）
- 状态：Done

### [Home] 聊天页重新设计 (更易用)
- 说明：仿 ChatGPT / Claude 的简洁布局：可折叠左侧栏（新对话、工作区文件、长期记忆、打开 IDE、设置、主题），居中对话栏，底部输入框内置「＋ 附件 / 🌐 联网 / 💡 深度思考 / 模型」；技术指标收进「设置 → 开发者信息」，默认隐藏。
- 涉及文件/模块：`app/static/index.html`、`style.css`、`app.js`
- 实现要点：浅色/深色/跟随系统三种主题（CSS 变量，AA 对比度）；空状态建议卡片；消息悬停操作（复制 / 重新生成 / ⓘ 统计）；模型未启动时显示友好提示卡与「重试」；<900px 侧栏变抽屉；保留全部安全处理与流式渲染优化；style.css 2657→623 行。
- 相关测试：`app/tests/e2e/test_ui_smoke.py`（21 项）、截图脚本 `app/tests/e2e/ui_screenshots.py`
- 状态：Done

### 工具 / MCP / 技能 / 系统提示词 (Agent)
- 说明：聊天页与 IDE AI 面板都可开启「🧰 工具」：模型可调用内置工具（联网搜索、抓取网页、记忆检索、读目录/读文件/搜索、写文件/编辑文件/执行命令、时间）、MCP 服务器工具（stdio 本地命令 / Streamable HTTP / SSE）与技能（SKILL.md，按需 load_skill 读取）；系统提示词预设可「追加」或「替换」内置提示。
- 涉及文件/模块：`app/agent/`（config / prompts / skills / builtin_tools / mcp_client / registry / runner / router）、`app/main.py`（/api/chat 工具循环）、`app/static/app.js`（设置 → 工具/MCP/技能/系统提示词、工具卡片）、`app/static/ide-ai.js`、`docs/AGENT_API.md`
- 实现要点：只读工具自动执行，写文件/命令/非只读 MCP 工具弹出「允许 / 始终允许 / 拒绝」；最多 6 轮工具调用、最多 24 个工具；工具结果按 n_ctx 截断；模型不支持工具时自动回退普通对话并提示；MCP 进程随服务关闭；配置存于 `app/storage/agent/`（原子写，密钥显示为 ***）。
- 相关测试：`test_agent_config_prompts_skills.py`、`test_agent_tools.py`、`test_agent_mcp.py`、`test_agent_chat_loop.py`、`e2e/test_agent_ui.py`、`e2e/test_ide_ai_e2e.py`
- 状态：Done

### 新增模型 Qwen3 4B / 8B
- 说明：`llm_models.json` 新增 [4] Qwen3 4B、[5] Qwen3 8B（官方 Qwen GGUF Q4_K_M），用 `python scratch\download_model.py 4 5` 下载（断点续传，hf-mirror 优先）。所有模型共用同一套 --fit + 自动加长流程；8B 超出 4GB 显存，标记 `allow_cpu_offload`，由 --fit 自动 GPU+CPU 混合，不做 -ngl 99 加长。
- 涉及文件/模块：`llm_models.json`、`scratch/download_model.py`、`app/llm_launcher.py`、`run_local_llm.ps1/.bat`
- 相关测试：`test_llm_launcher.py::test_cpu_offload_model_skips_boost / test_missing_download_model_hints_command`
- 状态：Done（模型文件需在本机下载）

### Reasoning 过程压缩 + KV 前缀缓存复用
- 说明：思考全文只在当次回答里显示；写入会话历史与长期记忆时去掉 `<think>` / `[Reasoning]` 段，只保留答案和 1–3 条「思考要点」。同时让 system prompt 在多轮之间保持逐字不变，每轮变化的内容（记忆检索、联网结果、按问题触发的规则）放进最后一条 user 消息，llama-server 可复用上一轮的 KV cache 前缀，只需处理新增内容。
- 涉及文件/模块：`app/main.py`（`split_reasoning`、`compress_reasoning`、`prepare_turn`、`make_turn`、`save_turn_to_memory`、`build_comprehensive_system_prompt`、`compose_messages`）、`llm_models.json`（`--cache-reuse 256`）
- 实现要点：抽取式压缩，不调用模型、零 GPU 开销；system prompt 去掉显存读数/记忆条数等实时数字；请求带 `cache_prompt: true`。KV Cache Eviction（H2O/SnapKV 这类按注意力丢弃 token）llama.cpp 不支持，本项目用「前缀复用 + 历史压缩窗口 + 按预算裁剪」代替；Qwen3.5 属于混合架构，`--cache-reuse` 会被引擎自动忽略，但前缀复用仍然有效。
- 相关测试：`test_chat_logic.py::test_system_prompt_is_stable_across_turns / test_reasoning_split_and_compress / test_history_and_memory_drop_reasoning`
- 状态：Done

### [Home] 知识图谱记忆 (Graph RAG)
- 说明：助手在用户空闲时用本地模型把长期记忆（对话、文件切片、联网结果）整理成「实体 — 关系 — 实体」，回答时按问题里提到的实体（+ 名称向量近邻）取 1~2 跳关系，作为 `【知识图谱】` 参考块放进本轮 user 消息。设置 → 知识图谱 可查看状态、启用/暂停、选择空闲时长、整理已有记忆 / 重新整理全部，并浏览、搜索、合并、删除实体与关系（附来源片段）；侧边栏显示一行状态小字。
- 涉及文件/模块：`app/kg/`（store / extract / activity / retrieval / service / router）、`app/main.py`（`add_memory_entries` 排队、`compose_messages` 的 KG 块与裁剪阶段、`lifespan` 启停、`/api/status.kg`、`ActivityMiddleware`）、`app/static/index.html` / `app.js` / `style.css`、`docs/KG_API.md`
- 实现要点：sqlite3 WAL（entities / aliases / relations / evidence / queue / meta），名称 NFKC+小写+去标点归一；抽取流式调用 llama（temperature 0、max_tokens 512、`cache_prompt:false`、json_object 不支持时自动回退、`/no_think`），容错解析 JSON；后台任务只在「启用、未暂停、模型就绪、非切换中、n_ctx ≥ 4096、用户空闲 ≥ idle_seconds」时一次处理一条；用户请求一开始（/api/chat、/v1/chat/completions、IDE AI）就取消正在进行的抽取流并把任务放回队列（attempts+1，最多 3 次）；KG 块 ≤ 500 tokens，裁剪时先于记忆切片丢弃，system prompt 保持不变（KV 前缀缓存）；名称向量复用 ONNX embedder（每批 16）并缓存在库中。
- 相关测试：`app/tests/test_kg_store.py`、`test_kg_extract.py`、`test_kg_retrieval.py`、`test_kg_api.py`、`app/tests/e2e/test_kg_ui.py`（`KG_TAB_SCREENSHOT=<path>` 可保存标签页截图）
- 状态：Done
