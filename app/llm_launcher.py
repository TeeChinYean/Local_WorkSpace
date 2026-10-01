"""Native llama-server launcher with VRAM-aware context auto-expansion.

Why: llama-server pre-allocates the whole KV cache for ``-c`` at start-up, so VRAM
usage is flat while it runs. The only way to "use every MB up to the red line" is
to pick ``-c`` right before launch. This module:

1. estimates ``-c`` from the real KV size per token (layers x kv_heads x head_dim x
   bytes(K)+bytes(V)) and the VRAM already used by other apps;
2. after the server is healthy, measures real VRAM with ``nvidia-smi``; if there is
   still room below the red line (total - vram_reserve_mb) it relaunches once with a
   larger context, and if it is above the red line it shrinks;
3. stores the measured non-KV overhead per model in
   ``app/storage/ctx_calibration.json`` so the next launch is right the first time.

Stdlib only, so ``run_local_llm.ps1`` can call it without the RAG dependencies:

    python app/llm_launcher.py --model 1          # foreground (logs in this window)
    python app/llm_launcher.py --model 1 --dry-run
"""
from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import subprocess
import sys
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(APP_DIR)
CONFIG_FILE = os.environ.get("TV_LLM_CONFIG", os.path.join(ROOT_DIR, "llm_models.json"))
STORAGE_DIR = os.path.join(APP_DIR, "storage")
CALIB_FILE = os.path.join(STORAGE_DIR, "ctx_calibration.json")
KEY_FILE = os.path.join(STORAGE_DIR, "llama_api_key.txt")
SERVER_LOG = os.path.join(STORAGE_DIR, "llama_server.log")

# bytes per element of each llama.cpp KV cache type (block size 32)
KV_TYPE_BYTES = {
    "f32": 4.0, "f16": 2.0, "bf16": 2.0,
    "q8_0": 34 / 32, "q5_1": 24 / 32, "q5_0": 22 / 32,
    "q4_1": 20 / 32, "q4_0": 18 / 32, "iq4_nl": 18 / 32,
}
CTX_STEP = 1024

Log = Callable[[str], None]


# ----------------------------------------------------------------------------- config
def load_config(path: str = CONFIG_FILE) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as f:
        cfg = json.load(f)
    # Resolve relative paths and environment variables dynamically
    if "llama_exe" in cfg:
        exe = os.path.expanduser(os.path.expandvars(cfg["llama_exe"]))
        if not os.path.isabs(exe):
            exe = os.path.normpath(os.path.join(ROOT_DIR, exe))
        cfg["llama_exe"] = exe
    for m in cfg.get("models", []):
        if "path" in m:
            p = os.path.expanduser(os.path.expandvars(m["path"]))
            if not os.path.isabs(p):
                p = os.path.normpath(os.path.join(ROOT_DIR, p))
            m["path"] = p
    return cfg


def find_model(cfg: Dict[str, Any], key_or_id: str) -> Dict[str, Any]:
    for m in cfg["models"]:
        if key_or_id in (m.get("key"), m.get("id")):
            return m
    raise KeyError(f"unknown model: {key_or_id}")


def kv_mib_per_1k(model: Dict[str, Any], ctk: str, ctv: str) -> float:
    """KV cache MiB per 1024 tokens."""
    elems = model["kv_layers"] * model["kv_heads"] * model["head_dim"]
    return elems * (KV_TYPE_BYTES[ctk] + KV_TYPE_BYTES[ctv]) * 1024 / (1024 * 1024)


def model_file_mb(model: Dict[str, Any]) -> float:
    try:
        return os.path.getsize(model["path"]) / (1024 * 1024)
    except OSError:
        return float(model.get("weight_mb", 3000))


# ----------------------------------------------------------------------------- state files
def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def calib_key(cfg: Dict[str, Any], model: Dict[str, Any]) -> str:
    size = int(model_file_mb(model))
    return f"{model['id']}|{cfg['cache_type_k']}|{cfg['cache_type_v']}|ub{model.get('ub', 512)}|{size}MB"


def load_calibration(cfg, model) -> Optional[Dict[str, Any]]:
    return _read_json(CALIB_FILE, {}).get(calib_key(cfg, model))


def save_calibration(cfg, model, entry: Dict[str, Any]) -> None:
    data = _read_json(CALIB_FILE, {})
    data[calib_key(cfg, model)] = entry
    _write_json(CALIB_FILE, data)


