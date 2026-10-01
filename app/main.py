import os
import sys

# 强制标准输入输出为 UTF-8 编码，防止 Windows 终端默认 cp1252 / gbk 触发 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import ujson as json
except ImportError:
    import json

import time
import re
import shutil
import asyncio
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import List, Dict, Optional, Any, Tuple
from contextlib import asynccontextmanager

# 设置国内 HuggingFace 镜像源
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import numpy as np
# Embedding 后端 (默认 ONNX Runtime fp32；torch / sentence-transformers 仅在 EMBEDDING_BACKEND=torch
# 或首次导出 ONNX 时才需要，且导出在独立子进程中完成，网关进程不再常驻 torch)
import embedding_backend

from turbovec import TurboQuantIndex
import requests
import httpx
import uuid
from fastapi import FastAPI, Request, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

import llm_launcher  # 本地 llama-server 启动器 (上下文自动加长至显存红线 + 校准)

# 全局套接字连接池与流式透传加速 (零握手、长连接复用)
_sync_session = requests.Session()  # 通用外部请求 (HF 镜像等)，不携带本地密钥
# 与本机 llama-server 通信专用：携带 --api-key 共享密钥 (绝不用于外部网站)
LLAMA_API_KEY = llm_launcher.get_api_key()
_LLM_AUTH_HEADERS = {"Authorization": f"Bearer {LLAMA_API_KEY}"}
_llm_session = requests.Session()
_llm_session.headers.update(_LLM_AUTH_HEADERS)
_global_async_client: Optional[httpx.AsyncClient] = None

def get_async_client() -> httpx.AsyncClient:
    global _global_async_client
    if _global_async_client is None or _global_async_client.is_closed:
        _global_async_client = httpx.AsyncClient(
            headers=_LLM_AUTH_HEADERS,  # 该客户端只用于访问本机推理引擎
            limits=httpx.Limits(max_keepalive_connections=30, max_connections=100, keepalive_expiry=60.0),
            timeout=httpx.Timeout(180.0, connect=5.0)
        )
    return _global_async_client

def decode_file_content(content_bytes: bytes) -> str:
    """多编码安全解码，优先 UTF-8，回退 GBK / GB18030 / Latin-1"""
    for enc in ["utf-8", "gb18030", "gbk", "latin-1"]:
        try:
            return content_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return content_bytes.decode("utf-8", errors="replace")

def extract_code_outline(filename: str, text: str) -> str:
    """提取代码或文档的全局结构大纲（所有函数列表、类、Markdown 标题等）"""
    lines = text.split("\n")
    total_lines = len(lines)
    ext = os.path.splitext(filename)[1].lower()
    
    outline_items = []
    
    if ext in [".py"]:
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("class ") or stripped.startswith("def ") or stripped.startswith("async def "):
                indent = len(line) - len(line.lstrip())
                indent_str = "  " * (indent // 4)
                signature = stripped.split(":")[0].strip()
                outline_items.append(f"{indent_str}- [Line {i}] {signature}")
    elif ext in [".js", ".ts", ".jsx", ".tsx"]:
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if (re.match(r"^(export\s+)?(default\s+)?(async\s+)?function\s+", stripped) or
                re.match(r"^(export\s+)?(class|interface|type)\s+", stripped) or
                re.match(r"^(const|let|var)\s+\w+\s*=\s*(async\s*)?\([^)]*\)\s*=>", stripped)):
                outline_items.append(f"- [Line {i}] {stripped.split('{')[0].strip()}")
    elif ext in [".c", ".cpp", ".h", ".hpp", ".java", ".cs", ".go", ".rs"]:
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if (re.match(r"^(pub\s+)?(async\s+)?fn\s+", stripped) or
                re.match(r"^func\s+", stripped) or
                re.match(r"^(public|private|protected|static|class|interface|struct)\s+", stripped)):
                outline_items.append(f"- [Line {i}] {stripped.split('{')[0].strip()}")
    elif ext in [".html", ".htm", ".xhtml", ".vue", ".svelte", ".xml", ".php"]:
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if re.match(r"^<(title|h[1-6]|header|footer|nav|main|section|article|aside|form|table|script|style|template)\b", stripped, re.IGNORECASE):
                tag_preview = stripped.split(">")[0].strip() + ">"
                outline_items.append(f"- [Line {i}] {tag_preview}")
            elif re.search(r'id=["\']([^"\']+)["\']', stripped):
                m_id = re.search(r'id=["\']([^"\']+)["\']', stripped)
                outline_items.append(f"- [Line {i}] Element ID: #{m_id.group(1)}")
    elif ext in [".md", ".markdown"]:
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                outline_items.append(f"- [Line {i}] {stripped}")

    if not outline_items:
        return f"【文件全局概览 - {filename}】(共 {total_lines} 行文本/代码)"
    
    outline_text = "\n".join(outline_items)
    return f"【文件全局大纲与结构清单 - {filename}】(共 {total_lines} 行代码，包含以下全部定义列表):\n{outline_text}"

_RANGE_WITH_UNIT = re.compile(r'(?:第\s*)?(\d+)\s*(?:-|~|到|至|–)\s*(\d+)\s*行')
_RANGE_EN = re.compile(r'\blines?\s*(\d+)\s*(?:-|~|to|–)\s*(\d+)\b')
_RANGE_ONLY = re.compile(r'^(?:第\s*)?(\d+)\s*(?:-|~|到|至|–)\s*(\d+)\s*行?$')
_FIRST_N_UNIT = re.compile(r'(?:前|首)\s*(\d+)\s*行')
_FIRST_N_ONLY = re.compile(r'^(?:前|首)\s*(\d+)$')
_FIRST_N_EN = re.compile(r'\bfirst\s+(\d+)\s+lines?\b')

def detect_requested_line_range(query: str, total_lines: int) -> Optional[tuple]:
    """检测用户是否请求了具体行号范围。
    只有明确带“行 / line(s)”单位，或整句只有范围本身 (如 "1-200") 时才算，
    避免 “给我 3-5 个点子” 之类的普通数字区间被误判为行号切片。"""
    q = query.strip().lower()
    for pat in (_RANGE_WITH_UNIT, _RANGE_EN, _RANGE_ONLY):
        m = pat.search(q)
        if m:
            start = max(1, int(m.group(1)))
            end = min(total_lines, int(m.group(2)))
            if start <= end:
                return (start, end)
    for pat in (_FIRST_N_UNIT, _FIRST_N_ONLY, _FIRST_N_EN):
        m = pat.search(q)
        if m:
            end = min(total_lines, int(m.group(1)))
            if end >= 1:
                return (1, end)
    return None

def chunk_document_text(filename: str, text: str, chunk_size: int = 1200, overlap: int = 100) -> List[str]:
    """代码感知与智能切片：加大块大小至 1200 字符，按函数与段落分割"""
    lines = text.split("\n")
    ext = os.path.splitext(filename)[1].lower()
    is_code = ext in [".py", ".js", ".ts", ".jsx", ".tsx", ".c", ".cpp", ".java", ".go", ".rs", ".cs"]

    chunks = []
    current_chunk = []
    current_len = 0

    for line in lines:
        stripped = line.strip()
        # 如果是代码文件，在遇到新的类或函数定义时，若当前块已有一定长度，则触发分块，保证函数完整性
        if is_code and current_len >= 600:
            if (stripped.startswith("def ") or stripped.startswith("async def ") or 
                stripped.startswith("class ") or stripped.startswith("function ") or 
                stripped.startswith("func ") or stripped.startswith("pub fn ")):
                chunk_str = "\n".join(current_chunk).strip()
                if chunk_str:
                    chunks.append(chunk_str)
                current_chunk = current_chunk[-3:] if len(current_chunk) >= 3 else current_chunk
                current_len = sum(len(x) for x in current_chunk)

        if current_len + len(line) + 1 > chunk_size:
            chunk_str = "\n".join(current_chunk).strip()
            if chunk_str:
                chunks.append(chunk_str)
            if current_chunk:
                overlap_lines = current_chunk[-4:] if len(current_chunk) >= 4 else current_chunk
                current_chunk = overlap_lines + [line]
                current_len = sum(len(x) for x in current_chunk) + len(current_chunk)
            else:
                current_chunk = [line]
                current_len = len(line)
        else:
            current_chunk.append(line)
            current_len += len(line) + 1

    if current_chunk:
        chunk_str = "\n".join(current_chunk).strip()
        if chunk_str:
            chunks.append(chunk_str)

    return chunks

# 尝试导入 Laya 决策模型 (System 1 Decision Engine, 如 Jev / Laya)
try:
    import laya
    HAS_LAYA = True
except ImportError:
    HAS_LAYA = False

# 配置选项
LLM_DEFAULT_API_BASE = os.environ.get("LLM_API_BASE", "http://127.0.0.1:18089/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "docker.io/ai/qwen3.5:4b-q4_K_M")
EMBEDDING_MODEL_NAME = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")

LLM_CANDIDATE_BASES = [
    "http://127.0.0.1:18089/v1",
    "http://127.0.0.1:12434/v1",
    "http://127.0.0.1:11434/v1",
]

def probe_llm_base() -> str:
    """同步探测当前活跃的推理引擎 (优先原生 18089，其次 Docker 12434 / Ollama 11434)。只在后台监控线程或启动时调用。"""
    custom = os.environ.get("LLM_API_BASE", "")
    candidates = list(LLM_CANDIDATE_BASES)
    if custom and custom not in candidates:
        candidates.insert(0, custom)
    for base in candidates:
        for suffix in ("/health", "/api/tags"):
            try:
                r = _llm_session.get(base.replace("/v1", suffix), timeout=0.3)
                if r.status_code == 200:
                    return base
            except Exception:
                pass
    return custom or LLM_CANDIDATE_BASES[0]

def get_active_llm_base() -> str:
    """非阻塞：返回后台监控线程缓存的推理引擎地址；尚未探测过时才同步探测一次"""
    cached = getattr(state, "_cached_llm_base", None)
    if cached:
        return cached
    state._cached_llm_base = probe_llm_base()
    return state._cached_llm_base

def get_available_models() -> List[Dict[str, Any]]:
    """模型列表来自 llm_models.json (与 run_local_llm.ps1 共用)，显存标签来自最近一次实测校准"""
    try:
        cfg = llm_launcher.load_config()
    except Exception as e:
        print(f"[模型配置] ⚠ 读取 llm_models.json 失败: {e}", flush=True)
        return []
    out = []
    for m in cfg.get("models", []):
        out.append({
            "id": m["id"],
            "name": m.get("name", m["id"]),
            "tag": m.get("tag", ""),
            "desc": m.get("desc", ""),
            "speed": m.get("speed", ""),
            "vram": llm_launcher.describe_vram(cfg, m),
            "available": os.path.exists(m.get("path", "")),
            "download_hint": (f"python scratch\\download_model.py {m.get('key')}" if m.get("download") else ""),
        })
    return out

# 持久化存储路径
BASE_DIR = os.path.dirname(__file__)
STATIC_DIR = os.path.join(BASE_DIR, "static")
STORAGE_DIR = os.path.join(BASE_DIR, "storage")
INDEX_FILE = os.path.join(STORAGE_DIR, "memory_index.tv")
HISTORY_FILE = os.path.join(STORAGE_DIR, "memory_history.json")
ACTIVE_FILES_FILE = os.path.join(STORAGE_DIR, "active_files.json")
FILE_STORAGE_DIR = os.path.join(STORAGE_DIR, "files")
MCP_SETTINGS_FILE = os.path.join(STORAGE_DIR, "mcp_settings.json")
EMBEDDING_BACKEND_FILE = os.path.join(STORAGE_DIR, "embedding_backend.txt")  # 记录构建索引所用的 embedding 后端
os.makedirs(FILE_STORAGE_DIR, exist_ok=True)

# 全文直通字符上限 (20万字符 / ~4000行)，在 Qwen 2.5 32k 上下文窗口中 100% 满血装载，绝不丢弃任何一行代码
FULL_CONTEXT_CHAR_LIMIT = 200000
DIRECT_ANCHORS = [
    "你好", "hello", "hi", "你能做什么", "自我介绍一下",
    "能计算一些简单的算法吗", "帮我写一段代码", "写个贪吃蛇游戏",
    "什么是热平衡", "解释一下比热容", "做一道物理题", "计算一下公式",
    "写一首诗", "讲个笑话", "翻译一句话", "今天天气怎么样", "帮我算个数",
    "write a python snake game", "can you calculate an algorithm",
    "who are you", "what can you do", "tell me a joke", "write a poem",
    "what is physics", "how does thermal equilibrium work", "solve this equation"
]

MEMORY_ANCHORS = [
    "刚刚那个是什么", "刚才说的ordinary段落", "上面那句话是什么意思",
    "我们之前聊了什么", "还记得我的名字吗", "我的学习目标是什么来着",
    "继续刚才的话题", "按照前面的要求修改", "我刚才说了什么",
    "结合刚才讨论的内容", "那个概念怎么理解", "上面提到的内容", "我问的第一个问题",
    "what was the first question I asked", "what the first question i ask",
    "what did I ask earlier", "what did I say before", "what were we talking about",
    "do you remember my previous question", "what was the first thing I said",
    "recall the previous topic", "as mentioned earlier", "repeat the previous paragraph"
]

LAYA_THRESHOLD = 0.60

_vram_cache: Dict[str, Any] = {"data": None, "timestamp": 0.0}

def get_gpu_vram_info() -> Dict[str, Any]:
    """非阻塞：返回后台监控线程缓存的显存信息；缓存为空时才同步调用一次 nvidia-smi"""
    if _vram_cache["data"] is not None:
        return _vram_cache["data"]
    return refresh_gpu_vram_info()

def refresh_gpu_vram_info() -> Dict[str, Any]:
    """同步调用 nvidia-smi 刷新显存缓存 (只在后台监控线程 / 启动时调用)"""
    now = time.time()
    try:
        cmd = ["nvidia-smi", "--query-gpu=memory.total,memory.free,memory.used", "--format=csv,noheader,nounits"]
        output = subprocess.check_output(cmd, encoding="utf-8", timeout=1.5).strip()
        parts = [float(x.strip()) for x in output.split(",")]
        total_mb, free_mb, used_mb = parts[0], parts[1], parts[2]
        res = {
            "total_gb": round(total_mb / 1024.0, 2),
            "free_gb": round(free_mb / 1024.0, 2),
            "used_gb": round(used_mb / 1024.0, 2),
            "available": True
        }
        _vram_cache["data"] = res
        _vram_cache["timestamp"] = now
        return res
    except Exception:
        if _vram_cache["data"] is not None and _vram_cache["data"].get("available"):
            return _vram_cache["data"]
        fallback = {"total_gb": 4.0, "free_gb": 1.2, "used_gb": 2.8, "available": False}
        _vram_cache["data"] = fallback  # 缓存失败结果，避免每次请求都同步重跑 nvidia-smi
        _vram_cache["timestamp"] = now
        return fallback

def get_dynamic_max_buffer_gb() -> float:
    """
    用户核心准则：上下文只要不到 VRAM 极限的 -0.1GB 就不限制！
    总显存 - 0.1GB 为硬性极限保护边界 (如 4.0GB - 0.1GB = 3.9GB)。
    只要当前真正剩余显存大于 0.1GB，高速缓存与上下文全力放开！
    """
    vram = get_gpu_vram_info()
    free_gb = vram.get("free_gb", 1.2)
    # 只要不到极限的 -0.1GB，放开最大可用显存空间 (保底 0.2GB)
    cap = max(0.2, free_gb - 0.1)
    return round(cap, 2)

def get_dynamic_max_buffer_bytes() -> int:
    return int(get_dynamic_max_buffer_gb() * 1024 * 1024 * 1024)

# 兼容常量（供基础引用，具体逻辑统一采用 get_dynamic_max_buffer_bytes）
MAX_BUFFER_BYTES = int(0.8 * 1024 * 1024 * 1024)

# 全局单例持有者
class AppState:
    embedder: Optional[Any] = None      # OnnxBgeEmbedder 或 SentenceTransformer (接口兼容)
    embedding_backend: str = ""         # "onnx" / "onnx-int8" / "torch"
    vector_dim: int = 512
    direct_vecs: Optional[np.ndarray] = None
    memory_vecs: Optional[np.ndarray] = None
    laya_agent = None
    index: Optional[TurboQuantIndex] = None
    memory_history: List[str] = []
    session_turns: List[Dict[str, str]] = []
    compact_summary: str = ""           # 滑动窗口压缩后的历史摘要（Compact Window Memory）
    active_files: Dict[str, Dict] = {}  # 活跃工作区文件: filename -> {text, lines, chars, outline, mode, in_rag, bytes}
    buffer_bytes: int = 0               # 当前动态内存缓冲区大小 (bytes)
    model_path: str = ""
    current_model: str = LLM_MODEL

state = AppState()

# ==============================================================================
# Compact Window Memory — 滑动窗口上下文压缩
# 当 session_turns 积累超过 COMPACT_WINDOW_KEEP + COMPACT_TRIGGER 轮时，
# 将最旧的一批轮次蒸馏压缩为 compact_summary，避免 16k 上下文被历史撑满。
# ==============================================================================
COMPACT_WINDOW_KEEP = 4  # 保留最近 4 轮完整对话 (prompt 中会完整带上这 4 轮)
COMPACT_TRIGGER = COMPACT_WINDOW_KEEP  # 一旦超过保留轮数就压缩，避免出现“既不在窗口也不在摘要”的轮次
COMPACT_SNIPPET_LEN = 200  # 每轮压缩时用户/助手各保留前 N 字符

def apply_compact_window():
    """将超出滑动窗口的旧对话轮次压缩为 state.compact_summary，保持 session_turns 在可控数量内。"""
    total = len(state.session_turns)
    if total <= COMPACT_TRIGGER:
        return  # 未达阈值，无需压缩

    # 需要压缩的旧轮次（全部 - 保留最近 N 轮）
    compress_count = total - COMPACT_WINDOW_KEEP
    old_turns = state.session_turns[:compress_count]
    state.session_turns = state.session_turns[compress_count:]

    # 蒸馏：拼接每轮的关键片段
    lines = []
    for t in old_turns:
        u = (t["user"] or "")[:COMPACT_SNIPPET_LEN].strip()
        a = (t["assistant"] or "")[:COMPACT_SNIPPET_LEN].strip()
        lines.append(f"用: {u}" + ("..." if len(t["user"]) > COMPACT_SNIPPET_LEN else ""))
        lines.append(f"助: {a}" + ("..." if len(t["assistant"]) > COMPACT_SNIPPET_LEN else ""))

    new_fragment = "\n".join(lines)
    if state.compact_summary:
        state.compact_summary = state.compact_summary + "\n" + new_fragment
    else:
        state.compact_summary = new_fragment

    # 防止 compact_summary 本身无限膨胀：保留最后 2000 字符
    if len(state.compact_summary) > 2000:
        state.compact_summary = "...(更早期压缩摘要已截断)...\n" + state.compact_summary[-1800:]

    print(f"[Compact Window] ✔ 已将 {compress_count} 轮历史压缩入摘要 (摘要长度: {len(state.compact_summary)} 字符, 保留 {COMPACT_WINDOW_KEEP} 轮完整对话)", flush=True)

def get_embeddings(texts, model, batch_size=64):
    """批量向量化 (ONNX Runtime 或 sentence-transformers，均为多线程；返回 float32 连续内存供 Turbovec 零拷贝)"""
    if str(getattr(model, "backend_name", "torch")).startswith("onnx"):
        # ONNX 关闭内存池后 batch 16 的峰值内存远低于 64，吞吐几乎不变
        batch_size = min(batch_size, embedding_backend.ONNX_MAX_BATCH)
    embeddings = model.encode(
        texts, 
        batch_size=batch_size, 
        normalize_embeddings=True, 
        show_progress_bar=False,
        convert_to_numpy=True
    )
    # ascontiguousarray 若已为 float32 连续内存，直接返回原始内存视图，实现向 C++ 扩展零拷贝传递
    return np.ascontiguousarray(embeddings, dtype=np.float32)

def probe_server_n_ctx() -> Optional[int]:
    """同步查询推理引擎真实 n_ctx (/slots 或 /props)，失败返回 None。只在后台线程调用。"""
    base_url = get_active_llm_base().replace("/v1", "")
    try:
        r = _llm_session.get(f"{base_url}/slots", timeout=0.5)
        if r.status_code == 200:
            slots = r.json()
            if isinstance(slots, list) and slots:
                ctx = slots[0].get("n_ctx")
                if isinstance(ctx, int) and ctx > 0:
                    return ctx
    except Exception:
        pass
    try:
        r = _llm_session.get(f"{base_url}/props", timeout=0.5)
        if r.status_code == 200:
            ctx = (r.json().get("default_generation_settings") or {}).get("n_ctx")
            if isinstance(ctx, int) and ctx > 0:
                return ctx
    except Exception:
        pass
    return None

def get_active_server_n_ctx(default_ctx: int = 16384) -> int:
    """非阻塞：返回后台线程探测到的真实 n_ctx；尚未探测成功时返回 default_ctx (不缓存 fallback)"""
    live = getattr(state, "_live_n_ctx", None)
    return live if live else default_ctx

def count_text_tokens(text: str) -> int:
    """极速纳秒级 Token 估算：纯本地混合规则计算，彻底消除 /tokenize 网络往返 (0ms 延迟)"""
    if not text:
        return 0
    c_chinese = len(re.findall(r'[\u4e00-\u9fff]', text))
    c_other = len(text) - c_chinese
    return int(c_chinese + c_other / 1.4) + 10

def query_qwen(messages, model=None, temperature=0.7, max_tokens=4096, stream=False, tools=None, tool_choice=None):
    """调用本地 Qwen 聊天接口，支持多模型动态切换与流式并发"""
    active_model = model or getattr(state, "current_model", LLM_MODEL) or LLM_MODEL
    base_url = get_active_llm_base()
    url = f"{base_url}/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": active_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream,
        "stop": ["\n【对话记录】", "【对话记录】", "\n用户:", "\nUser:", "\nHuman:"],
        "presence_penalty": 0.2
    }
    if tools:
        payload["tools"] = tools
    if tool_choice:
        payload["tool_choice"] = tool_choice
    if stream:
        resp = _llm_session.post(url, headers=headers, json=payload, stream=True, timeout=120)
        if resp.status_code != 200:
            try:
                err_json = resp.json()
                err_msg = err_json.get("error", {}).get("message", resp.text)
            except Exception:
                err_msg = resp.text
            raise RuntimeError(f"大模型接口异常 ({resp.status_code}): {err_msg}")
        return resp
    try:
        response = _llm_session.post(url, headers=headers, json=payload, timeout=120)
        response.raise_for_status()
        data = response.json()
        msg_obj = data['choices'][0]['message']
        content = msg_obj.get('content', '')
        reasoning = msg_obj.get('reasoning_content', '')
        if reasoning:
            return f"<think>\n{reasoning}\n</think>\n\n{content}"
        return content
    except (requests.exceptions.RequestException, KeyError, IndexError):
        return query_qwen_ollama_native(messages, model=active_model, temperature=temperature, max_tokens=max_tokens)

def query_qwen_ollama_native(messages, model=None, temperature=0.7, max_tokens=4096):
    active_model = model or getattr(state, "current_model", LLM_MODEL) or LLM_MODEL
    base = get_active_llm_base().replace("/v1", "")
    url = f"{base}/api/chat"
    payload = {
        "model": active_model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens
        }
    }
    try:
        response = _llm_session.post(url, json=payload, timeout=120)
        response.raise_for_status()
        data = response.json()
        return data['message']['content']
    except Exception as e:
        return f"请求 LLM 失败: {e}"

