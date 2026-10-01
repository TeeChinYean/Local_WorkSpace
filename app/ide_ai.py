"""AI file editing for the /ide page ("Cursor-style" edit mode).

Wiring (app/main.py):

    from ide_ai import create_ide_ai_router
    app.include_router(create_ide_ai_router(
        check_security=check_request_security,
        llm_stream=ide_llm_stream,            # see contract below
        get_n_ctx=lambda: get_active_server_n_ctx(8192),
        get_current_model=lambda: getattr(state, "current_model", LLM_MODEL),
        get_system_suffix=lambda: agent_service.ide_prompt_suffix(),  # optional: active preset (append)
    ))

`llm_stream(messages, max_tokens, temperature)` must return an async iterator of
`(kind, text)` tuples, kind = "reasoning" | "content" (an `async def` generator is
the natural implementation). Exceptions it raises are reported to the client as
`{"error": ...}`.

Endpoint: POST /api/ide/ai/edit
  body  {instruction, files:[{path, content}], selection?:{path, from_line, to_line, text},
         history?:[{role, content}]}
  SSE   data: {"context": {...}}            (once, what was sent / truncated / omitted)
        data: {"reasoning": "..."}          (0..n)
        data: {"token": "..."}              (0..n, answer text incl. SEARCH/REPLACE blocks)
        data: {"error": "..."}              (optional)
        data: {"done": true, "stats": {prompt_tokens_est, elapsed_sec, model, ...}}  (always last)

The model answers with Aider-style SEARCH/REPLACE blocks which the browser
(static/ide-ai.js) parses, matches against the editor buffer and shows as diffs.
Nothing is written to disk here.
"""
from __future__ import annotations

import inspect
import json
import re
import time
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# ----------------------------------------------------------------------------- limits
MAX_FILES = 20
MAX_FILE_CHARS = 2_000_000          # same cap as GET /api/ide/file
MAX_INSTRUCTION_CHARS = 20_000
MAX_HISTORY_MESSAGES = 6
MAX_HISTORY_MSG_CHARS = 2_000
TEMPERATURE = 0.2
DEFAULT_N_CTX = 8192
MIN_FILE_TOKENS = 96                # below this a file is omitted instead of truncated

SYSTEM_PROMPT = """你是一个代码编辑助手 (code editing assistant)。请根据用户的指令修改下面提供的文件。
You edit the user's code files. First give a very short explanation (1-3 sentences, in the user's language), then output the edits.

每一处修改都必须使用下面的 SEARCH/REPLACE 格式 (use EXACTLY this format for every change):

path/to/file.ext
<<<<<<< SEARCH
要被替换的原文 (existing lines, copied exactly)
=======
替换后的新内容 (new lines)
>>>>>>> REPLACE

规则 Rules:
1. 第一行只写文件的相对路径，与上面给出的路径完全一致。 The first line is the file path exactly as given, nothing else.
2. SEARCH 部分必须从文件中逐字复制（包括缩进、空格、注释），只包含要修改的几行和 1-3 行上下文，不要复制整个文件。 Copy SEARCH lines verbatim from the file (same indentation); keep it short: the changed lines plus 1-3 lines of context.
3. 每个块只改一处；可以输出多个块，也可以修改多个文件。 One change per block; multiple blocks and multiple files are allowed.
4. 创建新文件：SEARCH 部分留空。 To create a new file, leave SEARCH empty and put the whole file in REPLACE.
5. 删除代码：REPLACE 部分留空。 To delete code, leave REPLACE empty.
6. 不要输出行号，不要重复整个文件，不要修改没有提供内容的文件。 Never output line numbers, never repeat the whole file, never edit files whose content was not provided.
7. 如果指令不需要修改代码，直接回答即可，不要输出 SEARCH/REPLACE 块。 If no code change is needed, just answer without blocks.

示例 Example — 指令: "让 greet 接收 name 参数"

app/hello.py
<<<<<<< SEARCH
def greet():
    print("hello")
=======
def greet(name):
    print(f"hello {name}")
>>>>>>> REPLACE
"""

_LANG_BY_EXT = {
    "py": "python", "js": "javascript", "mjs": "javascript", "cjs": "javascript", "jsx": "jsx", "ts": "typescript",
    "tsx": "tsx", "html": "html", "htm": "html", "css": "css", "scss": "scss", "json": "json", "md": "markdown",
    "ps1": "powershell", "psm1": "powershell", "bat": "bat", "cmd": "bat", "sh": "bash", "yml": "yaml",
    "yaml": "yaml", "toml": "toml", "ini": "ini", "sql": "sql", "java": "java", "kt": "kotlin", "c": "c",
    "h": "c", "cpp": "cpp", "hpp": "cpp", "cs": "csharp", "rs": "rust", "go": "go", "xml": "xml",
}

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


