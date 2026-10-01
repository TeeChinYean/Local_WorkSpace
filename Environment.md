# 环境与工具

| 类别 | 名称 | 版本 | 说明 |
|---|---|---|---|
| 操作系统 | Windows 10/11 | - | 原生运行，无需 Docker / WSL |
| GPU | NVIDIA 4GB (RTX 3050 级) | - | Vulkan 设备 `Vulkan1`；显存红线 = 总显存 − 204MB |
| 推理引擎 | llama-server | - | `%USERPROFILE%\.docker\bin\inference\com.docker.llama-server.exe` 或任意从 llama.cpp 官方 Releases 获取的 `llama-server.exe`，需支持 `--api-key`、`-fa on` |
| 语言运行时 | Python | 3.10+ | `python` 需在 PATH（`run_local_llm.ps1` 调用 `app\llm_launcher.py`） |
| 包管理器 | pip / uv（推荐） | - | `uv pip install -r app\requirements.txt` |
| 依赖 | 见 `app/requirements.txt` | - | fastapi、uvicorn、httpx、requests、numpy、turbovec、onnxruntime、tokenizers、psutil、beautifulsoup4、pymupdf、python-docx、ujson、laya |
| Embedding 推理 | onnxruntime + tokenizers | onnxruntime ≥1.17（测试用 1.25；Windows + Python 3.14 用 1.30，有 cp314 wheel）、tokenizers ≥0.15 | 默认 `EMBEDDING_BACKEND=onnx`（CPU fp32，线程 = 物理核心数，`EMBED_THREADS` 可覆盖）；网关不再导入 torch |
| 可选依赖 | torch、sentence-transformers、onnx | - | 仅首次导出 ONNX（独立子进程，一次性）或 `EMBEDDING_BACKEND=torch` 时需要 |
| Git | git for Windows | 2.x | IDE 源代码管理调用 `git.exe`（需在 PATH） |
| 终端 | pywinpty | 3.x | IDE 真终端 (PTY)，`pip install pywinpty`；缺失时降级为 pipe 模式 |
| 前端依赖 | CodeMirror 6 / xterm.js 6 | 见 `app/static/vendor/VENDOR.md` | 已离线打包进仓库，无 CDN |
| 测试 | pytest、playwright | - | `cd app && python -m pytest -q tests`（e2e 需 Chromium） |
| 本地服务 | 网关 18088 / llama-server 18089 | - | 均默认只监听 127.0.0.1 |
| AI / 模型 | Qwen3.5-4B Q4_K_M、Phi-4-mini Q4_K_M、Qwen2.5-7B Q3_K_S | - | KV cache K=q8_0 / V=q4_0；配置见 `llm_models.json` |
| Embedding | bge-small-zh-v1.5 | 512 维 | 本地离线 `app/models/bge-small-zh-v1.5/`；ONNX 导出在 `app/models/bge-small-zh-v1.5-onnx/`（首次启动自动生成，`python -m embedding_backend --check` 自检） |