def _load_native_model_map() -> Dict[str, Dict[str, Any]]:
    try:
        cfg = llm_launcher.load_config()
        return {m["id"]: m for m in cfg.get("models", [])}
    except Exception:
        return {}

NATIVE_MODEL_MAP = _load_native_model_map()

def is_native_llama_running() -> bool:
    """非阻塞：返回后台监控线程缓存的结果；未初始化时同步检测一次"""
    cached = getattr(state, "_native_running", None)
    if cached is not None:
        return cached
    state._native_running = probe_native_llama_running()
    return state._native_running

def probe_native_llama_running() -> bool:
    """检测当前是否运行在本地原生 llama-server.exe 模式 (端口 18089)"""
    try:
        import psutil
        for conn in psutil.net_connections():
            if conn.laddr.port == 18089 and conn.status == 'LISTEN' and conn.pid:
                p = psutil.Process(conn.pid)
                if "llama" in p.name().lower():
                    return True
    except Exception:
        pass
    return False

def _stop_llama_on_port(port: int = 18089) -> Tuple[bool, str]:
    """只终止监听该端口且进程名含 llama 的进程，绝不误杀其他程序"""
    try:
        import psutil
    except ImportError:
        return False, "未安装 psutil (pip install psutil)，无法安全终止旧 llama-server"
    killed = []
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr and conn.laddr.port == port and conn.status == psutil.CONN_LISTEN and conn.pid:
                try:
                    p = psutil.Process(conn.pid)
                    if "llama" not in p.name().lower():
                        return False, f"端口 {port} 被非 llama 进程占用 ({p.name()} PID {conn.pid})，已中止切换"
                    p.terminate()
                    try:
                        p.wait(timeout=5)
                    except psutil.TimeoutExpired:
                        p.kill()
                    killed.append(conn.pid)
                except psutil.NoSuchProcess:
                    pass
    except Exception as e:
        return False, f"终止旧进程异常: {e}"
    return True, f"已终止 {len(killed)} 个旧 llama-server 进程"

def restart_native_llama_server(model_id: str) -> Tuple[bool, str]:
    """热切换原生 llama-server：先校验 → 再终止旧进程 → 用 llm_launcher 按显存红线自动规划并校准上下文"""
    cfg = NATIVE_MODEL_MAP.get(model_id)
    if not cfg:
        return False, f"llm_models.json 中未配置模型: {model_id}"
    if not os.path.exists(cfg["path"]):
        hint = f"（先运行 python scratch\\download_model.py {cfg.get('key')} 下载）" if cfg.get("download") else ""
        return False, f"未找到 GGUF 文件: {cfg['path']}{hint}"
    ok, msg = _stop_llama_on_port(18089)
    print(f"[原生引擎热切换] {msg}", flush=True)
    if not ok:
        return False, msg
    time.sleep(1.5)  # 等待驱动回收显存
    proc, ctx, info = llm_launcher.launch(model_id, foreground=False, log=lambda m: print(f"[原生引擎热切换] {m}", flush=True))
    if proc is None:
        return False, info.get("error", "llama-server 启动失败")
    state._llama_server_proc = proc
    return True, f"{cfg.get('name', model_id)} 已就绪 (上下文 {ctx} tokens)"

def unload_runner_model(model_name: str):
    """通知 Docker Model Runner / Ollama 立即从显存中彻底卸载指定模型 (释放 VRAM)"""
    base = get_active_llm_base().replace("/v1", "")
    url = f"{base}/api/generate"
    try:
        resp = _llm_session.post(url, json={"model": model_name, "keep_alive": 0}, timeout=8)
        if resp.status_code == 200:
            print(f"  [显存释放成功] ✔ 已从 GPU 彻底卸载模型: {model_name}", flush=True)
    except Exception as e:
        print(f"  [显存卸载警告] 卸载 {model_name} 失败: {e}", flush=True)

def unload_other_runner_models(keep_model: str):
    """检测当前已加载到显存的模型，将除 keep_model 以外的所有模型彻底卸载，防止多模型挤占 4GB 显存"""
    if is_native_llama_running() or "18089" in get_active_llm_base():
        # 本地原生 llama-server 为独立进程管理，无 /api/ps 接口，直接返回以杜绝 5s 超时卡顿
        return
    base = get_active_llm_base().replace("/v1", "")
    ps_url = f"{base}/api/ps"
    try:
        resp = _llm_session.get(ps_url, timeout=2)
        if resp.status_code == 200:
            data = resp.json()
            unloaded_any = False
            for m in data.get("models", []):
                m_name = m.get("name") or m.get("model")
                if m_name and m_name != keep_model:
                    print(f"[显存防爆自清洁] 正在从 GPU 卸载旧模型: {m_name} (保持: {keep_model})", flush=True)
                    unload_runner_model(m_name)
                    unloaded_any = True
            if unloaded_any:
                time.sleep(0.5)  # 等待驱动与 llama-server 完全退出并回收 VRAM
    except Exception as e:
        print(f"[显存检查警告] 查询活动模型失败: {e}", flush=True)

def preload_runner_model(model_name: str) -> bool:
    """通知 Docker Model Runner / Ollama 立即预热加载模型至 GPU 显存 (Warmup)"""
    if is_native_llama_running() or "18089" in get_active_llm_base():
        return True
    base = get_active_llm_base().replace("/v1", "")
    url = f"{base}/api/generate"
    try:
        print(f"[模型预载] ⏳ 正在将 {model_name} 预热载入 GPU 显存...", flush=True)
        # prompt 为空，keep_alive 为 -1，只触发装入显存并驻留
        resp = _llm_session.post(url, json={"model": model_name, "prompt": "", "keep_alive": -1}, timeout=10)
        if resp.status_code == 200:
            print(f"  [模型预载完成] ✔ {model_name} 已成功装载入 GPU 显存并常驻！", flush=True)
            return True
        else:
            chat_url = f"{get_active_llm_base()}/chat/completions"
            resp2 = _llm_session.post(chat_url, json={"model": model_name, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}, timeout=180)
            if resp2.status_code == 200:
                print(f"  [模型预载完成 (回退)] ✔ {model_name} 已成功装载入 GPU 显存！", flush=True)
                return True
    except Exception as e:
        print(f"  [模型预载警告] 预加载 {model_name} 失败: {e}", flush=True)
    return False

# ==============================================================================
# 后台运行时监控线程 (Runtime Monitor)
# 所有会阻塞的探测 (nvidia-smi / psutil / HTTP 健康检查 / n_ctx / 当前模型) 都在此线程中完成，
# 请求处理路径只读取缓存，彻底避免阻塞 asyncio 事件循环。
# ==============================================================================
MONITOR_INTERVAL_SEC = 3.0
_monitor_stop = threading.Event()

def sync_current_model_from_props() -> None:
    base_url = get_active_llm_base().replace("/v1", "")
    try:
        r = _llm_session.get(f"{base_url}/props", timeout=0.5)
        if r.status_code == 200:
            pdata = r.json()
            malias = (pdata.get("model_alias", "") or "") + " " + (pdata.get("model_path", "") or "")
            for mid, mcfg in NATIVE_MODEL_MAP.items():
                if os.path.basename(mcfg["path"]).lower() in malias.lower():
                    state.current_model = mid
                    break
    except Exception:
        pass

def refresh_runtime_state() -> None:
    """同步刷新一次所有运行时缓存"""
    state._cached_llm_base = probe_llm_base()
    refresh_gpu_vram_info()
    state._native_running = probe_native_llama_running()
    ctx = probe_server_n_ctx()
    state._live_n_ctx = ctx  # 探测失败时为 None → 调用方使用各自的默认值，绝不永久缓存 fallback
    if ctx and (state._native_running or "18089" in state._cached_llm_base):
        sync_current_model_from_props()

def _monitor_loop() -> None:
    while not _monitor_stop.wait(MONITOR_INTERVAL_SEC):
        if getattr(state, "_switching_model", False):
            continue
        try:
            refresh_runtime_state()
        except Exception as e:
            print(f"[运行时监控] 刷新异常: {e}", flush=True)

def start_runtime_monitor() -> threading.Thread:
    _monitor_stop.clear()
    t = threading.Thread(target=_monitor_loop, name="tv-runtime-monitor", daemon=True)
    t.start()
    return t

def init_laya_router():
    """初始化 Laya 决策引擎 (Laya-typed-decisions 架构)"""
    if not HAS_LAYA:
        print("[Laya 状态] 未安装 laya 包，使用内置轻量级 System 1 决策路由器。", flush=True)
        return None
    candidates = [
        os.path.join(BASE_DIR, "models", "laya-typed-decisions"),
        os.path.join(os.path.dirname(BASE_DIR), "models", "laya-typed-decisions"),
        os.path.expanduser("~/.cache/huggingface/hub/models--convaiinnovations--laya-typed-decisions/snapshots/1a793eb568e6718f15941d08f85432581df534e3")
    ]
    target_model = "convaiinnovations/laya-typed-decisions"
    for c in candidates:
        if os.path.exists(os.path.join(c, "model.safetensors")):
            target_model = c
            break
    try:
        print(f"[Laya 状态] 正在初始化 Laya-typed-decisions 决策引擎 ({target_model})...", flush=True)
        agent = laya.load(target_model)
        print("  ✔ Laya-typed-decisions 决策模型加载成功！将作为前置路由判断：直接生成 vs 搜索旧记忆", flush=True)
        return agent
    except Exception as e:
        print(f"[Laya 状态] 加载 Laya-typed-decisions 模型异常: {e}，将使用内置轻量级决策路由器。", flush=True)
        return None

def route_decision(user_input, laya_agent, has_history, embedder, direct_vecs, memory_vecs, threshold=LAYA_THRESHOLD):
    """
    智能决策层 (System 1 Decision Engine):
    1. 优先使用 Laya-typed-decisions 极速决策模型
    2. 兜底使用毫秒级 BGE 中英文双语语义相似度路由 (Semantic Router)
    3. 严格执行门限逻辑：直接生成需同时满足 >= 阈值 且 > 记忆相似度
    """
    if not has_history:
        return "direct_generation", 1.0

    # 0. 社区推荐优化：超极速确定性旁路 (Fast-Path Bypass, < 0.05ms)
    # 对高频问候与基础互动直接 0ms 直通，省去 Laya 神经网络前向推理
    u_raw = user_input.strip().lower()
    fast_greetings = {"你好", "您好", "hello", "hi", "hey", "早", "早安", "早上好", "晚安", "在吗", "在不在", "自我介绍", "介绍一下你自己", "你是谁", "who are you"}
    ref_keywords = [
        "刚刚", "刚才", "之前", "前面", "上面", "上一句", "还记得", "继续", "第一个", "第一句",
        "文件", "文档", "代码", "上传", "这个函数", "这个类", "这段代码",
        "first question", "first thing", "earlier", "before", "previously", "did i ask", "did i say", "remember",
        "file", "document", "upload", "code"
    ]
    if (u_raw in fast_greetings or (len(u_raw) <= 8 and any(u_raw.startswith(g) for g in ["你好", "hello", "hi", "hey"]))) and not any(kw in u_raw for kw in ref_keywords):
        return "direct_generation", 1.0

    # 1. Laya-typed-decisions 模型单次前向极速概率决策 (~30ms)
    if laya_agent is not None:
        try:
            result = laya_agent.predict(
                state=user_input,
                questions={
                    "intent": {
                        "type": "choice",
                        "instructions": "Determine if the user's input refers to past conversation history or asks about previously discussed facts, or if it can be directly generated independently.",
                        "criteria": {
                            "search_memory": "The user refers to previous context, asks about past facts, mentions 'before', 'earlier', 'we discussed', 'first question', or follows up on previous discussion.",
                            "direct_generation": "The user is greeting, asking a standalone general knowledge question, requesting creative generation, or starting a new topic."
                        }
                    }
                }
            )
            answers = result.get("answers", {}) if isinstance(result, dict) and "answers" in result else result
            intent_info = answers.get("intent", {})
            choice = intent_info.get("choice", "search_memory")
            prob = intent_info.get("answer_confidence", intent_info.get("confidence", 0.9))
            
            if choice == "search_memory":
                if prob >= threshold:
                    return "search_memory", prob
                else:
                    return "direct_generation", 1.0 - prob
            else:
                if prob >= threshold:
                    return "direct_generation", prob
                else:
                    return "search_memory", 1.0 - prob
        except Exception:
            pass

    # 2. 毫秒级中英文双语语义相似度路由 (BGE Semantic Router)
    q_vec = embedder.encode([user_input], normalize_embeddings=True)[0]
    sim_direct = float(np.max(np.dot(direct_vecs, q_vec)))
    sim_memory = float(np.max(np.dot(memory_vecs, q_vec)))

    # 中英文显式指代词与文档提问强化
    ref_keywords = [
        "刚刚", "刚才", "之前", "前面", "上面", "上一句", "还记得", "继续", "第一个", "第一句",
        "文件", "文档", "代码", "上传", "这个函数", "这个类", "这段代码",
        "first question", "first thing", "earlier", "before", "previously", "did i ask", "did i say", "remember",
        "file", "document", "upload", "code"
    ]
    u_lower = user_input.lower()
    if any(kw in u_lower for kw in ref_keywords):
        sim_memory += 0.35

    # 严格门限判断
    if sim_direct >= threshold and sim_direct > sim_memory:
        return "direct_generation", min(sim_direct, 0.99)
    else:
        conf = max(sim_memory, threshold)
        return "search_memory", min(conf, 0.99)

# 记忆库全局可重入锁：向量索引 add/search 与 memory_history 追加/持久化必须在同一把锁内完成，
# 保证 index 第 i 条向量永远对应 memory_history[i]
_memory_lock = threading.RLock()
_storage_lock = _memory_lock  # 兼容旧名称

def _atomic_write_json(path: str, obj: Any) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)  # 原子替换：中途断电 / 关窗口不会留下半截 JSON

def save_storage(index, memory_history, active_files=None):
    """持久化保存到 storage 目录 (记忆锁保护 + 临时文件原子替换)"""
    with _memory_lock:
        os.makedirs(STORAGE_DIR, exist_ok=True)
        try:
            if index is not None:
                tmp_index = f"{INDEX_FILE}.tmp"
                index.write(tmp_index)
                os.replace(tmp_index, INDEX_FILE)
            _atomic_write_json(HISTORY_FILE, list(memory_history))
            if active_files is not None:
                _atomic_write_json(ACTIVE_FILES_FILE, dict(active_files))
            print("  [持久化] ✔ 记忆与工作区状态已成功压缩并保存至 storage 目录！", flush=True)
        except Exception as e:
            print(f"  [持久化警告] 保存失败: {e}", flush=True)

def add_memory_entries(entries: List[str]) -> int:
    """向量化并追加记忆条目；向量与文本在同一把锁内写入，保证一一对应。无 embedder 时不写入 (避免错位)"""
    entries = [e for e in entries if e and e.strip()]
    if not entries:
        return 0
    if state.embedder is None or state.index is None:
        print("  [记忆写入] ⚠ Embedding 模型或索引未就绪，跳过写入以保持索引与文本对齐", flush=True)
        return 0
    vecs = get_embeddings(entries, state.embedder)  # 计算耗时，放在锁外
    with _memory_lock:
        start_index = len(state.memory_history)
        state.index.add(vecs)
        state.memory_history.extend(entries)
    _kg_enqueue_memory(entries, start_index)
    return len(entries)

def _kg_enqueue_memory(entries: List[str], start_index: int) -> None:
    """新记忆写入后排入知识图谱整理队列 (空闲时由本地模型抽取实体/关系)；失败不影响记忆写入"""
    try:
        svc = globals().get("kg_service")
        if svc is not None:
            svc.enqueue_memory(entries, start_index)
    except Exception as e:
        print(f"  [知识图谱] ⚠ 排队失败: {e}", flush=True)

def search_memory(query_vec, k: int) -> List[Tuple[float, str]]:
    """线程安全的向量检索，返回 [(score, text)]"""
    with _memory_lock:
        n = len(state.memory_history)
        if n == 0 or state.index is None or k <= 0:
            return []
        scores, indices = state.index.search(query_vec, k=min(k, n))
        out = []
        for score, idx in zip(scores[0], indices[0]):
            if 0 <= idx < n:
                out.append((float(score), state.memory_history[idx]))
        return out

def rebuild_index_from_history() -> None:
    """索引与文本条数不一致时，按 memory_history 重新向量化重建索引"""
    with _memory_lock:
        history = list(state.memory_history)
        new_index = TurboQuantIndex(dim=state.vector_dim, bit_width=4)
        if history and state.embedder is not None:
            for i in range(0, len(history), 256):
                new_index.add(get_embeddings(history[i:i + 256], state.embedder))
        state.index = new_index
    save_storage(state.index, state.memory_history)

