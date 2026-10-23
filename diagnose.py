"""GPU / local model verification for Phase A.

Example:
    python diagnose.py
    python diagnose.py --audio samples\\jfk.wav --beam-size 5
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import platform
import sys
import time
import traceback
from pathlib import Path

import numpy as np

from runtime_setup import (
    DEFAULT_MODEL_DIR,
    DEFAULT_SAMPLE,
    add_cuda_dll_directories,
    gpu_memory_text,
    nvidia_smi_value,
)

add_cuda_dll_directories()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify faster-whisper on the local NVIDIA GPU.")
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument(
        "--audio",
        type=Path,
        action="append",
        help="English audio file to transcribe (wav/mp3/m4a). Default: samples/jfk.wav",
    )
    parser.add_argument("--compute-type", default="float16")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--skip-warmup", action="store_true")
    args = parser.parse_args()

    print("=" * 72)
    print("Local ASR diagnostics")
    print("=" * 72)
    print(f"Python         : {sys.version.split()[0]} ({platform.architecture()[0]})")
    print(f"Executable     : {sys.executable}")
    print(f"Platform       : {platform.platform()}")
    print(f"Model directory: {args.model_dir}")
    print(f"GPU (nvidia-smi): {nvidia_smi_value('name,driver_version,memory.total') or 'unavailable'}")
    print(f"VRAM before    : {gpu_memory_text()}")

    try:
        import ctranslate2
        import faster_whisper

        print(f"ctranslate2    : {ctranslate2.__version__}")
        print(f"faster-whisper : {faster_whisper.__version__}")
        print(f"CUDA devices   : {ctranslate2.get_cuda_device_count()}")
        print(f"CUDA compute   : {sorted(ctranslate2.get_supported_compute_types('cuda'))}")
    except Exception as exc:
        print(f"ERROR importing runtime: {exc}")
        return 1

    model_bin = args.model_dir / "model.bin"
    if not model_bin.is_file():
        print(f"\nERROR: model not found: {model_bin}")
        print("Run: python download_model.py")
        return 1
    print(f"model.bin bytes: {model_bin.stat().st_size}")
    print("Hashing model.bin (optional integrity check)…")
    print(f"model.bin SHA256: {sha256_file(model_bin)}")
    print(f"VRAM before load: {gpu_memory_text()}")

    model = None
    try:
        from faster_whisper import WhisperModel

        print("\nLoading model on CUDA (float16, local files only)…")
        started = time.perf_counter()
        model = WhisperModel(
            str(args.model_dir),
            device="cuda",
            device_index=0,
            compute_type=args.compute_type,
            local_files_only=True,
            num_workers=1,
        )
        print(f"Load time       : {time.perf_counter() - started:.2f} s")
        print(f"VRAM after load : {gpu_memory_text()}")

        if not args.skip_warmup:
            print("Warming up the GPU (the first inference is much slower)…")
            started = time.perf_counter()
            segments, _ = model.transcribe(
                np.zeros(16000, dtype=np.float32),
                language="en",
                beam_size=1,
                condition_on_previous_text=False,
                vad_filter=False,
                without_timestamps=True,
            )
            for _ in segments:
                pass
            print(f"Warm-up time    : {time.perf_counter() - started:.2f} s")
            print(f"VRAM after warm : {gpu_memory_text()}")

        audios = args.audio or ([DEFAULT_SAMPLE] if DEFAULT_SAMPLE.exists() else [])
        if not audios:
            print("No audio supplied and samples/jfk.wav is missing.")
            return 2

        for audio in audios:
            if not audio.is_file():
                print(f"Audio not found: {audio}")
                continue
            print(f"\nTranscribing: {audio}")
            started = time.perf_counter()
            segments, info = model.transcribe(
                str(audio),
                language="en",
                task="transcribe",
                beam_size=args.beam_size,
                condition_on_previous_text=False,
                vad_filter=False,
                without_timestamps=True,
            )
            texts = []
            for segment in segments:
                texts.append(segment.text.strip())
                print(
                    f"  [{segment.start:7.2f} – {segment.end:7.2f}] "
                    f"nsp={segment.no_speech_prob:.3f} "
                    f"logp={segment.avg_logprob:.3f}  {segment.text.strip()}"
                )
            elapsed = time.perf_counter() - started
            text = " ".join(t for t in texts if t)
            print(f"  language={info.language} p={info.language_probability:.3f}")
            print(f"  audio={info.duration:.2f}s elapsed={elapsed:.2f}s "
                  f"speed={info.duration / max(elapsed, 1e-9):.1f}x realtime")
            print(f"  transcript: {text}")
        print(f"\nVRAM after inference: {gpu_memory_text()}")
        print("PASS: CUDA model loaded and completed inference.")
        return 0
    except Exception as exc:
        print(f"\nFAIL: {exc.__class__.__name__}: {exc}")
        traceback.print_exc()
        return 1
    finally:
        if model is not None:
            del model
            gc.collect()
        time.sleep(0.5)
        print(f"VRAM after release : {gpu_memory_text()}")


if __name__ == "__main__":
    raise SystemExit(main())
