"""Download GGUF models listed in llm_models.json (entries with a "download" field).

Usage (Windows, from the repo root):
    python scratch\\download_model.py          # list models and download status
    python scratch\\download_model.py 4        # Qwen3 4B
    python scratch\\download_model.py 4 5      # Qwen3 4B + 8B
    python scratch\\download_model.py missing  # everything not downloaded yet

Resumable (.part file + HTTP Range), tries hf-mirror.com first then huggingface.co.
Set HF_ENDPOINT to force a mirror. Stdlib only.
"""
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "llm_models.json")
MIRRORS = [os.environ["HF_ENDPOINT"]] if os.environ.get("HF_ENDPOINT") else ["https://hf-mirror.com", "https://huggingface.co"]
CHUNK = 4 * 1024 * 1024

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def resolve_path(p: str) -> str:
    p = os.path.expanduser(os.path.expandvars(p))
    if not os.path.isabs(p):
        p = os.path.normpath(os.path.join(ROOT, p))
    return p


def load_models():
    with open(CONFIG, "r", encoding="utf-8-sig") as f:
        models = json.load(f)["models"]
    for m in models:
        if "path" in m:
            m["path"] = resolve_path(m["path"])
    return models


def remote_size(url):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return int(r.headers.get("Content-Length") or 0)


def download(model):
    d = model["download"]
    dest = model["path"]
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest):
        print(f"✔ 已存在: {dest}")
        return True
    part = dest + ".part"
    for base in MIRRORS:
        url = f"{base.rstrip('/')}/{d['repo']}/resolve/main/{d['file']}"
        try:
            total = remote_size(url)
        except Exception as e:
            print(f"  ⚠ {base} 不可用: {e}")
            continue
        for attempt in range(1, 6):
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if total and have >= total:
                break
            headers = {"User-Agent": "Mozilla/5.0"}
            if have:
                headers["Range"] = f"bytes={have}-"
            print(f"⬇ {model['name']} ← {url}\n  已下载 {have / 2**30:.2f} / {total / 2**30:.2f} GB (第 {attempt} 次尝试)")
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r, \
                        open(part, "ab" if have and r.status == 206 else "wb") as f:
                    if have and r.status != 206:
                        have = 0
                    t0, done = time.time(), have
                    while True:
                        buf = r.read(CHUNK)
                        if not buf:
                            break
                        f.write(buf)
                        done += len(buf)
                        el = max(time.time() - t0, 1e-6)
                        speed = (done - have) / el / 2**20
                        pct = done * 100 / total if total else 0
                        print(f"\r  {pct:5.1f}%  {done / 2**30:.2f} GB  {speed:.1f} MB/s   ", end="", flush=True)
                print()
            except Exception as e:
                print(f"\n  ⚠ 中断: {e}，5 秒后续传…")
                time.sleep(5)
                continue
            break
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if total and have == total:
            os.replace(part, dest)
            print(f"✔ 完成: {dest}")
            return True
        print(f"  ⚠ 未完成 ({have}/{total})，尝试下一个镜像")
    print(f"❌ 下载失败: {model['name']}")
    return False


def main(argv):
    models = [m for m in load_models() if m.get("download")]
    if not argv:
        for m in models:
            state = "已下载" if os.path.exists(m["path"]) else f"未下载 (~{m['download'].get('size_gb', '?')} GB)"
            print(f"[{m['key']}] {m['name']:<12} {state}")
        print("\n用法: python scratch\\download_model.py <序号...|missing>")
        return 0
    if argv == ["missing"]:
        todo = [m for m in models if not os.path.exists(m["path"])]
    else:
        todo = [m for m in models if m["key"] in argv or m["id"] in argv]
    ok = all(download(m) for m in todo) if todo else True
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