def read_recorded_embedding_backend() -> Optional[str]:
    """读取上次构建索引所用的 embedding 后端 (storage/embedding_backend.txt)，不存在返回 None"""
    try:
        with open(EMBEDDING_BACKEND_FILE, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None

def record_embedding_backend(name: str) -> None:
    try:
        os.makedirs(os.path.dirname(EMBEDDING_BACKEND_FILE), exist_ok=True)
        tmp = EMBEDDING_BACKEND_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write((name or "") + "\n")
        os.replace(tmp, EMBEDDING_BACKEND_FILE)
    except OSError as e:
        print(f"  [持久化警告] 记录 embedding 后端失败: {e}", flush=True)

def sync_embedding_backend_record(current: str, already_rebuilt: bool = False) -> bool:
    """启动时比较记录的后端与当前后端：torch 与 onnx fp32 向量一致 (cos 1.000000) 无需重建；
    若在 fp32 (torch/onnx) 与 int8 之间切换，则按记忆文本重建索引。返回是否执行了重建。"""
    current = current or "torch"
    previous = read_recorded_embedding_backend()
    if previous is None and state.memory_history:
        previous = "torch"  # 旧版本只有 torch 后端，已有索引即由 torch 生成
    rebuilt = False
    if (previous is not None and not already_rebuilt and state.memory_history
            and embedding_backend.backend_family(previous) != embedding_backend.backend_family(current)):
        print(f"[持久化] ⚠ Embedding 后端由 {previous} 切换为 {current} (向量不兼容)，正在按记忆文本重建索引...", flush=True)
        rebuild_index_from_history()
        rebuilt = True
    if previous != current or rebuilt or not os.path.exists(EMBEDDING_BACKEND_FILE):
        record_embedding_backend(current)
    return rebuilt

# ==============================================================================
# Reasoning 过程压缩：思考全文只用于当次显示；历史与长期记忆只保留答案 + 1-3 条思考要点
# ==============================================================================
_THINK_RE = re.compile(r'<think>(.*?)</think>', re.S | re.I)
_CONCLUSION_WORDS = ("therefore", "thus", "hence", "so ", "conclusion", "answer", "result",
                     "所以", "因此", "结论", "综上", "最终", "答案", "即", "关键")

def split_reasoning(text: str) -> Tuple[str, str]:
    """把回答拆成 (正式答案, 思考全文)，支持 <think>…</think> 与 [Reasoning]…[Answer] 两种格式"""
    parts: List[str] = []
    t = text or ""

    def _take(m):
        parts.append(m.group(1))
        return ""

    t = _THINK_RE.sub(_take, t)
    if re.search(r'<think>', t, re.I):  # 未闭合的 <think>
        before, after = re.split(r'<think>', t, maxsplit=1, flags=re.I)
        parts.append(after)
        t = before
    if "[Reasoning]" in t:
        pre, rest = t.split("[Reasoning]", 1)
        if "[Answer]" in rest:
            r, ans = rest.split("[Answer]", 1)
            parts.append(r)
            t = pre + ans
        else:
            parts.append(rest)
            t = pre
    t = t.replace("[Answer]", "")
    return t.strip(), "\n".join(p.strip() for p in parts if p and p.strip())

def compress_reasoning(reasoning: str, max_items: int = 3, max_chars: int = 180) -> str:
    """抽取式压缩思考过程 (不调用模型，零 GPU 开销)：优先保留结论句与靠后的句子"""
    if not reasoning or not reasoning.strip():
        return ""
    raw = re.split(r'[\n]+|(?<=[。！？!?；;])|(?<=\.)\s+', reasoning)
    cands = []
    for i, c in enumerate(raw):
        c = re.sub(r'^[\s\-\*•\d\.\)、]+', '', (c or "")).strip()
        if len(c) < 6:
            continue
        cands.append((i, c))
    if not cands:
        return reasoning.strip()[:max_chars]
    n = len(cands)
    scored = []
    for rank, (i, c) in enumerate(cands):
        low = c.lower()
        score = 2.0 if any(w in low for w in _CONCLUSION_WORDS) else 0.0
        score += 1.0 if rank >= n * 0.7 else 0.0
        score += 0.5 if re.search(r'\d', c) else 0.0
        scored.append((score, -rank, rank, c))
    top = sorted(scored, reverse=True)[:max_items]
    picked = [c if len(c) <= 70 else c[:69] + "…" for _, _, _, c in sorted(top, key=lambda x: x[2])]
    out = "；".join(picked)
    return out if len(out) <= max_chars else out[:max_chars - 1] + "…"

def prepare_turn(answer_text: str, extra_reasoning: str = "") -> Tuple[str, str]:
    """返回 (去掉思考后的答案, 思考要点)"""
    answer, inband = split_reasoning(answer_text)
    reasoning = "\n".join(x for x in (extra_reasoning, inband) if x)
    return answer, compress_reasoning(reasoning)

def make_turn(user_text: str, answer: str, reasoning_summary: str = "") -> Dict[str, str]:
    turn = {"user": user_text, "assistant": answer}
    if reasoning_summary:
        turn["reasoning_summary"] = reasoning_summary
    return turn

def save_turn_to_memory(user_text: str, answer: str, reasoning_summary: str = "") -> None:
    """保存一轮对话至长期记忆：只存答案 (+ 思考要点)，不存思考全文；空回答不保存"""
    answer, inband = split_reasoning(answer or "")
    if not reasoning_summary and inband:
        reasoning_summary = compress_reasoning(inband)
    if not user_text or not answer.strip():
        return
    turn_entry = f"【对话记录】用户: {user_text} | 助手: {answer}"
    if reasoning_summary:
        turn_entry += f" | 思考要点: {reasoning_summary}"
    if add_memory_entries([turn_entry]):
        save_storage(state.index, state.memory_history)

def load_storage(vector_dim):
    """从 storage 目录加载已有的持久化索引与记忆"""
    if os.path.exists(INDEX_FILE) and os.path.exists(HISTORY_FILE):
        try:
            index = TurboQuantIndex.load(INDEX_FILE)
            stored_dim = getattr(index, 'dim', None)
            if stored_dim is not None and stored_dim != vector_dim:
                print(f"[持久化] ⚠ 向量维度不匹配 (存储:{stored_dim} vs 当前模型:{vector_dim})，丢弃旧索引，重新创建。", flush=True)
                index = TurboQuantIndex(dim=vector_dim, bit_width=4)
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    memory_history = json.load(f)
                return index, memory_history
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                memory_history = json.load(f)
            print(f"[持久化] ✔ 成功从 storage 加载已有记忆 ({len(memory_history)} 条历史状态)！", flush=True)
            return index, memory_history
        except Exception as e:
            print(f"[持久化] 加载历史索引失败，重新创建: {e}", flush=True)
    
    index = TurboQuantIndex(dim=vector_dim, bit_width=4)
    return index, []

def _embedding_log(msg: str) -> None:
    print(msg, flush=True)

def _worker_init_embedder_and_anchors(model_path):
    t_start = time.time()
    print(f"[并行任务 1/3] 开始加载 Embedding 向量模型 [{model_path}]...", flush=True)
    embedder = None
    # 后端由 EMBEDDING_BACKEND 决定 (默认 onnx)；本地目录优先离线加载，缺少 ONNX 时一次性子进程导出
    try:
        embedder = embedding_backend.load_embedder(model_path, log=_embedding_log)
    except Exception as e:
        print(f"  ⚠ 默认加载 [{model_path}] 失败 ({e})，正在自动扫描备选本地模型目录...", flush=True)
        fallback_dirs = [
            os.path.join(BASE_DIR, "models", "bge-small-zh-v1.5"),
            os.path.join(BASE_DIR, "models", "bge-large-zh-v1.5"),
            os.path.expanduser("~/.cache/huggingface/hub/models--BAAI--bge-small-zh-v1.5/snapshots/7999e1d3359715c523056ef9478215996d62a620"),
        ]
        for fb in fallback_dirs:
            if os.path.isdir(fb) and os.path.abspath(fb) != os.path.abspath(model_path):
                try:
                    embedder = embedding_backend.load_embedder(fb, log=_embedding_log)
                    print(f"  ✔ 成功回退加载本地嵌入模型: {fb}", flush=True)
                    break
                except Exception:
                    pass
        if embedder is None:
            raise e

    # 新版 sentence-transformers 把 get_sentence_embedding_dimension 重命名为 get_embedding_dimension
    dim = embedding_backend.embedding_dimension(embedder)
    direct_vecs = embedder.encode(DIRECT_ANCHORS, normalize_embeddings=True)
    memory_vecs = embedder.encode(MEMORY_ANCHORS, normalize_embeddings=True)
    backend = getattr(embedder, "backend_name", "torch")
    threads = getattr(embedder, "threads", None)
    rss = embedding_backend.rss_mb()
    print(f"  ✔ [任务 1/3] Embedding 模型与锚点预计算完成 (耗时: {time.time() - t_start:.2f}s, 向量维度: {dim})", flush=True)
    print(f"  [Embedding] 后端: {backend} | 线程: {threads} | 进程内存 RSS: "
          f"{f'{rss:.0f} MB' if rss is not None else '未知'}", flush=True)
    return embedder, dim, direct_vecs, memory_vecs

def _worker_init_laya():
    t_start = time.time()
    print(f"[并行任务 2/3] 开始初始化 Laya 决策路由器...", flush=True)
    agent = init_laya_router()
    print(f"  ✔ [任务 2/3] Laya 决策路由器初始化完成 (耗时: {time.time() - t_start:.2f}s)", flush=True)
    return agent

def _worker_init_storage():
    t_start = time.time()
    print(f"[并行任务 3/3] 开始读取持久化历史记忆与索引文件...", flush=True)
    raw_index = None
    memory_history = []
    active_files = {}
    if os.path.exists(INDEX_FILE):
        try:
            raw_index = TurboQuantIndex.load(INDEX_FILE)
        except Exception as e:
            print(f"  ⚠ 加载历史索引文件异常: {e}", flush=True)
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                memory_history = json.load(f)
        except Exception as e:
            print(f"  ⚠ 加载历史记忆文本异常: {e}", flush=True)
    if os.path.exists(ACTIVE_FILES_FILE):
        try:
            with open(ACTIVE_FILES_FILE, "r", encoding="utf-8") as f:
                active_files = json.load(f)
        except Exception as e:
            print(f"  ⚠ 加载历史工作区活跃文件异常: {e}", flush=True)
    print(f"  ✔ [任务 3/3] 历史存储读取完成 ({len(memory_history)} 条记录, {len(active_files)} 个活跃文件, 耗时: {time.time() - t_start:.2f}s)", flush=True)
    return raw_index, memory_history, active_files

# FastAPI Lifespan 生命周期管理 (并行加速启动)
@asynccontextmanager
async def lifespan(app: FastAPI):
    t_total_start = time.time()
    print("=" * 68)
    print(" Turbovec 记忆管理系统 + Laya/Jev 智能决策路由 (并行多线程加速启动)")
    print("=" * 68)
    
    local_small = os.path.join(BASE_DIR, "models", "bge-small-zh-v1.5")
    local_large = os.path.join(BASE_DIR, "models", "bge-large-zh-v1.5")
    user_cache = os.path.expanduser("~/.cache/huggingface/hub/models--BAAI--bge-small-zh-v1.5/snapshots/7999e1d3359715c523056ef9478215996d62a620")
    if os.path.exists(local_small) and os.listdir(local_small):
        state.model_path = local_small
    elif os.path.exists(local_large) and os.listdir(local_large):
        state.model_path = local_large
    elif os.path.exists(user_cache):
        state.model_path = user_cache
    else:
        state.model_path = EMBEDDING_MODEL_NAME

    # 清理残留的 Hugging Face 锁文件，防止历史异常退出导致死锁挂起
    locks_dir = os.path.expanduser("~/.cache/huggingface/hub/.locks")
    if os.path.exists(locks_dir):
        try:
            shutil.rmtree(locks_dir, ignore_errors=True)
        except Exception:
            pass

    # 并行并发执行 3 大初始化任务
    with ThreadPoolExecutor(max_workers=3) as executor:
        loop = asyncio.get_running_loop()
        task_embedder = loop.run_in_executor(executor, _worker_init_embedder_and_anchors, state.model_path)
        task_laya = loop.run_in_executor(executor, _worker_init_laya)
        task_storage = loop.run_in_executor(executor, _worker_init_storage)

        (embedder_res, laya_res, storage_res) = await asyncio.gather(
            task_embedder, task_laya, task_storage
        )

    state.embedder, state.vector_dim, state.direct_vecs, state.memory_vecs = embedder_res
    state.embedding_backend = str(getattr(state.embedder, "backend_name", "torch"))
    state.laya_agent = laya_res
    raw_index, state.memory_history, loaded_active_files = storage_res
    if loaded_active_files:
        state.active_files = loaded_active_files
        print(f"  ✔ 成功恢复工作区活跃文件 ({len(state.active_files)} 个文件)！", flush=True)

    # 校验并对齐 Turbovec 索引维度
    if raw_index is not None:
        stored_dim = getattr(raw_index, 'dim', None)
        if stored_dim is not None and stored_dim != state.vector_dim:
            print(f"[持久化] ⚠ 向量维度不匹配 (存储:{stored_dim} vs 当前模型:{state.vector_dim})，丢弃旧索引重新创建。", flush=True)
            state.index = TurboQuantIndex(dim=state.vector_dim, bit_width=4)
        else:
            state.index = raw_index
            print(f"[持久化] ✔ 成功加载已有 Turbovec 索引 (维度: {state.vector_dim})！", flush=True)
    else:
        state.index = TurboQuantIndex(dim=state.vector_dim, bit_width=4)

    # 一致性校验：索引向量条数必须与文本条数相同，否则检索会返回错误的记忆
    try:
        n_vec = len(state.index)
    except TypeError:
        n_vec = None
    index_rebuilt = False
    if n_vec is not None and n_vec != len(state.memory_history):
        print(f"[持久化] ⚠ 索引条数 ({n_vec}) 与记忆文本条数 ({len(state.memory_history)}) 不一致，正在按文本重建索引...", flush=True)
        await asyncio.to_thread(rebuild_index_from_history)
        index_rebuilt = True

    # Embedding 后端记录：fp32 (torch/onnx) <-> int8 切换时重建索引；torch <-> onnx fp32 无需重建
    try:
        await asyncio.to_thread(sync_embedding_backend_record, state.embedding_backend, index_rebuilt)
    except Exception as e:
        print(f"[持久化] ⚠ 检查 embedding 后端记录失败: {e}", flush=True)

    state.session_turns = []
    
    # 启动时同步刷新一次运行时状态 (推理引擎地址 / 显存 / n_ctx / 当前模型)，随后交给后台监控线程
    try:
        await asyncio.to_thread(refresh_runtime_state)
    except Exception:
        pass
    start_runtime_monitor()

    # 知识图谱后台整理 (仅在用户空闲时用本地模型抽取实体/关系，用户请求开始即让位)
    try:
        kg_service.start()
    except Exception as e:
        print(f"  [知识图谱] ⚠ 后台整理启动失败: {e}", flush=True)

    # 启动时主动清理显存中可能残留的多余模型，保持干净的 4GB VRAM 运行环境
    initial_model = getattr(state, "current_model", LLM_MODEL) or LLM_MODEL
    try:
        await asyncio.to_thread(unload_other_runner_models, initial_model)
    except Exception:
        pass

    total_startup_sec = time.time() - t_total_start
    print(f"\n🚀 [并行加速] 所有组件加载完毕！总耗时: {total_startup_sec:.2f}s (原串行预计耗时大幅缩减)")
    print(">>> Turbovec RAG Web 服务与 OpenAI 兼容网关已就绪！请访问: http://localhost:18088\n", flush=True)
    yield
    _monitor_stop.set()

    try:
        await asyncio.wait_for(kg_service.stop(), timeout=5)
        kg_service.close()
    except Exception as e:
        print(f"  [知识图谱] ⚠ 停止后台整理异常: {e}", flush=True)

    # 关闭所有 MCP 连接并结束 stdio MCP 子进程 (Agent 工具)
    try:
        await asyncio.wait_for(agent_service.shutdown(), timeout=15)
    except Exception as e:
        print(f"  [Agent] ⚠ 关闭 MCP 连接异常: {e}", flush=True)

    # 关闭时释放全局连接池与套接字资源
    if _global_async_client is not None and not _global_async_client.is_closed:
        await _global_async_client.aclose()
    _sync_session.close()

    # 关闭时同步持久化
    if state.index is not None:
        print("\n服务关闭中，正在同步保存记忆与索引...", flush=True)
        save_storage(state.index, state.memory_history, state.active_files)

app = FastAPI(title="Turbovec RAG Web UI", lifespan=lifespan)

# ==============================================================================
# 安全防护 (Security Guard)
# - 默认只监听 127.0.0.1；如需局域网访问，设置 TV_HOST=0.0.0.0 且必须同时设置 TV_API_TOKEN
# - 非本机 (非 loopback) 请求必须携带 Authorization: Bearer <TV_API_TOKEN> 或 X-TV-Token
# - 本机请求校验 Host 头 (防 DNS rebinding)；写操作校验 Origin (防 CSRF)
# ==============================================================================
TV_HOST = os.environ.get("TV_HOST", "127.0.0.1")
TV_PORT = int(os.environ.get("PORT", 18088))
TV_API_TOKEN = os.environ.get("TV_API_TOKEN", "").strip()
_LOOPBACK_CLIENTS = {"127.0.0.1", "::1", "localhost", "testclient"}
_LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "[::1]", "::1"}
_EXTRA_ORIGINS = {o.strip().rstrip("/") for o in os.environ.get("TV_ALLOWED_ORIGINS", "").split(",") if o.strip()}
_EXTRA_HOSTS = {h.strip().lower() for h in os.environ.get("TV_ALLOWED_HOSTS", "").split(",") if h.strip()}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _split_host(host_header: str) -> str:
    h = (host_header or "").strip().lower()
    if h.startswith("["):  # IPv6 literal
        return h.split("]")[0] + "]"
    return h.rsplit(":", 1)[0] if ":" in h else h


def _request_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-tv-token", "").strip()


def check_request_security(method: str, client_host: str, host_header: str, origin: Optional[str], token: str) -> Optional[str]:
    """返回 None 表示放行，否则返回拒绝原因 (纯函数，便于单元测试)"""
    is_loopback = client_host in _LOOPBACK_CLIENTS
    if not is_loopback:
        if not TV_API_TOKEN:
            return "非本机访问已禁用：请设置环境变量 TV_API_TOKEN 后再开放局域网访问"
        import hmac
        if not hmac.compare_digest(token or "", TV_API_TOKEN):
            return "缺少或错误的访问令牌 (TV_API_TOKEN)"
    else:
        host = _split_host(host_header)
        if host and host not in _LOCAL_HOSTNAMES and host not in _EXTRA_HOSTS:
            return f"Host 头不被信任: {host} (如需放行请设置 TV_ALLOWED_HOSTS)"
    if method.upper() not in _SAFE_METHODS and origin and origin.lower() != "null":
        o = origin.rstrip("/").lower()
        o_host = _split_host(o.split("://", 1)[-1])
        if o not in _EXTRA_ORIGINS and o_host not in _LOCAL_HOSTNAMES and o_host not in _EXTRA_HOSTS:
            return f"跨站请求被拒绝 (Origin: {origin})，如需放行请设置 TV_ALLOWED_ORIGINS"
    elif method.upper() not in _SAFE_METHODS and origin and origin.lower() == "null":
        return "跨站请求被拒绝 (Origin: null)"
    return None


@app.middleware("http")
async def security_guard(request: Request, call_next):
    client_host = request.client.host if request.client else ""
    reason = check_request_security(
        request.method,
        client_host,
        request.headers.get("host", ""),
        request.headers.get("origin"),
        _request_token(request),
    )
    if reason:
        return JSONResponse(status_code=403, content={"detail": reason})
    return await call_next(request)

# 挂载静态文件
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

class ChatRequest(BaseModel):
    message: str
    stream: Optional[bool] = False
    model: Optional[str] = None
    thinking: Optional[bool] = None
    web_search: Optional[bool] = False
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Any] = None
    # Agent 工具调用 (docs/AGENT_API.md §3)
    tools_enabled: Optional[bool] = False
    prompt_id: Optional[str] = None
    approval_mode: Optional[str] = None  # "default" | "ask_all" | "auto"

def is_logic_or_reasoning_query(query: str) -> bool:
    """检测是否属于逻辑推导、算法设计、数学谜题、Bug根因排查或架构权衡题"""
    q = query.lower().strip()
    if re.match(r'^(?:第\s*)?\d+\s*[-~到至–]\s*\d+\s*行?$', q) or re.match(r'^(?:前|首)\s*\d+\s*行?$', q):
        return False
    logic_keywords = [
        "为什么", "原因", "分析", "推导", "证明", "算法", "复杂度", "逻辑", "难题", "谜题",
        "死锁", "并发", "优化", "权衡", "排查", "根因", "推演", "设计", "思路", "优缺点",
        "怎么解决", "如何解决", "边界", "架构设计", "方案对比", "区别", "原理", "机制",
        "why", "analyze", "analysis", "reason", "algorithm", "complexity", "logic", "puzzle",
        "deadlock", "concurrency", "optimize", "tradeoff", "diagnose", "root cause", "prove", "proof",
        "how to", "architecture", "mechanism"
    ]
    return any(kw in q for kw in logic_keywords)

class ModelSwitchRequest(BaseModel):
    model: str

@app.get("/", response_class=FileResponse)
async def read_index():
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return HTMLResponse("<h1>Turbovec RAG 系统启动中，请稍候刷新...</h1>")

def get_current_buffer_bytes() -> int:
    """计算当前活跃内存缓冲区总量 (bytes)，仅计入未固化至 RAG 且文本驻留在内存中的文件与会话记录"""
    total = sum(
        info.get("bytes", len(info["text"].encode("utf-8")))
        for info in list(state.active_files.values())
        if not info.get("in_rag", False) and info.get("text")
    )
    for t in list(state.session_turns):
        total += len(t["user"].encode("utf-8")) + len(t["assistant"].encode("utf-8"))
    return total

def get_file_content(filename: str) -> str:
    """获取文件内容：优先从内存活跃区读取；若已转入 RAG 并清理内存，则按需从持久化磁盘只读流加载"""
    info = state.active_files.get(filename)
    if not info:
        return ""
    text = info.get("text", "")
    if text:
        return text
    try:
        fpath = storage_path(filename)
    except ValueError:
        return ""
    if os.path.exists(fpath):
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception:
            return ""
    return ""

_flush_lock = threading.RLock()  # 防止 /api/flush 与上传触发的自动转存并发处理同一文件

def flush_file_to_turbovec(filename: str, info: Dict) -> int:
    with _flush_lock:
        return _flush_file_to_turbovec_locked(filename, info)

def _flush_file_to_turbovec_locked(filename: str, info: Dict) -> int:
    """
    将单个文件切片并向量化写入 Turbovec 4-bit 磁盘库，并彻底清理活跃内存：
    1. 持久化备份至 storage/files/{filename}
    2. 切片并向量化加入 Turbovec 索引与历史记忆
    3. 清理内存中驻留的大文本 text，重置 bytes 为 0，降级为 outline_rag 模式释放内存
    """
    if info.get("in_rag", False) and not info.get("text"):
        return 0
    text = get_file_content(filename)
    if not text:
        return 0

    # 1. 备份源文件到持久化存储目录
    try:
        with open(storage_path(filename), "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        print(f"  [文件备份] 写入持久化磁盘失败: {e}", flush=True)

    # 2. 切片并入库
    outline = info.get("outline", "")
    chunks = chunk_document_text(filename, text)
    if not chunks:
        return 0
    chunk_entries = [outline] + [
        f"【文件切片 - {filename} ({i+1}/{len(chunks)})】:\n{c}" 
        for i, c in enumerate(chunks)
    ]
    if not add_memory_entries(chunk_entries):
        return 0

    # 3. 满时自动放入 RAG 并清理活跃内存！
    info["in_rag"] = True
    info["chunks"] = len(chunk_entries)
    info["mode"] = "outline_rag"
    info["text"] = ""   # 彻底清空内存中的巨幅字符串，释放 RAM/VRAM
    info["bytes"] = 0   # 活跃缓冲区归零

    print(f"  [RAG 自动转存并清理] ✔ 文件 {filename} 已成功转存至 Turbovec (共 {len(chunk_entries)} 切片)，并清理释放了内存缓冲区！", flush=True)
    return len(chunk_entries)

def check_and_flush_buffer() -> int:
    """检查活跃缓存池是否触及当前动态显存上限，若是则自动存入 RAG 并清理内存"""
    with _flush_lock:
        return _check_and_flush_buffer_locked()

def _check_and_flush_buffer_locked() -> int:
    dynamic_max_bytes = get_dynamic_max_buffer_bytes()
    cur_bytes = get_current_buffer_bytes()
    flushed_chunks = 0
    if cur_bytes >= dynamic_max_bytes:
        dynamic_max_gb = get_dynamic_max_buffer_gb()
        print(f"[缓存池自适应] ⚠ 缓存池已满 ({cur_bytes / 1024 / 1024:.2f} MB >= {dynamic_max_gb * 1024:.1f} MB, VRAM 自适应上限: {dynamic_max_gb} GB)，执行自动放入 RAG 并清理内存...", flush=True)
        for fn, info in list(state.active_files.items()):
            if not info.get("in_rag", False) or info.get("text"):
                c = flush_file_to_turbovec(fn, info)
                flushed_chunks += c
            if get_current_buffer_bytes() < dynamic_max_bytes:
                break
        if state.index is not None:
            save_storage(state.index, state.memory_history, state.active_files)
        print(f"[缓存池自适应] ✔ 自动入库完成！内存已清理释放，当前剩余活跃缓存: {get_current_buffer_bytes() / 1024 / 1024:.2f} MB", flush=True)
    return flushed_chunks

@app.get("/api/status")
def get_status():
    vram_info = get_gpu_vram_info()
    max_gb = get_dynamic_max_buffer_gb()
    current_buffer = get_current_buffer_bytes()
    state.buffer_bytes = current_buffer
    active_base = get_active_llm_base()
    return {
        "llm_model": getattr(state, "current_model", LLM_MODEL),
        "available_models": get_available_models(),
        "active_llm_base": active_base,
        "has_laya": HAS_LAYA and (state.laya_agent is not None),
        "embedding_model": state.model_path,
        "memory_count": len(state.memory_history),
        "session_turns": len(state.session_turns),
        "buffer_mb": round(current_buffer / (1024 * 1024), 2),
        "max_buffer_gb": max_gb,
        "max_buffer_mb": int(max_gb * 1024),
        "server_n_ctx": get_active_server_n_ctx(),
        "vram_info": vram_info,
        "kg": kg_service.summary(),
        "active_files": [
            {
                "filename": fn,
                "lines": info.get("lines", 0),
                "chars": info.get("chars", 0),
                "mode": info.get("mode", "full_context"),
                "in_rag": info.get("in_rag", False),
                "bytes": info.get("bytes", 0),
                "local_path": info.get("local_path", ""),
                "source": info.get("source", "")
            }
            for fn, info in list(state.active_files.items())
        ]
    }

_model_switch_lock = asyncio.Lock()

@app.post("/api/model")
async def switch_model(req: ModelSwitchRequest):
    """切换大模型：串行加锁；只有新模型真正就绪后才更新 current_model，失败返回 503"""
    models = get_available_models()
    valid_ids = [m["id"] for m in models]
    if req.model not in valid_ids:
        raise HTTPException(status_code=400, detail=f"不支持的模型: {req.model}，支持的模型列表: {valid_ids}")
    if _model_switch_lock.locked():
        raise HTTPException(status_code=409, detail="已有模型切换正在进行，请稍候")

    name = next((m["name"] for m in models if m["id"] == req.model), req.model)
    async with _model_switch_lock:
        state._switching_model = True
        try:
            if is_native_llama_running() or ("18089" in get_active_llm_base()):
                ok, msg = await asyncio.to_thread(restart_native_llama_server, req.model)
            else:
                # Docker Model Runner / Ollama 模式：通过 REST 接口卸载旧模型并预载新模型
                await asyncio.to_thread(unload_other_runner_models, req.model)
                ok = await asyncio.to_thread(preload_runner_model, req.model)
                msg = "预载完成" if ok else "预载失败"
        finally:
            state._switching_model = False

        if not ok:
            await asyncio.to_thread(refresh_runtime_state)
            raise HTTPException(status_code=503, detail=f"模型切换失败：{msg}")

        state.current_model = req.model
        state._live_n_ctx = None
        await asyncio.to_thread(refresh_runtime_state)
        state.current_model = req.model

    print(f"[模型切换] 🔄 当前大模型已切换为: {name} ({req.model}) - {msg}", flush=True)
    return {
        "status": "ok",
        "current_model": state.current_model,
        "model_name": name,
        "preloaded": True,
        "server_n_ctx": get_active_server_n_ctx(),
        "message": f"大模型已切换为 {name}：{msg}"
    }

@app.post("/api/clear")
def clear_session():
    state.session_turns = []
    state.compact_summary = ""  # 同时清除 Compact Window 压缩摘要
    state.active_files.clear()
    state.buffer_bytes = 0
    save_storage(state.index, state.memory_history, state.active_files)  # 持久化清空结果，重启后不再复活
    return {"status": "ok", "message": "短期会话记忆与工作区活跃缓存已重置"}

@app.post("/api/clear_rag")
def clear_rag_storage():
    """彻底清空 Turbovec 4-bit 磁盘索引与所有历史记忆"""
    with _memory_lock:
        state.memory_history.clear()
        state.session_turns.clear()
        state.compact_summary = ""
        state.active_files.clear()
        state.buffer_bytes = 0
        if state.vector_dim:
            state.index = TurboQuantIndex(dim=state.vector_dim, bit_width=4)
        save_storage(state.index, state.memory_history, state.active_files)
    # 彻底清空：同时删除工作区文件副本 (保留 .gitkeep)
    for root, _dirs, fnames in os.walk(FILE_STORAGE_DIR):
        for fn in fnames:
            if fn != ".gitkeep":
                try:
                    os.remove(os.path.join(root, fn))
                except OSError:
                    pass
    print("  [RAG 清空] ✔ 已成功彻底清空所有 Turbovec 长期记忆与 RAG 索引！", flush=True)
    return {"status": "ok", "message": "已成功彻底清空所有 Turbovec 长期记忆与 RAG 索引！"}

@app.post("/api/flush")
def flush_all_to_turbovec():
    """手动将当前所有未持久化的文件切片并向量化写入 Turbovec，并清理内存活跃缓存"""
    total_flushed = 0
    for fn, info in list(state.active_files.items()):
        if not info.get("in_rag", False) or info.get("text"):
            total_flushed += flush_file_to_turbovec(fn, info)
    if total_flushed > 0 and state.index is not None:
        save_storage(state.index, state.memory_history, state.active_files)
    current_buffer = get_current_buffer_bytes()
    state.buffer_bytes = current_buffer
    return {
        "status": "ok",
        "flushed_chunks": total_flushed,
        "total_memory_count": len(state.memory_history),
        "buffer_mb": round(current_buffer / (1024 * 1024), 2)
    }


# ==============================================================================
# 本地文件系统访问 (Local Filesystem Access)
# 允许直接浏览、预览和挂载本地磁盘文件，无需手动上传。
# 特性：支持本机全盘符发现 (C:\, D:\, F:\ 等) + 快捷目录，大小写自适应，防拒绝访问异常。
# ==============================================================================

# 不展示/挂载的敏感文件后缀（允许查看与编辑代码与脚本）
_BLOCKED_EXTS = {".exe", ".dll", ".sys", ".msi", ".com", ".scr", ".lnk", ".key", ".pem", ".p12", ".pfx"}
# 安全禁止覆写写入的危险后缀
_BLOCKED_WRITE_EXTS = _BLOCKED_EXTS | {".so", ".pyd", ".bat", ".cmd", ".vbs", ".reg"}
# 默认允许浏览 C:\ 等整盘？默认关闭，设置 TV_ALLOW_ALL_DRIVES=1 开启
TV_ALLOW_ALL_DRIVES = os.environ.get("TV_ALLOW_ALL_DRIVES", "0") == "1"
PROJECT_ROOT = os.path.dirname(os.path.abspath(BASE_DIR))

def _get_available_drives() -> List[str]:
    r"""获取 Windows 系统所有可访问的物理磁盘驱动器 (C:\, D:\, F:\ 等)"""
    drives = []
    if os.name == 'nt':
        import string
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if os.path.exists(root):
                drives.append(root)
    else:
        drives.append("/")
    return drives

def _clean_path(p: str) -> str:
    r"""标准化文件路径，消除 Windows 盘符当前工作目录歧义 (例如 C: -> C:\)"""
    if not p:
        return ""
    p = p.strip().strip('"')
    if re.match(r'^[a-zA-Z]:$', p):
        p = p + "\\"
    p = os.path.normpath(p)
    if re.match(r'^[a-zA-Z]:$', p):
        p = p + "\\"
    return p

def _norm_real(p: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(p)))

