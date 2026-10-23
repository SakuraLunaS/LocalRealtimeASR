from __future__ import annotations

import ctypes
import gc
import itertools
import logging
import logging.handlers
import os
import re
import sys
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
from PySide6.QtCore import QStandardPaths, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from clipboard_utils import copy_text
from runtime_setup import (
    APP_NAME,
    DEFAULT_MODEL_DIR,
    add_cuda_dll_directories,
    gpu_memory_text,
    nvidia_smi_value,
)

# Must happen before faster_whisper / ctranslate2 are imported in worker threads.
add_cuda_dll_directories()

LOGGER = logging.getLogger(APP_NAME)

SAMPLE_RATE = 16_000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 320
CAPTURE_BLOCK = SAMPLE_RATE // 10  # 100 ms

END_SILENCE_MS = 700
PRE_ROLL_MS = 240
TRIGGER_FRAMES = 3
MIN_FINAL_MS = 350
MAX_UTTERANCE_MS = 12_000
PREVIEW_INTERVAL_MS = 1_600
PREVIEW_MIN_MS = 1_000
TRAILING_SILENCE_MS = 200

# Utterance IDs must remain unique across pause/resume and repeated
# start/stop cycles.  Otherwise the new capture's ID 1 would be rejected by
# the GUI deduplication set from the previous capture.
_UTTERANCE_SEQUENCE = itertools.count(1)


@dataclass(frozen=True)
class AsrJob:
    kind: str  # "preview" | "final"
    utterance_id: int
    audio: np.ndarray
    duration_s: float


