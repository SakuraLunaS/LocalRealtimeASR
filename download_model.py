"""Command-line model downloader (advanced use).

The normal setup flow is the GUI:  python setup_wizard.py  (or init.bat)

Examples:
    python download_model.py
    python download_model.py --proxy socks5h://127.0.0.1:7897
    python download_model.py --dest D:\\models\\faster-whisper-large-v3-turbo
"""

from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path

from app_config import get_proxy, masked_proxy, normalize_proxy
from model_downloader import (
    DownloadCancelled,
    DownloadError,
    DownloadProgress,
    ModelDownloader,
)
from runtime_setup import DEFAULT_MODEL_DIR, DEFAULT_MODEL_ID


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download and verify faster-whisper large-v3-turbo."
    )
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--revision", default="master")
    parser.add_argument("--dest", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument(
        "--proxy",
        default=None,
        help="SOCKS5 proxy, e.g. socks5h://127.0.0.1:7897. "
        "Defaults to the proxy saved by the setup wizard.",
    )
    args = parser.parse_args()

    try:
        proxy = normalize_proxy(args.proxy if args.proxy is not None else get_proxy())
    except ValueError as exc:
        print(f"代理参数错误：{exc}")
        return 2

    cancel = threading.Event()
    last_line = 0.0

    def on_progress(progress: DownloadProgress) -> None:
        nonlocal last_line
        now = time.monotonic()
        if now - last_line < 0.2 and progress.percent < 100:
            return
        last_line = now
        print(f"\r[{progress.percent:5.1f}%] {progress.message}          ", end="", flush=True)

    def on_log(message: str) -> None:
        print(f"\n{message}")

    downloader = ModelDownloader(
        model_id=args.model_id,
        revision=args.revision,
        destination=args.dest,
        proxy=proxy,
        progress_callback=on_progress,
        log_callback=on_log,
        cancel_event=cancel,
    )
    try:
        print(f"Model : {args.model_id}")
        print(f"Target: {Path(args.dest).resolve()}")
        print(f"Proxy : {masked_proxy(proxy)}")
        downloader.download()
        print("\n全部模型文件已下载并通过 SHA256 校验。")
        print("启动主程序：run.bat（或 python app.py）")
        return 0
    except KeyboardInterrupt:
        cancel.set()
        print("\n已中断；.part 文件已保留，下次可续传。")
        return 130
    except (DownloadError, DownloadCancelled) as exc:
        print(f"\n下载失败：{exc}")
        return 1
    finally:
        downloader.close()


if __name__ == "__main__":
    raise SystemExit(main())