def get_api_key() -> str:
    """Shared secret between the gateway and llama-server (created once, git-ignored)."""
    env = os.environ.get("TV_LLAMA_API_KEY", "").strip()
    if env:
        return env
    try:
        with open(KEY_FILE, "r", encoding="utf-8") as f:
            key = f.read().strip()
            if key:
                return key
    except OSError:
        pass
    os.makedirs(STORAGE_DIR, exist_ok=True)
    try:
        # O_EXCL：网关与启动器同时首次运行时只有一方能创建，另一方读取同一把密钥
        fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(secrets.token_urlsafe(24))
    except FileExistsError:
        time.sleep(0.2)
    with open(KEY_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()


# ----------------------------------------------------------------------------- gpu
def query_gpu() -> Optional[Tuple[int, int]]:
    """(used_mb, total_mb) of the first NVIDIA GPU, or None."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            timeout=3, encoding="utf-8",
        )
        used, total = [int(float(x.strip())) for x in out.strip().splitlines()[0].split(",")]
        return used, total
    except Exception:
        return None


# ----------------------------------------------------------------------------- planning
def estimated_overhead_mb(cfg, model) -> float:
    """Non-KV VRAM of the llama-server process (weights + compute buffers + fixed state)."""
    cal = load_calibration(cfg, model)
    if cal and cal.get("overhead_mb"):
        return float(cal["overhead_mb"])
    return model_file_mb(model) + model.get("buffer_mb", 200) + model.get("fixed_state_mb", 0)


def plan_ctx(cfg, model, other_used_mb: float, total_mb: float, overhead_mb: Optional[float] = None) -> Dict[str, Any]:
    """Largest ctx (multiple of 1024) that keeps total VRAM <= total - vram_reserve_mb."""
    kv = kv_mib_per_1k(model, cfg["cache_type_k"], cfg["cache_type_v"])
    target_mb = total_mb - cfg.get("vram_reserve_mb", 204)
    overhead = estimated_overhead_mb(cfg, model) if overhead_mb is None else overhead_mb
    free_for_kv = target_mb - other_used_mb - overhead
    raw = int(math.floor(free_for_kv / kv)) * CTX_STEP if free_for_kv > 0 else 0
    cal = load_calibration(cfg, model) or {}
    if cal.get("fail_ctx"):
        # 曾在该上下文因显存不足 (计算缓冲分配失败) 启动失败：规划时留出 2k tokens 余量
        raw = min(raw, int(cal["fail_ctx"]) - 2 * CTX_STEP)
    ctx = max(model.get("min_ctx", 4096), min(model.get("max_ctx", 32768), raw))
    ctx = (ctx // CTX_STEP) * CTX_STEP
    return {
        "ctx": ctx,
        "kv_mib_per_1k": round(kv, 2),
        "target_mb": target_mb,
        "other_used_mb": other_used_mb,
        "overhead_mb": round(overhead, 1),
        "est_total_mb": round(other_used_mb + overhead + kv * ctx / CTX_STEP, 1),
        "capped_by_max": raw > model.get("max_ctx", 32768),
        "at_min": raw < model.get("min_ctx", 4096),
    }


def build_cmd(cfg, model, ctx: int, api_key: Optional[str], fit: bool = False) -> List[str]:
    """fit=True：交给 llama.cpp 自带的 --fit 按设备真实可用显存 (含计算缓冲) 自动选择最大上下文，
    此时不能传 -ngl / -c (用户显式设置会让 --fit 放弃调整)。"""
    if fit:
        mem_args = [
            "--fit", "on",
            "--fit-target", str(int(cfg.get("vram_reserve_mb", 204))),
            "--fit-ctx", str(int(model.get("min_ctx", 4096))),
        ]
    else:
        mem_args = ["-ngl", "99", "-c", str(ctx)]
    cmd = [
        cfg["llama_exe"],
        "-m", model["path"],
        *mem_args,
        "-dev", cfg.get("device", "Vulkan1"),
        "-np", "1",
        "-ctk", cfg["cache_type_k"],
        "-ctv", cfg["cache_type_v"],
        "-fa", cfg.get("flash_attn", "on"),
        "-b", str(cfg.get("batch", 2048)),
        "-ub", str(model.get("ub", 512)),
        *cfg.get("extra_args", []),
        "--port", str(cfg.get("port", 18089)),
        "--host", cfg.get("host", "127.0.0.1"),
    ]
    if api_key:
        cmd += ["--api-key", api_key]
    return cmd


# ----------------------------------------------------------------------------- process
def fetch_server_ctx(port: int, api_key: Optional[str]) -> Optional[int]:
    """读取 llama-server 实际使用的 n_ctx (/props，需要 API Key)"""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/props")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
        ctx = (data.get("default_generation_settings") or {}).get("n_ctx") or data.get("n_ctx")
        return int(ctx) if ctx else None
    except Exception:
        return None


def wait_healthy(port: int, proc, timeout: float = 90.0) -> bool:
    url = f"http://127.0.0.1:{port}/health"
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def stop_proc(proc, timeout: float = 5.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout)


def _default_popen(cmd: List[str], foreground: bool):
    if foreground:
        return subprocess.Popen(cmd)
    os.makedirs(STORAGE_DIR, exist_ok=True)
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    with open(SERVER_LOG, "ab") as logf:  # 子进程继承句柄后父进程即可关闭，避免句柄泄漏
        return subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, creationflags=flags)


def _boost_after_fit(cfg, model, proc, ctx, after, api_key, port, foreground, popen, gpu, healthy, settle_sec, log, info):
    """--fit 以 Vulkan 报告的可用显存为准，在 Windows 上通常偏保守 (会留下数百 MB)。
    若 nvidia-smi 实测离红线仍有余量，则用 -ngl 99 -c N 显式再启动一次；
    斜率按 KV × 计算缓冲增长系数估算，并额外保留 vulkan_safety_mb，失败的上下文会被记住。"""
    used, total = after
    kv = kv_mib_per_1k(model, cfg["cache_type_k"], cfg["cache_type_v"])
    slope = kv * float(cfg.get("compute_growth_factor", 1.08))
    target = total - cfg.get("vram_reserve_mb", 204) - cfg.get("vulkan_safety_mb", 160)
    cal = load_calibration(cfg, model) or {}
    room = target - used
    cand = ctx + int(max(0.0, room) / slope) * CTX_STEP
    cand = min(cand, model.get("max_ctx", 32768))
    if cal.get("fail_ctx"):
        cand = min(cand, int(cal["fail_ctx"]) - 2 * CTX_STEP)
    cand = (cand // CTX_STEP) * CTX_STEP
    if cand - ctx < cfg.get("relaunch_min_gain_tokens", 2048):
        return proc, ctx, info
    log(f"[上下文自动加长] --fit 后仍有 {room:.0f}MB 余量 → 显式 -ngl 99 -c {cand} 重启 ...")
    stop_proc(proc)
    time.sleep(settle_sec)
    p2 = popen(build_cmd(cfg, model, cand, api_key), foreground)
    ok = healthy(port, p2)
    after2 = gpu() if ok else None
    if ok and after2 and after2[0] <= total - cfg.get("vram_reserve_mb", 204):
        cal = load_calibration(cfg, model) or {}
        cal.update(best_ok_ctx=cand, measured_ctx=cand, measured_used_mb=after2[0], total_mb=after2[1], ts=int(time.time()))
        save_calibration(cfg, model, cal)
        log(f"[上下文自动加长] ✔ -c {cand} | 显存 {after2[0]}/{after2[1]}MB")
        return p2, cand, dict(info, ctx=cand, relaunched=True, measured_used_mb=after2[0])
    stop_proc(p2)
    cal = load_calibration(cfg, model) or {}
    cal["fail_ctx"] = cand
    save_calibration(cfg, model, cal)
    log(f"[上下文自动加长] -c {cand} 失败或超出红线，已记录上限，回到 --fit 结果 -c {ctx}")
    time.sleep(settle_sec)
    p3 = popen(build_cmd(cfg, model, 0, api_key, fit=True), foreground)
    if healthy(port, p3):
        return p3, ctx, info
    stop_proc(p3)
    return None, ctx, {"error": "回退到 --fit 结果时启动失败", **info}


def launch(
    model_key: str,
    foreground: bool = False,
    calibrate: bool = True,
    log: Log = print,
    cfg: Optional[Dict[str, Any]] = None,
    popen: Callable = None,
    gpu: Callable[[], Optional[Tuple[int, int]]] = query_gpu,
    healthy: Callable = wait_healthy,
    settle_sec: float = 1.5,
) -> Tuple[Optional[Any], int, Dict[str, Any]]:
    """Start llama-server with the largest safe ctx. Returns (proc|None, ctx, info)."""
    cfg = cfg or load_config()
    model = find_model(cfg, model_key)
    popen = popen or _default_popen
    port = int(cfg.get("port", 18089))
    if not os.path.exists(cfg["llama_exe"]):
        return None, 0, {"error": f"未找到 llama-server: {cfg['llama_exe']}"}
    if "llama-server" not in os.path.basename(cfg["llama_exe"]).lower():
        return None, 0, {"error": f"llama_exe 必须指向 llama-server 可执行文件: {cfg['llama_exe']}"}
    if not os.path.exists(model["path"]):
        hint = f"，请先下载：python scratch\\download_model.py {model.get('key', '')}" if model.get("download") else ""
        return None, 0, {"error": f"未找到模型文件: {model['path']}{hint}"}

    api_key = get_api_key()
    g = gpu()

    # ---- 模式 1 (默认)：llama.cpp 原生 --fit，按真实分配 (含计算缓冲) 选出红线内最大上下文，一次启动即完成
    if cfg.get("ctx_mode", "fit") == "fit":
        log(f"[上下文规划] {model['name']}: 使用 llama.cpp --fit，保留 {cfg.get('vram_reserve_mb', 204)}MB 空闲显存，"
            f"最小上下文 {model.get('min_ctx', 4096)}")
        proc = popen(build_cmd(cfg, model, 0, api_key, fit=True), foreground)
        if healthy(port, proc):
            ctx = fetch_server_ctx(port, api_key) or 0
            time.sleep(settle_sec)
            after = gpu()
            used = f"{after[0]}/{after[1]}MB" if after else "未知"
            log(f"[上下文规划] ✔ --fit 完成：-c {ctx} | 显存 {used}")
            prev = load_calibration(cfg, model) or {}
            entry = dict(prev, mode="fit", fit_ctx=ctx, ts=int(time.time()))
            if after:
                entry.update(measured_ctx=ctx, measured_used_mb=after[0], total_mb=after[1])
            save_calibration(cfg, model, entry)
            if ctx and ctx < model.get("min_ctx", 4096):
                log("[上下文规划] ⚠ 上下文低于最小值，可能有部分层被放到 CPU")
            info = {"mode": "fit", "ctx": ctx, "measured_used_mb": after[0] if after else None}
            if calibrate and after and ctx and not model.get("allow_cpu_offload"):
                proc, ctx, info = _boost_after_fit(cfg, model, proc, ctx, after, api_key, port, foreground,
                                                   popen, gpu, healthy, settle_sec, log, info)
            return proc, ctx, info
        stop_proc(proc)
        log("[上下文规划] ⚠ --fit 启动失败 (该 llama-server 版本可能不支持)，改用估算 + 实测校准模式")
        time.sleep(settle_sec)
        g = gpu()

    # ---- 模式 2 (回退)：按 KV 估算 → nvidia-smi 实测 → 重启调整
    total_mb = g[1] if g else 4096
    other_used = g[0] if g else 300
    plan = plan_ctx(cfg, model, other_used, total_mb)
    ctx = plan["ctx"]
    kv = plan["kv_mib_per_1k"]
    log(f"[上下文规划] {model['name']}: KV {kv} MiB/1k ({cfg['cache_type_k']}/{cfg['cache_type_v']}) | "
        f"其他程序已用 {other_used}MB | 红线 {plan['target_mb']}MB | 预估开销 {plan['overhead_mb']}MB → -c {ctx}")

    attempts = 0
    proc = None
    while True:
        attempts += 1
        proc = popen(build_cmd(cfg, model, ctx, api_key), foreground)
        if healthy(port, proc):
            break
        stop_proc(proc)
        if attempts >= 4 or ctx <= model.get("min_ctx", 4096):
            return None, ctx, {"error": "llama-server 启动失败 (可能显存不足或参数错误)，详见 app/storage/llama_server.log", **plan}
        # 减半重试 (多半是首次估算偏乐观导致显存不足)；成功后的实测校准会再把上下文加长回红线
        ctx = max(model.get("min_ctx", 4096), (ctx // 2 // CTX_STEP) * CTX_STEP)
        log(f"[上下文规划] 启动失败，减半上下文重试: -c {ctx}")
        time.sleep(settle_sec)

    info = dict(plan, ctx=ctx, relaunched=False)
    if not calibrate or not g:
        return proc, ctx, info

    # measure real usage; relaunch (at most twice) if the red line allows a bigger ctx or requires a smaller one
    min_ctx = model.get("min_ctx", 4096)
    for _round in range(3):
        time.sleep(settle_sec)
        after = gpu()
        if not after:
            break
        used_total = after[0]
        target_mb = after[1] - cfg.get("vram_reserve_mb", 204)
        overhead = used_total - other_used - kv * ctx / CTX_STEP
        info.update(measured_used_mb=used_total)
        # 合理性校验：非 KV 开销不可能远小于模型文件 (多半是旧进程显存尚未释放导致基线偏高)，这种读数不写入校准
        if overhead < 0.5 * model_file_mb(model):
            log(f"[显存实测] ⚠ 读数异常 (非 KV 开销 {overhead:.0f}MB 明显小于模型文件 {model_file_mb(model):.0f}MB)，本次不写入校准")
            break
        prev = load_calibration(cfg, model) or {}
        entry = {
            "mode": "calibrate", "overhead_mb": round(overhead, 1), "measured_ctx": ctx, "measured_used_mb": used_total,
            "other_used_mb": other_used, "total_mb": after[1], "ts": int(time.time()),
        }
        if prev.get("fail_ctx") and prev["fail_ctx"] > ctx:
            entry["fail_ctx"] = prev["fail_ctx"]
        save_calibration(cfg, model, entry)
        info.update(overhead_mb=round(overhead, 1))
        ideal = plan_ctx(cfg, model, other_used, after[1], overhead_mb=overhead)["ctx"]
        over_red_line = used_total > target_mb
        log(f"[显存实测] 已用 {used_total}/{after[1]}MB (红线 {target_mb}MB) | 实测非 KV 开销 {overhead:.0f}MB | 红线内最优 -c {ideal}")
        if over_red_line and ideal >= ctx:
            ideal = max(min_ctx, ctx - CTX_STEP * max(1, math.ceil((used_total - target_mb) / kv)))
        if ideal == ctx:
            if over_red_line:
                log(f"[显存实测] ⚠ 已是最小上下文 {min_ctx} 仍超出红线 {used_total - target_mb}MB：请关闭其他占用显存的程序或换更小的模型")
                info["over_red_line"] = True
            break
        if not over_red_line and ideal - ctx < cfg.get("relaunch_min_gain_tokens", 2048):
            break
        log(f"[上下文自动{'缩小' if ideal < ctx else '加长'}] -c {ctx} → {ideal}，正在重启 llama-server ...")
        stop_proc(proc)
        time.sleep(settle_sec)
        proc = popen(build_cmd(cfg, model, ideal, api_key), foreground)
        if not healthy(port, proc):
            stop_proc(proc)
            cal = load_calibration(cfg, model) or {}
            cal["fail_ctx"] = ideal  # 记住失败的上下文，下次规划不再尝试
            save_calibration(cfg, model, cal)
            log(f"[上下文自动调整] -c {ideal} 显存不足 (计算缓冲分配失败)，回退到 -c {ctx}，并记录上限")
            time.sleep(settle_sec)
            proc = popen(build_cmd(cfg, model, ctx, api_key), foreground)
            if not healthy(port, proc):
                stop_proc(proc)
                return None, ctx, {"error": "llama-server 回退启动失败", **info}
            break
        ctx = ideal
        info.update(ctx=ctx, relaunched=True)
    return proc, ctx, info


def describe_vram(cfg, model) -> str:
    """Short VRAM label for the UI, based on the last calibration if any."""
    cal = load_calibration(cfg, model)
    if cal and cal.get("measured_used_mb") and cal.get("measured_ctx"):
        return f"实测 {cal['measured_used_mb'] / 1024:.2f} GB · {cal['measured_ctx'] // 1024}k 上下文"
    return f"KV {kv_mib_per_1k(model, cfg['cache_type_k'], cfg['cache_type_v']):.1f} MiB/1k · 启动时自动校准"


# ----------------------------------------------------------------------------- CLI
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Launch llama-server with VRAM-aware context")
    ap.add_argument("--model", default="1", help="menu key (1/2/3) or model id")
    ap.add_argument("--dry-run", action="store_true", help="print the plan only")
    ap.add_argument("--no-calibrate", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config()
    model = find_model(cfg, args.model)
    if args.dry_run:
        g = query_gpu() or (300, 4096)
        print(json.dumps(plan_ctx(cfg, model, g[0], g[1]), ensure_ascii=False, indent=2))
        return 0

    proc, ctx, info = launch(args.model, foreground=True, calibrate=not args.no_calibrate)
    if proc is None:
        print(f"❌ {info.get('error')}")
        return 1
    print(f"✔ {model['name']} 已就绪: -c {ctx} | http://127.0.0.1:{cfg.get('port', 18089)} (仅本机, 需 API Key)")
    try:
        return proc.wait()
    except KeyboardInterrupt:
        stop_proc(proc)
        return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main())
