"""Embedding 后端：ONNX Runtime fp32 (默认) / PyTorch sentence-transformers (可选)。

为什么：基准测试 (bge-small-zh-v1.5, CPU) 中 ONNX Runtime fp32 与 torch 结果 cos = 1.000000，
网关常驻内存 893 MB -> 170 MB，单条查询 6.1 ms -> 2.1 ms，且无需重建已有 Turbovec 索引。

- 本模块顶层不导入 torch；只有首次导出 ONNX (在独立子进程中) 或 EMBEDDING_BACKEND=torch 时才需要 torch。
- 环境变量:
    EMBEDDING_BACKEND   onnx (默认) | torch | onnx-int8 (仅当 model_int8.onnx 已存在; 与 fp32 索引不兼容, 启动时会自动重建索引)
    EMBED_THREADS       推理线程数 (默认 = 物理核心数)
    EMBEDDING_ONNX_AUTO_EXPORT  0 = 缺少 ONNX 文件时不自动导出
- ONNX 目录: <model_dir>-onnx (例: app/models/bge-small-zh-v1.5-onnx/)，内含 model_fp32.onnx 与分词器/配置文件。

命令行 (在 app 目录下):
    python -m embedding_backend --export <model_dir> [<onnx_dir>] [--int8]   # 一次性导出 (需要 torch + transformers + onnx)
    python -m embedding_backend --check [<model_dir>]                        # 打印后端 / 维度 / 线程 / RSS
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, List, Optional

APP_DIR = Path(__file__).resolve().parent
DEFAULT_MODEL_DIR = APP_DIR / "models" / "bge-small-zh-v1.5"

BACKENDS = ("onnx", "onnx-int8", "torch")
ONNX_FILES = {
    "onnx": ["model_fp32.onnx", "model.onnx"],
    "onnx-int8": ["model_int8.onnx", "model_quantized.onnx"],
}
# 导出时从原模型目录复制到 ONNX 目录的分词器 / 配置文件
TOKENIZER_FILES = ["tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "vocab.txt",
                   "config.json", "sentence_bert_config.json", "modules.json",
                   "config_sentence_transformers.json"]
EXPORT_TIMEOUT_S = 300
MAX_SEQ_LEN = 512
ONNX_MAX_BATCH = 16  # ONNX Runtime 关闭内存池后，batch 16 峰值内存远低于 64 (668 MB vs 3.9 GB)，吞吐几乎不变
VERIFY_TEXTS = ["如何启动本地大模型推理服务？", "Turbovec FastAPI index", "a",
                "mixed 中英文 text with padding, 用于测试动态序列长度与大小写处理 ABC"]
EXPORT_DEPS = ("torch", "transformers", "onnx")  # torch.onnx.export (TorchScript 导出器) 需要 onnx 包
FIX_EXPORT_CMD = "pip install torch transformers onnx"

Log = Callable[[str], Any]


# ----------------------------------------------------------------------------- helpers
def _have(name: str) -> bool:
    """模块是否可导入 (不真正导入；兼容测试中 sys.modules 里的桩模块 / None 屏蔽)。"""
    if name in sys.modules:
        return sys.modules[name] is not None
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def rss_mb() -> Optional[float]:
    try:
        import psutil
        return psutil.Process().memory_info().rss / 2**20
    except Exception:
        return None


def default_threads() -> int:
    """推理线程数：EMBED_THREADS > 物理核心数 (psutil) > 逻辑核心数 // 2 > 4。超线程会让 BERT 推理变慢。"""
    env = os.environ.get("EMBED_THREADS", "").strip()
    if env:
        try:
            n = int(env)
            if n > 0:
                return n
        except ValueError:
            pass
    try:
        import psutil
        n = psutil.cpu_count(logical=False)
        if n:
            return int(n)
    except Exception:
        pass
    n = (os.cpu_count() or 0) // 2
    return n if n > 0 else 4


