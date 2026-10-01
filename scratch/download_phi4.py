import os
import sys
import io
import time
import urllib.request
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# Set UTF-8 encoding for Windows console output
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST_DIR = os.path.join(ROOT_DIR, "models")
DEST_FILE = os.path.join(DEST_DIR, "microsoft_Phi-4-mini-instruct-Q4_K_M.gguf")
PART_FILE = DEST_FILE + ".part"
EXPECTED_SIZE = 2491874688  # ~2.32 GB
NUM_THREADS = 8
CHUNK_SIZE = 8 * 1024 * 1024  # 8MB per slice

def get_file_size():
    req = urllib.request.Request(URL, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as res:
        return int(res.headers.get("Content-Length", EXPECTED_SIZE))

def download_range(start, end, file_handle, lock, progress_tracker):
    max_retries = 5
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                URL,
                headers={"User-Agent": "Mozilla/5.0", "Range": f"bytes={start}-{end}"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                if len(data) != (end - start + 1):
                    raise IOError(f"Incomplete chunk: expected {end - start + 1}, got {len(data)}")
                with lock:
                    file_handle.seek(start)
                    file_handle.write(data)
                    progress_tracker["downloaded"] += len(data)
                return True
        except Exception as e:
            if attempt == max_retries - 1:
                print(f"\n[Error] Chunk {start}-{end} failed: {e}", flush=True)
                raise
            time.sleep(1 + attempt)

def main():
    os.makedirs(DEST_DIR, exist_ok=True)
    if os.path.exists(DEST_FILE) and os.path.getsize(DEST_FILE) == EXPECTED_SIZE:
        print(f"[Done] Model file already exists: {DEST_FILE} ({EXPECTED_SIZE/1024**3:.2f} GB)")
        return

    print("=" * 60)
    print("  Microsoft Phi-4-mini (3.8B · Q4_K_M) 多线程高速下载引擎")
    print(f"  目标路径: {DEST_FILE}")
    print("=" * 60)

    total_size = get_file_size()
    print(f"文件大小: {total_size} 字节 ({total_size / 1024**3:.2f} GB)")

    # Pre-allocate sparse/zero file
    if not os.path.exists(PART_FILE) or os.path.getsize(PART_FILE) != total_size:
        print("正在预分配目标文件空间...")
        with open(PART_FILE, "wb") as f:
            f.seek(total_size - 1)
            f.write(b"\0")

    file_handle = open(PART_FILE, "r+b")
    file_lock = threading.Lock()
    progress_tracker = {"downloaded": 0}

    # Slice into chunks
    chunks = []
    for start in range(0, total_size, CHUNK_SIZE):
        end = min(start + CHUNK_SIZE - 1, total_size - 1)
        chunks.append((start, end))

    print(f"切分任务块: {len(chunks)} 块 (每块 {CHUNK_SIZE/1024/1024:.0f} MB), 并发线程: {NUM_THREADS}")
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=NUM_THREADS) as executor:
        futures = {executor.submit(download_range, s, e, file_handle, file_lock, progress_tracker): (s, e) for s, e in chunks}

        while any(not f.done() for f in futures):
            time.sleep(1.0)
            elapsed = time.time() - start_time
            downloaded = progress_tracker["downloaded"]
            pct = (downloaded / total_size) * 100
            speed_mb = (downloaded / (1024 * 1024)) / elapsed if elapsed > 0 else 0
            remaining_bytes = total_size - downloaded
            eta_sec = remaining_bytes / (speed_mb * 1024 * 1024) if speed_mb > 0 else 0
            print(f"\r进度: {pct:5.1f}% | 已下载: {downloaded/1024**2:7.1f} MB / {total_size/1024**2:.1f} MB | 速度: {speed_mb:5.1f} MB/s | 剩余时间: {eta_sec:4.0f}s", end="", flush=True)

        for f in as_completed(futures):
            f.result()

    file_handle.close()
    if os.path.exists(DEST_FILE):
        os.remove(DEST_FILE)
    os.rename(PART_FILE, DEST_FILE)

    total_time = time.time() - start_time
    print(f"\n\n✔ 下载完成! 用时: {total_time:.1f} 秒, 平均速度: {total_size/1024**2/total_time:.1f} MB/s")
    print(f"✔ 校验完整度: {os.path.getsize(DEST_FILE)} 字节")

if __name__ == "__main__":
    main()