# ----------------------------------------------------------------------------- request models
class EditFile(BaseModel):
    path: str
    content: str = ""


class EditSelection(BaseModel):
    path: str
    from_line: int = 1
    to_line: int = 1
    text: str = ""


class HistoryTurn(BaseModel):
    role: str
    content: str = ""


class EditRequest(BaseModel):
    instruction: str
    files: List[EditFile] = Field(default_factory=list)
    selection: Optional[EditSelection] = None
    history: List[HistoryTurn] = Field(default_factory=list)


# ----------------------------------------------------------------------------- helpers
def validate_rel_path(p: str) -> str:
    """Workspace-relative path check. Returns the normalised path (forward slashes) or raises 400."""
    if not isinstance(p, str) or not p.strip():
        raise HTTPException(400, "路径不能为空")
    s = p.strip().replace("\\", "/")
    if "\x00" in s:
        raise HTTPException(400, f"非法路径: {p!r}")
    if s.startswith("/") or _DRIVE_RE.match(s) or s.startswith("~"):
        raise HTTPException(400, f"路径必须是工作区相对路径: {p}")
    parts = [x for x in s.split("/") if x not in ("", ".")]
    if not parts or any(x == ".." for x in parts):
        raise HTTPException(400, f"路径不能包含 '..': {p}")
    if any(":" in x for x in parts):
        raise HTTPException(400, f"非法路径: {p}")
    return "/".join(parts)


def estimate_tokens(text: str) -> int:
    """Cheap, conservative token estimate: ~3 ASCII chars per token, 1 token per non-ASCII char (CJK)."""
    if not text:
        return 0
    n_ascii = len(text.encode("ascii", "ignore"))
    return int(n_ascii / 3 + (len(text) - n_ascii)) + 1


def _fence_for(content: str) -> str:
    longest = max((len(m) for m in re.findall(r"`{3,}", content)), default=0)
    return "`" * max(3, longest + 1)