def _is_within(path: str, root: str) -> bool:
    """path 是否位于 root 之内 (realpath + normcase + commonpath，防 ../、符号链接/Junction 与大小写绕过)"""
    try:
        rp, rr = _norm_real(path), _norm_real(root)
        return os.path.commonpath([rp, rr]) == rr
    except ValueError:  # 不同盘符
        return False

# 默认白名单：桌面 / 文档 / 下载 / 项目目录 / 工作区存储；可用 ALLOWED_LOCAL_DIRS=路径1;路径2 追加
_DEFAULT_ALLOWED = [
    os.path.expanduser("~/Desktop"),
    os.path.expanduser("~/Documents"),
    os.path.expanduser("~/Downloads"),
    PROJECT_ROOT,
    FILE_STORAGE_DIR,
]
_extra = os.environ.get("ALLOWED_LOCAL_DIRS", "")
_DEFAULT_ALLOWED += [p.strip() for p in _extra.split(";") if p.strip()]
ALLOWED_LOCAL_DIRS: List[str] = []
for _p in _DEFAULT_ALLOWED:
    if os.path.exists(_p) and os.path.normpath(_p) not in ALLOWED_LOCAL_DIRS:
        ALLOWED_LOCAL_DIRS.append(os.path.normpath(_p))

def get_read_roots() -> List[str]:
    roots = list(ALLOWED_LOCAL_DIRS)
    if TV_ALLOW_ALL_DRIVES:
        roots += _get_available_drives()
    return roots

def _has_windows_trick(path: str) -> bool:
    """拦截 Windows 特殊路径：ADS (file.txt::$DATA)、结尾点/空格、设备名"""
    tail = path[2:] if re.match(r'^[a-zA-Z]:', path) else path
    if ":" in tail:
        return True
    name = os.path.basename(path)
    if name and (name.endswith(".") or name.endswith(" ")) and name not in (".", ".."):
        return True
    if os.path.splitext(name)[0].upper() in {"CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "LPT1", "LPT2"}:
        return True
    return False

def _is_allowed_path(path: str) -> bool:
    """读取 / 浏览 / 挂载白名单校验"""
    target = _clean_path(path)
    if not target or _has_windows_trick(target):
        return False
    return any(_is_within(target, root) for root in get_read_roots())

def _is_allowed_write_path(path: str) -> bool:
    """写回白名单：在读白名单内，但禁止改写本服务自身代码 (app/ 下除 storage/files 外) 与 .git 目录"""
    target = _clean_path(path)
    if not _is_allowed_path(target):
        return False
    if os.path.splitext(target)[1].lower() in _BLOCKED_WRITE_EXTS:
        return False
    real = _norm_real(target)
    # .git 目录 (含 Windows 8.3 短名 GIT~1)
    parts = {p.lower() for p in re.split(r'[\\/]+', real)}
    if ".git" in parts or any(p.startswith("git~") for p in parts):
        return False
    if _is_within(target, BASE_DIR) and not _is_within(target, FILE_STORAGE_DIR):
        return False
    # 项目根目录下的启动脚本与推理引擎配置 (改写它们等于改写将被执行的命令)
    if os.path.dirname(real) == _norm_real(PROJECT_ROOT):
        if os.path.splitext(real)[1].lower() in {".ps1", ".bat", ".cmd", ".json", ".py"}:
            return False
    return True

_WIN_RESERVED_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

def safe_filename(name: Optional[str], default: str = "untitled.txt") -> str:
    """把客户端提供的文件名收敛为单层安全文件名 (去掉目录、../、盘符与非法字符)"""
    raw = (name or "").replace("\\", "/").split("/")[-1]
    raw = _WIN_RESERVED_CHARS.sub("_", raw).strip().strip(".").strip()
    if not raw or raw in {".", ".."}:
        raw = default
    return raw[:180]

def storage_path(filename: str, subdir: str = "") -> str:
    """返回工作区存储目录内的安全路径；若越界则抛 ValueError"""
    base = os.path.join(FILE_STORAGE_DIR, subdir) if subdir else FILE_STORAGE_DIR
    os.makedirs(base, exist_ok=True)
    p = os.path.join(base, safe_filename(filename))
    if not _is_within(p, base):
        raise ValueError(f"非法存储路径: {filename}")
    return p


class FsListRequest(BaseModel):
    path: str = ""           # 要列出的目录路径，空则列出白名单根目录
    max_depth: int = 1       # 最大展开层级（1=仅列当前目录）
    show_hidden: bool = False

class FsReadRequest(BaseModel):
    path: str                # 文件绝对路径
    max_chars: int = 50000   # 最多预览字符数

class FsMountRequest(BaseModel):
    path: str                # 本地文件绝对路径，直接挂载进工作区
    use_ocr: bool = False    # 对 PDF 是否启用 OCR

class FsWriteRequest(BaseModel):
    path: str                # 本地文件绝对路径
    content: str             # 要写入的文件内容
    create_backup: bool = True  # 是否在覆写前自动生成 .bak 备份文件


@app.post("/api/fs/list")
def fs_list(req: FsListRequest):
    r"""
    列出本地目录内容。
    - path 为空时返回快捷访问与本机物理磁盘驱动器 (C:\, F:\ 等)。
    - 每个条目包含: name, path, type(file/dir), size, ext, modified。
    """
    # 无路径 → 返回快捷访问入口 + 本地磁盘盘符
    if not req.path.strip():
        roots = []
        # 1. 快捷访问入口
        quick_folders = [
            ("🖥️ 桌面", os.path.expanduser("~/Desktop")),
            ("📁 文档", os.path.expanduser("~/Documents")),
            ("📥 下载", os.path.expanduser("~/Downloads")),
            ("⚡ 当前项目工作区", PROJECT_ROOT),
        ]
        for label, p in quick_folders:
            if os.path.exists(p) and _is_allowed_path(p):
                norm_p = os.path.normpath(p)
                roots.append({
                    "name": label,
                    "path": norm_p,
                    "type": "dir",
                    "category": "quick",
                    "size": 0,
                    "ext": "",
                    "modified": ""
                })
        # 2. 本地驱动器 / 磁盘 (需 TV_ALLOW_ALL_DRIVES=1)
        for drv in (_get_available_drives() if TV_ALLOW_ALL_DRIVES else []):
            drv_name = f"💾 本地磁盘 ({drv.rstrip(os.sep)})"
            roots.append({
                "name": drv_name,
                "path": drv,
                "type": "dir",
                "category": "drive",
                "size": 0,
                "ext": "",
                "modified": ""
            })
        return {"entries": roots, "path": "", "allowed_roots": [r["path"] for r in roots]}

    target = _clean_path(req.path)
    if not _is_allowed_path(target):
        raise HTTPException(status_code=403, detail=f"路径不在允许范围内: {target}")
    if not os.path.isdir(target):
        raise HTTPException(status_code=400, detail=f"不是有效目录: {target}")

    entries = []
    try:
        filenames = os.listdir(target)
    except (PermissionError, OSError) as e:
        raise HTTPException(status_code=403, detail=f"无权限或无法读取目录: {e}")

    for name in sorted(filenames, key=lambda x: x.lower()):
        if not req.show_hidden and name.startswith("."):
            continue
        full = os.path.join(target, name)
        try:
            stat = os.stat(full)
            is_dir = os.path.isdir(full)
            ext = os.path.splitext(name)[1].lower()
            if not is_dir and ext in _BLOCKED_EXTS:
                continue
            entries.append({
                "name": name,
                "path": full,
                "type": "dir" if is_dir else "file",
                "size": 0 if is_dir else stat.st_size,
                "ext": "" if is_dir else ext,
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            })
        except (PermissionError, OSError, FileNotFoundError):
            continue

    parent = os.path.dirname(target)
    # 如果 target 是盘符根目录 (如 C:\ 或 F:\)，dirname 也是 C:\，此时 parent 应设为空以便返回根目录
    drive_roots = [os.path.normcase(d) for d in _get_available_drives()]
    if os.path.normcase(target) in drive_roots or os.path.normcase(target) + os.sep in drive_roots or parent == target:
        parent = ""
    elif parent and not _is_allowed_path(parent):
        parent = ""  # 已到白名单根目录，返回上一级即回到根列表

    return {
        "entries": entries,
        "path": target,
        "parent": parent,
        "count": len(entries)
    }


@app.post("/api/fs/read")
def fs_read(req: FsReadRequest):
    """
    读取本地文件内容预览（白名单保护）。
    - 返回文件内容字符串（最多 max_chars 字符）。
    - 支持文本、代码、PDF（PyMuPDF）、DOCX（python-docx）。
    """
    path = _clean_path(req.path)
    if not _is_allowed_path(path):
        raise HTTPException(status_code=403, detail=f"路径不在允许范围内: {path}")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"文件不存在: {path}")

    ext = os.path.splitext(path)[1].lower()
    if ext in _BLOCKED_EXTS:
        raise HTTPException(status_code=403, detail="该文件类型不允许读取")

    try:
        with open(path, "rb") as f:
            raw = f.read()
    except PermissionError:
        raise HTTPException(status_code=403, detail="无读取权限")

    size_bytes = len(raw)

    if ext == ".pdf":
        content = extract_pdf_text_pymupdf(raw, use_ocr_fallback=False)
    elif ext in (".docx", ".doc"):
        content = extract_docx_text(raw)
    else:
        content = decode_file_content(raw)

    truncated = len(content) > req.max_chars
    preview = content[:req.max_chars] if truncated else content

    return {
        "path": path,
        "filename": os.path.basename(path),
        "ext": ext,
        "size_bytes": size_bytes,
        "size_kb": round(size_bytes / 1024, 1),
        "content": preview,
        "chars": len(content),
        "truncated": truncated,
        "truncated_at": req.max_chars
    }


@app.post("/api/fs/mount")
def fs_mount(req: FsMountRequest):
    """
    将本地文件直接挂载到工作区（无需上传，零拷贝链接读取）。
    支持: 文本/代码 / PDF / DOCX / Markdown 等。
    挂载后与普通上传文件完全一致，可立即对话。
    """
    path = _clean_path(req.path)
    if not _is_allowed_path(path):
        raise HTTPException(status_code=403, detail=f"路径不在允许范围内: {path}")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"文件不存在: {path}")

    ext = os.path.splitext(path)[1].lower()
    if ext in _BLOCKED_EXTS:
        raise HTTPException(status_code=403, detail="该文件类型不允许挂载")

    filename = os.path.basename(path)

    try:
        with open(path, "rb") as f:
            raw = f.read()
    except PermissionError:
        raise HTTPException(status_code=403, detail="无读取权限")

    if ext == ".pdf":
        text = extract_pdf_text_pymupdf(raw, use_ocr_fallback=req.use_ocr)
        source_type = "local_pdf_ocr" if req.use_ocr else "local_pdf"
    elif ext in (".docx", ".doc"):
        text = extract_docx_text(raw)
        source_type = "local_docx"
    else:
        text = decode_file_content(raw)
        source_type = "local_file"

    if not text.strip():
        raise HTTPException(status_code=422, detail="文件无可提取文本内容")

    lines = text.split("\n")
    total_lines = len(lines)
    chars = len(text)
    outline = extract_code_outline(filename, text)
    mode = "full_context" if chars <= FULL_CONTEXT_CHAR_LIMIT else "outline_rag"

    state.active_files[filename] = {
        "text": text,
        "lines": total_lines,
        "chars": chars,
        "outline": outline,
        "mode": mode,
        "in_rag": False,
        "bytes": len(raw),
        "chunks": 0,
        "source": source_type,
        "local_path": path   # 记录原始本地路径，便于后续重载
    }

    # 也在工作区存储目录保留一份文本副本
    try:
        with open(storage_path(filename), "w", encoding="utf-8") as fw:
            fw.write(text)
    except Exception:
        pass

    flushed_chunks = check_and_flush_buffer()
    if state.index is not None:
        save_storage(state.index, state.memory_history, state.active_files)

    current_buffer = get_current_buffer_bytes()
    state.buffer_bytes = current_buffer

    print(f"[本地挂载] ✔ {filename} ({chars} 字符, {total_lines} 行) 直接挂载自: {path}", flush=True)

    return {
        "status": "ok",
        "filename": filename,
        "local_path": path,
        "lines": total_lines,
        "chars": chars,
        "mode": mode,
        "source": source_type,
        "flushed_to_rag": (flushed_chunks > 0),
        "buffer_mb": round(current_buffer / (1024 * 1024), 2)
    }


@app.get("/api/fs/allowed_dirs")
async def fs_get_allowed_dirs():
    """返回当前系统配置的白名单可访问目录列表"""
    return {
        "allowed_dirs": get_read_roots(),
        "all_drives_enabled": TV_ALLOW_ALL_DRIVES,
        "blocked_extensions": sorted(_BLOCKED_EXTS)
    }


@app.post("/api/fs/write")
def fs_write(req: FsWriteRequest):
    """
    保存并写回本地磁盘文件（支持自动备份、PowerShell BOM 兼容、实时同步工作区 RAG 状态）。
    实现 Antigravity 类似的代码与文件改写落地能力。
    """
    path = _clean_path(req.path)
    if not path:
        raise HTTPException(status_code=400, detail="文件路径不能为空")
    if not _is_allowed_write_path(path):
        raise HTTPException(status_code=403, detail=f"目标路径不在安全写入白名单内 (禁止写入服务自身代码、.git 与危险类型文件): {path}")

    ext = os.path.splitext(path)[1].lower()
    if ext in _BLOCKED_WRITE_EXTS:
        raise HTTPException(status_code=403, detail=f"安全保护限制：不允许向该类型文件 ({ext}) 写入内容")

    # 1. 自动安全备份：若目标文件已存在，创建 .bak 历史副本
    backup_path = None
    if os.path.isfile(path) and req.create_backup:
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            candidate = f"{path}.{ts}.bak"
            shutil.copy2(path, candidate)
            backup_path = candidate  # 仅在复制成功后才记录，避免返回不存在的备份路径
            print(f"[fs_write] ✔ 自动备份创建成功: {backup_path}", flush=True)
        except Exception as e:
            # 备份失败时中止写入，防止用户在无备份的情况下丢失原文件
            raise HTTPException(status_code=500, detail=f"备份创建失败，已中止写入以保护原文件: {e}")

    # 2. 写入文件（如果是 .ps1 脚本，使用 utf-8-sig 自动写入 BOM，彻底规避 PowerShell 5.1 解析报错）
    try:
        parent_dir = os.path.dirname(path)
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)

        encoding = "utf-8-sig" if ext == ".ps1" else "utf-8"
        with open(path, "w", encoding=encoding) as f:
            f.write(req.content)
    except PermissionError:
        raise HTTPException(status_code=403, detail="写入失败：无文件写入或修改权限（可能被其他进程独占锁定）")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文件写入异常: {str(e)}")

    stat = os.stat(path)
    filename = os.path.basename(path)
    lines = req.content.split("\n")
    total_lines = len(lines)
    chars = len(req.content)

    # 3. 实时联动工作区：若该文件已挂载在活跃会话中，立即热刷新上下文与缓存
    synced = False
    for active_fn, active_info in list(state.active_files.items()):
        if active_info.get("local_path") == path or active_fn == filename:
            active_info["text"] = req.content
            active_info["lines"] = total_lines
            active_info["chars"] = chars
            active_info["outline"] = extract_code_outline(filename, req.content)
            active_info["mode"] = "full_context" if chars <= FULL_CONTEXT_CHAR_LIMIT else "outline_rag"
            active_info["bytes"] = stat.st_size
            try:
                with open(storage_path(active_fn), "w", encoding="utf-8") as fw:
                    fw.write(req.content)
            except Exception:
                pass
            synced = True

    if synced and state.index is not None:
        try:
            save_storage(state.index, state.memory_history, state.active_files)
        except Exception:
            pass

    print(f"[fs_write] ✔ 成功写回本地文件: {path} ({total_lines} 行, {chars} 字符)", flush=True)

    return {
        "status": "ok",
        "path": path,
        "filename": filename,
        "chars": chars,
        "lines": total_lines,
        "size_bytes": stat.st_size,
        "backup_path": backup_path,
        "backup_filename": os.path.basename(backup_path) if backup_path else None,
        "synced_active_file": synced,
        "message": f"成功写入 {filename}（共 {total_lines} 行，{chars} 字符）" + (f"，已自动备份至 {os.path.basename(backup_path)}" if backup_path else "")
    }



@app.post("/api/upload")
async def upload_files(files: List[UploadFile] = File(...)):
    """
    极速工作区上传：先进入动态 VRAM 自适应内存工作缓存（0 毫秒 Embedding 阻塞，即刻可聊）。
    当累计缓存达到根据剩余 VRAM 计算的动态上限（最高 0.8GB）时，自动转存入 Turbovec 并清理内存缓存。
    """
    if not files:
        raise HTTPException(status_code=400, detail="未收到上传文件")
    
    uploaded_info = []

    for file in files:
        filename = safe_filename(file.filename, "untitled.txt")
        content_bytes = await file.read()
        text = decode_file_content(content_bytes)
        if not text.strip():
            continue
        
        # 始终备份源文件至持久化磁盘目录 (文件名已收敛，杜绝路径穿越)
        fpath = storage_path(filename)
        try:
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass

        lines = text.split("\n")
        total_lines = len(lines)
        chars = len(text)
        file_bytes = len(content_bytes)
        
        # 1. 自动提取代码/文档全局结构大纲（函数清单、类名、行号等，毫秒级纯正则）
        outline = extract_code_outline(filename, text)
        
        # 2. 存入当前会话动态高速内存工作区
        mode = "full_context" if chars <= FULL_CONTEXT_CHAR_LIMIT else "outline_rag"
        state.active_files[filename] = {
            "text": text,
            "lines": total_lines,
            "chars": chars,
            "outline": outline,
            "mode": mode,
            "in_rag": False,
            "bytes": file_bytes,
            "chunks": 0
        }
        
        uploaded_info.append({
            "filename": filename,
            "lines": total_lines,
            "chars": chars,
            "bytes": file_bytes,
            "mode": mode,
            "in_rag": False
        })

    # 3. 检查当前缓存总量是否触及根据可用 VRAM 计算的动态上限（满时自动转入 RAG 并清理内存）
    flushed_chunks = await asyncio.to_thread(check_and_flush_buffer)

    if state.index is not None:
        await asyncio.to_thread(save_storage, state.index, state.memory_history, state.active_files)

    current_buffer = get_current_buffer_bytes()
    state.buffer_bytes = current_buffer
    buffer_mb = current_buffer / (1024 * 1024)
    max_gb = get_dynamic_max_buffer_gb()

    print(f"[极速挂载] ✔ 成功载入 {len(uploaded_info)} 个文件至工作区 (当前活跃缓存: {buffer_mb:.2f} MB / {max_gb} GB)！", flush=True)

    return {
        "status": "ok",
        "message": f"成功挂载 {len(uploaded_info)} 个文件至工作区 (当前活跃缓存: {buffer_mb:.2f} MB / {max_gb} GB)",
        "files": uploaded_info,
        "buffer_mb": round(buffer_mb, 2),
        "max_buffer_gb": max_gb,
        "max_buffer_mb": int(max_gb * 1024),
        "flushed_to_rag": (flushed_chunks > 0),
        "total_memory_count": len(state.memory_history)
    }


# ==============================================================================
# PDF / DOCX / OCR 上传端口 — PyMuPDF + python-docx 文本提取与编辑
# 前端切换「OCR 模式」后，PDF/DOCX 文件走此端点而非普通 /api/upload
# ==============================================================================

