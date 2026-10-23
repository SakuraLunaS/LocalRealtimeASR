"""ModelScope downloader used by both the CLI and the setup wizard.

Features:
- direct download or SOCKS5 proxy (requests + PySocks)
- resume via HTTP Range and .part files
- pinned SHA256 trust anchors
- callbacks suitable for a Qt progress bar
- safe cancellation that keeps partial files for a later resume
"""

from __future__ import annotations

import hashlib
import shutil
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import requests

from app_config import normalize_proxy
from runtime_setup import DEFAULT_MODEL_DIR, DEFAULT_MODEL_ID

API = (
    "https://www.modelscope.cn/api/v1/models/"
    "{model_id}/repo/files?Revision={revision}&Root="
)
RESOLVE = (
    "https://www.modelscope.cn/models/{model_id}/resolve/{revision}/{filename}"
)
USER_AGENT = "LocalASRV2/2.0 (local offline Windows app)"

WANTED = (
    "model.bin",
    "config.json",
    "preprocessor_config.json",
    "tokenizer.json",
    "vocabulary.json",
    "README.md",
)

# Pinned revision trust anchor. The downloader refuses files whose API hash
# differs from these values.
PINNED_SHA256 = {
    "model.bin": "e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da",
    "config.json": "a0feddc18de0c285ed147e5483c8d1bc911bd45e23104ae0726d79594e7a6b1d",
    "preprocessor_config.json": "7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711",
    "tokenizer.json": "b3c8202bbf06d8ee4232c5984baa563784ac4737e2e7fdc42fa180200d3cfcdb",
    "vocabulary.json": "c69260f2ab26d659b7c398f9a2b2b48ed0df16c3b47d7326782fd9cba71690c1",
    "README.md": "5d11569097395622e37deb136f3177d1d07753177d828ee300068711bbce3ba2",
}


class DownloadCancelled(Exception):
    pass


class DownloadError(Exception):
    pass


@dataclass(frozen=True)
class DownloadProgress:
    percent: float
    message: str
    filename: str = ""
    file_done: int = 0
    file_total: int = 0
    overall_done: int = 0
    overall_total: int = 0
    bytes_per_second: float = 0.0