def resolve_backend(backend: Optional[str] = None) -> str:
    raw = (backend if backend is not None else os.environ.get("EMBEDDING_BACKEND", "onnx")) or "onnx"
    b = raw.strip().lower().replace("_", "-")
    if b in ("onnx", "onnx-fp32", "ort", "onnxruntime"):
        return "onnx"
    if b in ("onnx-int8", "int8", "onnx-q8"):
        return "onnx-int8"
    if b in ("torch", "pytorch", "sentence-transformers", "st"):
        return "torch"
    print(f"[Embedding] ⚠ 未知 EMBEDDING_BACKEND={raw!r}，使用 onnx", flush=True)
    return "onnx"


def backend_family(name: Optional[str]) -> str:
    """向量兼容族：torch 与 onnx fp32 输出一致 (cos 1.000000) -> fp32；int8 量化向量与之不兼容。"""
    return "int8" if "int8" in (name or "").lower() else "fp32"


def onnx_dir_for(model_dir: str) -> Path:
    """ONNX 目录 = <model_dir>-onnx；HF 缓存 snapshot 目录则放到 app/models/<仓库名>-onnx。"""
    p = Path(model_dir).resolve()
    if p.parent.name == "snapshots" and p.parent.parent.name.startswith("models--"):
        repo = p.parent.parent.name.split("--")[-1]
        return APP_DIR / "models" / f"{repo}-onnx"
    return p.parent / f"{p.name}-onnx"