def extract_pdf_text_pymupdf(content_bytes: bytes, use_ocr_fallback: bool = False) -> str:
    """使用 PyMuPDF (fitz) 提取 PDF 文本。
    - use_ocr_fallback=False (默认): 仅提取已嵌入的文本层 (速度极快，适合可复制 PDF)。
    - use_ocr_fallback=True: 对空文本页额外尝试 fitz 的内置 OCR (需系统安装 Tesseract)；
      若 Tesseract 不可用则回退到提示占位符。
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return "[错误] PyMuPDF 未安装，请运行 pip install pymupdf"

    pages_text = []
    try:
        doc = fitz.open(stream=content_bytes, filetype="pdf")
        for page_num, page in enumerate(doc, 1):
            text = page.get_text("text").strip()
            if not text and use_ocr_fallback:
                # 尝试 fitz 内置 OCR（需要 Tesseract）
                try:
                    text = page.get_textpage_ocr(flags=0, dpi=150).extractText().strip()
                except Exception:
                    text = f"[第 {page_num} 页: 扫描页，OCR 引擎不可用，请安装 Tesseract]"
            if text:
                pages_text.append(f"--- 第 {page_num} 页 ---\n{text}")
            elif not use_ocr_fallback:
                pages_text.append(f"--- 第 {page_num} 页 ---\n[空文本层，可能为扫描 PDF，请开启 OCR 模式]")
        doc.close()
    except Exception as e:
        return f"[PDF 解析错误] {e}"

    return "\n\n".join(pages_text) if pages_text else "[PDF 无可提取文本]"


def extract_docx_text(content_bytes: bytes) -> str:
    """使用 python-docx 提取 DOCX 全部文本内容。
    提取范围：正文段落 + 所有表格单元格 + 页眉 / 页脚。
    保留段落层级缩进标记，便于模型理解文档结构。
    """
    try:
        from docx import Document as DocxDocument
        import io
    except ImportError:
        return "[错误] python-docx 未安装，请运行 pip install python-docx"

    try:
        doc = DocxDocument(io.BytesIO(content_bytes))
        parts = []

        # 1. 正文段落（保留标题层级标记）
        for para in doc.paragraphs:
            txt = para.text.strip()
            if not txt:
                continue
            style_name = para.style.name if para.style else ""
            if style_name.startswith("Heading"):
                level = style_name.replace("Heading ", "").strip()
                prefix = "#" * int(level) if level.isdigit() else "##"
                parts.append(f"{prefix} {txt}")
            else:
                parts.append(txt)

        # 2. 表格（以 Markdown 格式提取）
        for tbl_idx, table in enumerate(doc.tables, 1):
            parts.append(f"\n[表格 {tbl_idx}]")
            for row in table.rows:
                cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
                parts.append("| " + " | ".join(cells) + " |")

        # 3. 页眉 / 页脚
        for section in doc.sections:
            for hdr_para in section.header.paragraphs:
                t = hdr_para.text.strip()
                if t:
                    parts.append(f"[页眉] {t}")
            for ftr_para in section.footer.paragraphs:
                t = ftr_para.text.strip()
                if t:
                    parts.append(f"[页脚] {t}")

        return "\n".join(parts) if parts else "[DOCX 无可提取文本]"
    except Exception as e:
        return f"[DOCX 解析错误] {e}"


@app.post("/api/upload_pdf")
async def upload_pdf(files: List[UploadFile] = File(...), ocr: str = "false"):
    """
    专用 PDF / DOCX 上传端口。
    - .pdf  → PyMuPDF 高速提取嵌入文本层，ocr=true 时启用 Tesseract 扫描识别。
    - .docx → python-docx 提取正文、表格、页眉页脚，完整保留结构。
    - 提取后与普通文件上传完全一致，进入工作区高速缓存可立即对话。
    """
    if not files:
        raise HTTPException(status_code=400, detail="未收到文件")

    use_ocr = (ocr.lower() == "true")
    uploaded_info = []

    for file in files:
        filename = safe_filename(file.filename, "document")
        content_bytes = await file.read()

        ext = os.path.splitext(filename)[1].lower()
        if ext == ".pdf":
            text = await asyncio.to_thread(extract_pdf_text_pymupdf, content_bytes, use_ocr)
            source_type = "pdf_ocr" if use_ocr else "pdf"
        elif ext in (".docx", ".doc"):
            text = await asyncio.to_thread(extract_docx_text, content_bytes)
            source_type = "docx"
        else:
            # 其他文本文件直接解码
            text = decode_file_content(content_bytes)
            source_type = "text"

        if not text.strip():
            continue

        # 持久化已提取文本（工作区以原名为 key）；原始二进制单独存放于 originals/，避免被文本覆盖
        try:
            with open(storage_path(filename), "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass

        # 额外保存原始二进制（供 /api/edit_docx 读取修改）
        if ext in (".docx", ".doc"):
            orig_path = storage_path(filename, "originals")
            try:
                with open(orig_path, "wb") as fb:
                    fb.write(content_bytes)
            except Exception:
                pass

        lines = text.split("\n")
        total_lines = len(lines)
        chars = len(text)
        file_bytes = len(content_bytes)
        outline = extract_code_outline(filename, text)
        mode = "full_context" if chars <= FULL_CONTEXT_CHAR_LIMIT else "outline_rag"

        state.active_files[filename] = {
            "text": text,
            "lines": total_lines,
            "chars": chars,
            "outline": outline,
            "mode": mode,
            "in_rag": False,
            "bytes": file_bytes,
            "chunks": 0,
            "source": source_type
        }

        pages_count = text.count("--- 第") if ext == ".pdf" else 0
        uploaded_info.append({
            "filename": filename,
            "type": ext.lstrip("."),
            "lines": total_lines,
            "chars": chars,
            "bytes": file_bytes,
            "mode": mode,
            "in_rag": False,
            "pages": pages_count,
            "ocr_used": use_ocr and ext == ".pdf"
        })

    flushed_chunks = await asyncio.to_thread(check_and_flush_buffer)
    if state.index is not None:
        await asyncio.to_thread(save_storage, state.index, state.memory_history, state.active_files)

    current_buffer = get_current_buffer_bytes()
    state.buffer_bytes = current_buffer
    buffer_mb = current_buffer / (1024 * 1024)
    max_gb = get_dynamic_max_buffer_gb()

    type_labels = {"pdf": "PDF 嵌入文本层", "pdf_ocr": "PDF + OCR 扫描", "docx": "DOCX 结构化提取"}
    print(f"[文档挂载] ✔ 成功解析 {len(uploaded_info)} 个文档，已载入工作区！", flush=True)

    return {
        "status": "ok",
        "message": f"成功挂载 {len(uploaded_info)} 个文档至工作区",
        "files": uploaded_info,
        "buffer_mb": round(buffer_mb, 2),
        "max_buffer_gb": max_gb,
        "max_buffer_mb": int(max_gb * 1024),
        "flushed_to_rag": (flushed_chunks > 0),
        "total_memory_count": len(state.memory_history),
        "ocr_used": use_ocr
    }


# ==============================================================================
# DOCX 编辑端点 — 对工作区已挂载的 DOCX 文件执行结构化编辑操作
# 支持: find_replace (查找替换), append_paragraph (追加段落), set_style (设置样式)
# 编辑完成后返回修改后的 .docx 文件供下载
# ==============================================================================

class DocxEditRequest(BaseModel):
    filename: str                    # 工作区中已挂载的 DOCX 文件名
    operations: List[Dict[str, Any]] # 操作列表，见下方说明

@app.post("/api/edit_docx")
def edit_docx(req: DocxEditRequest):
    """
    对工作区已挂载的 DOCX 文件执行批量编辑操作，返回修改后的文件流供下载。

    operations 支持以下类型（每条为一个字典）：

    1. 查找替换文本：
       {"op": "find_replace", "find": "旧文字", "replace": "新文字"}

    2. 追加段落到文档末尾：
       {"op": "append_paragraph", "text": "内容", "style": "Normal" (可选)}

    3. 设置段落样式（按索引）：
       {"op": "set_style", "paragraph_index": 0, "style": "Heading 1"}

    4. 删除包含指定文本的段落：
       {"op": "delete_paragraph", "contains": "要删除的关键词"}
    """
    try:
        from docx import Document as DocxDocument
        from docx.shared import Pt
        import io
    except ImportError:
        raise HTTPException(status_code=500, detail="python-docx 未安装")

    # 查找原始 DOCX 文件
    fname = safe_filename(req.filename, "document.docx")
    if fname != req.filename or not fname.lower().endswith((".docx", ".doc")):
        raise HTTPException(status_code=400, detail=f"非法的 DOCX 文件名: {req.filename}")
    orig_path = storage_path(fname, "originals")
    if not os.path.exists(orig_path):
        # 兼容旧版本：原件曾与文本存放在同一路径
        legacy = storage_path(fname)
        import zipfile
        if os.path.exists(legacy) and zipfile.is_zipfile(legacy):
            orig_path = legacy
        else:
            raise HTTPException(status_code=404, detail=f"工作区中未找到 DOCX 原始文件: {req.filename}")

    try:
        doc = DocxDocument(orig_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DOCX 打开失败: {e}")

    applied_ops = []
    errors = []

    for op_dict in (req.operations or []):
        op = op_dict.get("op", "")
        try:
            if op == "find_replace":
                find_text = op_dict.get("find", "")
                replace_text = op_dict.get("replace", "")
                count = 0
                for para in doc.paragraphs:
                    if find_text in para.text:
                        for run in para.runs:
                            if find_text in run.text:
                                run.text = run.text.replace(find_text, replace_text)
                                count += 1
                # 表格内也替换
                for table in doc.tables:
                    for row in table.rows:
                        for cell in row.cells:
                            for para in cell.paragraphs:
                                for run in para.runs:
                                    if find_text in run.text:
                                        run.text = run.text.replace(find_text, replace_text)
                                        count += 1
                applied_ops.append(f"find_replace: '{find_text}' → '{replace_text}' ({count} 处)")

            elif op == "append_paragraph":
                text = op_dict.get("text", "")
                style = op_dict.get("style", "Normal")
                try:
                    doc.add_paragraph(text, style=style)
                except Exception:
                    doc.add_paragraph(text)
                applied_ops.append(f"append_paragraph: '{text[:40]}...' (style={style})")

            elif op == "set_style":
                idx = int(op_dict.get("paragraph_index", 0))
                style = op_dict.get("style", "Normal")
                if 0 <= idx < len(doc.paragraphs):
                    try:
                        doc.paragraphs[idx].style = doc.styles[style]
                    except Exception:
                        pass
                    applied_ops.append(f"set_style: paragraph[{idx}] → '{style}'")

            elif op == "delete_paragraph":
                keyword = op_dict.get("contains", "")
                removed = 0
                for para in doc.paragraphs:
                    if keyword in para.text:
                        p = para._element
                        p.getparent().remove(p)
                        removed += 1
                applied_ops.append(f"delete_paragraph: 含 '{keyword}' 的段落 × {removed}")

            else:
                errors.append(f"未知操作类型: {op}")
        except Exception as e:
            errors.append(f"操作 '{op}' 执行异常: {e}")

    # 保存修改后的文件到内存，供下载
    out_buf = io.BytesIO()
    doc.save(out_buf)
    out_buf.seek(0)

    # 同时更新工作区文本缓存（重新提取文本）
    out_buf_copy = io.BytesIO(out_buf.getvalue())
    out_buf_copy.seek(0)
    updated_text = extract_docx_text(out_buf_copy.read())
    out_buf.seek(0)

    # 覆盖保存原始文件
    try:
        with open(orig_path, "wb") as fb:
            fb.write(out_buf.getvalue())
        out_buf.seek(0)
    except Exception:
        pass

    if req.filename in state.active_files:
        lines = updated_text.split("\n")
        state.active_files[req.filename]["text"] = updated_text
        state.active_files[req.filename]["lines"] = len(lines)
        state.active_files[req.filename]["chars"] = len(updated_text)

    print(f"[DOCX 编辑] ✔ {req.filename}: {len(applied_ops)} 个操作完成, {len(errors)} 个错误", flush=True)

    # 返回修改后的 .docx 文件
    safe_name = req.filename.encode("utf-8").decode("ascii", errors="replace")
    headers = {
        "Content-Disposition": f'attachment; filename="{safe_name}"',
        "X-Applied-Ops": str(len(applied_ops)),
        "X-Errors": "; ".join(errors) if errors else "none"
    }
    return StreamingResponse(
        out_buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers=headers
    )




def extract_clean_web_content(html_str: str) -> Tuple[str, str]:
    """从原始网页 HTML 中提取标题与纯净正文，剔除所有 script/style/广告代码"""
    title_match = re.search(r'<title[^>]*>(.*?)</title>', html_str, re.IGNORECASE | re.DOTALL)
    title = title_match.group(1).strip() if title_match else "网页资料"
    title = re.sub(r'[\r\n\t]+', ' ', title).strip()

    # 剔除 script, style, svg, noscript, iframe, header, footer, nav
    clean = re.sub(r'<(script|style|svg|noscript|iframe|nav|footer|header)[^>]*>.*?</\1>', '', html_str, flags=re.IGNORECASE | re.DOTALL)
    # 常见块级标签换行
    clean = re.sub(r'<(p|div|h[1-6]|li|br|tr|article|section)[^>]*>', '\n', clean, flags=re.IGNORECASE)
    # 剔除剩余 HTML 标签
    clean = re.sub(r'<[^>]+>', ' ', clean)
    import html
    clean = html.unescape(clean)
    # 清洗多余空白行
    clean = re.sub(r'[ \t]+', ' ', clean)
    clean = re.sub(r'\n\s*\n+', '\n\n', clean).strip()
    return title or "网页正文", clean

class UnsafeUrlError(ValueError):
    pass

def assert_public_url(url: str) -> None:
    """SSRF 防护：只允许 http(s)，且目标主机解析后的所有 IP 均为公网地址"""
    import ipaddress
    import socket
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeUrlError("仅支持 http:// 或 https:// 网址")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as e:
        raise UnsafeUrlError(f"无法解析域名: {parts.hostname} ({e})")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise UnsafeUrlError(f"禁止抓取内网 / 本机地址: {parts.hostname} -> {ip}")

def safe_public_get(url: str, headers: Optional[dict] = None, timeout: float = 20, max_redirects: int = 5):
    """逐跳校验重定向目标的 GET，防止经 302 跳转到内网"""
    from urllib.parse import urljoin
    current = url
    for _ in range(max_redirects + 1):
        assert_public_url(current)
        resp = requests.get(current, headers=headers, timeout=timeout, allow_redirects=False)
        if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("location")
            if not loc:
                return resp
            current = urljoin(current, loc)
            continue
        return resp
    raise UnsafeUrlError("重定向次数过多")

class UrlFetchRequest(BaseModel):
    url: str

@app.post("/api/fetch_url")
async def fetch_and_ingest_url(req: UrlFetchRequest):
    """自动抓取外部网页，清洗广告与 HTML 标签，并自动切块写入 Turbovec 记忆库"""
    url = req.url.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="请输入以 http:// 或 https:// 开头的有效网址")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    try:
        resp = await asyncio.to_thread(safe_public_get, url, headers, 20)
        resp.raise_for_status()
        raw_html = decode_file_content(resp.content)
    except UnsafeUrlError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"抓取网页失败: {e}")

    title, clean_text = extract_clean_web_content(raw_html)
    if not clean_text or len(clean_text) < 10:
        raise HTTPException(status_code=400, detail="网页抓取成功，但未能提取到有效的正文文本（可能目标网站有登录墙或反爬）")

    # 构造虚拟文件名
    safe_title = re.sub(r'[\\/*?:"<>|]', '_', title)[:30].strip() or "webpage"
    virtual_filename = f"🌐 {safe_title}.txt"

    lines = clean_text.splitlines()
    total_lines = len(lines)
    chars = len(clean_text)
    file_bytes = len(clean_text.encode('utf-8'))

    # 生成大纲
    outline = f"【网页大纲: {title}】\n- 来源网址: {url}\n- 正文字数: {chars} 字符\n"

    # 网页正文直通上限按真实 n_ctx 计算 (约占上下文 60%，1 token ≈ 1.35 字符)；超出则切片入 Turbovec
    web_full_context_limit = int(get_active_server_n_ctx(8192) * 0.6 * 1.35)

    mode = "full_context" if chars <= web_full_context_limit else "outline_rag"
    file_info = {
        "text": clean_text,
        "lines": total_lines,
        "chars": chars,
        "outline": outline,
        "mode": mode,
        "in_rag": False,
        "bytes": file_bytes,
        "chunks": 0,
        "source_url": url
    }
    state.active_files[virtual_filename] = file_info

    # 备份至持久化磁盘目录
    try:
        with open(storage_path(virtual_filename), "w", encoding="utf-8") as f:
            f.write(clean_text)
    except Exception:
        pass

    # 若网页篇幅较长 (超 25000 字)，立即自动分块并向量化写入 Turbovec 4-bit 长期库，彻底规避上下文爆窗报错
    if mode == "outline_rag":
        flushed_count = await asyncio.to_thread(flush_file_to_turbovec, virtual_filename, file_info)
        print(f"[网页向量入库] 🚀 超长网页《{title}》已自动转存入 Turbovec 4-bit 索引 (共 {flushed_count} 切片)，防止 12334 tokens 爆窗！", flush=True)
    else:
        # 短篇网页 0 延迟直通，同时监测动态显存缓存池
        await asyncio.to_thread(check_and_flush_buffer)

    if state.index is not None:
        await asyncio.to_thread(save_storage, state.index, state.memory_history, state.active_files)

    print(f"[网页抓取成功] ✔ 《{title}》 ({url}) 已成功导入，共 {chars} 字符，模式: {mode}", flush=True)

    return {
        "status": "ok",
        "title": title,
        "filename": virtual_filename,
        "url": url,
        "chars": chars,
        "lines": total_lines,
        "bytes": file_bytes,
        "mode": mode,
        "message": f"成功抓取并导入网页《{title}》（{chars} 字），已入库 Turbovec 记忆系统！"
    }

# ==============================================================================
# 联网检索与 Exa / MCP 智能集成引擎 (支持 Exa AI 神经搜索与免Key实时回退)
# ==============================================================================

def load_mcp_settings() -> dict:
    defaults = {
        "exa_api_key": os.environ.get("EXA_API_KEY", ""),
        "web_search_enabled": True,
        "hf_search_enabled": True,
        "auto_save_to_turbovec": False,
        "num_results": 4
    }
    if os.path.exists(MCP_SETTINGS_FILE):
        try:
            with open(MCP_SETTINGS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                defaults.update(saved)
        except Exception:
            pass
    if os.environ.get("EXA_API_KEY"):
        defaults["exa_api_key"] = os.environ.get("EXA_API_KEY", "")
    return defaults

def save_mcp_settings(settings: dict):
    try:
        with open(MCP_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  [MCP 设置异常] 无法写入设置文件: {e}", flush=True)

def free_web_search(query: str, num_results: int = 4) -> List[Dict[str, Any]]:
    import urllib.parse
    from bs4 import BeautifulSoup
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    items = []
    # 1. 优先使用 DuckDuckGo Lite (纯文本极速免反爬)
    try:
        r = requests.post("https://lite.duckduckgo.com/lite/", data={"q": query}, headers=headers, timeout=6)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            links = soup.select(".result-link")
            snippets = soup.select(".result-snippet")
            for i in range(min(len(links), len(snippets))):
                title = links[i].get_text(strip=True)
                href = links[i].get("href", "")
                snip = snippets[i].get_text(strip=True)
                if href and href.startswith("http") and "duckduckgo.com" not in href:
                    items.append({
                        "title": title,
                        "url": href,
                        "snippet": snip,
                        "published_date": "",
                        "source": "Web Search (实时网络检索)"
                    })
                if len(items) >= num_results:
                    break
    except Exception as e:
        print(f"  [免Key网络搜索 DDG Lite 异常] {e}", flush=True)

    # 2. 若未获取到结果，回退使用百度
    if not items:
        try:
            url = f"https://www.baidu.com/s?wd={urllib.parse.quote(query)}"
            r = requests.get(url, headers=headers, timeout=6)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                containers = soup.select(".result") or soup.select(".c-container")
                for c in containers:
                    h3 = c.find("h3")
                    a = h3.find("a") if h3 else None
                    p = c.select_one(".c-abstract") or c.find("p")
                    if a and a.get("href"):
                        title = a.get_text(strip=True)
                        link = a.get("href", "")
                        snip = p.get_text(strip=True) if p else ""
                        if title and link:
                            items.append({
                                "title": title,
                                "url": link,
                                "snippet": snip,
                                "published_date": "",
                                "source": "Baidu (实时网络检索)"
                            })
                    if len(items) >= num_results:
                        break
        except Exception as e:
            print(f"  [免Key网络搜索 Baidu 异常] {e}", flush=True)

    return items

def exa_neural_search(query: str, api_key: str, num_results: int = 4) -> List[Dict[str, Any]]:
    url = "https://api.exa.ai/search"
    headers = {
        "x-api-key": api_key.strip(),
        "Content-Type": "application/json"
    }
    payload = {
        "query": query,
        "type": "neural",
        "useAutoprompt": True,
        "numResults": num_results,
        "contents": {
            "text": {
                "maxCharacters": 1000
            }
        }
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=12)
    resp.raise_for_status()
    data = resp.json()
    items = []
    for item in data.get("results", []):
        raw_text = (item.get("text") or "").strip()
        clean_text = re.sub(r'\n\s*\n+', '\n', raw_text)[:800]
        items.append({
            "title": item.get("title") or "网页搜索结果",
            "url": item.get("url") or "",
            "snippet": clean_text,
            "published_date": item.get("publishedDate") or "",
            "author": item.get("author") or "",
            "source": "Exa Neural Search (AI 神经检索)"
        })
    return items

def perform_web_search(query: str, num_results: int = 4, exa_api_key: Optional[str] = None) -> List[Dict[str, Any]]:
    settings = load_mcp_settings()
    key = exa_api_key or settings.get("exa_api_key") or os.environ.get("EXA_API_KEY", "")
    items = []
    if key and key.strip():
        try:
            print(f"  [联网检索] 正在调用 Exa Neural Search AI 检索: '{query}'...", flush=True)
            items = exa_neural_search(query, key, num_results=num_results)
            print(f"  [联网检索] ✔ Exa 神经检索成功获取 {len(items)} 条高相关知识！", flush=True)
        except Exception as e:
            print(f"  [联网检索警告] Exa API 调用失败 ({e})，正在自动无缝回退至免Key实时网络检索...", flush=True)
            items = []

    if not items:
        print(f"  [联网检索] 正在执行零配置免Key实时网络检索: '{query}'...", flush=True)
        items = free_web_search(query, num_results=num_results)
        print(f"  [联网检索] ✔ 实时网络检索获取 {len(items)} 条结果！", flush=True)

    return items

def perform_huggingface_search(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """搜索 Hugging Face 官方开源大模型与数据集 (国内镜像 hf-mirror 优先，极速零拷贝请求)"""
    # 提取核心搜索词（去除多余修饰词以提高 API 检索准确率）
    clean_q = re.sub(r'(?:hugging\s*face|hf|模型|权重|数据集|开源|搜索|查询|下载|推荐|最新|找一下|帮我查|仓库)', '', query, flags=re.IGNORECASE).strip()
    search_term = clean_q if len(clean_q) >= 2 else query.strip()
    if not search_term:
        search_term = query.strip()

    endpoints = [
        "https://hf-mirror.com/api",
        "https://huggingface.co/api"
    ]
    results = []

    # 判定是否包含明确的数据集倾向
    is_dataset_query = any(w in query.lower() for w in ["dataset", "data", "数据集", "语料", "训练集"])
    target_types = ["datasets", "models"] if is_dataset_query else ["models", "datasets"]

    for base_api in endpoints:
        try:
            t_type = target_types[0]
            url = f"{base_api}/{t_type}?search={requests.utils.quote(search_term)}&limit={limit}&full=false"
            r = _sync_session.get(url, timeout=3.5)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and len(data) > 0:
                    for item in data[:limit]:
                        m_id = item.get("id") or item.get("_id")
                        if not m_id:
                            continue
                        downloads = item.get("downloads", 0)
                        likes = item.get("likes", 0)
                        pipeline = item.get("pipeline_tag", "")
                        tags = item.get("tags", [])
                        last_mod = (item.get("lastModified") or "")[:10]
                        results.append({
                            "type": "model" if t_type == "models" else "dataset",
                            "title": m_id,
                            "id": m_id,
                            "url": f"https://huggingface.co/{m_id}",
                            "downloads": downloads,
                            "likes": likes,
                            "task": pipeline,
                            "tags": tags[:6] if tags else [],
                            "last_modified": last_mod,
                            "snippet": f"类型: {'AI模型' if t_type == 'models' else '数据集'} | 任务: {pipeline or '通用语言'} | 下载: {downloads:,}次 | 获赞: {likes:,} | 标签: {', '.join(tags[:4]) if tags else '无'} | 更新: {last_mod}",
                            "source": "Hugging Face"
                        })
                    if results:
                        break
        except Exception:
            continue

    return results

def should_trigger_huggingface_search(query: str) -> bool:
    """检测用户输入是否涉及大模型、权重、开源库、数据集或 Hugging Face 资源"""
    q = query.lower()
    keywords = [
        "huggingface", "hugging face", "hf", "model", "models", "checkpoint", "checkpoints",
        "gguf", "safetensors", "lora", "dataset", "datasets", "数据集", "语料",
        "模型", "权重", "开源模型", "开源权重", "预训练", "微调", "量化模型", "embedding",
        "qwen", "llama", "deepseek", "mistral", "gemma", "phi", "baai", "bge",
        "transformers", "diffusers", "timm", "vllm", "ollama", "sglang"
    ]
    return any(kw in q for kw in keywords)

def should_trigger_web_search(query: str) -> bool:
    """检测用户输入是否需要实时联网搜索最新信息"""
    q = query.lower()
    keywords = [
        "搜索", "联网", "查一下", "最新", "今天", "新闻", "资讯", "动态",
        "search", "news", "today", "latest", "recent", "2025", "2026",
        "谁是", "官网", "官方文档", "教程", "发布", "版本", "更新了什么", "怎么样"
    ]
    return any(kw in q for kw in keywords)

def format_search_context_for_system(web_items: List[Dict[str, Any]], hf_items: List[Dict[str, Any]]) -> str:
    """将联网检索与 Hugging Face 检索结果格式化为高结构化的 System Prompt 注入内容"""
    blocks = []

    if hf_items:
        hf_lines = []
        for i, item in enumerate(hf_items, 1):
            hf_lines.append(
                f"[{i}] {item.get('title', item.get('id'))}\n"
                f"    - 类型: {'AI 模型' if item.get('type') == 'model' else '数据集'} | 任务: {item.get('task') or '通用'}\n"
                f"    - 下载量: {item.get('downloads', 0):,} | 获赞: {item.get('likes', 0):,} | 最新维护: {item.get('last_modified') or '近期'}\n"
                f"    - 标签: {', '.join(item.get('tags', []))}\n"
                f"    - 官方链接: {item.get('url')}"
            )
        blocks.append(
            "【Hugging Face 官方模型与数据集实时检索库 (Live Hugging Face Search)】:\n" +
            "\n".join(hf_lines) +
            "\n提示: 如需向用户推荐或引用模型与数据集，请附带上述官方真实 Hugging Face 链接与指标。"
        )

    if web_items:
        web_lines = []
        for i, item in enumerate(web_items, 1):
            pub = f" ({item.get('published_date')})" if item.get('published_date') else ""
            web_lines.append(
                f"[{i}] 《{item.get('title', '网页资料')}》{pub}\n"
                f"    - 来源链接: {item.get('url')}\n"
                f"    - 摘要: {item.get('snippet')}"
            )
        blocks.append(
            "【实时网络搜索参考资料 (Live Web Search)】:\n" +
            "\n".join(web_lines) +
            "\n提示: 请综合上述实时网页信息进行专业、准确的回答，引用关键事实时建议标注来源。"
        )

    return "\n\n".join(blocks)

def build_comprehensive_system_prompt(active_model: str, user_query: str = "") -> str:
    """
    统一构建内部计算端口 (Port 18088 / 18089) 的全功能与运行时配置声明 System Prompt。
    将内部计算端口的 6 大核心功能体系、实时配置、硬件状态与极速深度思考协议完整声明给模型。
    """
    mcp_cfg = load_mcp_settings()
    vram = get_gpu_vram_info()
    # 根据模型自动选择默认上下文窗口
    if "3.5" in active_model.lower():
        default_ctx = 24576   # Qwen 3.5 4B: 满血 24k
    elif "phi" in active_model.lower() or "phi-4" in active_model.lower():
        default_ctx = 16384   # Microsoft Phi-4-mini: 16k
    elif "7b" in active_model.lower():
        default_ctx = 16384   # Qwen 2.5 7B: 16k
    else:
        default_ctx = 16384   # 安全默认
    server_n_ctx = get_active_server_n_ctx(default_ctx)
    
    # 活跃工作区文件状态统计
    active_files_summary = []
    for fn, info in state.active_files.items():
        active_files_summary.append(f"{fn} ({info.get('lines', 0)}行, {info.get('mode', 'normal')})")
    files_str = ", ".join(active_files_summary) if active_files_summary else "无 (工作区就绪)"
    
    web_status = f"已启用 (召回条数: {mcp_cfg.get('num_results', 4)}, Exa Key: {'已配置' if mcp_cfg.get('exa_api_key') else '免Key高速通道'})" if mcp_cfg.get("web_search_enabled", True) else "已停用"
    hf_status = "已启用 (国内镜像: hf-mirror.com / 官方: huggingface.co, 召回上限: 5)" if mcp_cfg.get("hf_search_enabled", True) else "已停用"
    rag_auto_save = "已开启 (检索结果自动切块入库)" if mcp_cfg.get("auto_save_to_turbovec", False) else "按需手动入库"
    # 注意：此 system prompt 必须在多轮对话间保持逐字不变 (不放显存读数、记忆条数等实时数字)，
    # 这样 llama-server 才能复用上一轮已计算的 KV cache 前缀 (prompt cache)，每轮只需处理新增内容。
    vram_status = f"总显存 {vram.get('total_gb', 4.0):.0f}GB (上下文长度已在启动时按显存红线自动确定)"

    return f"""你是由本地内部计算端口 (Local Computation Port 18088 & Port 18089) 驱动的高性能多模态与系统工程 AI 助手，基于当前预设的大语言模型 [{active_model}] 深度赋能。

