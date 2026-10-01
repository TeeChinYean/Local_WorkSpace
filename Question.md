# 待确认问题

### [2026-09-25] Docker 附带的 llama-server 是否支持 `--api-key`
- 背景：G6 起 llama-server 启动时带 `--api-key`（上游 llama.cpp 支持，`/health` 无需密钥）。
- 待确认：在你机器上运行 `run_local_llm.bat` 是否正常启动；若报 unknown argument，需要换用官方 llama.cpp 构建或去掉该参数。
- 临时处理：已确认可用（2026-09-26，模型正常回答）
- 状态：Resolved

### [2026-09-25] `python` 是否在 PATH 且不是 Microsoft Store 占位程序
- 背景：`run_local_llm.ps1` 现在调用 `python app\llm_launcher.py`。
- 待确认：命令行执行 `python --version` 能否正常输出。
- 临时处理：已确认（run_local_llm.ps1 通过 python 启动成功）
- 状态：Resolved

### [2026-09-25] 是否需要局域网访问
- 背景：网关默认改为只监听 127.0.0.1（安全修复 B02）。
- 待确认：是否有手机 / 其他电脑访问 18088 的需求。
- 临时处理：需要时设置 `TV_HOST=0.0.0.0` 与 `TV_API_TOKEN=<随机串>`，客户端带 `Authorization: Bearer <token>`。
- 状态：Open

### [2026-09-25] `/api/clear` 是否应删除 storage/files 中的文件副本
- 背景：`/api/clear` 现在会持久化清空 active_files，但保留 `storage/files` 里的副本（`/api/clear_rag` 会全部删除）。
- 待确认：“新话题”时是否也要删除这些副本。
- 临时处理：保留副本。
- 状态：Open

### [2026-09-26] 是否把 Embedding 换成 ONNX Runtime
- 背景：基准测试（scratch/bench_embedding.py，云端 2 核）显示 ONNX fp32 与 torch 向量一致（cos 1.000000，无需重建索引），加载后 RAM 893MB → 170MB，单条查询约快 3 倍，依赖少约 1.2GB；int8 更快更省但向量有偏差（需重建索引，top-1 一致 26/30）。
- 待确认：是否切换到 `EMBEDDING_BACKEND=onnx`（fp32）。
- 临时处理：用户已确认切换（2026-09-26），默认后端改为 onnx fp32（提交 f2748a2）。
- 状态：Resolved

### [2026-09-26] 本机 llama-server 是否支持 --fit
- 背景：B25 修复默认使用 `--fit`；日志中已出现 `common_fit_params`，大概率支持。
- 待确认：重新运行 `run_local_llm.bat`，日志是否出现 `[上下文规划] ✔ --fit 完成：-c N`。
- 临时处理：已确认：--fit 可用；Qwen3.5 4B 自动加长到 71680，Qwen3 4B 到 20480；--jinja 工具调用正常
- 状态：Resolved