def human_size(size: float) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ModelDownloader:
    def __init__(
        self,
        *,
        model_id: str = DEFAULT_MODEL_ID,
        revision: str = "master",
        destination: Path | str = DEFAULT_MODEL_DIR,
        proxy: str = "",
        progress_callback: Callable[[DownloadProgress], None] | None = None,
        log_callback: Callable[[str], None] | None = None,
        cancel_event=None,
    ) -> None:
        self.model_id = model_id
        self.revision = revision
        self.destination = Path(destination)
        self.proxy = normalize_proxy(proxy)
        self._progress_callback = progress_callback
        self._log_callback = log_callback
        self._cancel = cancel_event
        self._last_emit = 0.0

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        # Do not silently inherit HTTP(S)_PROXY from the environment; the user
        # chose direct or an explicit SOCKS5 proxy in the GUI.
        self.session.trust_env = False
        if self.proxy:
            self.session.proxies.update({"http": self.proxy, "https": self.proxy})

    # ---- callbacks / helpers ------------------------------------------
    def _log(self, message: str) -> None:
        if self._log_callback:
            self._log_callback(message)

    def _emit(self, progress: DownloadProgress, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_emit >= 0.15:
            self._last_emit = now
            if self._progress_callback:
                self._progress_callback(progress)

    def _check_cancel(self) -> None:
        if self._cancel is not None and self._cancel.is_set():
            raise DownloadCancelled("用户取消了下载")

    def _url_for(self, filename: str) -> str:
        return RESOLVE.format(
            model_id=self.model_id,
            revision=urllib.parse.quote(self.revision, safe=""),
            filename=urllib.parse.quote(filename, safe=""),
        )

    # ---- network ------------------------------------------------------
    def test_connection(self) -> str:
        self._check_cancel()
        url = API.format(
            model_id=self.model_id,
            revision=urllib.parse.quote(self.revision, safe=""),
        )
        try:
            response = self.session.get(url, timeout=(10, 25))
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise DownloadError(f"连接 ModelScope 失败：{exc}") from exc
        except ValueError as exc:
            raise DownloadError(f"ModelScope 返回了无效数据：{exc}") from exc
        if not payload.get("Success", True):
            raise DownloadError(f"ModelScope API 错误：{payload}")
        count = len(payload.get("Data", {}).get("Files", []))
        return f"连接成功，模型仓库包含 {count} 个文件"

    def list_files(self) -> dict:
        self._check_cancel()
        url = API.format(
            model_id=self.model_id,
            revision=urllib.parse.quote(self.revision, safe=""),
        )
        try:
            response = self.session.get(url, timeout=(10, 30))
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise DownloadError(
                "无法获取 ModelScope 文件列表，请检查网络或 SOCKS5 代理。\n"
                f"详细信息：{exc}"
            ) from exc
        except ValueError as exc:
            raise DownloadError(f"ModelScope 返回了无效 JSON：{exc}") from exc
        if not payload.get("Success", True):
            raise DownloadError(f"ModelScope API 错误：{payload}")
        files = {
            item["Name"]: item
            for item in payload.get("Data", {}).get("Files", [])
            if item.get("Type") == "blob"
        }
        missing = [name for name in WANTED if name not in files]
        if missing:
            raise DownloadError(f"仓库缺少预期文件：{', '.join(missing)}")
        for name in WANTED:
            api_hash = str(files[name].get("Sha256") or "")
            pinned = PINNED_SHA256[name]
            if api_hash and api_hash.lower() != pinned.lower():
                raise DownloadError(
                    f"{name} 的上游 SHA256 与固定值不一致，已拒绝下载。\n"
                    f"期望：{pinned}\n实际：{api_hash}"
                )
        return files

    # ---- files --------------------------------------------------------
    def _download_once(
        self,
        filename: str,
        part: Path,
        expected_size: int,
        overall_done: int,
        overall_total: int,
    ) -> None:
        self._check_cancel()
        existing = part.stat().st_size if part.exists() else 0
        if expected_size and existing > expected_size:
            part.unlink()
            existing = 0
        if expected_size and existing == expected_size:
            return

        headers = {"Accept-Encoding": "identity"}
        if existing:
            headers["Range"] = f"bytes={existing}-"
        try:
            response = self.session.get(
                self._url_for(filename),
                headers=headers,
                stream=True,
                timeout=(15, 90),
            )
        except requests.RequestException as exc:
            raise DownloadError(f"下载请求失败：{exc}") from exc
        with response:
            if response.status_code == 416 and expected_size and existing == expected_size:
                return
            if response.status_code not in (200, 206):
                raise DownloadError(
                    f"下载 {filename} 失败：HTTP {response.status_code}"
                )
            mode = "ab"
            done = existing
            if existing and response.status_code != 206:
                # Server ignored Range; restart from zero.
                mode = "wb"
                done = 0
            started = time.monotonic()
            last_emit = 0.0
            with part.open(mode) as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    self._check_cancel()
                    if not chunk:
                        continue
                    handle.write(chunk)
                    done += len(chunk)
                    now = time.monotonic()
                    if now - last_emit >= 0.15:
                        last_emit = now
                        speed = done / max(now - started, 1e-6)
                        file_pct = (done * 100 / expected_size) if expected_size else 0
                        overall = (
                            (overall_done + done) * 100 / overall_total
                            if overall_total
                            else 0
                        )
                        self._emit(
                            DownloadProgress(
                                percent=max(0.0, min(100.0, overall)),
                                message=(
                                    f"正在下载 {filename}：{file_pct:.0f}%"
                                    f"（{human_size(done)} / {human_size(expected_size)}"
                                    f"，{human_size(speed)}/s）"
                                ),
                                filename=filename,
                                file_done=done,
                                file_total=expected_size,
                                overall_done=overall_done + done,
                                overall_total=overall_total,
                                bytes_per_second=speed,
                            )
                        )
        if expected_size and part.stat().st_size != expected_size:
            raise DownloadError(
                f"{filename} 下载不完整：{part.stat().st_size} / {expected_size} 字节"
            )

    def _download_file(
        self,
        filename: str,
        expected_size: int,
        expected_hash: str,
        overall_done: int,
        overall_total: int,
    ) -> None:
        target = self.destination / filename
        if target.is_file() and expected_size and target.stat().st_size == expected_size:
            self._log(f"[校验] {filename}")
            if sha256_file(target).lower() == expected_hash.lower():
                self._log(f"[完成] {filename} 已存在且校验通过")
                self._emit(
                    DownloadProgress(
                        percent=(overall_done + expected_size) * 100 / overall_total,
                        message=f"{filename} 已存在且校验通过",
                        filename=filename,
                        file_done=expected_size,
                        file_total=expected_size,
                        overall_done=overall_done + expected_size,
                        overall_total=overall_total,
                    ),
                    force=True,
                )
                return
            self._log(f"[重下] {filename} 校验失败，重新下载")
            target.unlink()
        elif target.exists():
            target.unlink()

        part = self.destination / (filename + ".part")
        self._log(f"[下载] {filename} ({human_size(expected_size)})")
        last_error: Exception | None = None
        for attempt in range(1, 5):
            self._check_cancel()
            try:
                self._download_once(
                    filename, part, expected_size, overall_done, overall_total
                )
                self._log(f"[校验] {filename} SHA256…")
                self._emit(
                    DownloadProgress(
                        percent=(overall_done + expected_size) * 100 / overall_total,
                        message=f"正在校验 {filename}…",
                        filename=filename,
                        file_done=expected_size,
                        file_total=expected_size,
                        overall_done=overall_done + expected_size,
                        overall_total=overall_total,
                    ),
                    force=True,
                )
                actual = sha256_file(part)
                if actual.lower() != expected_hash.lower():
                    part.unlink(missing_ok=True)
                    raise DownloadError(
                        f"{filename} SHA256 校验失败：{actual}"
                    )
                shutil.move(str(part), str(target))
                self._log(f"[完成] {filename}")
                self._emit(
                    DownloadProgress(
                        percent=(overall_done + expected_size) * 100 / overall_total,
                        message=f"{filename} 完成",
                        filename=filename,
                        file_done=expected_size,
                        file_total=expected_size,
                        overall_done=overall_done + expected_size,
                        overall_total=overall_total,
                    ),
                    force=True,
                )
                return
            except DownloadCancelled:
                raise
            except Exception as exc:
                last_error = exc
                self._log(f"[重试 {attempt}/4] {filename}: {exc}")
                if attempt < 4:
                    time.sleep(min(attempt * 2, 6))
        raise DownloadError(f"{filename} 下载失败：{last_error}")

    def download(self) -> Path:
        self.destination.mkdir(parents=True, exist_ok=True)
        self._emit(
            DownloadProgress(percent=0, message="正在连接 ModelScope…"),
            force=True,
        )
        files = self.list_files()
        total = sum(int(files[name].get("Size") or 0) for name in WANTED)
        done = 0
        self._log(
            f"目标目录：{self.destination}\n"
            f"总计：{human_size(total)}；网络：{'SOCKS5 代理' if self.proxy else '直连'}"
        )
        for name in WANTED:
            info = files[name]
            size = int(info.get("Size") or 0)
            expected_hash = PINNED_SHA256[name]
            self._download_file(name, size, expected_hash, done, total)
            done += size
        self._emit(
            DownloadProgress(
                percent=100,
                message="全部模型文件已下载并通过 SHA256 校验",
                overall_done=total,
                overall_total=total,
            ),
            force=True,
        )
        self._log("全部模型文件已就绪。")
        return self.destination

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> ModelDownloader:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
