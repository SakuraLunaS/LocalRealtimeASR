"""Local, non-secret configuration for LocalASR V2.

The only configuration required by the initializer is an optional SOCKS5
proxy.  It is stored outside the repository:

    %APPDATA%\\LocalASRV2\\config.json

The main recognition program does not need the proxy at runtime because the
model runs entirely offline.  Never commit this file to GitHub.
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.parse
from pathlib import Path

from runtime_setup import APP_NAME


def appdata_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ".config" / APP_NAME


def config_path() -> Path:
    return appdata_dir() / "config.json"


def load_config() -> dict:
    path = config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(data: dict) -> None:
    directory = appdata_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = config_path()
    fd, temp_name = tempfile.mkstemp(
        prefix="config-", suffix=".tmp", dir=str(directory)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except OSError:
            pass


def normalize_proxy(value: str) -> str:
    """Validate and normalize a SOCKS5 proxy URL.

    Accepts ``127.0.0.1:7897``, ``socks5://127.0.0.1:7897`` and
    ``socks5h://user:pass@host:port``.  Returns an empty string for disabled.
    """

    value = (value or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = "socks5h://" + value
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("socks5", "socks5h"):
        raise ValueError("只支持 socks5:// 或 socks5h:// 代理")
    if not parsed.hostname:
        raise ValueError("代理地址缺少主机名")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("代理端口无效") from exc
    if port is None:
        raise ValueError("代理地址需要包含端口，例如 127.0.0.1:7897")
    if not (1 <= port <= 65535):
        raise ValueError("代理端口必须在 1–65535 之间")
    # rebuilding also strips stray whitespace and keeps optional credentials
    netloc = ""
    if parsed.username:
        netloc += urllib.parse.quote(parsed.username, safe="")
        if parsed.password:
            netloc += ":" + urllib.parse.quote(parsed.password, safe="")
        netloc += "@"
    netloc += parsed.hostname
    netloc += f":{port}"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, "", "", ""))


def get_proxy() -> str:
    return normalize_proxy(str(load_config().get("proxy", "")))


def set_proxy(proxy: str) -> None:
    data = load_config()
    data["proxy"] = normalize_proxy(proxy)
    data["schema_version"] = 1
    save_config(data)


def masked_proxy(proxy: str) -> str:
    """Proxy URL safe to show in logs (credentials masked)."""

    if not proxy:
        return "直连"
    try:
        parsed = urllib.parse.urlsplit(proxy)
        host = parsed.hostname or "?"
        port = parsed.port or 0
        auth = "***@" if parsed.username else ""
        return f"{parsed.scheme}://{auth}{host}:{port}"
    except ValueError:
        return "(无效代理)"
