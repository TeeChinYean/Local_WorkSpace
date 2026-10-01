"""Entity / relation extraction with the local LLM, run by an idle-time background worker.

* ``parse_extraction`` — tolerant JSON parsing (code fences, <think>, trailing commas, leading/trailing prose).
* ``KGWorker`` — asyncio loop: one job at a time, only while the user is idle; the running llama stream
  is cancelled the moment a user request starts (pre-emption) and the job goes back to the queue.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from .store import DESC_MAX, ENTITY_TYPES, NAME_MAX, clean_relation, clean_text, clean_type, normalize_name

if TYPE_CHECKING:  # pragma: no cover
    from .service import KGService

JOB_TEXT_CAP = 1500
MAX_ENTITIES = 16
MAX_RELATIONS = 16
EXTRACT_MAX_TOKENS = 512
MIN_N_CTX = 4096
CHECK_INTERVAL = 2.0          # seconds between idle checks (never a busy loop)
BETWEEN_JOBS = 1.0
EXTRACT_TIMEOUT = 150.0

SYSTEM_PROMPT = (
    "你是知识图谱抽取器 (knowledge-graph extractor)。从用户给出的文本中找出重要的实体和它们之间的关系。\n"
    "只输出一个 JSON 对象，不要解释，不要 Markdown。Output ONLY one JSON object:\n"
    '{"entities":[{"name":"实体名","type":"person|org|place|concept|file|code|product|event|other","description":"≤40字简介"}],'
    '"relations":[{"head":"实体名","relation":"简短动词短语","tail":"实体名"}]}\n'
    "规则 Rules：1) name 用原文写法，简短具体，不要代词；2) relation 为 2-8 字的动词短语（如 使用/属于/位于/负责/喜欢/depends on）；"
    "3) head 与 tail 必须出现在 entities 中；4) 最多 12 个实体、12 条关系；5) 忽略寒暄与无信息内容；"
    '6) 没有可抽取的内容时输出 {"entities":[],"relations":[]}。'
)


class ExtractionError(ValueError):
    pass


class Preempted(Exception):
    """The running extraction was aborted because a user request started."""


# ----------------------------------------------------------------------------- parsing
_THINK_RE = re.compile(r"<think>.*?(</think>|$)", re.S | re.I)
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _balanced_object(s: str) -> Optional[str]:
    """First balanced {...} in ``s`` (string/escape aware)."""
    start = s.find("{")
    while start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(s)):
            c = s[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return s[start:i + 1]
        start = s.find("{", start + 1)
    return None


def _loads_lenient(candidate: str) -> Any:
    try:
        return json.loads(candidate)
    except ValueError:
        pass
    fixed = _TRAILING_COMMA_RE.sub(r"\1", candidate)
    fixed = fixed.replace("“", '"').replace("”", '"')
    return json.loads(fixed)


def parse_extraction(text: str) -> Dict[str, List[Dict[str, str]]]:
    """Parse the model output into ``{"entities": [...], "relations": [...]}`` or raise ExtractionError."""
    raw = _THINK_RE.sub("", text or "").strip()
    if not raw:
        raise ExtractionError("模型没有输出内容")
    candidates: List[str] = []
    for m in _FENCE_RE.finditer(raw):
        candidates.append(m.group(1))
    candidates.append(raw)
    data: Any = None
    last_err: Optional[Exception] = None
    for cand in candidates:
        obj = _balanced_object(cand)
        if obj is None:
            continue
        try:
            data = _loads_lenient(obj)
            break
        except ValueError as e:
            last_err = e
    if not isinstance(data, dict):
        raise ExtractionError(f"无法解析 JSON: {last_err or '没有找到 JSON 对象'}; 输出开头: {raw[:120]!r}")
    return normalize_extraction(data)


def normalize_extraction(data: Dict[str, Any]) -> Dict[str, List[Dict[str, str]]]:
    ents_in = data.get("entities") if isinstance(data.get("entities"), list) else []
    rels_in = data.get("relations") if isinstance(data.get("relations"), list) else []
    if not isinstance(data.get("entities"), list) and not isinstance(data.get("relations"), list):
        raise ExtractionError("JSON 缺少 entities / relations 字段")
    entities: List[Dict[str, str]] = []
    seen = set()
    for e in ents_in:
        if isinstance(e, str):
            e = {"name": e}
        if not isinstance(e, dict):
            continue
        name = clean_text(e.get("name"), NAME_MAX)
        norm = normalize_name(name)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        entities.append({"name": name, "type": clean_type(e.get("type")), "description": clean_text(e.get("description"), DESC_MAX)})
        if len(entities) >= MAX_ENTITIES:
            break
    relations: List[Dict[str, str]] = []
    rseen = set()
    for r in rels_in:
        if isinstance(r, (list, tuple)) and len(r) == 3:
            r = {"head": r[0], "relation": r[1], "tail": r[2]}
        if not isinstance(r, dict):
            continue
        head = clean_text(r.get("head") or r.get("source") or r.get("subject"), NAME_MAX)
        tail = clean_text(r.get("tail") or r.get("target") or r.get("object"), NAME_MAX)
        rel = clean_relation(r.get("relation") or r.get("type") or r.get("predicate"))
        hn, tn = normalize_name(head), normalize_name(tail)
        if not hn or not tn or not rel or hn == tn:
            continue
        key = (hn, rel, tn)
        if key in rseen:
            continue
        rseen.add(key)
        relations.append({"head": head, "relation": rel, "tail": tail})
        if len(relations) >= MAX_RELATIONS:
            break
    return {"entities": entities, "relations": relations}


def build_messages(text: str) -> List[Dict[str, str]]:
    body = (text or "")[:JOB_TEXT_CAP]
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"文本 Text:\n<<<\n{body}\n>>>\n只输出 JSON。 /no_think"},
    ]


# ----------------------------------------------------------------------------- worker
class KGWorker:
    def __init__(self, service: "KGService"):
        self.service = service
        self.running_job: Optional[int] = None
        self._task: Optional[asyncio.Task] = None
        self._job_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop = False
        self._preempted = False
        self._json_mode = True       # response_format json_object; disabled after the server rejects it
        service.activity.add_listener(self.preempt)

    # ---------------------------------------------------------------- gating
    def can_run(self) -> Tuple[bool, str]:
        svc = self.service
        s = svc.settings()
        if not s["enabled"]:
            return False, "disabled"
        if s["paused"]:
            return False, "paused"
        if svc.activity.active > 0 or svc.activity.idle_for() < s["idle_seconds"]:
            return False, "busy"
        if svc.deps.is_switching():
            return False, "switching"
        n_ctx = svc.deps.get_n_ctx()
        if not n_ctx:
            return False, "llm_unavailable"
        if n_ctx < MIN_N_CTX:
            return False, "ctx_too_small"
        if not svc.store.has_pending():
            return False, "queue_empty"
        return True, ""

    # ---------------------------------------------------------------- pre-emption
    def preempt(self) -> None:
        """Called at the start of every user request (from any thread)."""
        task, loop = self._job_task, self._loop
        if task is None or task.done() or loop is None:
            return
        self._preempted = True
        try:
            if loop.is_running() and _running_loop() is not loop:
                loop.call_soon_threadsafe(task.cancel)
            else:
                task.cancel()
        except RuntimeError:
            pass

    # ---------------------------------------------------------------- one job
    async def run_once(self) -> str:
        """Process at most one queued job. -> "idle:<reason>" | "done" | "error" | "preempted"."""
        ok, reason = self.can_run()
        if not ok:
            return f"idle:{reason}"
        store = self.service.store
        job = store.claim_next()
        if job is None:
            return "idle:queue_empty"
        self.running_job = int(job["id"])
        self._loop = asyncio.get_running_loop()
        self._preempted = False
        self._job_task = asyncio.ensure_future(self._extract(job["text"]))
        try:
            try:
                raw = await asyncio.wait_for(self._job_task, EXTRACT_TIMEOUT)
            except asyncio.TimeoutError:
                raise ExtractionError("模型响应超时")
            except asyncio.CancelledError:
                if not self._preempted:
                    raise  # the worker itself is being cancelled (shutdown)
                store.requeue_job(job["id"], "已让位给用户请求，稍后重试")
                return "preempted"
            except Preempted:
                store.requeue_job(job["id"], "已让位给用户请求，稍后重试")
                return "preempted"
            result = parse_extraction(raw)
            await asyncio.to_thread(store.ingest, result, job["kind"], job["ref"], job["text"])
            store.finish_job(job["id"])
            store.set_meta("last_run_at", str(time.time()))
            try:
                await asyncio.to_thread(self.service.fill_name_vectors, 64)
            except Exception as e:  # embeddings are optional (substring matching still works)
                print(f"  [知识图谱] ⚠ 实体向量计算失败: {e}", flush=True)
            return "done"
        except asyncio.CancelledError:
            if self._job_task is not None and not self._job_task.done():
                self._job_task.cancel()
            store.requeue_job(job["id"], "服务关闭时中断")
            raise
        except Exception as e:  # noqa: BLE001 — record, never crash the loop
            msg = f"{type(e).__name__}: {e}" if not isinstance(e, ExtractionError) else str(e)
            store.fail_job(job["id"], msg)
            store.set_meta("last_error", msg[:500])
            store.set_meta("last_error_at", str(time.time()))
            store.set_meta("last_run_at", str(time.time()))
            return "error"
        finally:
            self.running_job = None
            self._job_task = None
            self._preempted = False

    async def _extract(self, text: str) -> str:
        deps = self.service.deps
        payload: Dict[str, Any] = {
            "model": deps.get_model(),
            "messages": build_messages(text),
            "max_tokens": EXTRACT_MAX_TOKENS,
            "temperature": 0,
            "stream": True,
            "cache_prompt": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if self._json_mode:
            payload["response_format"] = {"type": "json_object"}
        url = f"{deps.get_llm_base()}/chat/completions"
        client = deps.get_client()
        if self.service.activity.active > 0:
            raise Preempted()      # a user request started between claim and send
        for attempt in (1, 2):
            parts: List[str] = []
            reasoning: List[str] = []
            async with client.stream("POST", url, json=payload, headers={"Content-Type": "application/json"},
                                     timeout=EXTRACT_TIMEOUT) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    if attempt == 1 and "response_format" in payload and resp.status_code in (400, 422, 500):
                        self._json_mode = False
                        payload.pop("response_format", None)
                        continue
                    raise ExtractionError(f"大模型接口异常 ({resp.status_code}): {body[:200]}")
                async for line in resp.aiter_lines():
                    if self.service.activity.active > 0:
                        raise Preempted()
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or choice.get("message") or {}
                    if delta.get("content"):
                        parts.append(delta["content"])
                    if delta.get("reasoning_content"):
                        reasoning.append(delta["reasoning_content"])
            out = "".join(parts)
            return out if out.strip() else "".join(reasoning)
        raise ExtractionError("大模型接口异常")

    # ---------------------------------------------------------------- loop
    async def run_forever(self) -> None:
        self._stop = False
        try:
            self.service.store.reset_running()
        except Exception:
            pass
        while not self._stop:
            outcome = "idle"
            try:
                outcome = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                print(f"  [知识图谱] ⚠ 后台整理异常: {e}", flush=True)
            await asyncio.sleep(BETWEEN_JOBS if outcome in ("done", "error") else CHECK_INTERVAL)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.create_task(self.run_forever(), name="kg-worker")

    async def stop(self) -> None:
        self._stop = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


def _running_loop() -> Optional[asyncio.AbstractEventLoop]:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


__all__ = ["KGWorker", "parse_extraction", "normalize_extraction", "build_messages", "ExtractionError",
           "SYSTEM_PROMPT", "JOB_TEXT_CAP", "ENTITY_TYPES"]