class VadSegmenter:
    """Turn continuous audio into speech utterances with WebRTC VAD.

    One utterance is transcribed exactly once as a final result, so there is
    no overlapping audio and therefore no overlap deduplication needed.
    """

    def __init__(
        self,
        on_preview: Callable[[AsrJob], None],
        on_final: Callable[[AsrJob], None],
    ) -> None:
        import webrtcvad

        self._vad = webrtcvad.Vad(2)
        self._on_preview = on_preview
        self._on_final = on_final
        self._active = False
        self._frames: list[np.ndarray] = []
        self._pre_roll: deque[np.ndarray] = deque(
            maxlen=max(1, PRE_ROLL_MS // FRAME_MS)
        )
        self._trigger = 0
        self._silence_ms = 0
        self._last_voiced_index = 0
        self._last_preview_ms = 0
        self._utterance_id = 0

    @staticmethod
    def _frame_bytes(frame: np.ndarray) -> bytes:
        pcm = np.clip(frame * 32768.0, -32768, 32767).astype(np.int16)
        return pcm.tobytes()

    def process(self, audio: np.ndarray) -> None:
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        if audio.size == 0:
            return
        usable = (audio.size // FRAME_SAMPLES) * FRAME_SAMPLES
        for offset in range(0, usable, FRAME_SAMPLES):
            frame = audio[offset : offset + FRAME_SAMPLES]
            self._process_frame(frame)

    def _process_frame(self, frame: np.ndarray) -> None:
        voiced = self._vad.is_speech(self._frame_bytes(frame), SAMPLE_RATE)

        if not self._active:
            self._pre_roll.append(frame.copy())
            self._trigger = self._trigger + 1 if voiced else 0
            if self._trigger >= TRIGGER_FRAMES:
                self._active = True
                self._utterance_id = next(_UTTERANCE_SEQUENCE)
                self._frames = list(self._pre_roll)
                self._pre_roll.clear()
                self._silence_ms = 0
                self._last_voiced_index = len(self._frames) - 1
                self._last_preview_ms = len(self._frames) * FRAME_MS
            return

        self._frames.append(frame)
        if voiced:
            self._silence_ms = 0
            self._last_voiced_index = len(self._frames) - 1
        else:
            self._silence_ms += FRAME_MS

        total_ms = len(self._frames) * FRAME_MS
        if (
            total_ms >= PREVIEW_MIN_MS
            and total_ms - self._last_preview_ms >= PREVIEW_INTERVAL_MS
        ):
            self._last_preview_ms = total_ms
            audio = np.concatenate(self._frames)
            self._on_preview(
                AsrJob("preview", self._utterance_id, audio, total_ms / 1000.0)
            )

        if self._silence_ms >= END_SILENCE_MS:
            self._finish(force=False)
        elif total_ms >= MAX_UTTERANCE_MS:
            self._finish(force=True)

    def _finish(self, force: bool) -> None:
        if not self._active:
            return
        if force:
            frames = self._frames
        else:
            keep_frames = TRAILING_SILENCE_MS // FRAME_MS
            end = min(len(self._frames), self._last_voiced_index + 1 + keep_frames)
            frames = self._frames[:end]
        duration_ms = len(frames) * FRAME_MS
        if duration_ms >= MIN_FINAL_MS:
            audio = np.concatenate(frames)
            self._on_final(
                AsrJob("final", self._utterance_id, audio, duration_ms / 1000.0)
            )
        self._active = False
        self._frames = []
        self._pre_roll.clear()
        self._trigger = 0
        self._silence_ms = 0
        self._last_voiced_index = 0
        self._last_preview_ms = 0

    def flush(self) -> None:
        self._finish(force=False)


class AudioCaptureThread(QThread):
    preview_job = Signal(object)
    final_job = Signal(object)
    capture_error = Signal(str)
    audio_info = Signal(str)
    stopped = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._stop_event = threading.Event()
        self._failed = False

    def request_stop(self) -> None:
        self._stop_event.set()

    @staticmethod
    def _co_initialize() -> bool:
        """Initialize COM (MTA) for this capture thread.

        soundcard only initializes COM once, on the thread where it is
        imported.  Pause/resume creates a new QThread, so each thread must
        initialize COM itself or WASAPI fails with 0x800401f0.
        """

        hr = ctypes.windll.ole32.CoInitializeEx(None, 0) & 0xFFFFFFFF
        if hr == 0:
            return True
        if hr == 1:  # S_FALSE: already initialized, still needs balancing
            return True
        if hr == 0x80010106:  # RPC_E_CHANGED_MODE
            return False
        raise RuntimeError(f"COM 初始化失败（HRESULT 0x{hr:08X}）")

    def run(self) -> None:
        segmenter: VadSegmenter | None = None
        com_initialized = False
        try:
            # IMPORTANT: import soundcard only in the capture thread, never on
            # the GUI thread.  soundcard initializes COM as MTA at import time.
            # If that happens on the Qt GUI thread, Qt can no longer initialize
            # OLE as STA, which breaks the clipboard and native file dialogs.
            import soundcard

            module_com = getattr(soundcard, "_com", None)
            if module_com is not None and getattr(module_com, "com_loaded", False):
                # SoundCard just initialized COM on this worker thread.  Take
                # ownership of the balance so its global destructor does not
                # call CoUninitialize on the wrong thread at shutdown.
                module_com.com_loaded = False
                com_initialized = True
            else:
                com_initialized = self._co_initialize()

            segmenter = VadSegmenter(
                on_preview=self.preview_job.emit,
                on_final=self.final_job.emit,
            )
            speaker = soundcard.default_speaker()
            if speaker is None:
                raise RuntimeError("Windows 没有默认播放设备。请先连接或启用一个扬声器/耳机。")
            try:
                loopback = soundcard.get_microphone(
                    id=str(speaker.name), include_loopback=True
                )
            except Exception as exc:
                raise RuntimeError(
                    f"无法打开默认播放设备的 WASAPI 回环：{speaker.name}。"
                    "请确认该设备正在作为默认输出使用。"
                ) from exc
            self.audio_info.emit(
                f"{speaker.name} · WASAPI Loopback · 16 kHz/单声道"
            )
            LOGGER.info("Capture device: %s", speaker.name)
            with loopback.recorder(
                samplerate=SAMPLE_RATE, channels=1, blocksize=CAPTURE_BLOCK
            ) as recorder:
                while not self._stop_event.is_set():
                    block = recorder.record(numframes=CAPTURE_BLOCK)
                    if block is None or len(block) == 0:
                        time.sleep(0.01)
                        continue
                    segmenter.process(block[:, 0])
        except Exception as exc:
            self._failed = True
            LOGGER.exception("System audio capture failed")
            message = str(exc).strip() or exc.__class__.__name__
            self.capture_error.emit(message)
        finally:
            if segmenter is not None and not self._failed:
                try:
                    segmenter.flush()
                except Exception:
                    LOGGER.exception("Failed to flush the last utterance")
            if com_initialized:
                try:
                    ctypes.windll.ole32.CoUninitialize()
                except OSError:
                    LOGGER.debug("CoUninitialize failed", exc_info=True)
            self.stopped.emit()


class AsrWorker(QThread):
    stage = Signal(str)
    model_ready = Signal(str)
    preview_text = Signal(int, str)
    final_text = Signal(int, str)
    stats = Signal(str)
    warning = Signal(str)
    error = Signal(str)

    def __init__(self, model_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self._model_dir = Path(model_dir)
        self._condition = threading.Condition()
        self._finals: deque[AsrJob] = deque()
        self._previews: OrderedDict[int, AsrJob] = OrderedDict()
        self._finish_requested = False
        self._processed_final_ids: set[int] = set()
        self._model = None
        self._device_label = "未知设备"
        self._last_gpu_query = 0.0
        self._last_gpu_text = ""

    # ---- thread-safe producer API -------------------------------------
    def enqueue_preview(self, job: AsrJob) -> None:
        with self._condition:
            if self._finish_requested or job.utterance_id in self._processed_final_ids:
                return
            self._previews[job.utterance_id] = job
            # Only the newest preview for the newest utterance matters.
            while len(self._previews) > 2:
                self._previews.popitem(last=False)
            self._condition.notify()

    def enqueue_final(self, job: AsrJob) -> None:
        with self._condition:
            self._previews.pop(job.utterance_id, None)
            self._finals.append(job)
            self._condition.notify_all()

    def request_finish(self) -> None:
        with self._condition:
            self._finish_requested = True
            self._previews.clear()
            self._condition.notify_all()

    @property
    def finish_requested(self) -> bool:
        with self._condition:
            return self._finish_requested

    def _next_job(self) -> AsrJob | None:
        with self._condition:
            while not self._finals and not self._previews:
                if self._finish_requested:
                    return None
                self._condition.wait(timeout=0.2)
            if self._finals:
                return self._finals.popleft()
            if self._previews:
                _, job = self._previews.popitem(last=False)
                return job
            return None

    # ---- model lifecycle ----------------------------------------------
    def _check_model_files(self) -> None:
        required = (
            "model.bin",
            "config.json",
            "preprocessor_config.json",
            "tokenizer.json",
            "vocabulary.json",
        )
        missing = [name for name in required if not (self._model_dir / name).is_file()]
        if missing:
            raise FileNotFoundError(
                f"模型文件不完整，缺少：{', '.join(missing)}。\n"
                f"模型目录：{self._model_dir}\n"
                "请先运行初始化向导：双击 init.bat（或 python setup_wizard.py）。"
            )

    def _warmup(self, model) -> None:
        # The first CUDA inference initializes kernels and can take 10-20 s.
        # Do it on silence while the user sees "正在预热模型".
        warm = np.zeros(SAMPLE_RATE, dtype=np.float32)
        segments, _ = model.transcribe(
            warm,
            language="en",
            beam_size=1,
            condition_on_previous_text=False,
            vad_filter=False,
            without_timestamps=True,
        )
        for _ in segments:  # consume the lazy generator
            pass

    def _load_model(self):
        self._check_model_files()
        from faster_whisper import WhisperModel

        gpu_error = None
        try:
            import ctranslate2

            cuda_count = ctranslate2.get_cuda_device_count()
            if cuda_count < 1:
                raise RuntimeError("CUDA 设备数量为 0（NVIDIA 驱动或运行库不可用）")
            gpu_name = nvidia_smi_value("name")
            gpu_name = gpu_name.splitlines()[0].strip() if gpu_name else "NVIDIA GPU"
            self.stage.emit(f"正在加载 large-v3-turbo 到 GPU（{gpu_name}）…")
            model = WhisperModel(
                str(self._model_dir),
                device="cuda",
                device_index=0,
                compute_type="float16",
                local_files_only=True,
                num_workers=1,
                cpu_threads=4,
            )
            if not self.finish_requested:
                self.stage.emit("模型已载入，正在预热 GPU（首次约 10–20 秒）…")
                self._warmup(model)
            self._device_label = f"{gpu_name} · CUDA float16"
            return model, self._device_label
        except Exception as exc:
            gpu_error = exc
            LOGGER.exception("GPU model load/warm-up failed; trying CPU fallback")
            self.warning.emit(
                "GPU 加载或预热失败，已切换到 CPU 模式。识别速度会明显下降。\n"
                f"GPU 错误：{str(exc).strip() or exc.__class__.__name__}"
            )

        self.stage.emit("正在以 CPU 模式加载模型（int8）…")
        try:
            model = WhisperModel(
                str(self._model_dir),
                device="cpu",
                compute_type="int8",
                local_files_only=True,
                num_workers=1,
                cpu_threads=min(8, os.cpu_count() or 4),
            )
            if not self.finish_requested:
                self._warmup(model)
            self._device_label = "CPU · int8（GPU 不可用）"
            return model, self._device_label
        except Exception as exc:
            raise RuntimeError(
                "模型加载失败，GPU 和 CPU 模式均不可用。\n"
                f"GPU：{str(gpu_error).strip() if gpu_error else '未知'}\n"
                f"CPU：{str(exc).strip()}"
            ) from exc

    def _decode(self, audio: np.ndarray, beam_size: int) -> str:
        if self._model is None:
            raise RuntimeError("模型未加载")
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        segments, _info = self._model.transcribe(
            audio,
            language="en",
            task="transcribe",
            beam_size=beam_size,
            best_of=beam_size,
            condition_on_previous_text=False,
            without_timestamps=True,
            word_timestamps=False,
            vad_filter=False,
        )
        pieces: list[str] = []
        for segment in segments:
            text = segment.text.strip()
            if not text:
                continue
            # Drop likely silence hallucinations. Speech has already passed VAD,
            # so these thresholds only remove very low-confidence garbage.
            if (
                segment.no_speech_prob > 0.85
                and segment.avg_logprob < -1.2
            ):
                continue
            pieces.append(text)
        return re.sub(r"\s+", " ", " ".join(pieces)).strip()

    def _stats_text(self, duration_s: float, elapsed_s: float) -> str:
        now = time.monotonic()
        if "CPU" not in self._device_label and now - self._last_gpu_query >= 5.0:
            self._last_gpu_query = now
            self._last_gpu_text = gpu_memory_text()
        speed = duration_s / max(elapsed_s, 1e-6)
        memory = (
            f" · {self._last_gpu_text}"
            if self._last_gpu_text and "CPU" not in self._device_label
            else ""
        )
        return f"{self._device_label}{memory} · 最近 {elapsed_s:.2f}s（约 {speed:.0f}× 实时）"

    def run(self) -> None:
        model = None
        try:
            model, label = self._load_model()
            self._model = model
            if self.finish_requested:
                return
            self.model_ready.emit(label)
            LOGGER.info("ASR ready: %s", label)

            while True:
                job = self._next_job()
                if job is None:
                    break
                if job.kind == "preview":
                    if job.utterance_id in self._processed_final_ids:
                        continue
                    started = time.perf_counter()
                    text = self._decode(job.audio, beam_size=1)
                    if text:
                        self.preview_text.emit(job.utterance_id, text)
                    LOGGER.debug(
                        "preview #%s audio=%.2fs took=%.3fs",
                        job.utterance_id,
                        job.duration_s,
                        time.perf_counter() - started,
                    )
                    continue

                if job.utterance_id in self._processed_final_ids:
                    continue
                started = time.perf_counter()
                text = self._decode(job.audio, beam_size=5)
                elapsed = time.perf_counter() - started
                self._processed_final_ids.add(job.utterance_id)
                if text:
                    self.final_text.emit(job.utterance_id, text)
                    self.stats.emit(self._stats_text(job.duration_s, elapsed))
                LOGGER.info(
                    "final #%s audio=%.2fs took=%.3fs chars=%s",
                    job.utterance_id,
                    job.duration_s,
                    elapsed,
                    len(text),
                )
        except Exception as exc:
            LOGGER.exception("ASR worker failed")
            message = str(exc).strip() or exc.__class__.__name__
            self.error.emit(f"本地识别失败：{message}")
        finally:
            self._model = None
            if model is not None:
                del model
            gc.collect()
            LOGGER.info("ASR worker stopped; model released")


class TranscriptStore:
    def __init__(self, documents_dir: Path) -> None:
        self.base_dir = Path(documents_dir) / APP_NAME
        self.transcript_dir = self.base_dir / "transcripts"
        self.exports_dir = self.base_dir / "exports"
        self.transcript_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)

    @property
    def today_path(self) -> Path:
        return self.transcript_dir / f"transcripts-{datetime.now():%Y-%m-%d}.txt"

    def append(self, text: str) -> Path:
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return self.today_path
        path = self.today_path
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"[{stamp}] {text}\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        return path

    def latest_log(self) -> Path | None:
        files = sorted(
            self.transcript_dir.glob("transcripts-*.txt"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return files[0] if files else None

    def load_latest(self) -> tuple[Path | None, list[str]]:
        path = self.latest_log()
        if path is None:
            return None, []
        texts: list[str] = []
        line_re = re.compile(r"^\[[^\]]+\]\s?(.*)$")
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for raw in handle:
                    line = raw.rstrip("\r\n")
                    if not line.strip():
                        continue
                    match = line_re.match(line)
                    texts.append((match.group(1) if match else line).strip())
        except OSError:
            LOGGER.exception("Unable to read history log")
            return None, []
        return path, [t for t in texts if t]

    def default_export_path(self) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return self.exports_dir / f"transcripts-{stamp}.txt"


def write_text_file(target: Path, text: str) -> None:
    """Atomically write a UTF-8 text export."""

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def configure_logging() -> None:
    root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME / "logs"
    root.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        root / "app.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler])


class CopyablePlainTextEdit(QPlainTextEdit):
    """Read-only text area with a clipboard path independent of Qt's default."""

    copy_status = Signal(bool, str)

    def _copy_selection(self) -> None:
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return
        text = cursor.selectedText().replace("\u2029", "\n")
        if not text.strip():
            self.copy_status.emit(False, "选中的内容为空。")
            return
        ok, error = copy_text(text)
        if ok:
            self.copy_status.emit(True, f"已复制选中的 {len(text)} 个字符。")
        else:
            self.copy_status.emit(False, f"复制失败：{error}")

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if event.matches(QKeySequence.StandardKey.Copy):
            self._copy_selection()
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802 (Qt API)
        menu = QMenu(self)
        copy_action = menu.addAction("复制选中内容")
        copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        copy_action.setEnabled(self.textCursor().hasSelection())
        select_all_action = menu.addAction("全选")
        chosen = menu.exec(event.globalPos())
        if chosen is copy_action:
            self._copy_selection()
        elif chosen is select_all_action:
            self.selectAll()
        event.accept()


class MainWindow(QMainWindow):
    STATE_STOPPED = "stopped"
    STATE_LOADING = "loading"
    STATE_LISTENING = "listening"
    STATE_PAUSED = "paused"
    STATE_STOPPING = "stopping"
    STATE_ERROR = "error"

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("本地英文语音转写 V2")
        self.resize(980, 720)
        self.setMinimumSize(780, 560)

        documents = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation
        )
        if not documents:
            documents = str(Path.home() / "Documents")
        self.store = TranscriptStore(Path(documents))

        self.state = self.STATE_STOPPED
        self.asr_worker: AsrWorker | None = None
        self.capture_thread: AudioCaptureThread | None = None
        self.final_texts: list[str] = []
        self.seen_final_ids: set[int] = set()
        self.preview_id: int | None = None
        self.device_label = ""
        self._closing = False

        self._build_ui()
        self._set_state(self.STATE_STOPPED)
        self._load_history()
        self._update_actions()

    # ---- UI -----------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(22, 18, 22, 16)
        root.setSpacing(10)

        title = QLabel("本地英文语音转写 V2")
        title.setObjectName("title")
        subtitle = QLabel(
            "持续监听 Windows 系统播放的英语音频 · faster-whisper large-v3-turbo · 完全离线"
        )
        subtitle.setObjectName("subtitle")
        root.addWidget(title)
        root.addWidget(subtitle)

        status_row = QHBoxLayout()
        status_row.setSpacing(12)
        self.status_label = QLabel("● 未开始")
        self.status_label.setObjectName("status")
        self.device_info = QLabel("模型未加载")
        self.device_info.setObjectName("deviceInfo")
        self.device_info.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        status_row.addWidget(self.status_label)
        status_row.addStretch(1)
        status_row.addWidget(self.device_info)
        root.addLayout(status_row)

        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setObjectName("notice")
        self.notice.hide()
        root.addWidget(self.notice)

        transcript_header = QHBoxLayout()
        transcript_label = QLabel("已确认文本")
        transcript_label.setObjectName("sectionLabel")
        self.count_label = QLabel("0 段")
        self.count_label.setObjectName("muted")
        transcript_header.addWidget(transcript_label)
        transcript_header.addStretch(1)
        transcript_header.addWidget(self.count_label)
        root.addLayout(transcript_header)

        self.transcript = CopyablePlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setPlaceholderText(
            "识别出的最终文本会持续追加在这里。\n\n"
            "点击“开始监听”后，请播放英语音频。首次启动需要加载并预热本地模型。"
        )
        self.transcript.setObjectName("transcript")
        self.transcript.setFont(QFont("Segoe UI", 12))
        self.transcript.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.transcript.copy_status.connect(self._on_copy_result)
        root.addWidget(self.transcript, 1)

        preview_label = QLabel("当前句（临时结果，不会写入永久日志）")
        preview_label.setObjectName("sectionLabel")
        root.addWidget(preview_label)
        self.preview = CopyablePlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFixedHeight(58)
        self.preview.setPlaceholderText("等待语音…")
        self.preview.setObjectName("preview")
        self.preview.setFont(QFont("Segoe UI", 11))
        self.preview.copy_status.connect(self._on_copy_result)
        root.addWidget(self.preview)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.start_button = QPushButton("开始监听")
        self.start_button.setObjectName("primary")
        self.pause_button = QPushButton("暂停监听")
        self.stop_button = QPushButton("停止监听")
        self.copy_button = QPushButton("复制全部")
        self.copy_button.setToolTip("复制所有已确认的最终文本（不含临时句）")
        self.copy_selected_button = QPushButton("复制选中")
        self.copy_selected_button.setToolTip("复制文本框中当前选中的内容")
        self.save_button = QPushButton("保存 TXT")
        self.clear_button = QPushButton("清空显示")
        self.clear_button.setToolTip("只清空界面，不删除自动保存的日志")
        self.folder_button = QPushButton("打开日志文件夹")
        controls.addWidget(self.start_button)
        controls.addWidget(self.pause_button)
        controls.addWidget(self.stop_button)
        controls.addSpacing(14)
        controls.addWidget(self.copy_button)
        controls.addWidget(self.copy_selected_button)
        controls.addWidget(self.save_button)
        controls.addWidget(self.clear_button)
        controls.addStretch(1)
        controls.addWidget(self.folder_button)
        root.addLayout(controls)

        footer = QFrame()
        footer.setObjectName("footer")
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(0, 4, 0, 0)
        footer_layout.setSpacing(2)
        self.autosave_label = QLabel(f"自动日志：{self.store.today_path}")
        self.autosave_label.setObjectName("footerText")
        self.autosave_label.setWordWrap(True)
        self.autosave_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.metrics_label = QLabel("GPU：待加载 · 显存：— · 识别速度：—")
        self.metrics_label.setObjectName("footerText")
        self.feedback_label = QLabel("")
        self.feedback_label.setObjectName("feedback")
        footer_layout.addWidget(self.autosave_label)
        footer_layout.addWidget(self.metrics_label)
        footer_layout.addWidget(self.feedback_label)
        root.addWidget(footer)

        self.start_button.clicked.connect(self._on_start_clicked)
        self.pause_button.clicked.connect(self._on_pause)
        self.stop_button.clicked.connect(self._on_stop)
        self.copy_button.clicked.connect(self._copy_all)
        self.copy_selected_button.clicked.connect(self._copy_selected)
        self.save_button.clicked.connect(self._save_text)
        self.clear_button.clicked.connect(self._clear_display)
        self.folder_button.clicked.connect(self._open_log_folder)

        self._apply_styles()

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow { background: #f5f7fa; }
            QLabel#title { font-size: 22px; font-weight: 600; color: #17212b; }
            QLabel#subtitle { color: #667085; font-size: 12px; }
            QLabel#status { font-size: 14px; font-weight: 600; color: #667085; }
            QLabel#deviceInfo, QLabel#muted { color: #667085; font-size: 12px; }
            QLabel#sectionLabel { font-weight: 600; color: #344054; }
            QLabel#footerText { color: #667085; font-size: 11px; }
            QLabel#feedback { color: #1769e0; font-size: 11px; }
            QLabel#notice {
                background: #fff6e5; color: #7a4b00; border: 1px solid #f1d18a;
                border-radius: 5px; padding: 8px 10px; font-size: 12px;
            }
            QPlainTextEdit#transcript {
                background: #ffffff; border: 1px solid #d7dde7; border-radius: 7px;
                padding: 10px; color: #101828; selection-background-color: #b7d3ff;
            }
            QPlainTextEdit#preview {
                background: #fbfcfe; border: 1px dashed #cfd8e6; border-radius: 5px;
                padding: 7px; color: #475467; selection-background-color: #b7d3ff;
            }
            QPushButton {
                background: #ffffff; border: 1px solid #cfd8e6; border-radius: 5px;
                padding: 7px 13px; color: #253044;
            }
            QPushButton:hover { background: #f0f4fa; }
            QPushButton:disabled { color: #abb4c2; background: #f4f6f8; border-color: #e2e7ee; }
            QPushButton#primary {
                background: #1769e0; border-color: #1769e0; color: white; font-weight: 600;
            }
            QPushButton#primary:hover { background: #0e57bd; }
            QPushButton#primary:disabled {
                background: #c5d3e8; border-color: #c5d3e8; color: #ffffff;
            }
            """
        )

    def _set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        labels = {
            self.STATE_STOPPED: "● 已停止",
            self.STATE_LOADING: "● 正在加载模型…",
            self.STATE_LISTENING: "● 正在监听",
            self.STATE_PAUSED: "● 已暂停",
            self.STATE_STOPPING: "● 正在停止…",
            self.STATE_ERROR: "● 发生错误",
        }
        colors = {
            self.STATE_STOPPED: "#667085",
            self.STATE_LOADING: "#b25e09",
            self.STATE_LISTENING: "#0b8a4b",
            self.STATE_PAUSED: "#b25e09",
            self.STATE_STOPPING: "#667085",
            self.STATE_ERROR: "#c4320a",
        }
        text = labels.get(state, "● 未知状态")
        if detail:
            text += f"  {detail}"
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            f"font-size: 14px; font-weight: 600; color: {colors.get(state, '#667085')};"
        )
        self._update_actions()

    def _update_actions(self) -> None:
        state = self.state
        self.start_button.setEnabled(
            state in (self.STATE_STOPPED, self.STATE_PAUSED, self.STATE_ERROR)
            and not (state == self.STATE_ERROR and self.asr_worker is not None
                     and self.asr_worker.isRunning())
        )
        self.start_button.setText("继续监听" if state == self.STATE_PAUSED else "开始监听")
        self.pause_button.setEnabled(state == self.STATE_LISTENING)
        self.stop_button.setEnabled(state in (self.STATE_LISTENING, self.STATE_PAUSED, self.STATE_LOADING))
        has_text = bool(self.final_texts)
        self.copy_button.setEnabled(has_text)
        self.copy_selected_button.setEnabled(has_text or bool(self.preview.toPlainText()))
        self.save_button.setEnabled(has_text)
        self.clear_button.setEnabled(has_text or bool(self.preview.toPlainText()))

    def _show_notice(self, text: str, error: bool = False) -> None:
        self.notice.setText(text)
        if error:
            self.notice.setStyleSheet(
                "background:#fdecea;color:#a61b1b;border:1px solid #f2b8b5;"
                "border-radius:5px;padding:8px 10px;font-size:12px;"
            )
        else:
            self.notice.setStyleSheet(
                "background:#fff6e5;color:#7a4b00;border:1px solid #f1d18a;"
                "border-radius:5px;padding:8px 10px;font-size:12px;"
            )
        self.notice.show()

    def _hide_notice(self) -> None:
        self.notice.hide()

    def _feedback(self, text: str) -> None:
        self.feedback_label.setText(text)
        QTimer.singleShot(
            5000,
            lambda: self.feedback_label.setText("")
            if self.feedback_label.text() == text
            else None,
        )

    def _on_copy_result(self, ok: bool, message: str) -> None:
        if ok:
            self._feedback(message)
        else:
            self._show_notice(message, error=True)

    # ---- history and display -----------------------------------------
    def _load_history(self) -> None:
        path, texts = self.store.load_latest()
        if texts:
            self.final_texts = list(texts)
            self.transcript.setPlainText("\n".join(texts))
            bar = self.transcript.verticalScrollBar()
            bar.setValue(bar.maximum())
            self.count_label.setText(f"{len(texts)} 段")
            self._feedback(f"已恢复历史日志 {len(texts)} 段：{path}")
        self.autosave_label.setText(f"自动日志：{self.store.today_path}")

    def _append_final(self, text: str) -> None:
        self.final_texts.append(text)
        bar = self.transcript.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        self.transcript.appendPlainText(text)
        if at_bottom:
            bar.setValue(bar.maximum())
        self.count_label.setText(f"{len(self.final_texts)} 段")
        self._update_actions()

    def _on_preview(self, utterance_id: int, text: str) -> None:
        if utterance_id in self.seen_final_ids:
            return
        self.preview_id = utterance_id
        self.preview.setPlainText(text)
        self._update_actions()

    def _on_final(self, utterance_id: int, text: str) -> None:
        if utterance_id in self.seen_final_ids:
            return
        self.seen_final_ids.add(utterance_id)
        if self.preview_id == utterance_id:
            self.preview.clear()
            self.preview_id = None
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            self._update_actions()
            return
        self._append_final(text)
        if self.state in (self.STATE_LISTENING, self.STATE_PAUSED, self.STATE_STOPPING):
            try:
                saved_path = self.store.append(text)
                self.autosave_label.setText(f"自动日志：{saved_path}")
            except Exception as exc:
                LOGGER.exception("Unable to append transcript log")
                self._show_notice(
                    f"自动保存失败：{exc}. 文本仍保留在界面中，请立即手动保存。",
                    error=True,
                )

    # ---- session lifecycle -------------------------------------------
    def _on_start_clicked(self) -> None:
        if self.state == self.STATE_PAUSED:
            self._resume_listening()
        elif self.state in (self.STATE_STOPPED, self.STATE_ERROR):
            self._start_session()

    def _start_session(self) -> None:
        if self.asr_worker is not None and self.asr_worker.isRunning():
            return
        self._hide_notice()
        self.preview.clear()
        self.preview_id = None
        self.device_label = ""
        self.device_info.setText("正在加载模型…")
        self.metrics_label.setText("设备：加载中 · 显存：— · 识别速度：—")
        self._set_state(self.STATE_LOADING)

        worker = AsrWorker(DEFAULT_MODEL_DIR, self)
        worker.stage.connect(self._on_stage)
        worker.model_ready.connect(self._on_model_ready)
        worker.preview_text.connect(self._on_preview)
        worker.final_text.connect(self._on_final)
        worker.stats.connect(self._on_stats)
        worker.warning.connect(self._on_warning)
        worker.error.connect(self._on_asr_error)
        worker.finished.connect(self._on_asr_finished)
        self.asr_worker = worker
        worker.start()
        self._update_actions()

    def _on_stage(self, text: str) -> None:
        if self.state == self.STATE_LOADING:
            self.device_info.setText(text)

    def _on_warning(self, text: str) -> None:
        LOGGER.warning("ASR warning: %s", text.replace("\n", " | "))
        self._show_notice(text, error=False)

    def _on_model_ready(self, device_label: str) -> None:
        self.device_label = device_label
        self.device_info.setText(device_label)
        self.metrics_label.setText(f"{device_label} · 显存：— · 识别速度：—")
        if self.state != self.STATE_LOADING:
            return
        self._start_capture()
        self._set_state(self.STATE_LISTENING)

    def _start_capture(self) -> None:
        if self.asr_worker is None or not self.asr_worker.isRunning():
            self._fail("识别线程未运行，无法开始采集。")
            return
        if self.capture_thread is not None and self.capture_thread.isRunning():
            return
        capture = AudioCaptureThread(self)
        # Direct connections guarantee that a final job emitted while stopping
        # is queued before request_finish() is called.
        capture.preview_job.connect(
            self.asr_worker.enqueue_preview, Qt.ConnectionType.DirectConnection
        )
        capture.final_job.connect(
            self.asr_worker.enqueue_final, Qt.ConnectionType.DirectConnection
        )
        capture.audio_info.connect(self._on_audio_info)
        capture.capture_error.connect(self._on_capture_error)
        capture.stopped.connect(self._on_capture_stopped)
        self.capture_thread = capture
        capture.start()
        LOGGER.info("Audio capture started")

    def _on_audio_info(self, info: str) -> None:
        self.device_info.setText(info)
        LOGGER.info("Audio endpoint: %s", info)

    def _on_capture_stopped(self) -> None:
        LOGGER.info("Audio capture thread stopped")

    def _on_capture_error(self, message: str) -> None:
        LOGGER.error("Capture error: %s", message)
        self._stop_capture(wait=True)
        self._fail(f"系统音频采集失败：{message}")

    def _stop_capture(self, wait: bool = True) -> None:
        capture = self.capture_thread
        if capture is None:
            return
        capture.request_stop()
        if wait and not capture.wait(3000):
            LOGGER.warning("Capture thread did not stop within 3 seconds")
        self.capture_thread = None
        capture.deleteLater()

    def _on_pause(self) -> None:
        if self.state != self.STATE_LISTENING:
            return
        self._stop_capture()
        self._set_state(self.STATE_PAUSED)
        self.device_info.setText("已暂停（模型保持加载）")
        self._feedback("已暂停。再次点击“继续监听”即可恢复。")

    def _resume_listening(self) -> None:
        if self.asr_worker is None or not self.asr_worker.isRunning():
            self._fail("识别线程已结束，请点击“停止监听”后重新开始。")
            return
        self._hide_notice()
        self._start_capture()
        self._set_state(self.STATE_LISTENING)
        if self.device_label:
            self.device_info.setText(self.device_label)

    def _on_stop(self) -> None:
        if self.state in (self.STATE_STOPPED, self.STATE_STOPPING, self.STATE_ERROR):
            return
        self._set_state(self.STATE_STOPPING)
        self.device_info.setText("正在停止并释放模型…")
        self._stop_capture()
        worker = self.asr_worker
        if worker is not None and worker.isRunning():
            worker.request_finish()
            # If the worker is still loading, this can take a few seconds.
            QTimer.singleShot(100, self._check_worker_finished)
        else:
            self._on_asr_finished()

    def _check_worker_finished(self) -> None:
        if self.asr_worker is None:
            if self.state == self.STATE_STOPPING:
                self._set_state(self.STATE_STOPPED)
            return
        if self.asr_worker.isFinished():
            self._on_asr_finished()

    def _on_asr_finished(self) -> None:
        worker = self.asr_worker
        self.asr_worker = None
        if worker is not None:
            worker.deleteLater()
        if self._closing:
            return
        if self.state == self.STATE_ERROR:
            self.device_info.setText("识别已停止")
        elif self.state == self.STATE_STOPPING:
            self._set_state(self.STATE_STOPPED)
            self.device_info.setText("模型已释放，音频设备已关闭")
            self.metrics_label.setText("识别速度：— · 显存：— · 模型：已释放")
        elif self.state in (self.STATE_LISTENING, self.STATE_PAUSED):
            self._stop_capture(wait=True)
            self._fail("识别线程意外结束。请点击“开始监听”重试。")
        self._update_actions()

    def _on_asr_error(self, message: str) -> None:
        LOGGER.error("ASR error: %s", message.replace("\n", " | "))
        self._stop_capture(wait=True)
        self._show_notice(message, error=True)
        self.device_info.setText("识别失败")
        self._set_state(self.STATE_ERROR)
        self._update_actions()

    def _fail(self, message: str) -> None:
        self._show_notice(message, error=True)
        if self.state != self.STATE_ERROR:
            self._set_state(self.STATE_ERROR)
        worker = self.asr_worker
        if worker is not None and worker.isRunning():
            worker.request_finish()
        self._update_actions()

    def _on_stats(self, text: str) -> None:
        self.metrics_label.setText(text)

    # ---- text actions -------------------------------------------------
    def _full_text(self) -> str:
        return "\n".join(self.final_texts).strip()

    def _copy_all(self) -> None:
        text = self._full_text()
        if not text:
            self._feedback("没有已确认的文本可复制。")
            return
        ok, error = copy_text(text)
        if ok:
            self._feedback(f"已复制 {len(text)} 个字符到剪贴板。")
        else:
            self._show_notice(f"复制失败：{error}", error=True)

    def _copy_selected(self) -> None:
        for widget in (self.transcript, self.preview):
            cursor = widget.textCursor()
            if cursor.hasSelection():
                text = cursor.selectedText().replace("\u2029", "\n")
                if text.strip():
                    ok, error = copy_text(text)
                    if ok:
                        self._feedback(f"已复制选中的 {len(text)} 个字符。")
                    else:
                        self._show_notice(f"复制失败：{error}", error=True)
                    return
        self._feedback("请先在文本区域中用鼠标选中要复制的内容。")

    def _save_text(self) -> None:
        text = self._full_text()
        if not text:
            self._feedback("没有可保存的已确认文本。")
            return
        suggested = self.store.default_export_path()

        # Qt's native Windows dialog is backed by COM/shell.  The app can run
        # on systems where that path is unreliable (and a non-STA audio library
        # could previously break it), so force Qt's own dialog and keep the
        # operation entirely inside the Qt event loop.
        dialog = QFileDialog(self, "保存转写文本")
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        dialog.setNameFilter("文本文件 (*.txt);;所有文件 (*)")
        dialog.setDefaultSuffix("txt")
        dialog.setDirectory(str(suggested.parent))
        dialog.selectFile(suggested.name)
        if dialog.exec() != QFileDialog.DialogCode.Accepted:
            return
        selected = dialog.selectedFiles()
        if not selected:
            return
        target = Path(selected[0])
        if not target.suffix:
            target = target.with_suffix(".txt")
        try:
            write_text_file(target, text)
        except OSError as exc:
            LOGGER.exception("Manual export failed")
            QMessageBox.critical(self, "保存失败", f"无法保存文件：\n{exc}")
            return
        self._feedback(f"已保存：{target}")

    def _clear_display(self) -> None:
        has_text = bool(self.transcript.toPlainText().strip() or self.preview.toPlainText().strip())
        if not has_text:
            return
        answer = QMessageBox.question(
            self,
            "清空显示",
            "仅清空当前界面中的文本，不会删除已经自动保存的日志。\n\n确定要清空显示吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.transcript.clear()
        self.preview.clear()
        self.preview_id = None
        self.final_texts = []
        self.count_label.setText("0 段")
        self._feedback(
            f"界面已清空；自动日志仍保存在：{self.store.today_path.parent}"
        )
        self._update_actions()

    def _open_log_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.store.transcript_dir)))

    # ---- shutdown -----------------------------------------------------
    def closeEvent(self, event) -> None:
        self._closing = True
        self._stop_capture(wait=True)
        worker = self.asr_worker
        if worker is not None and worker.isRunning():
            worker.request_finish()
            if not worker.wait(8000):
                LOGGER.warning("ASR worker did not stop cleanly; terminating")
                worker.terminate()
                worker.wait(1000)
        # Deliver any queued final_text signals so the last sentence is written
        # to the automatic log before the process exits.
        QApplication.processEvents()
        event.accept()


def main() -> int:
    configure_logging()
    LOGGER.info("Starting %s", APP_NAME)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName("LocalASR V2")
    app.setOrganizationName(APP_NAME)
    app.setStyle("Fusion")
    try:
        window = MainWindow()
    except Exception:
        LOGGER.exception("Failed to create main window")
        raise
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