def find_onnx_file(onnx_dir, backend: str = "onnx") -> Optional[Path]:
    for name in ONNX_FILES.get(backend, []):
        p = Path(onnx_dir) / name
        if p.is_file():
            return p
    return None


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ----------------------------------------------------------------------------- ONNX embedder
class OnnxBgeEmbedder:
    """与 sentence-transformers 接口兼容的最小 ONNX Runtime 封装 (encode / get_*_dimension)。"""

    def __init__(self, onnx_path, tokenizer_dir, threads: Optional[int] = None):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        from tokenizers.normalizers import Lowercase, Sequence

        self._np = np
        self.onnx_path = str(onnx_path)
        self.threads = int(threads or default_threads())
        self.backend_name = "onnx-int8" if "int8" in Path(onnx_path).name or "quant" in Path(onnx_path).name else "onnx"
        tok_dir = Path(tokenizer_dir)
        st_cfg = _read_json(tok_dir / "sentence_bert_config.json")
        tk_cfg = _read_json(tok_dir / "tokenizer_config.json")
        pool_cfg = _read_json(tok_dir / "1_Pooling" / "config.json") or {"pooling_mode_cls_token": True}
        if pool_cfg.get("pooling_mode_cls_token"):
            self.pooling = "cls"
        elif pool_cfg.get("pooling_mode_mean_tokens"):
            self.pooling = "mean"
        else:
            self.pooling = "cls"
        self.max_len = min(int(st_cfg.get("max_seq_length") or MAX_SEQ_LEN), MAX_SEQ_LEN)
        self.do_lower_case = bool(st_cfg.get("do_lower_case") or tk_cfg.get("do_lower_case"))

        self.tok = Tokenizer.from_file(str(tok_dir / "tokenizer.json"))
        if self.do_lower_case:  # sentence-transformers 会在分词前 lower()；bge 的 tokenizer.json 本身 lowercase=False
            norm = self.tok.normalizer
            self.tok.normalizer = Sequence([Lowercase(), norm]) if norm is not None else Lowercase()
        self.tok.enable_truncation(max_length=self.max_len)
        pad_id = self.tok.token_to_id("[PAD]")
        self.tok.enable_padding(pad_id=0 if pad_id is None else pad_id, pad_token="[PAD]")

        so = ort.SessionOptions()
        so.intra_op_num_threads = self.threads
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.enable_cpu_mem_arena = False  # 每次推理后把激活内存还给操作系统 (常驻内存低)
        self.sess = ort.InferenceSession(self.onnx_path, so, providers=["CPUExecutionProvider"])
        self.input_names = {i.name for i in self.sess.get_inputs()}
        dim = self.sess.get_outputs()[0].shape[-1]
        if not isinstance(dim, int):
            dim = (pool_cfg.get("word_embedding_dimension")
                   or _read_json(tok_dir / "config.json").get("hidden_size") or 512)
        self.dim = int(dim)

    def get_embedding_dimension(self) -> int:
        return self.dim

    def get_sentence_embedding_dimension(self) -> int:
        return self.dim

    def encode(self, texts, batch_size: int = 16, normalize_embeddings: bool = True,
               show_progress_bar: bool = False, convert_to_numpy: bool = True, **_):
        np = self._np
        single = isinstance(texts, str)
        if single:
            texts = [texts]
        texts = list(texts)
        batch_size = max(1, int(batch_size or 16))
        out = np.empty((len(texts), self.dim), dtype=np.float32)
        # 与 sentence-transformers 相同：按长度降序分批 (减少 padding)，最后按原顺序写回
        order = sorted(range(len(texts)), key=lambda i: -len(texts[i]))
        for s in range(0, len(order), batch_size):
            idx = order[s:s + batch_size]
            enc = self.tok.encode_batch([texts[i] for i in idx])
            ids = np.asarray([e.ids for e in enc], dtype=np.int64)
            mask = np.asarray([e.attention_mask for e in enc], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.input_names:
                feed["token_type_ids"] = np.zeros_like(ids)
            hidden = self.sess.run(None, feed)[0]
            if self.pooling == "cls":
                vec = hidden[:, 0]
            else:
                m = mask[..., None].astype(np.float32)
                vec = (hidden * m).sum(1) / np.clip(m.sum(1), 1e-9, None)
            if normalize_embeddings:
                vec = vec / np.clip(np.linalg.norm(vec, axis=1, keepdims=True), 1e-12, None)
            out[idx] = vec
        return out[0] if single else out

    def __repr__(self):
        return f"OnnxBgeEmbedder({self.onnx_path!r}, dim={self.dim}, threads={self.threads}, pooling={self.pooling})"


# ----------------------------------------------------------------------------- export (torch, 一次性)
def _reference_embeddings(model_dir: Path, texts: List[str]):
    """用 torch 计算参考向量 (优先 sentence-transformers，完整复现大小写/池化/归一化)。"""
    import numpy as np
    if _have("sentence_transformers"):
        from sentence_transformers import SentenceTransformer
        m = SentenceTransformer(str(model_dir), device="cpu", local_files_only=True)
        return np.asarray(m.encode(texts, batch_size=16, normalize_embeddings=True, show_progress_bar=False,
                                   convert_to_numpy=True), dtype=np.float32)
    import torch
    from transformers import AutoModel, AutoTokenizer
    st_cfg = _read_json(model_dir / "sentence_bert_config.json")
    if st_cfg.get("do_lower_case"):
        texts = [t.lower() for t in texts]
    tok = AutoTokenizer.from_pretrained(str(model_dir))
    m = AutoModel.from_pretrained(str(model_dir)).eval()
    e = tok(texts, padding=True, truncation=True, max_length=MAX_SEQ_LEN, return_tensors="pt")
    with torch.no_grad():
        v = m(**e).last_hidden_state[:, 0].numpy()
    return (v / np.linalg.norm(v, axis=1, keepdims=True)).astype(np.float32)


def export_onnx(model_dir, onnx_dir, opset: int = 17, int8: bool = False, verify: bool = True) -> Path:
    """把 HF / sentence-transformers 模型导出为 ONNX fp32 (需要 torch + transformers + onnx)。返回 model_fp32.onnx 路径。"""
    import numpy as np
    import torch
    from transformers import AutoModel, AutoTokenizer

    model_dir, onnx_dir = Path(model_dir).resolve(), Path(onnx_dir).resolve()
    if not (model_dir / "config.json").is_file():
        raise FileNotFoundError(f"模型目录缺少 config.json: {model_dir}")
    onnx_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    try:
        m = AutoModel.from_pretrained(str(model_dir), attn_implementation="eager").eval()
    except TypeError:  # 旧版 transformers
        m = AutoModel.from_pretrained(str(model_dir)).eval()
    tok = AutoTokenizer.from_pretrained(str(model_dir))

    class _Wrap(torch.nn.Module):
        def __init__(self, inner):
            super().__init__()
            self.inner = inner

        def forward(self, input_ids, attention_mask, token_type_ids):
            return self.inner(input_ids=input_ids, attention_mask=attention_mask,
                              token_type_ids=token_type_ids).last_hidden_state

    names = ["input_ids", "attention_mask", "token_type_ids"]
    ex = tok(["你好 world", "测试一下 a longer example sentence"], padding=True, return_tensors="pt")
    args = tuple(ex[k] for k in names)
    fp32 = onnx_dir / "model_fp32.onnx"
    tmp = onnx_dir / "model_fp32.onnx.tmp"
    kw = dict(input_names=names, output_names=["last_hidden_state"],
              dynamic_axes={k: {0: "batch", 1: "seq"} for k in names + ["last_hidden_state"]},
              opset_version=opset, do_constant_folding=True)
    with torch.no_grad():
        try:
            torch.onnx.export(_Wrap(m), args, str(tmp), dynamo=False, **kw)
        except TypeError:  # 旧版 torch 没有 dynamo 参数
            torch.onnx.export(_Wrap(m), args, str(tmp), **kw)
    del m
    for f in TOKENIZER_FILES:
        if (model_dir / f).is_file():
            shutil.copy2(model_dir / f, onnx_dir / f)
    if (model_dir / "1_Pooling" / "config.json").is_file():
        (onnx_dir / "1_Pooling").mkdir(exist_ok=True)
        shutil.copy2(model_dir / "1_Pooling" / "config.json", onnx_dir / "1_Pooling" / "config.json")
    print(f"[export] 导出完成 {fp32.name} ({tmp.stat().st_size / 2**20:.1f} MB, {time.time() - t0:.1f}s)", flush=True)

    info = {"source": str(model_dir), "torch": getattr(torch, "__version__", "?"), "opset": opset,
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if verify:
        # 用 ONNX 封装 (含大小写/截断/CLS/L2) 与 torch 参考向量逐条对比，确保可直接复用旧索引
        emb = OnnxBgeEmbedder(tmp, onnx_dir, threads=default_threads())
        got = emb.encode(VERIFY_TEXTS, batch_size=4)
        del emb
        ref = _reference_embeddings(model_dir, VERIFY_TEXTS)
        cos = (got * ref).sum(1) / (np.linalg.norm(got, axis=1) * np.linalg.norm(ref, axis=1))
        info["verify_cos_min"] = float(cos.min())
        print(f"[export] 校验: ONNX vs torch cos min = {cos.min():.6f}", flush=True)
        if cos.min() < 0.9999:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"ONNX 导出校验失败: cos min = {cos.min():.6f} < 0.9999")
    os.replace(tmp, fp32)
    if int8:
        from onnxruntime.quantization import QuantType, quantize_dynamic  # 需要 `pip install onnx`
        q = onnx_dir / "model_int8.onnx"
        quantize_dynamic(str(fp32), str(q), weight_type=QuantType.QInt8, per_channel=True)
        print(f"[export] int8 量化完成 {q.name} ({q.stat().st_size / 2**20:.1f} MB)", flush=True)
    (onnx_dir / "export_info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    return fp32


def _export_via_subprocess(model_dir, onnx_dir, log: Log = print, timeout: int = EXPORT_TIMEOUT_S) -> bool:
    """在独立子进程里导出 (torch 的内存随子进程退出全部释放，网关进程永不导入 torch)。"""
    if os.environ.get("EMBEDDING_ONNX_AUTO_EXPORT", "1").strip() == "0":
        log("  ⚠ [Embedding] EMBEDDING_ONNX_AUTO_EXPORT=0，跳过自动导出 ONNX")
        return False
    missing = [m for m in EXPORT_DEPS if not _have(m)]
    if missing:
        log(f"  ⚠ [Embedding] 未找到 ONNX 模型，且无法自动导出 (缺少 {', '.join(missing)})。\n"
            f"    修复方法 (一次性)：pip install {' '.join(missing)}\n"
            f"    然后重启，或手动执行：cd app && python -m embedding_backend --export \"{model_dir}\" \"{onnx_dir}\"")
        return False
    log(f"  ⏳ [Embedding] 首次启动：正在把模型导出为 ONNX (一次性，约 10-60 秒)\n"
        f"    {model_dir} -> {onnx_dir}")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", HF_HUB_OFFLINE="1",
               TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false")
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable, "-m", "embedding_backend", "--export", str(model_dir), str(onnx_dir)],
                           cwd=str(APP_DIR), env=env, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"  ⚠ [Embedding] ONNX 导出超时 (> {timeout}s)。可手动执行：cd app && python -m embedding_backend "
            f"--export \"{model_dir}\" \"{onnx_dir}\"")
        return False
    except Exception as e:
        log(f"  ⚠ [Embedding] 无法启动 ONNX 导出子进程: {e}")
        return False
    for line in (p.stdout or "").splitlines():
        if line.startswith("[export]"):
            log("    " + line)
    if p.returncode != 0 or not find_onnx_file(onnx_dir, "onnx"):
        tail = " | ".join((p.stderr or p.stdout or "").strip().splitlines()[-4:])
        log(f"  ⚠ [Embedding] ONNX 导出失败 (exit {p.returncode}): {tail[:600]}\n"
            f"    修复方法：{FIX_EXPORT_CMD}，然后手动执行：cd app && python -m embedding_backend "
            f"--export \"{model_dir}\" \"{onnx_dir}\"")
        return False
    log(f"  ✔ [Embedding] ONNX 导出完成 (耗时 {time.time() - t0:.1f}s)，以后启动直接加载，无需 torch")
    return True


