"""Runtime paths and Windows CUDA DLL discovery.

This module must be imported *before* faster_whisper/ctranslate2 when the
CUDA libraries come from the pip packages (nvidia-cublas-cu12 and
nvidia-cudnn-cu12).  Windows does not search site-packages for DLLs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

APP_NAME = "LocalASRV2"
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_MODEL_DIR = BASE_DIR / "models" / "faster-whisper-large-v3-turbo"
DEFAULT_SAMPLE = BASE_DIR / "samples" / "jfk.wav"
DEFAULT_MODEL_ID = "pengzhendong/faster-whisper-large-v3-turbo"


def _site_packages() -> Path:
    import sysconfig

    return Path(sysconfig.get_paths()["purelib"])


def add_cuda_dll_directories() -> list[Path]:
    """Make pip-provided CUDA/cuDNN DLLs visible to CTranslate2.

    Returns the directories that were found.  Safe to call repeatedly and on
    non-Windows systems (it is a no-op there).
    """

    if sys.platform != "win32":
        return []

    candidates: list[Path] = []
    nvidia = _site_packages() / "nvidia"
    for rel in (
        ("cublas", "bin"),
        ("cudnn", "bin"),
        ("cuda_nvrtc", "bin"),
    ):
        candidates.append(nvidia.joinpath(*rel))

    cuda_path = os.environ.get("CUDA_PATH")
    if cuda_path:
        candidates.append(Path(cuda_path) / "bin")

    found: list[Path] = []
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    for directory in candidates:
        if not directory.is_dir():
            continue
        try:
            # Keep the handles alive for the lifetime of the process.
            os.add_dll_directory(str(directory))
        except (OSError, AttributeError):
            pass
        text = str(directory)
        if text not in path_parts:
            os.environ["PATH"] = text + os.pathsep + os.environ.get("PATH", "")
            path_parts.insert(0, text)
        found.append(directory)
    return found


def no_window_flag() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def nvidia_smi_value(fields: str) -> str | None:
    """Query nvidia-smi without opening a console window."""

    if sys.platform != "win32":
        return None
    try:
        result = subprocess.run(
            ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=no_window_flag(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def gpu_memory_text() -> str:
    value = nvidia_smi_value("memory.used,memory.total")
    if not value:
        return "显存不可读"
    first = value.splitlines()[0]
    parts = [part.strip() for part in first.split(",")]
    if len(parts) >= 2:
        try:
            used = int(parts[0]) / 1024
            total = int(parts[1]) / 1024
            return f"显存 {used:.1f}/{total:.1f} GB"
        except ValueError:
            pass
    return "显存不可读"
