import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests
import time

TARGET_DIR = os.path.abspath("models/laya-typed-decisions")
os.makedirs(os.path.join(TARGET_DIR, "encoder"), exist_ok=True)
os.makedirs(os.path.join(TARGET_DIR, "tokenizer"), exist_ok=True)

BASE_URL = "https://hf-mirror.com/convaiinnovations/laya-typed-decisions/resolve/main"

FILES = [
    ("rl_agent_config.json", "rl_agent_config.json"),
    ("encoder/config.json", "encoder/config.json"),
    ("tokenizer/tokenizer_config.json", "tokenizer/tokenizer_config.json"),
    ("tokenizer/tokenizer.json", "tokenizer/tokenizer.json"),
    ("model.safetensors", "model.safetensors"),
]

print(f"Target folder: {TARGET_DIR}")

session = requests.Session()
adapter = requests.adapters.HTTPAdapter(max_retries=5)
session.mount("https://", adapter)

for remote_rel, local_rel in FILES:
    target_path = os.path.join(TARGET_DIR, local_rel)
    url = f"{BASE_URL}/{remote_rel}"
    
    # Check if already downloaded
    remote_head = session.head(url, timeout=10)
    expected_size = int(remote_head.headers.get("content-length", 0))
    
    if os.path.exists(target_path):
        current_size = os.path.getsize(target_path)
        if expected_size > 0 and current_size == expected_size:
            print(f"✔ [Already exists] {local_rel} ({current_size:,} bytes)")
            continue
            
    print(f"⏳ Downloading {local_rel} ({expected_size:,} bytes)...")
    start_t = time.time()
    
    # Resume support
    headers = {}
    mode = "wb"
    existing_len = 0
    if os.path.exists(target_path + ".tmp"):
        existing_len = os.path.getsize(target_path + ".tmp")
        if existing_len < expected_size:
            headers["Range"] = f"bytes={existing_len}-"
            mode = "ab"
            print(f"  Resuming from {existing_len:,} bytes...")
            
    with session.get(url, headers=headers, stream=True, timeout=30) as r:
        r.raise_for_status()
        downloaded = existing_len
        with open(target_path + ".tmp", mode) as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if expected_size > 0 and downloaded % (20 * 1024 * 1024) < len(chunk):
                        pct = (downloaded / expected_size) * 100
                        speed = downloaded / (time.time() - start_t + 0.1) / (1024 * 1024)
                        print(f"  -> {downloaded / 1024 / 1024:.1f} MB / {expected_size / 1024 / 1024:.1f} MB ({pct:.1f}%) @ {speed:.2f} MB/s", flush=True)

    if os.path.exists(target_path):
        os.remove(target_path)
    os.rename(target_path + ".tmp", target_path)
    elapsed = time.time() - start_t
    print(f"✔ Downloaded {local_rel} in {elapsed:.1f}s")

print("\n🎉 All Laya-typed-decisions model files are ready in:", TARGET_DIR)