# ----------------------------------------------------------------------------- loading
def _load_torch(model_dir: str, threads: int, log: Log = print):
    """旧行为：sentence-transformers + PyTorch。"""
    import torch
    if hasattr(torch, "set_num_threads"):
        torch.set_num_threads(threads)
    from sentence_transformers import SentenceTransformer
    model = None
    if os.path.isdir(model_dir):
        try:
            model = SentenceTransformer(model_dir, local_files_only=True)
        except Exception as e:
            log(f"  ⚠ 离线加载指定路径异常 ({e})，尝试回退模式...")
    if model is None:
        model = SentenceTransformer(model_dir)
    try:
        model.backend_name = "torch"
        model.threads = threads
    except Exception:
        pass
    return model


def _torch_available() -> bool:
    return _have("torch") and _have("sentence_transformers")


def _resolve_local_dir(model_dir: str, log: Log = print) -> Optional[str]:
    """HF 模型名 -> 本地 snapshot 目录 (先离线缓存，再联网下载)；失败返回 None。"""
    if os.path.isdir(model_dir):
        return model_dir
    try:
        from huggingface_hub import snapshot_download
    except Exception:
        return None
    for offline in (True, False):
        try:
            return snapshot_download(model_dir, local_files_only=offline)
        except Exception as e:
            if not offline:
                log(f"  ⚠ [Embedding] 下载模型 {model_dir} 失败: {e}")
    return None