【内部计算端口全功能体系 (Internal Computation Port Capabilities)】
1. Turbovec 4-bit 神经向量量化记忆 (TurboQuant Vector Memory RAG)：
   - 底层集成 C++ 编译加速的 TurboQuant 4-bit 极速向量量化索引与 BGE 向量嵌入模型 (512/1024 维)。
   - 配备 Laya-typed-decisions 意图决策路由器 (System 1)：日常对话与常识问答 0ms 直通秒级生成；涉及历史事实、讨论细节与上下文回溯时，自动激活毫秒级向量语义检索。
2. Live Web Search 实时网络检索通道：
   - 内置 Exa Neural AI 神经搜索与免 Key 极速网络抓取双通道，智能识别时效性、最新科技资讯、新闻与事实性问题，杜绝大模型过时幻觉。
3. Hugging Face 官方开源生态检索 (Live Hugging Face Search)：
   - 直连国内极速镜像源 hf-mirror.com 及官方 huggingface.co API，实时检索最新开源大模型权重、GGUF/Safetensors 量化版本、微调数据集与官方链接。
4. 工作区代码与长文全景解析 (Workspace Code Intelligence)：
   - 毫秒级多语言函数与类符号大纲提取 (Python, JS/TS, C/C++, Java, Go, Rust, C#, HTML/Vue, Markdown)。
   - 支持精准行号切片（如请求 1-200 行源码），严格如实按行输出真实代码，杜绝杜撰、模板化篡改或省略。
   - PDF/OCR 文档解析通道：使用 PyMuPDF 高速提取 PDF 嵌入文本层，可选 Tesseract OCR 对扫描页进行识别。
5. 显存极限保护与动态安全上下文滑动窗口 (Dynamic Safe Context Guard)：
   - 物理显存极限保护 (-0.2GB 极限硬防护)，针对当前模型设置原生预设基线；当检测到显存仍有富余空间时，全自动动态加长上下文窗口，精准压榨至显存极限 -0.2GB (约 3.89GB)，兼备超大容量与零爆显存换页保证。
   - Compact Window 滑动压缩记忆：当会话超过 {COMPACT_TRIGGER} 轮时，自动将早期对话蒸馏为压缩摘要，以节省上下文 token 预算并维持连贯记忆。
6. 深度推理推演与回答协议 (Reasoning Protocol - 方案 1)：
   - 当激活深度思维推演时，严格按照两段式结构输出：
     [Reasoning]
     (严格使用纯英文 English 进行 3-5 点精炼逻辑推导、几何与物理约束分析，杜绝未经推敲的直觉幻觉)

     [Answer]
     (给出条理严整、清晰专业的正式解答，语言自然跟随用户的输入提问，不强制限定语种)
7. 工具调用 (Tools / MCP / Skills)：
   - 仅当本轮请求附带了工具列表时才可调用工具；有副作用的工具 (写文件、执行命令等) 需用户确认后才会执行。

【语言与思维链准则 (Language & Reasoning Directives)】
1. 思考推演过程 (Reasoning Steps)：在 [Reasoning] 结构下，一律使用【纯英文】(Reason strictly in English) 进行精准约束与逻辑推导，确保最高因果严密性与逻辑精度。
2. 正式答复呈现 (Final Answer)：在 [Answer] 结构下，给出正式解答；语言自然契合用户的输入提问语言（如用户用华文提问则用华文解答，用户用英文提问则用英文解答），不强化/不强制特定限定为某一语种。

【当前运行时配置与系统环境 (Active System Settings & Hardware Status)】
- 活跃推理模型: {active_model} (默认预设)
- 前置决策引擎: Laya-typed-decisions (System 1 意图决策校准模型)
- 物理安全上下文窗口: {server_n_ctx} Tokens
- 硬件显存监控: {vram_status}
- 实时网络搜索: {web_status}
- Hugging Face 检索: {hf_status}
- 检索自动持久化: {rag_auto_save}
- 工作区挂载状态: [{files_str}]

请严格遵守上述语言与思维链准则，以专业、严谨、条理清晰且富有洞察力的方式解答用户的问题，并在必要时充分结合上述系统工具与检索知识。"""

def save_search_results_to_turbovec(query: str, results: List[Dict[str, Any]]) -> int:
    if not results:
        return 0
    entries = []
    for i, r in enumerate(results, 1):
        title = r.get("title") or f"检索资料 {i}"
        url = r.get("url") or ""
        snippet = r.get("snippet") or ""
        pub = r.get("published_date") or ""
        source = r.get("source") or "Web Search"
        pub_line = f"- 发布时间: {pub}\n" if pub else ""
        entry = (
            f"【联网知识记忆 ({source}) - {title}】\n"
            f"- 检索关键词: {query}\n"
            f"- 来源网址: {url}\n"
            f"{pub_line}"
            f"- 内容核心摘要:\n{snippet}"
        )
        entries.append(entry)

    if not add_memory_entries(entries):
        return 0
    if state.index is not None:
        save_storage(state.index, state.memory_history, state.active_files)
    print(f"  [RAG 知识存入] ✔ 已将 {len(entries)} 条联网检索知识向量化存入 Turbovec 4-bit 长期库！", flush=True)
    return len(entries)

class McpSettingsUpdateRequest(BaseModel):
    exa_api_key: Optional[str] = None
    web_search_enabled: Optional[bool] = None
    hf_search_enabled: Optional[bool] = None
    auto_save_to_turbovec: Optional[bool] = None
    num_results: Optional[int] = None

class WebSearchRequest(BaseModel):
    query: str
    num_results: Optional[int] = 4
    save_to_rag: Optional[bool] = False

class HuggingFaceSearchRequest(BaseModel):
    query: str
    limit: Optional[int] = 5

class SaveSearchToRagRequest(BaseModel):
    query: str
    results: List[Dict[str, Any]]

@app.get("/api/mcp_settings")
async def get_mcp_settings():
    s = load_mcp_settings()
    key = s.get("exa_api_key", "")
    masked_key = ("*" * 8 + key[-4:]) if len(key) >= 12 else ("***" if key else "")
    return {
        "has_exa_key": bool(key),
        "exa_api_key_masked": masked_key,
        "web_search_enabled": s.get("web_search_enabled", True),
        "hf_search_enabled": s.get("hf_search_enabled", True),
        "auto_save_to_turbovec": s.get("auto_save_to_turbovec", False),
        "num_results": s.get("num_results", 4)
    }

@app.post("/api/mcp_settings")
async def update_mcp_settings(req: McpSettingsUpdateRequest):
    s = load_mcp_settings()
    if req.exa_api_key is not None:
        s["exa_api_key"] = req.exa_api_key.strip()
        if req.exa_api_key.strip():
            os.environ["EXA_API_KEY"] = req.exa_api_key.strip()
    if req.web_search_enabled is not None:
        s["web_search_enabled"] = req.web_search_enabled
    if req.hf_search_enabled is not None:
        s["hf_search_enabled"] = req.hf_search_enabled
    if req.auto_save_to_turbovec is not None:
        s["auto_save_to_turbovec"] = req.auto_save_to_turbovec
    if req.num_results is not None:
        s["num_results"] = max(1, min(10, req.num_results))
    save_mcp_settings(s)
    return {"status": "ok", "message": "联网搜索与 Hugging Face 搜索设置已成功保存并立即生效"}

@app.post("/api/huggingface_search")
async def api_huggingface_search(req: HuggingFaceSearchRequest):
    q = req.query.strip()
    if not q:
        raise HTTPException(status_code=400, detail="搜索关键词不能为空")
    results = await asyncio.to_thread(perform_huggingface_search, q, req.limit or 5)
    return {"results": results, "count": len(results)}

@app.post("/api/web_search")
async def api_web_search(req: WebSearchRequest):
    q = req.query.strip()
    if not q:
        raise HTTPException(status_code=400, detail="搜索关键词不能为空")
    results = await asyncio.to_thread(perform_web_search, q, req.num_results or 4)
    saved_count = 0
    if req.save_to_rag and results:
        saved_count = await asyncio.to_thread(save_search_results_to_turbovec, q, results)
    return {
        "status": "ok",
        "query": q,
        "count": len(results),
        "results": results,
        "saved_to_turbovec": (saved_count > 0),
        "total_memory_count": len(state.memory_history)
    }

@app.post("/api/save_search_to_rag")
async def api_save_search_to_rag(req: SaveSearchToRagRequest):
    q = req.query.strip()
    if not req.results:
        raise HTTPException(status_code=400, detail="没有可存入的检索结果")
    saved_count = await asyncio.to_thread(save_search_results_to_turbovec, q, req.results)
    return {
        "status": "ok",
        "saved_count": saved_count,
        "total_memory_count": len(state.memory_history),
        "message": f"成功将 {saved_count} 条联网知识切块并存入 Turbovec 4-bit 长期记忆库！"
    }

OUTLINE_CHAR_CAP = 4000  # 单个文件符号大纲最多装入的字符数，防止大量文件时大纲本身撑爆上下文

def resolve_range_target(user_input: str) -> Optional[str]:
    """行号范围请求只作用于用户点名的文件；只挂载了 1 个文件时默认就是它"""
    low = user_input.lower()
    for fn in state.active_files.keys():
        if fn.lower() in low:
            return fn
    if len(state.active_files) == 1:
        return next(iter(state.active_files))
    return None

def build_file_context_blocks(user_input: str, char_budget: int, outline_cap: int = OUTLINE_CHAR_CAP) -> Tuple[List[str], int]:
    """按共享字符预算构造工作区文件上下文块，返回 (blocks, 实际使用的正文字符数)。
    - 请求了行号范围：装入切片 + 符号大纲
    - 否则：预算内装全文；预算不足时装入前 N 行 + 符号大纲；预算耗尽只给大纲
    """
    blocks: List[str] = []
    remaining = max(0, int(char_budget))
    used = 0
    range_target = resolve_range_target(user_input)
    for fn, info in list(state.active_files.items()):
        text = get_file_content(fn)
        all_lines = text.split("\n")
        total_lines = info.get("lines", len(all_lines))
        chars = info.get("chars", len(text))
        outline = info.get('outline', '') or ''
        if len(outline) > outline_cap:
            outline = outline[:outline_cap] + "\n...[大纲过长已截断]"
        outline_block = (
            f"<file_symbol_outline name=\"{fn}\" total_lines=\"{total_lines}\" mode=\"complete_symbol_index\">\n"
            f"{outline}\n"
            f"</file_symbol_outline>"
        )
        range_tuple = detect_requested_line_range(user_input, total_lines) if fn == range_target else None
        if range_tuple:
            start_l, end_l = range_tuple
            slice_lines, cur = [], 0
            for i, line in enumerate(all_lines[start_l - 1: end_l], start_l):
                row = f"{i}: {line}"
                if cur + len(row) + 1 > remaining:
                    break  # 行号切片同样受共享预算约束，超出部分提示用户分段查看
                slice_lines.append(row)
                cur += len(row) + 1
            end_l = start_l + len(slice_lines) - 1 if slice_lines else start_l - 1
            numbered_text = "\n".join(slice_lines)
            blocks.append(
                f"<requested_line_slice file=\"{fn}\" start_line=\"{start_l}\" end_line=\"{end_l}\" count=\"{len(slice_lines)}\">\n"
                f"【真实源代码第 {start_l} 行至第 {end_l} 行（共 {len(slice_lines)} 行真实代码，严禁虚构杜撰或省略）：】\n"
                f"{numbered_text}\n"
                f"</requested_line_slice>"
            )
            blocks.append(outline_block)
            used += len(numbered_text)
            remaining = max(0, remaining - len(numbered_text))
            continue
        if chars <= remaining:
            blocks.append(
                f"<active_workspace_file name=\"{fn}\" total_lines=\"{total_lines}\" mode=\"100%_full_visibility\">\n"
                f"{text}\n"
                f"</active_workspace_file>"
            )
            used += chars
            remaining -= chars
            continue
        kept_lines, cur_len = [], 0
        for line in all_lines:
            if cur_len + len(line) + 1 > remaining:
                break
            kept_lines.append(line)
            cur_len += len(line) + 1
        if kept_lines:
            kept_line_count = len(kept_lines)
            blocks.append(
                f"<active_workspace_file name=\"{fn}\" total_lines=\"{total_lines}\" mode=\"safe_window_slice\" shown_lines=\"1-{kept_line_count}\">\n"
                f"{chr(10).join(kept_lines).rstrip()}\n\n"
                f"【系统自适应窗口提示】：本文件总长 {total_lines} 行（约 {chars} 字符），受当前上下文预算限制仅装载前 {kept_line_count} 行真实源码与全量结构大纲。如需查询后续代码，请输入行号范围（如 '{kept_line_count + 1}-{min(total_lines, kept_line_count + 200)}行'）！\n"
                f"</active_workspace_file>"
            )
            used += cur_len
            remaining = max(0, remaining - cur_len)
        blocks.append(outline_block)
    return blocks, used

def retrieve_ranked_memories(user_input: str, k_val: int) -> List[str]:
    """两阶段混合检索：向量粗排 (2x 候选) + 词法关键词加权精排 (同步函数，在线程中执行)"""
    if k_val <= 0 or state.embedder is None:
        return []
    query_vec = get_embeddings([user_input], state.embedder)
    hits = search_memory(query_vec, k_val * 2)
    query_terms = [t for t in re.findall(r'[\w\.\-]+', user_input.lower()) if len(t) >= 2]
    scored = []
    for score, mem in hits:
        mem_lower = mem.lower()
        lexical_boost = sum(0.12 for term in query_terms if term in mem_lower)
        scored.append((score + lexical_boost, mem))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [mem for _, mem in scored[:k_val]]

@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    user_input = req.message.strip()
    if not user_input:
        raise HTTPException(status_code=400, detail="消息内容不能为空")

    t0 = time.time()
    
    # 联网检索与 Hugging Face 智能检索判定
    mcp_cfg = load_mcp_settings()
    do_web_search = bool(req.web_search) or (req.web_search is None and mcp_cfg.get("web_search_enabled", True) and should_trigger_web_search(user_input))
    do_hf_search = mcp_cfg.get("hf_search_enabled", True) and should_trigger_huggingface_search(user_input)

    web_search_items = []
    hf_search_items = []

    if do_web_search or do_hf_search:
        try:
            tasks = []
            if do_web_search:
                tasks.append(("web", asyncio.to_thread(perform_web_search, user_input, mcp_cfg.get("num_results", 4))))
            if do_hf_search:
                tasks.append(("hf", asyncio.to_thread(perform_huggingface_search, user_input, 4)))
            if tasks:
                search_res = await asyncio.gather(*(t[1] for t in tasks), return_exceptions=True)
                for (tag, _), res in zip(tasks, search_res):
                    if isinstance(res, list):
                        if tag == "web":
                            web_search_items = res
                        elif tag == "hf":
                            hf_search_items = res

            if (web_search_items or hf_search_items) and mcp_cfg.get("auto_save_to_turbovec", False):
                all_to_save = web_search_items + hf_search_items
                asyncio.create_task(asyncio.to_thread(save_search_results_to_turbovec, user_input, all_to_save))
        except Exception as e:
            print(f"  [知识检索执行异常] {e}", flush=True)

    # 检测是否为全局性汇总问题（如“列出全部”、“所有函数”、“架构大纲”、“全部代码”等）
    is_global_query = any(kw in user_input.lower() for kw in [
        "全部", "所有", "清单", "大纲", "概览", "整体", "结构", "架构", "总结", "全貌", "list all",
        "list out", "all functions", "outline", "overview", "summary", "structure", "architecture",
        "全部函数", "全部类", "所有定义", "逐一列出", "列出", "都写出来", "不省略", "全部内容"
    ])

    active_model = req.model or getattr(state, "current_model", LLM_MODEL) or LLM_MODEL
    if active_model != getattr(state, "current_model", None):
        state.current_model = active_model
        await asyncio.to_thread(unload_other_runner_models, active_model)

    # 动态探测大模型后端真实物理最大上下文窗口 (自动感知 16k, 32k, 64k 等)
    # 真实 n_ctx 由后台监控线程从 /slots 探测；未知时用保守的 8192，宁可少装也不溢出
    server_n_ctx = get_active_server_n_ctx(8192)
    # 保留充足的输出与深度思考 Tokens (最高 4096，最低 2048)
    reserved_output_tokens = min(4096, max(2048, int(server_n_ctx * 0.12)))
    # 留出 256 tokens 绝对安全余量，计算单次输入 Prompt 硬上限
    max_prompt_token_budget = max(4096, server_n_ctx - reserved_output_tokens - 256)

    # llama-server 启动时已按 -c 预分配全部 KV 显存，prompt 长短不会改变显存占用，
    # 因此文件装载预算只取决于真实 n_ctx (1 token ≈ 1.35 字符)，不再按“已用显存”收紧
    safe_char_limit = int(max_prompt_token_budget * 1.35)

    # 优先检查是否为直接索取/提取特定行号代码意图（如 "0-200", "拿取0-200行内容", "前200行", "200-500" 等）
    is_fetch_intent = any(kw in user_input.lower() for kw in [
        "拿取", "提取", "获取", "列出", "查看", "显示", "输出", "读", "展示", "给我", "打印",
        "get", "fetch", "show", "display", "print", "read", "view", "list"
    ]) or bool(re.match(r'^(?:第\s*)?\d+\s*[-~到至–]\s*\d+\s*行?$', user_input.strip().lower())) or bool(re.match(r'^(?:前|首)\s*\d+\s*行?$', user_input.strip().lower()))

    is_analysis_intent = any(kw in user_input.lower() for kw in [
        "解释", "分析", "优化", "重构", "为什么", "怎么改", "总结", "改写", "说明", "找bug", "问题",
        "explain", "analyze", "why", "review", "bug", "optimize", "refactor"
    ])

    # 如果存在活跃文件且用户明确索要特定行号的源码切片
    if state.active_files and (is_fetch_intent or not is_analysis_intent):
        target_fn = None
        for fn in state.active_files.keys():
            if fn.lower() in user_input.lower():
                target_fn = fn
                break
        if not target_fn and len(state.active_files) == 1:
            target_fn = list(state.active_files.keys())[0]

        if target_fn:
            info = state.active_files[target_fn]
            text = get_file_content(target_fn)
            all_lines = text.split("\n")
            total_lines = info.get("lines", len(all_lines))
            range_tuple = detect_requested_line_range(user_input, total_lines)
            
            if range_tuple:
                start_l, end_l = range_tuple
                slice_lines = all_lines[start_l - 1 : end_l]
                ext = os.path.splitext(target_fn)[1].lower().replace('.', '') or 'text'
                numbered_code = "\n".join([f"{i:4d}  {line}" for i, line in enumerate(slice_lines, start_l)])
                direct_ans = (
                    f"根据您的要求，以下是文件 `{target_fn}` 的第 {start_l} 到 {end_l} 行真实源代码（共 {len(slice_lines)} 行）：\n\n"
                    f"```{ext}\n{numbered_code}\n```\n\n"
                    f"💡 提示：文件共 {total_lines} 行。如需继续查看下一段代码（如第 {end_l + 1} 到 {min(total_lines, end_l + 200)} 行），请随时输入！"
                )

                state.session_turns.append({"user": user_input, "assistant": direct_ans})
                current_buffer_mb = round(get_current_buffer_bytes() / (1024 * 1024), 2)
                dynamic_max_gb = get_dynamic_max_buffer_gb()

                slice_tok = count_text_tokens(direct_ans)
                prompt_tok_est = count_text_tokens(user_input) + 60
                if req.stream:
                    async def direct_stream_generator():
                        chunk_size = 80
                        cur_tok = 0
                        for i in range(0, len(direct_ans), chunk_size):
                            chunk_text = direct_ans[i : i + chunk_size]
                            cur_tok += count_text_tokens(chunk_text)
                            ctx_stats = {
                                "phase": "generation",
                                "prompt_tokens": prompt_tok_est,
                                "reasoning_tokens": 0,
                                "content_tokens": cur_tok,
                                "total_context_tokens": prompt_tok_est + cur_tok,
                                "server_n_ctx": server_n_ctx
                            }
                            yield f"data: {json.dumps({'token': chunk_text, 'ctx_stats': ctx_stats}, ensure_ascii=False)}\n\n"
                            await asyncio.sleep(0.004)
                        yield f"data: {json.dumps({'done': True, 'stats': {'memory_count': len(state.memory_history), 'session_turns': len(state.session_turns), 'buffer_mb': current_buffer_mb, 'max_buffer_gb': dynamic_max_gb, 'active_model': active_model, 'token_count': slice_tok, 'prompt_tokens': prompt_tok_est, 'reasoning_tokens': 0, 'content_tokens': slice_tok, 'total_context_tokens': prompt_tok_est + slice_tok, 'server_n_ctx': server_n_ctx}}, ensure_ascii=False)}\n\n"

                    return StreamingResponse(direct_stream_generator(), media_type="text/event-stream")
                else:
                    return {
                        "answer": direct_ans,
                        "stats": {
                            "memory_count": len(state.memory_history),
                            "session_turns": len(state.session_turns),
                            "active_files_count": len(state.active_files),
                            "buffer_mb": current_buffer_mb,
                            "max_buffer_gb": dynamic_max_gb,
                            "active_model": active_model,
                            "token_count": slice_tok,
                            "prompt_tokens": prompt_tok_est,
                            "reasoning_tokens": 0,
                            "content_tokens": slice_tok,
                            "total_context_tokens": prompt_tok_est + slice_tok,
                            "server_n_ctx": server_n_ctx
                        }
                    }

    # --- 1. 构造活跃文件上下文：所有文件共享同一个字符预算 (不再每个文件各自占满预算) ---
    has_range_slice = any(
        detect_requested_line_range(user_input, info.get("lines", 0) or 1) for info in state.active_files.values()
    )

    # --- 2. Laya / Jev 决策层 (零延迟直通加速) ---
    t_laya_start = time.time()
    retrieved_memories = []
    search_ms = 0.0

    has_history = (len(state.memory_history) > 0 or len(state.session_turns) > 0 or len(state.active_files) > 0)
    # 极速直通：若当前既无工作区文件也无历史记忆，完全跳过 CPU 向量运算 (0ms)
    if not state.active_files and not state.memory_history and not is_global_query:
        decision = "direct_generation"
        confidence = 1.0
        laya_ms = 0.0
    else:
        decision, confidence = await asyncio.to_thread(
            route_decision,
            user_input,
            state.laya_agent,
            has_history,
            state.embedder,
            state.direct_vecs,
            state.memory_vecs,
        )
        laya_ms = (time.time() - t_laya_start) * 1000

    if decision == "direct_generation" and not is_global_query and not state.active_files and not has_range_slice:
        print(f"[Laya 路由 -> 直接生成] 无需调取旧记忆 (置信度: {confidence:.2f})", flush=True)
    else:
        t_search_start = time.time()
        k_val = min(12 if is_global_query else 5, len(state.memory_history))
        if k_val > 0:
            print(f"[Laya 路由 -> 搜索内容] 正在检索 Turbovec 记忆 (置信度: {confidence:.2f}, k={k_val}, 全局模式={is_global_query})...", flush=True)
            
            if is_global_query:
                for item in state.memory_history:
                    if "【文件全局大纲" in item or "【文件全局概览" in item:
                        if item not in retrieved_memories:
                            retrieved_memories.append(item)

            if len(state.memory_history) > 0 and state.index is not None:
                ranked = await asyncio.to_thread(retrieve_ranked_memories, user_input, k_val)
                for mem in ranked:
                    if mem not in retrieved_memories:
                        retrieved_memories.append(mem)

        search_ms = (time.time() - t_search_start) * 1000

    # --- 2.5 知识图谱 (Graph RAG)：问题中提到的实体 + 名称向量近邻 → 1~2 跳关系，作为每轮参考资料 ---
    kg_block = ""
    try:
        kg_block = await asyncio.to_thread(kg_service.build_block, user_input, KG_BLOCK_MAX_TOKENS)
    except Exception as e:
        print(f"  [知识图谱] ⚠ 检索失败: {e}", flush=True)

    # --- 3. 组装 Prompt 调用本地模型 (注入内部计算端口全功能体系与运行时配置) ---
    base_system_content = build_comprehensive_system_prompt(active_model, user_input)
    # 系统提示词预设 (append: 追加在内置能力声明之后；replace: 替换内置能力声明，文件/记忆/检索上下文块仍保留)
    base_system_content = agent_service.apply_system_prompt(base_system_content, req.prompt_id)

    # Agent 工具 (内置 + MCP + Skills)：仅 tools_enabled 时启用 (docs/AGENT_API.md §3)
    agent_entries, agent_tools = [], []
    if req.tools_enabled:
        try:
            agent_entries, agent_tools = await agent_service.prepare_tools()
        except Exception as e:
            print(f"  [Agent] ⚠ 构建工具列表失败: {e}", flush=True)
        if agent_tools:
            base_system_content += "\n\n" + agent_service.tools_system_block()
    agent_tools_tokens = count_text_tokens(json.dumps(agent_tools, ensure_ascii=False)) if agent_tools else 0

    is_coding_request = any(kw in user_input.lower() for kw in [
        "写", "编写", "实现", "代码", "工程", "系统", "项目", "模块", "开发", "code", "implement", "build", "create", "write"
    ])
    coding_rules = (
        "\n\n【代码与工程生成准则】\n"
        "1. 编写代码或系统时，提供完整且高质量的代码实现，包含必要的类型提示与业务逻辑，避免空洞的省略。\n"
        "2. 若单次生成受上下文限制未完，请在末尾提示用户回复'继续'即可无缝生成后续模块。"
    ) if is_coding_request else ""

    # 动态逻辑思考检测与 方案 1 (Reasoning / Answer) 协议注入
    # 核心准则：Phi-4 需要开启 thinking 才能准确推演、杜绝未经分析的直觉幻觉！
    is_phi = "phi" in active_model.lower()
    if req.thinking is not None:
        enable_thinking = req.thinking
    elif is_phi:
        enable_thinking = True  # Phi-4 默认保持开启 thinking 以获得最高精度
    else:
        enable_thinking = is_logic_or_reasoning_query(user_input)

    reasoning_rules = (
        "\n\n[Reasoning Protocol: 方案 1 (Reasoning & Answer Structure)]\n"
        "You must organize your entire response strictly into two distinct sections:\n"
        "[Reasoning]\n"
        "1. Strictly in English, write concise bullet points analyzing constraints, physical/spatial geometry, logic rules, and step-by-step causal mechanisms.\n"
        "2. Verify all edge cases and state transitions to eliminate intuitive fallacies or trick-question errors.\n\n"
        "[Answer]\n"
        "Present your final, direct, and well-structured answer. Match the language of the user question naturally without rigid language restrictions."
    ) if enable_thinking else ""

    search_context_str = format_search_context_for_system(web_search_items, hf_search_items)

    # 滑动窗口压缩：超过保留轮数时将旧轮次蒸馏为 compact_summary (无“既不在窗口也不在摘要”的空档)
    apply_compact_window()

    # 智能修剪历史回合，防止超长历史代码切片撑爆显存上下文
    def sanitize_history_text(txt: str, limit: int = 500) -> str:
        if not txt or len(txt) <= limit:
            return txt
        return txt[:limit] + f"\n... [已折叠后续 {len(txt) - limit} 字符历史输出] ..."

    def compose_messages(files_budget: int, mem_limit: int, turns_keep: int, outline_cap: int = OUTLINE_CHAR_CAP,
                         kg_on: bool = True):
        blocks, files_used = build_file_context_blocks(user_input, files_budget, outline_cap)
        sc = base_system_content
        if blocks:
            sc += (
                f"\n\n【用户上传的当前工作区文件】\n"
                f"{chr(10).join(blocks)}\n\n"
                f"【文件处理规范】\n"
                f"1. 当上下文包含 <requested_line_slice> 时，严格逐行如实输出真实源码（保留行号），严禁杜撰、省略或使用通用模板！\n"
                f"2. 标签 <active_workspace_file> 内为真实全文源码，回答必须基于此源码。\n"
                f"3. 标签 <file_symbol_outline> 内为超长文件提取的全量符号大纲。\n"
            )
        # 每轮都会变化的内容 (记忆检索、联网结果、按问题触发的规则) 放进最后一条 user 消息，
        # 让 system + 历史轮次成为稳定前缀，llama-server 可直接复用其 KV cache
        per_turn = ""
        if kg_block and kg_on:
            per_turn += f"\n\n{kg_block}"
        if retrieved_memories and mem_limit > 0:
            memory_text = "\n".join(f"- {m}" for m in retrieved_memories[:mem_limit])
            per_turn += f"\n\n【Turbovec 长期记忆与历史检索切片】\n{memory_text}"
        if search_context_str:
            per_turn += f"\n\n{search_context_str}"
        per_turn += coding_rules + reasoning_rules
        msgs = [{"role": "system", "content": sc}]
        # Compact Window 历史摘要作为 assistant 消息注入
        if state.compact_summary:
            msgs.append({"role": "assistant", "content": "[以下为更早期对话的自动压缩摘要，供参考背景]\n" + state.compact_summary})
        recent = state.session_turns[-turns_keep:] if turns_keep > 0 else []
        for t in recent:
            msgs.append({"role": "user", "content": sanitize_history_text(t["user"], 400)})
            msgs.append({"role": "assistant", "content": sanitize_history_text(t["assistant"], 600)})
        if per_turn.strip():
            msgs.append({"role": "user", "content": f"【本轮参考资料与要求】{per_turn}\n\n【用户问题】\n{user_input}"})
        else:
            msgs.append({"role": "user", "content": user_input})
        return msgs, files_used

    def count_messages_tokens(msgs) -> int:
        return count_text_tokens("\n".join(m.get("content", "") for m in msgs))

    # 物理防爆窗防线：超出上限时按优先级逐步裁剪 —— 先缩文件正文，再丢知识图谱块、减记忆切片，最后减历史轮次；
    # 系统协议、检索结果与用户问题本身永远保留
    hard_prompt_limit = server_n_ctx - reserved_output_tokens - 128
    if agent_tools:
        # 工具 schema 占用 prompt；另为工具返回结果预留约 20% (≤4096) 的空间
        hard_prompt_limit -= agent_tools_tokens + min(4096, int(server_n_ctx * 0.2))
        hard_prompt_limit = max(1024, hard_prompt_limit)
    files_budget, mem_limit, turns_keep, outline_cap = safe_char_limit, 6, COMPACT_WINDOW_KEEP, OUTLINE_CHAR_CAP
    kg_on = bool(kg_block)
    messages, files_used = compose_messages(files_budget, mem_limit, turns_keep, outline_cap, kg_on)
    prompt_tokens = count_messages_tokens(messages)
    for _ in range(16):
        if prompt_tokens <= hard_prompt_limit:
            break
        excess_chars = int((prompt_tokens - hard_prompt_limit) * 1.6) + 200
        if files_used > 0:
            files_budget = max(0, files_used - excess_chars)      # 1. 文件正文
        elif state.active_files and outline_cap > 0:
            outline_cap = 0 if outline_cap <= 500 else outline_cap // 4  # 2. 符号大纲
        elif kg_on:
            kg_on = False                                          # 3. 知识图谱块 (先于记忆切片丢弃)
        elif mem_limit > 0:
            mem_limit = max(0, mem_limit - 2)                      # 4. 记忆切片
        elif turns_keep > 0:
            turns_keep -= 1                                        # 5. 历史轮次
        else:
            break
        messages, files_used = compose_messages(files_budget, mem_limit, turns_keep, outline_cap, kg_on)
        prompt_tokens = count_messages_tokens(messages)
    if prompt_tokens > hard_prompt_limit:
        print(f"[上下文预算] ⚠ 裁剪后 prompt 仍约 {prompt_tokens} tokens，超过上限 {hard_prompt_limit}", flush=True)

    # 留出 32 tokens 物理绝对缓冲，其余预算全部开放给输出与深度思维链推演，上限放宽至 16384 tokens
    rem_budget = max(128, server_n_ctx - prompt_tokens - 32)
    safe_max_tokens = min(16384, rem_budget)

    if req.stream:
        reasoning_chunks: List[str] = []  # 本轮思考全文 (只用于生成要点，不进入历史)

        async def event_generator():
            full_chunks = []
            turn_saved = False

            def _persist_turn_once():
                # 正常结束、出错、客户端断开 (Stop) 都只保存一次；空回答不保存
                nonlocal turn_saved
                if turn_saved:
                    return
                turn_saved = True
                answer_txt, r_summary = prepare_turn("".join(full_chunks), "".join(reasoning_chunks))
                if answer_txt.strip():
                    state.session_turns.append(make_turn(user_input, answer_txt, r_summary))
                    apply_compact_window()
                    threading.Thread(target=save_turn_to_memory, args=(user_input, answer_txt, r_summary), name="tv-save-turn", daemon=True).start()

            body = _agent_event_body if agent_tools else _event_body
            try:
                async for chunk in body(full_chunks, _persist_turn_once):
                    yield chunk
            finally:
                _persist_turn_once()

        async def _agent_event_body(full_chunks, persist_turn):
            """工具调用循环：tool_call / tool_result / notice 事件 + 与普通模式相同的 token / done 事件"""
            from agent import ChatLoop, encode_event
            t_gen_start = time.time()
            all_search_items = web_search_items + hf_search_items
            if all_search_items:
                yield f"data: {json.dumps({'search_results': all_search_items}, ensure_ascii=False)}\n\n"
            loop_runner = ChatLoop(
                agent_service,
                client=get_async_client(),
                url=f"{get_active_llm_base()}/chat/completions",
                headers={"Content-Type": "application/json"},
                payload_base={
                    "model": active_model,
                    "temperature": 0.7,
                    "cache_prompt": True,
                    "stop": ["\n【对话记录】", "【对话记录】", "\n用户:", "\nUser:", "\nHuman:"],
                    "presence_penalty": 0.2,
                },
                messages=[dict(m) for m in messages],
                entries=agent_entries,
                openai_tools=agent_tools,
                n_ctx=server_n_ctx,
                reserved_output=reserved_output_tokens,
                max_tokens_cap=safe_max_tokens,
                approval_mode=req.approval_mode,
                max_rounds=agent_service.config().get("max_tool_rounds", 6),
                full_chunks=full_chunks,
                count_tokens=count_text_tokens,
            )
            try:
                async for ev in loop_runner.run():
                    yield encode_event(ev)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
            st = loop_runner.stats
            answer = "".join(full_chunks)
            persist_turn()
            elapsed_sec = round(time.time() - t_gen_start, 2)
            tok_per_sec = round(st.stream_tokens / elapsed_sec, 1) if elapsed_sec > 0 else 0
            char_count = len(answer)
            char_per_sec = round(char_count / elapsed_sec, 1) if elapsed_sec > 0 else 0
            done_stats = {
                'elapsed_sec': elapsed_sec, 'tokens_per_sec': tok_per_sec, 'token_count': st.stream_tokens,
                'reasoning_tokens': st.reasoning_tokens, 'content_tokens': st.content_tokens,
                'prompt_tokens': st.prompt_tokens, 'total_context_tokens': st.prompt_tokens + st.stream_tokens,
                'server_n_ctx': server_n_ctx, 'char_count': char_count, 'chars_per_sec': char_per_sec,
                'memory_count': len(state.memory_history), 'session_turns': len(state.session_turns),
                'buffer_mb': round(get_current_buffer_bytes() / (1024 * 1024), 2), 'max_buffer_gb': get_dynamic_max_buffer_gb(),
                'active_model': active_model, 'is_thinking': enable_thinking,
                'web_search_count': len(web_search_items) + len(hf_search_items),
                'tool_calls': st.tool_calls,
            }
            yield f"data: {json.dumps({'done': True, 'stats': done_stats}, ensure_ascii=False)}\n\n"

        async def _event_body(full_chunks, persist_turn):
            in_reasoning = False
            t_gen_start = time.time()
            stream_tokens_count = 0
            reasoning_tokens_count = 0
            content_tokens_count = 0
            all_search_items = web_search_items + hf_search_items
            if all_search_items:
                yield f"data: {json.dumps({'search_results': all_search_items}, ensure_ascii=False)}\n\n"
            try:
                client = get_async_client()
                active_base = get_active_llm_base()
                url = f"{active_base}/chat/completions"
                headers = {"Content-Type": "application/json"}
                payload = {
                    "model": active_model,
                    "messages": messages,
                    "temperature": 0.7,
                    "max_tokens": safe_max_tokens,
                    "stream": True,
                    "cache_prompt": True,  # 复用与上一轮相同前缀的 KV cache
                    "stop": ["\n【对话记录】", "【对话记录】", "\n用户:", "\nUser:", "\nHuman:"],
                    "presence_penalty": 0.2
                }
                if req.tools:
                    payload["tools"] = req.tools
                if req.tool_choice:
                    payload["tool_choice"] = req.tool_choice
                async with client.stream("POST", url, headers=headers, json=payload, timeout=180.0) as resp:
                    if resp.status_code != 200:
                        err_bytes = await resp.aread()
                        err_msg = err_bytes.decode('utf-8', errors='ignore')
                        yield f"data: {json.dumps({'error': f'大模型接口异常 ({resp.status_code}): {err_msg}'}, ensure_ascii=False)}\n\n"
                        return

                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        if line.startswith("data: "):
                            raw_data = line[6:].strip()
                            if raw_data == "[DONE]":
                                break
                            try:
                                chunk = json.loads(raw_data)
                                delta = chunk['choices'][0].get('delta', {})
                                reasoning_token = delta.get('reasoning_content', '')
                                content_token = delta.get('content', '')
                                tool_calls = delta.get('tool_calls', None)

                                if reasoning_token:
                                    reasoning_chunks.append(reasoning_token)
                                    stream_tokens_count += 1
                                    reasoning_tokens_count += 1
                                    ctx_stats = {
                                        "phase": "reasoning",
                                        "prompt_tokens": prompt_tokens,
                                        "reasoning_tokens": reasoning_tokens_count,
                                        "content_tokens": content_tokens_count,
                                        "total_context_tokens": prompt_tokens + stream_tokens_count,
                                        "server_n_ctx": server_n_ctx
                                    }
                                    if not in_reasoning:
                                        in_reasoning = True
                                        payload_str = json.dumps({"token": "[Reasoning]\n" + reasoning_token, "ctx_stats": ctx_stats}, ensure_ascii=False)
                                        yield f"data: {payload_str}\n\n"
                                    else:
                                        payload_str = json.dumps({"token": reasoning_token, "ctx_stats": ctx_stats}, ensure_ascii=False)
                                        yield f"data: {payload_str}\n\n"

                                if tool_calls:
                                    ctx_stats = {
                                        "phase": "tool_calling",
                                        "prompt_tokens": prompt_tokens,
                                        "reasoning_tokens": reasoning_tokens_count,
                                        "content_tokens": content_tokens_count,
                                        "total_context_tokens": prompt_tokens + stream_tokens_count,
                                        "server_n_ctx": server_n_ctx
                                    }
                                    payload_str = json.dumps({"tool_calls": tool_calls, "ctx_stats": ctx_stats}, ensure_ascii=False)
                                    yield f"data: {payload_str}\n\n"

                                if content_token:
                                    stream_tokens_count += 1
                                    content_tokens_count += 1
                                    ctx_stats = {
                                        "phase": "generation",
                                        "prompt_tokens": prompt_tokens,
                                        "reasoning_tokens": reasoning_tokens_count,
                                        "content_tokens": content_tokens_count,
                                        "total_context_tokens": prompt_tokens + stream_tokens_count,
                                        "server_n_ctx": server_n_ctx
                                    }
                                    if in_reasoning:
                                        in_reasoning = False
                                        payload_str = json.dumps({"token": "\n\n[Answer]\n" + content_token, "ctx_stats": ctx_stats}, ensure_ascii=False)
                                        yield f"data: {payload_str}\n\n"
                                    else:
                                        payload_str = json.dumps({"token": content_token, "ctx_stats": ctx_stats}, ensure_ascii=False)
                                        yield f"data: {payload_str}\n\n"
                                    full_chunks.append(content_token)
                            except Exception:
                                continue
                if in_reasoning:
                    in_reasoning = False
                    ctx_stats = {
                        "phase": "generation",
                        "prompt_tokens": prompt_tokens,
                        "reasoning_tokens": reasoning_tokens_count,
                        "content_tokens": content_tokens_count,
                        "total_context_tokens": prompt_tokens + stream_tokens_count,
                        "server_n_ctx": server_n_ctx
                    }
                    payload_str = json.dumps({"token": "\n\n[Answer]\n", "ctx_stats": ctx_stats}, ensure_ascii=False)
                    yield f"data: {payload_str}\n\n"
            except Exception as e:
                err_payload = json.dumps({"error": str(e)}, ensure_ascii=False)
                yield f"data: {err_payload}\n\n"

            answer = "".join(full_chunks)
            persist_turn()  # 在发送 done 统计前保存本轮，使 session_turns 计数准确
            current_buffer_mb = round(get_current_buffer_bytes() / (1024 * 1024), 2)
            dynamic_max_gb = get_dynamic_max_buffer_gb()
            elapsed_sec = round(time.time() - t_gen_start, 2)
            tok_per_sec = round(stream_tokens_count / elapsed_sec, 1) if elapsed_sec > 0 else 0
            char_count = len(answer)
            char_per_sec = round(char_count / elapsed_sec, 1) if elapsed_sec > 0 else 0

            yield f"data: {json.dumps({'done': True, 'stats': {'elapsed_sec': elapsed_sec, 'tokens_per_sec': tok_per_sec, 'token_count': stream_tokens_count, 'reasoning_tokens': reasoning_tokens_count, 'content_tokens': content_tokens_count, 'prompt_tokens': prompt_tokens, 'total_context_tokens': prompt_tokens + stream_tokens_count, 'server_n_ctx': server_n_ctx, 'char_count': char_count, 'chars_per_sec': char_per_sec, 'memory_count': len(state.memory_history), 'session_turns': len(state.session_turns), 'buffer_mb': current_buffer_mb, 'max_buffer_gb': dynamic_max_gb, 'active_model': active_model, 'is_thinking': enable_thinking, 'web_search_count': len(web_search_items) + len(hf_search_items), 'tool_calls': 0}}, ensure_ascii=False)}\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    t_llm_start = time.time()
    agent_extra: Dict[str, Any] = {}
    if agent_tools:
        # 非流式 + 工具：只自动执行无需批准的工具 (无法在非流式响应中等待用户批准)
        from agent import ChatLoop
        loop_runner = ChatLoop(
            agent_service, client=get_async_client(), url=f"{get_active_llm_base()}/chat/completions",
            headers={"Content-Type": "application/json"},
            payload_base={"model": active_model, "temperature": 0.7, "presence_penalty": 0.2,
                          "stop": ["\n【对话记录】", "【对话记录】", "\n用户:", "\nUser:", "\nHuman:"]},
            messages=[dict(m) for m in messages], entries=agent_entries, openai_tools=agent_tools,
            n_ctx=server_n_ctx, reserved_output=reserved_output_tokens, max_tokens_cap=safe_max_tokens,
            approval_mode=req.approval_mode, max_rounds=agent_service.config().get("max_tool_rounds", 6),
            interactive=False, count_tokens=count_text_tokens)
        answer_parts, errors = [], []
        async for ev in loop_runner.run():
            if "token" in ev:
                answer_parts.append(ev["token"])
            elif "error" in ev:
                errors.append(ev["error"])
        answer = "".join(answer_parts)
        if not answer and errors:
            raise HTTPException(status_code=502, detail=errors[-1])
        agent_extra = {"tool_events": loop_runner.stats.events, "notices": loop_runner.stats.notices,
                       "tool_calls": loop_runner.stats.tool_calls}
    else:
        answer = await asyncio.to_thread(query_qwen, messages, model=active_model, max_tokens=safe_max_tokens, stream=False, tools=req.tools, tool_choice=req.tool_choice)
    llm_sec = round(time.time() - t_llm_start, 2)
    total_sec = round(time.time() - t0, 2)
    char_count = len(answer)
    tok_est = max(1, count_text_tokens(answer))
    tok_per_sec = round(tok_est / llm_sec, 1) if llm_sec > 0 else 0
    char_per_sec = round(char_count / llm_sec, 1) if llm_sec > 0 else 0

    reasoning_tok_count = 0
    content_tok_count = tok_est
    if "[Reasoning]" in answer and "[Answer]" in answer:
        r_part = answer.split("[Answer]")[0].replace("[Reasoning]", "").strip()
        a_part = answer.split("[Answer]")[1].strip()
        reasoning_tok_count = count_text_tokens(r_part)
        content_tok_count = count_text_tokens(a_part)
    elif "<think>" in answer and "</think>" in answer:
        r_part = answer.split("</think>")[0].replace("<think>", "").strip()
        a_part = answer.split("</think>")[1].strip()
        reasoning_tok_count = count_text_tokens(r_part)
        content_tok_count = count_text_tokens(a_part)

    print(f"⏱ [处理完成] 耗时: {total_sec:.2f}s (模型: {active_model} | 生成耗时: {llm_sec:.2f}s | 速度: {tok_per_sec} tok/s | 思考tokens: {reasoning_tok_count} | 生成tokens: {content_tok_count} | 总上下文: {prompt_tokens + tok_est})", flush=True)

    # --- 4. 记录当前轮次并异步后台持久化，不阻塞 HTTP 响应 ---
    stored_answer, r_summary = prepare_turn(answer or "")
    if stored_answer.strip():
        state.session_turns.append(make_turn(user_input, stored_answer, r_summary))
        asyncio.create_task(asyncio.to_thread(save_turn_to_memory, user_input, stored_answer, r_summary))

    return {
        "answer": answer,
        "search_results": web_search_items + hf_search_items,
        "stats": {
            "elapsed_sec": llm_sec,
            "tokens_per_sec": tok_per_sec,
            "token_count": tok_est,
            "reasoning_tokens": reasoning_tok_count,
            "content_tokens": content_tok_count,
            "prompt_tokens": prompt_tokens,
            "total_context_tokens": prompt_tokens + tok_est,
            "server_n_ctx": server_n_ctx,
            "char_count": char_count,
            "chars_per_sec": char_per_sec,
            "memory_count": len(state.memory_history),
            "session_turns": len(state.session_turns),
            "active_files_count": len(state.active_files),
            "buffer_mb": round(get_current_buffer_bytes() / (1024 * 1024), 2),
            "max_buffer_gb": get_dynamic_max_buffer_gb(),
            "active_model": active_model,
            "is_thinking": enable_thinking,
            "web_search_count": len(web_search_items),
            "tool_calls": agent_extra.get("tool_calls", 0),
        },
        **({"tool_events": agent_extra["tool_events"], "notices": agent_extra["notices"]} if agent_extra else {}),
    }

# ==============================================================================
# OpenAI 兼容 API 网关与记忆检索增强路由 (/v1/chat/completions, /v1/models, etc.)
# 使得第三方客户端 (如 Docker AI Chat UI, Cursor, Python OpenAI 库) 接入 18088 时
# 自动享有 Turbovec 4-bit 长期记忆、工作区文件大纲与联网检索能力！
# ==============================================================================

@app.get("/v1/models")
async def openai_list_models():
    """兼容 OpenAI 标准模型列表查询接口"""
    now = int(time.time())
    models_data = []
    for m in get_available_models():
        models_data.append({
            "id": m["id"],
            "object": "model",
            "created": now,
            "owned_by": "turbovec-rag",
            "permission": [],
            "root": m["id"],
            "parent": None
        })
    return {"object": "list", "data": models_data}

@app.get("/health")
@app.get("/v1/health")
async def health_check():
    return {
        "status": "ok",
        "service": "Turbovec RAG + OpenAI Gateway",
        "port": 18088,
        "active_backend": get_active_llm_base(),
        "memory_count": len(state.memory_history)
    }

@app.get("/slots")
async def get_slots():
    base_url = get_active_llm_base().replace("/v1", "")
    try:
        r = await get_async_client().get(f"{base_url}/slots", timeout=0.8)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return [{"id": 0, "n_ctx": get_active_server_n_ctx(), "is_processing": False}]

@app.get("/props")
async def get_props():
    base_url = get_active_llm_base().replace("/v1", "")
    try:
        r = await get_async_client().get(f"{base_url}/props", timeout=0.8)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {"default_generation_settings": {"n_ctx": get_active_server_n_ctx()}}

@app.post("/tokenize")
async def handle_tokenize(request: Request):
    base_url = get_active_llm_base().replace("/v1", "")
    try:
        body = await request.json()
        r = await get_async_client().post(f"{base_url}/tokenize", json=body, timeout=0.8)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {"tokens": []}

class SSEContentCapture:
    """增量解析 OpenAI 流式 SSE 字节流，累计 choices[0].delta.content"""

    def __init__(self):
        self._buf = b""
        self.parts: List[str] = []

    def _handle_line(self, line: bytes) -> None:
        line = line.strip()
        if not line.startswith(b"data:"):
            return
        data = line[5:].strip()
        if not data or data == b"[DONE]":
            return
        try:
            obj = json.loads(data.decode("utf-8"))
            delta = (obj.get("choices") or [{}])[0].get("delta") or {}
            content = delta.get("content")
            if content:
                self.parts.append(content)
        except Exception:
            pass

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            self._handle_line(line)

    def finish(self) -> str:
        if self._buf:
            self._handle_line(self._buf)
            self._buf = b""
        return "".join(self.parts)

@app.post("/v1/chat/completions")
async def openai_chat_completions(request: Request):
    """
    OpenAI 兼容的标准 Chat 接口：
    1. 解析接收 OpenAI 格式请求 (messages, model, stream, temperature, max_tokens, etc.)
    2. 提取用户最后一条输入，通过 Laya/Turbovec 索引与活跃工作区文件检索相关记忆
    3. 智能将长期记忆切片以 System 提示或 Context 前缀形式无缝注入 messages
    4. 转发给底层推理引擎 (18088)，支持流式 SSE 和全量 JSON 返回
    5. 异步保存本轮对话至 Turbovec 4-bit 长期向量库
    """
    try:
        req_data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    messages = req_data.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="messages 不能为空")

    stream = req_data.get("stream", False)
    requested_model = req_data.get("model") or getattr(state, "current_model", LLM_MODEL) or LLM_MODEL
    temperature = req_data.get("temperature", 0.7)
    max_tokens = req_data.get("max_tokens", 4096)

    # 提取最后一条用户消息
    user_query = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                user_query = content.strip()
            elif isinstance(content, list):
                user_query = " ".join([item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"]).strip()
            break

    # 1. 记忆检索与上下文增强 (Turbovec 4-bit RAG)
    augmented_contexts = []
    
    # 联网检索与 Hugging Face 智能检索注入 (统一加入 System Prompt)
    mcp_cfg = load_mcp_settings()
    web_enabled = mcp_cfg.get("web_search_enabled", True)
    hf_enabled = mcp_cfg.get("hf_search_enabled", True)

    need_hf = hf_enabled and should_trigger_huggingface_search(user_query)
    need_web = web_enabled and (should_trigger_web_search(user_query) or mcp_cfg.get("always_web_search", False))

    web_items = []
    hf_items = []

    if (need_web or need_hf) and user_query:
        try:
            tasks = []
            if need_web:
                tasks.append(("web", asyncio.to_thread(perform_web_search, user_query, mcp_cfg.get("num_results", 4))))
            if need_hf:
                tasks.append(("hf", asyncio.to_thread(perform_huggingface_search, user_query, 4)))
            if tasks:
                res_list = await asyncio.gather(*(t[1] for t in tasks), return_exceptions=True)
                for (tag, _), res in zip(tasks, res_list):
                    if isinstance(res, list):
                        if tag == "web":
                            web_items = res
                        elif tag == "hf":
                            hf_items = res
        except Exception as e:
            print(f"  [/v1/chat/completions 搜索异常] {e}", flush=True)

    search_block = format_search_context_for_system(web_items, hf_items)
    if search_block:
        augmented_contexts.append(search_block)

    # 工作区活跃文件上下文
    if state.active_files:
        for fn, info in state.active_files.items():
            outline = info.get("outline", "")
            if outline:
                augmented_contexts.append(f"<active_file name=\"{fn}\">\n{outline}\n</active_file>")

    # Turbovec 向量检索
    if user_query and state.embedder is not None and state.index is not None and len(state.memory_history) > 0:
        try:
            q_vec = await asyncio.to_thread(get_embeddings, [user_query], state.embedder)
            hits = await asyncio.to_thread(search_memory, q_vec, 4)
            retrieved = [text for _, text in hits]
            if retrieved:
                mem_block = "\n".join([f"【Turbovec 长期记忆参考】:\n{m}" for m in retrieved])
                augmented_contexts.append(mem_block)
        except Exception as e:
            print(f"  [/v1/chat/completions RAG 检索警告] {e}", flush=True)

    # 2. 构造增强后的 messages (将内部计算端口全功能体系、实时配置、网络/HF与Turbovec上下文完整注入 system)
    base_system_prompt = build_comprehensive_system_prompt(requested_model, user_query)
    
    extra_instructions = []
    if augmented_contexts:
        context_str = "\n\n".join(augmented_contexts)
        extra_instructions.append(f"【当前请求检索增强知识库 (包含实时网络 / Hugging Face 官方开源库 / Turbovec 长期记忆)】:\n{context_str}")
        
    final_messages = list(messages)
    system_found = False
    for msg in final_messages:
        if msg.get("role") == "system":
            original_sys = str(msg.get("content", "")).strip()
            augmented_sys = f"{base_system_prompt}\n\n[外部客户端自定义 System 指令]:\n{original_sys}" if original_sys else base_system_prompt
            if extra_instructions:
                augmented_sys += "\n\n" + "\n\n".join(extra_instructions)
            msg["content"] = augmented_sys
            system_found = True
            break
            
    if not system_found:
        # 客户端未携带 system 指令时才应用系统提示词预设 (append / replace)
        full_content = agent_service.apply_system_prompt(base_system_prompt)
        if extra_instructions:
            full_content += "\n\n" + "\n\n".join(extra_instructions)
        final_messages.insert(0, {
            "role": "system",
            "content": full_content
        })

    # 3. 构造转发至底层推理后端 (18088) 的请求
    backend_base = get_active_llm_base()
    target_url = f"{backend_base}/chat/completions"
    payload = dict(req_data)
    payload["messages"] = final_messages
    payload["model"] = requested_model

    req_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created_time = int(time.time())

    # 4. 流式传输处理 (零拷贝直接字节透传 Zero-Copy Direct Byte Streaming)
    if stream:
        async def openai_stream_generator():
            capture = SSEContentCapture()
            try:
                client = get_async_client()
                async with client.stream("POST", target_url, json=payload, headers={"Content-Type": "application/json"}) as resp:
                    async for raw_bytes in resp.aiter_bytes():
                        if not raw_bytes:
                            continue
                        # 原样透传 llama-server 的 SSE 字节流给客户端
                        yield raw_bytes
                        # 另行按行缓冲解析，正确处理跨 chunk 的事件、JSON 转义与被截断的 UTF-8 字符
                        capture.feed(raw_bytes)
            except Exception as e:
                err_chunk = {
                    "id": req_id,
                    "object": "chat.completion.chunk",
                    "created": created_time,
                    "model": requested_model,
                    "choices": [{
                        "index": 0,
                        "delta": {"content": f"\n\n[Gateway Error connecting to {target_url}: {e}]"},
                        "finish_reason": "stop"
                    }]
                }
                yield f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"

            # 异步存储该轮对话至 Turbovec 长期记忆
            full_ans = capture.finish()
            if user_query and full_ans:
                asyncio.create_task(asyncio.to_thread(save_turn_to_memory, user_query, full_ans))

        return StreamingResponse(openai_stream_generator(), media_type="text/event-stream")

    # 5. 非流式传输处理 (使用全局连接池 client)
    try:
        client = get_async_client()
        resp = await client.post(target_url, json=payload, headers={"Content-Type": "application/json"})
        resp_data = resp.json()
        
        # 异步存储记忆
        if user_query and "choices" in resp_data and len(resp_data["choices"]) > 0:
            ans_text = resp_data["choices"][0].get("message", {}).get("content", "")
            if ans_text:
                asyncio.create_task(asyncio.to_thread(save_turn_to_memory, user_query, ans_text))

        return resp_data
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM 推理后端连接失败 ({target_url}): {e}")

# ==============================================================================
# Web IDE (/ide)：资源管理器 · 编辑器 · 终端 · Git 版本管理 (见 docs/IDE_API.md)
# ==============================================================================
from ide_api import create_ide_router  # noqa: E402

ide_router = create_ide_router(
    project_root=PROJECT_ROOT,
    storage_dir=STORAGE_DIR,
    is_allowed_path=_is_allowed_path,
    check_security=check_request_security,
)
app.include_router(ide_router)

# ==============================================================================
# Agent：工具 · MCP · Skills · 系统提示词预设 (见 docs/AGENT_API.md)
# 所有依赖以 lambda 注入 (运行时再取全局变量)，便于测试 monkeypatch 与 STORAGE_DIR 重定向
# ==============================================================================
from agent import AgentDeps, AgentService, create_agent_router  # noqa: E402

_AGENT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _agent_fetch_url(url: str) -> Tuple[str, str]:
    resp = safe_public_get(url, {"User-Agent": _AGENT_UA}, 20)
    resp.raise_for_status()
    ctype = (resp.headers.get("content-type") or "").lower()
    raw = decode_file_content(resp.content[: 5 * 1024 * 1024])
    if "html" in ctype or not ctype or raw.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        return extract_clean_web_content(raw)
    if ctype.startswith("text/") or "json" in ctype or "xml" in ctype or "javascript" in ctype:
        return url, raw
    raise RuntimeError(f"不支持的内容类型: {ctype}")


def _agent_memory_search(query: str, k: int) -> List[str]:
    if state.embedder is None or state.index is None:
        return []
    return retrieve_ranked_memories(query, k)


agent_service = AgentService(AgentDeps(
    get_storage_dir=lambda: STORAGE_DIR,
    check_security=check_request_security,
    get_llm_base=lambda: get_active_llm_base(),
    get_client=lambda: get_async_client(),
    get_model=lambda: getattr(state, "current_model", LLM_MODEL) or LLM_MODEL,
    get_n_ctx=lambda: get_active_server_n_ctx(8192),
    count_tokens=lambda text: count_text_tokens(text),
    web_search=lambda q, n: perform_web_search(q, n),
    fetch_url=lambda url: _agent_fetch_url(url),
    memory_search=lambda q, k: _agent_memory_search(q, k),
    get_workspace=lambda: getattr(ide_router, "workspace", None),
))
app.include_router(create_agent_router(agent_service))

from ide_ai import create_ide_ai_router  # noqa: E402

async def ide_llm_stream(messages: List[Dict[str, Any]], max_tokens: int, temperature: float):
    """IDE AI 编辑专用：直连本机推理引擎流式输出，产出 ("reasoning"|"content", text)"""
    if getattr(state, "_switching_model", False):
        raise RuntimeError("模型切换中，请稍候再试")
    url = f"{get_active_llm_base()}/chat/completions"
    payload = {
        "model": getattr(state, "current_model", LLM_MODEL) or LLM_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    async with get_async_client().stream("POST", url, json=payload, timeout=180.0) as resp:
        if resp.status_code != 200:
            body = (await resp.aread()).decode("utf-8", errors="replace")
            raise RuntimeError(f"大模型接口异常 ({resp.status_code}): {body[:500]}")
        async for line in resp.aiter_lines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                delta = (json.loads(data).get("choices") or [{}])[0].get("delta") or {}
            except Exception:
                continue
            if delta.get("reasoning_content"):
                yield ("reasoning", delta["reasoning_content"])
            if delta.get("content"):
                yield ("content", delta["content"])

app.include_router(create_ide_ai_router(
    check_security=check_request_security,
    llm_stream=ide_llm_stream,
    get_n_ctx=lambda: get_active_server_n_ctx(8192),
    get_current_model=lambda: getattr(state, "current_model", LLM_MODEL),
    get_system_suffix=lambda: agent_service.ide_prompt_suffix(),  # 系统提示词预设 (仅 append)
))

# ==============================================================================
# 知识图谱记忆 (Graph RAG)：空闲时用本地模型把记忆整理成实体/关系 (见 docs/KG_API.md)
# 用户的 /api/chat、/v1/chat/completions、IDE AI 请求一开始就中断后台抽取 (llama-server 只有 1 个 slot)
# ==============================================================================
from kg import ActivityMiddleware, KGDeps, KGService, create_kg_router, is_user_llm_request  # noqa: E402

KG_BLOCK_MAX_TOKENS = 500


def _kg_embed(texts: List[str]) -> np.ndarray:
    if state.embedder is None:
        raise RuntimeError("Embedding 模型未就绪")
    return get_embeddings(list(texts), state.embedder, batch_size=16)


kg_service = KGService(KGDeps(
    get_storage_dir=lambda: STORAGE_DIR,
    get_llm_base=lambda: get_active_llm_base(),
    get_client=lambda: get_async_client(),
    get_model=lambda: getattr(state, "current_model", LLM_MODEL) or LLM_MODEL,
    get_n_ctx=lambda: getattr(state, "_live_n_ctx", None),
    is_switching=lambda: bool(getattr(state, "_switching_model", False)),
    embed=lambda texts: _kg_embed(texts),
    count_tokens=lambda text: count_text_tokens(text),
    get_memory_history=lambda: list(state.memory_history),
    check_security=check_request_security,
))
app.include_router(create_kg_router(kg_service))
app.add_middleware(ActivityMiddleware, tracker=kg_service.activity, match=is_user_llm_request)

@app.get("/ide", response_class=FileResponse)
def read_ide():
    return FileResponse(os.path.join(STATIC_DIR, "ide.html"))


if __name__ == "__main__":
    if TV_HOST not in ("127.0.0.1", "localhost", "::1") and not TV_API_TOKEN:
        print("⚠️ TV_HOST 指向非本机地址但未设置 TV_API_TOKEN：局域网请求将全部被拒绝 (403)。", flush=True)
    # 直接传入 app 对象，避免 "main:app" 字符串导致模块被二次导入、顶层代码执行两遍
    uvicorn.run(app, host=TV_HOST, port=TV_PORT, reload=False)