def _lang_for(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    return _LANG_BY_EXT.get(ext, "")


def _truncate_lines(lines: List[str], budget: int, focus: Optional[Tuple[int, int]]) -> Tuple[int, int, int]:
    """Pick a contiguous window [a, b) of lines whose estimated tokens fit `budget`.
    With `focus` (0-based inclusive line range) the window grows around it, else from the top.
    Returns (a, b, tokens_used)."""
    cost = [estimate_tokens(ln) + 1 for ln in lines]
    n = len(lines)
    if focus:
        a = max(0, min(focus[0], n - 1))
        b = max(a, min(focus[1], n - 1)) + 1
        used = sum(cost[a:b])
        while used > budget and b - a > 1:      # focus itself too large: keep its head
            b -= 1
            used -= cost[b]
        grow_up = True
        while True:
            can_up, can_down = a > 0 and used + cost[a - 1] <= budget, b < n and used + cost[b] <= budget
            if not can_up and not can_down:
                break
            if (grow_up and can_up) or not can_down:
                a -= 1
                used += cost[a]
            else:
                used += cost[b]
                b += 1
            grow_up = not grow_up
        return a, b, used
    used, b = 0, 0
    while b < n and used + cost[b] <= budget:
        used += cost[b]
        b += 1
    return 0, b, used


def build_edit_messages(req: EditRequest, n_ctx: int, extra_system: str = "") -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    """Build the chat messages for an edit request within the context budget.

    ``extra_system`` (the active system-prompt preset, append mode) is appended to SYSTEM_PROMPT.

    Returns (messages, meta) where meta = {prompt_tokens_est, max_tokens, n_ctx, budget,
    files:[paths fully included], truncated:[{path, from_line, to_line, total_lines}], omitted:[paths]}.
    """
    n_ctx = int(n_ctx) if n_ctx and n_ctx > 0 else DEFAULT_N_CTX
    reserved = min(4096, int(n_ctx * 0.3))
    budget = max(256, n_ctx - reserved - 64)     # 64 ≈ chat template overhead

    instruction = req.instruction.strip()
    sel = req.selection
    sel_block = ""
    if sel and sel.text.strip():
        sel_text = sel.text
        sel_cap_chars = max(400, budget // 4 * 3)  # never let the selection eat the whole budget
        if len(sel_text) > sel_cap_chars:
            sel_text = sel_text[:sel_cap_chars] + "\n…(选中内容过长，已截断 / selection truncated)"
        fence = _fence_for(sel_text)
        sel_block = (f"用户在编辑器中选中的代码 (selected code) — {sel.path} 第 {sel.from_line}-{sel.to_line} 行 "
                     f"(lines {sel.from_line}-{sel.to_line}):\n{fence}\n{sel_text}\n{fence}\n\n"
                     "请优先修改选中的这部分代码。 Focus the change on the selected code.\n\n")
    instr_block = f"指令 (instruction):\n{instruction}\n"
    system_prompt = SYSTEM_PROMPT + (extra_system or "")

    used = estimate_tokens(system_prompt) + estimate_tokens(sel_block) + estimate_tokens(instr_block) + 40

    # history: newest first, small share of the budget
    hist_msgs: List[Dict[str, str]] = []
    hist_budget = int(budget * 0.15)
    for turn in reversed(req.history[-MAX_HISTORY_MESSAGES:]):
        role = turn.role if turn.role in ("user", "assistant") else None
        content = (turn.content or "").strip()
        if not role or not content:
            continue
        if len(content) > MAX_HISTORY_MSG_CHARS:
            content = content[:MAX_HISTORY_MSG_CHARS] + "\n…(truncated)"
        c = estimate_tokens(content) + 8
        if c > hist_budget:
            break
        hist_budget -= c
        used += c
        hist_msgs.insert(0, {"role": role, "content": content})
    if hist_msgs and hist_msgs[0]["role"] == "assistant":
        used -= estimate_tokens(hist_msgs[0]["content"]) + 8
        hist_msgs.pop(0)

    # files: selection file first, then in the given order (the UI puts the active file first)
    files: List[EditFile] = list(req.files)
    if sel:
        files.sort(key=lambda f: 0 if f.path == sel.path else 1)
    remaining = budget - used
    parts: List[str] = []
    notes: List[str] = []
    meta_full: List[str] = []
    meta_trunc: List[Dict[str, Any]] = []
    meta_omit: List[str] = []
    for f in files:
        content = f.content.replace("\r\n", "\n")
        lang = _lang_for(f.path)
        header_cost = estimate_tokens(f.path) + 12
        cost = estimate_tokens(content) + content.count("\n") + header_cost  # same per-line cost as _truncate_lines
        if cost <= remaining:
            fence = _fence_for(content)
            body = content if (content.endswith("\n") or not content) else content + "\n"
            parts.append(f"{f.path}\n{fence}{lang}\n{body}{fence}\n")
            remaining -= cost
            meta_full.append(f.path)
            continue
        avail = remaining - header_cost - 40
        lines = content.split("\n")
        if avail < MIN_FILE_TOKENS:
            meta_omit.append(f.path)
            continue
        focus = None
        if sel and sel.path == f.path:
            focus = (max(0, sel.from_line - 1), max(0, sel.to_line - 1))
        a, b, chunk_cost = _truncate_lines(lines, avail, focus)
        if b <= a:
            meta_omit.append(f.path)
            continue
        chunk = "\n".join(lines[a:b])
        fence = _fence_for(chunk)
        parts.append(f"{f.path}  (仅显示第 {a + 1}-{b} 行，共 {len(lines)} 行 / truncated: showing lines {a + 1}-{b} of {len(lines)})\n"
                     f"{fence}{lang}\n{chunk}\n{fence}\n")
        remaining -= chunk_cost + header_cost + 40
        meta_trunc.append({"path": f.path, "from_line": a + 1, "to_line": b, "total_lines": len(lines)})
        notes.append(f"- {f.path}: 只提供了第 {a + 1}-{b} 行 (lines {a + 1}-{b} of {len(lines)} only); "
                     "SEARCH 只能引用这些行 (SEARCH must only quote these lines).")
    for p in meta_omit:
        notes.append(f"- {p}: 因上下文长度限制未提供内容，不要修改它 (omitted for context length — do not edit it).")

    user_parts: List[str] = []
    if parts:
        user_parts.append("以下是相关文件 (files):\n\n" + "\n".join(parts))
    else:
        user_parts.append("（没有提供文件内容 / no file content provided — 如需新建文件请使用空 SEARCH。）\n")
    if notes:
        user_parts.append("注意 (notes):\n" + "\n".join(notes) + "\n")
    if sel_block:
        user_parts.append(sel_block)
    user_parts.append(instr_block)
    user_msg = "\n".join(user_parts)

    messages = [{"role": "system", "content": system_prompt}] + hist_msgs + [{"role": "user", "content": user_msg}]
    prompt_tokens = sum(estimate_tokens(m["content"]) + 6 for m in messages)
    max_tokens = max(reserved, min(8192, n_ctx - prompt_tokens - 64))
    max_tokens = max(128, min(max_tokens, n_ctx - 64))
    meta = {
        "prompt_tokens_est": prompt_tokens, "max_tokens": max_tokens, "n_ctx": n_ctx, "budget": budget,
        "files": meta_full, "truncated": meta_trunc, "omitted": meta_omit, "history_messages": len(hist_msgs),
    }
    return messages, meta


def _sse(obj: Dict[str, Any]) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


# ----------------------------------------------------------------------------- router
def create_ide_ai_router(*,
                         check_security: Callable[[str, str, str, Optional[str], str], Optional[str]],
                         llm_stream: Callable[[List[Dict[str, str]], int, float], AsyncIterator[Tuple[str, str]]],
                         get_n_ctx: Callable[[], int],
                         get_current_model: Callable[[], str],
                         get_system_suffix: Optional[Callable[[], str]] = None) -> APIRouter:
    router = APIRouter(prefix="/api/ide/ai", tags=["ide-ai"])

    def _guard(request: Request) -> None:
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-tv-token", "").strip()
        reason = check_security(request.method, request.client.host if request.client else "",
                                request.headers.get("host", ""), request.headers.get("origin"), token)
        if reason:
            raise HTTPException(403, reason)

    def _safe(fn: Callable[[], Any], default: Any) -> Any:
        try:
            v = fn()
            return v if v else default
        except Exception:
            return default

    @router.post("/edit")
    async def ai_edit(req: EditRequest, request: Request):
        _guard(request)
        if not req.instruction or not req.instruction.strip():
            raise HTTPException(400, "指令不能为空")
        if len(req.instruction) > MAX_INSTRUCTION_CHARS:
            raise HTTPException(400, f"指令过长（最多 {MAX_INSTRUCTION_CHARS} 字符）")
        if len(req.files) > MAX_FILES:
            raise HTTPException(400, f"最多附带 {MAX_FILES} 个文件")
        seen: Dict[str, EditFile] = {}
        for f in req.files:
            f.path = validate_rel_path(f.path)
            if len(f.content) > MAX_FILE_CHARS:
                raise HTTPException(413, f"文件过大: {f.path}")
            seen.setdefault(f.path, f)
        req.files = list(seen.values())
        if req.selection is not None:
            req.selection.path = validate_rel_path(req.selection.path)
            if req.selection.from_line > req.selection.to_line:
                req.selection.from_line, req.selection.to_line = req.selection.to_line, req.selection.from_line
            if len(req.selection.text) > MAX_FILE_CHARS:
                raise HTTPException(413, "选中内容过大")

        n_ctx = _safe(lambda: int(get_n_ctx()), DEFAULT_N_CTX)
        extra_system = str(_safe(get_system_suffix, "") or "") if get_system_suffix else ""
        messages, meta = build_edit_messages(req, n_ctx, extra_system)
        model = str(_safe(get_current_model, "") or "")

        async def gen():
            t0 = time.time()
            out_chars = 0
            reasoning_chars = 0
            yield _sse({"context": {"files": meta["files"], "truncated": meta["truncated"], "omitted": meta["omitted"],
                                    "prompt_tokens_est": meta["prompt_tokens_est"], "n_ctx": meta["n_ctx"]}})
            it = None
            try:
                it = llm_stream(messages, meta["max_tokens"], TEMPERATURE)
                if inspect.isawaitable(it):
                    it = await it
                async for item in it:
                    try:
                        kind, text = item
                    except (TypeError, ValueError):
                        continue
                    if not text:
                        continue
                    if kind == "reasoning":
                        reasoning_chars += len(text)
                        yield _sse({"reasoning": text})
                    else:
                        out_chars += len(text)
                        yield _sse({"token": text})
            except Exception as e:  # noqa: BLE001 — report any backend failure to the UI
                yield _sse({"error": f"大模型接口异常: {e}" if str(e) else f"大模型接口异常: {type(e).__name__}"})
            finally:
                close = getattr(it, "aclose", None)
                if close is not None:
                    try:
                        await close()
                    except Exception:
                        pass
            yield _sse({"done": True, "stats": {
                "prompt_tokens_est": meta["prompt_tokens_est"], "elapsed_sec": round(time.time() - t0, 2),
                "model": model, "n_ctx": meta["n_ctx"], "max_tokens": meta["max_tokens"],
                "output_chars": out_chars, "reasoning_chars": reasoning_chars,
                "truncated": [t["path"] for t in meta["truncated"]], "omitted": meta["omitted"],
            }})

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return router