def _fallback_torch(model_dir: str, threads: int, log: Log, reason: str):
    if not _torch_available():
        raise RuntimeError(f"[Embedding] {reason}；且未安装 torch / sentence-transformers，无法回退。\n"
                           f"  修复方法：pip install onnxruntime tokenizers (推荐)，或一次性安装 {FIX_EXPORT_CMD} "
                           f"以便自动导出 ONNX")
    log(f"  ⚠ [Embedding] {reason}，回退到 torch 后端 (内存占用较高)")
    return _load_torch(model_dir, threads, log)


def load_embedder(model_dir: str, backend: Optional[str] = None, log: Log = print, threads: Optional[int] = None):
    """按 EMBEDDING_BACKEND 加载 embedder。返回对象带 .backend_name / .threads 属性，接口兼容 SentenceTransformer。"""
    model_dir = str(model_dir)
    backend = resolve_backend(backend)
    threads = int(threads or default_threads())
    if backend == "torch":
        return _load_torch(model_dir, threads, log)

    if not (_have("onnxruntime") and _have("tokenizers")):
        return _fallback_torch(model_dir, threads, log,
                               "未安装 onnxruntime / tokenizers (pip install onnxruntime tokenizers)")

    local_dir = _resolve_local_dir(model_dir, log)
    if local_dir is None:
        return _fallback_torch(model_dir, threads, log, f"找不到本地模型目录 {model_dir}，无法使用 ONNX")
    onnx_dir = onnx_dir_for(local_dir)

    onnx_file = None
    if backend == "onnx-int8":
        onnx_file = find_onnx_file(onnx_dir, "onnx-int8")
        if onnx_file is None:
            log(f"  ⚠ [Embedding] 未找到 {onnx_dir / 'model_int8.onnx'} (可用 python -m embedding_backend --export "
                f"<model_dir> --int8 生成)，改用 ONNX fp32")
    if onnx_file is None:
        onnx_file = find_onnx_file(onnx_dir, "onnx")
    if onnx_file is None:
        if not _export_via_subprocess(local_dir, onnx_dir, log) or find_onnx_file(onnx_dir, "onnx") is None:
            return _fallback_torch(local_dir, threads, log, f"ONNX 模型不存在且导出失败 ({onnx_dir})")
        onnx_file = find_onnx_file(onnx_dir, "onnx")

    tok_dir = onnx_dir if (onnx_dir / "tokenizer.json").is_file() else Path(local_dir)
    if not (tok_dir / "tokenizer.json").is_file():
        return _fallback_torch(local_dir, threads, log, f"缺少 tokenizer.json ({onnx_dir})")
    return OnnxBgeEmbedder(onnx_file, tok_dir, threads)


