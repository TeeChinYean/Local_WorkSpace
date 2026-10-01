import os
import sys
import io
import time
import urllib.request
import concurrent.futures

# Set UTF-8 encoding for Windows console output
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST_DIR = os.path.join(ROOT_DIR, "models")
DEST_FILE = os.path.join(DEST_DIR, "Qwen2.5-7B-Instruct-Q3_K_S.gguf")
PART_FILE = DEST_FILE + ".part"
EXPECTED_SIZE = 3492369088  # ~3.25 GB
NUM_THREADS = 8
CHUNK_SIZE = 8 * 1024 * 1024  # 8MB per task slice

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
                print(f"\n[Error] Chunk {start}-{end} failed after {max_retries} attempts: {e}", flush=True)
                raise
            time.sleep(1 + attempt)

def main():
    os.makedirs(DEST_DIR, exist_ok=True)
    if os.path.exists(DEST_FILE) and os.path.getsize(DEST_FILE) == EXPECTED_SIZE:
        print(f"[Done] Model file already exists and size matches: {DEST_FILE} ({EXPECTED_SIZE/1024**3:.2f} GB)")
        return

    print("=" * 60)
    print("  Qwen 2.5 7B-Instruct (Q3_K_S) 高速多线程多路下载引擎")
    print(f"  目标文件: {DEST_FILE}")
    print("=" * 60)

    total_size = get_file_size()
    print(f"远程文件大小确认: {total_size} 字节 ({total_size / 1024**3:.2f} GB)")

    # Pre-allocate sparse file if not exists
    if not os.path.exists(PART_FILE) or os.path.getsize(PART_FILE) != total_size:
        print("正在预分配目标磁盘空间...")
        with open(PART_FILE, "wb") as f:
            f.seek(total_size - 1)
            f.write(b"\0")

    import threading
    lock = threading.Lock()
    progress_tracker = {"downloaded": 0, "start_time": time.time()}

    # Divide into slices
    ranges = []
    curr = 0
    while curr < total_size:
        end = min(curr + CHUNK_SIZE - 1, total_size - 1)
        ranges.append((curr, end))
        curr = end + 1

    print(f"分片总数: {len(ranges)} 块 (每块 {CHUNK_SIZE/1024**2:.1f} MB)，并发线程: {NUM_THREADS}")
    t_start = time.time()

    with open(PART_FILE, "r+b") as fh:
        with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_THREADS) as executor:
            future_to_range = {
                executor.submit(download_range, r[0], r[1], fh, lock, progress_tracker): r
                for r in ranges
            }

            done_count = 0
            for future in concurrent.futures.as_completed(future_to_range):
                future.result()
                done_count += 1
                dl = progress_tracker["downloaded"]
                elapsed = time.time() - t_start
                speed = (dl / (1024 * 1024)) / max(0.1, elapsed)
                pct = (dl / total_size) * 100
                rem_bytes = total_size - dl
                eta_sec = rem_bytes / max(1, (dl / max(0.1, elapsed)))
                print(
                    f"\r下载进度: [{pct:5.1f}%] {dl/1024**2:.1f} MB / {total_size/1024**2:.1f} MB "
                    f"| 速度: {speed:5.1f} MB/s | 剩余时间: {eta_sec:4.0f}s ({done_count}/{len(ranges)})",
                    end="",
                    flush=True
                )

    print("\n\n✔ 下载完成，正在校验文件完整性...")
    if os.path.getsize(PART_FILE) == total_size:
        if os.path.exists(DEST_FILE):
            os.remove(DEST_FILE)
        os.rename(PART_FILE, DEST_FILE)
        print(f"🎉 文件校验成功！已就绪: {DEST_FILE} ({total_size / 1024**3:.2f} GB)")
    else:
        print("❌ 文件大小不一致，请重试！")

if __name__ == "__main__":
    main()