def embedding_dimension(embedder) -> int:
    """优先新接口 get_embedding_dimension (sentence-transformers 新版已重命名，避免 FutureWarning)。"""
    fn = getattr(embedder, "get_embedding_dimension", None)
    if callable(fn):
        return int(fn())
    return int(embedder.get_sentence_embedding_dimension())


# ----------------------------------------------------------------------------- CLI
def _main(argv=None) -> int:
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="python -m embedding_backend", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--export", nargs="+", metavar="DIR", help="<model_dir> [<onnx_dir>] 导出 ONNX fp32")
    g.add_argument("--check", nargs="?", const=str(DEFAULT_MODEL_DIR), metavar="MODEL_DIR",
                   help="加载后端并打印 backend / dim / threads / RSS")
    ap.add_argument("--int8", action="store_true", help="导出时额外生成 model_int8.onnx (需要 pip install onnx)")
    ap.add_argument("--no-verify", action="store_true", help="导出后跳过与 torch 的一致性校验")
    ap.add_argument("--backend", default=None, help="--check 时覆盖 EMBEDDING_BACKEND")
    a = ap.parse_args(argv)

    if a.export:
        model_dir = a.export[0]
        onnx_dir = a.export[1] if len(a.export) > 1 else str(onnx_dir_for(model_dir))
        try:
            export_onnx(model_dir, onnx_dir, int8=a.int8, verify=not a.no_verify)
        except Exception as e:
            print(f"[export] 失败: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            return 1
        return 0

    t0 = time.time()
    emb = load_embedder(a.check, backend=a.backend, log=lambda m: print(m, flush=True))
    t_load = time.time() - t0
    emb.encode(["warm up"])
    t1 = time.perf_counter()
    v = emb.encode(["如何重建向量索引"], batch_size=16, normalize_embeddings=True)
    ms = (time.perf_counter() - t1) * 1000
    r = rss_mb()
    print(json.dumps({"backend": getattr(emb, "backend_name", "torch"), "dim": embedding_dimension(emb),
                      "threads": getattr(emb, "threads", None), "load_s": round(t_load, 2),
                      "query_ms": round(ms, 2), "norm": round(float((v[0] ** 2).sum() ** 0.5), 6),
                      "rss_mb": round(r, 1) if r is not None else None,
                      "torch_imported": "torch" in sys.modules}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
