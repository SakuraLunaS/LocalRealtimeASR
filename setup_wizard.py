"""LocalASR V2 setup wizard.

This is intentionally independent from ``app.py``: it configures the optional
SOCKS5 download proxy and downloads/verifies the local model with a progress
GUI.  It never loads the speech model or touches the audio device.

Start it with:
    python setup_wizard.py
or double-click init.bat.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app_config import (
    config_path,
    get_proxy,
    normalize_proxy,
    set_proxy,
)
from model_downloader import (
    DownloadCancelled,
    DownloadError,
    DownloadProgress,
    ModelDownloader,
)
from runtime_setup import BASE_DIR, DEFAULT_MODEL_DIR, DEFAULT_MODEL_ID

REQUIRED_MODEL_FILES = (
    "model.bin",
    "config.json",
    "preprocessor_config.json",
    "tokenizer.json",
    "vocabulary.json",
)


def model_looks_ready(directory: Path = DEFAULT_MODEL_DIR) -> bool:
    return all((directory / name).is_file() for name in REQUIRED_MODEL_FILES)


class DownloadWorker(QThread):
    progress = Signal(int, str)
    log = Signal(str)
    succeeded = Signal()
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, proxy: str, destination: Path, parent=None) -> None:
        super().__init__(parent)
        self._proxy = proxy
        self._destination = destination
        self._cancel = threading.Event()

    def request_cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        downloader = ModelDownloader(
            model_id=DEFAULT_MODEL_ID,
            destination=self._destination,
            proxy=self._proxy,
            progress_callback=self._on_progress,
            log_callback=self.log.emit,
            cancel_event=self._cancel,
        )
        try:
            downloader.download()
            self.succeeded.emit()
        except DownloadCancelled:
            self.cancelled.emit()
        except DownloadError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(f"{exc.__class__.__name__}: {exc}")
        finally:
            downloader.close()

    def _on_progress(self, item: DownloadProgress) -> None:
        self.progress.emit(int(round(item.percent)), item.message)


class ProxyTestWorker(QThread):
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, proxy: str, parent=None) -> None:
        super().__init__(parent)
        self._proxy = proxy

    def run(self) -> None:
        downloader = ModelDownloader(proxy=self._proxy)
        try:
            result = downloader.test_connection()
            self.succeeded.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            downloader.close()


class SetupWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("LocalASR V2 · 初始化")
        self.resize(820, 680)
        self.setMinimumSize(720, 600)
        self._download_worker: DownloadWorker | None = None
        self._proxy_worker: ProxyTestWorker | None = None

        saved_proxy = ""
        try:
            saved_proxy = get_proxy()
        except ValueError:
            saved_proxy = ""

        self._build_ui(saved_proxy)
        self._apply_styles()
        self._refresh_model_state()

    # ---- UI -----------------------------------------------------------
    def _build_ui(self, saved_proxy: str) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(11)

        title = QLabel("LocalASR V2 · 初始化")
        title.setObjectName("title")
        subtitle = QLabel(
            "独立初始化向导：配置可选 SOCKS5 代理，下载并校验本地 Whisper 模型。"
            "主程序不会被启动，也不会加载模型。"
        )
        subtitle.setWordWrap(True)
        subtitle.setObjectName("subtitle")
        root.addWidget(title)
        root.addWidget(subtitle)

        proxy_box = QGroupBox("下载网络（可选）")
        proxy_layout = QVBoxLayout(proxy_box)
        proxy_layout.setSpacing(7)
        self.proxy_checkbox = QCheckBox("通过 SOCKS5 代理下载")
        self.proxy_edit = QLineEdit()
        self.proxy_edit.setPlaceholderText("socks5h://127.0.0.1:7897")
        self.proxy_edit.setText(saved_proxy)
        self.proxy_edit.setEnabled(bool(saved_proxy))
        self.proxy_checkbox.setChecked(bool(saved_proxy))
        self.proxy_checkbox.toggled.connect(self._on_proxy_toggled)
        proxy_row = QHBoxLayout()
        proxy_row.addWidget(self.proxy_edit, 1)
        self.test_proxy_button = QPushButton("测试连接")
        proxy_row.addWidget(self.test_proxy_button)
        proxy_layout.addWidget(self.proxy_checkbox)
        proxy_layout.addLayout(proxy_row)
        self.proxy_status = QLabel(
            "直连即可使用 ModelScope；如海外下载缓慢，可填写本机代理，"
            "例如 socks5h://127.0.0.1:7897。"
        )
        self.proxy_status.setWordWrap(True)
        self.proxy_status.setObjectName("hint")
        proxy_layout.addWidget(self.proxy_status)
        config_hint = QLabel(
            f"代理会保存在本机：{config_path()}（请勿把该文件提交到 GitHub）"
        )
        config_hint.setObjectName("muted")
        config_hint.setWordWrap(True)
        proxy_layout.addWidget(config_hint)
        root.addWidget(proxy_box)

        model_box = QGroupBox("本地模型")
        model_layout = QVBoxLayout(model_box)
        model_form = QFormLayout()
        model_form.setHorizontalSpacing(12)
        model_form.setVerticalSpacing(6)
        model_form.addRow("模型", QLabel("Whisper large-v3-turbo（CTranslate2 FP16）"))
        model_form.addRow("来源", QLabel(f"ModelScope · {DEFAULT_MODEL_ID}"))
        target_row = QHBoxLayout()
        self.dest_edit = QLineEdit(str(DEFAULT_MODEL_DIR))
        self.dest_edit.setReadOnly(True)
        self.dest_edit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        target_row.addWidget(self.dest_edit, 1)
        self.open_dir_button = QPushButton("打开目录")
        target_row.addWidget(self.open_dir_button)
        model_form.addRow("目标目录", target_row)
        model_layout.addLayout(model_form)

        self.ready_label = QLabel()
        self.ready_label.setWordWrap(True)
        model_layout.addWidget(self.ready_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        model_layout.addWidget(self.progress)

        self.status_label = QLabel("就绪。点击下方按钮开始下载并校验。")
        self.status_label.setObjectName("statusLine")
        self.status_label.setWordWrap(True)
        model_layout.addWidget(self.status_label)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(130)
        self.log_view.setPlaceholderText("下载日志会显示在这里…")
        self.log_view.setObjectName("logView")
        self.log_view.setFont(QFont("Consolas", 9))
        model_layout.addWidget(self.log_view)

        buttons = QHBoxLayout()
        self.download_button = QPushButton("开始下载 / 校验模型")
        self.download_button.setObjectName("primary")
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setEnabled(False)
        self.launch_button = QPushButton("启动主程序")
        self.close_button = QPushButton("关闭")
        buttons.addWidget(self.download_button)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        buttons.addWidget(self.launch_button)
        buttons.addWidget(self.close_button)
        model_layout.addLayout(buttons)
        root.addWidget(model_box, 1)

        footer = QFrame()
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.setSpacing(3)
        note = QLabel(
            "首次安装需要先运行 install.bat 安装 Python 依赖。"
            "模型下载约 1.62 GB，下载完成后可完全离线使用。"
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        footer_layout.addWidget(note)
        root.addWidget(footer)

        self.download_button.clicked.connect(self._start_download)
        self.cancel_button.clicked.connect(self._cancel_download)
        self.test_proxy_button.clicked.connect(self._test_proxy)
        self.open_dir_button.clicked.connect(self._open_model_dir)
        self.launch_button.clicked.connect(self._launch_app)
        self.close_button.clicked.connect(self.close)

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow { background: #f5f7fa; }
            QLabel#title { font-size: 22px; font-weight: 600; color: #17212b; }
            QLabel#subtitle { color: #667085; font-size: 12px; }
            QLabel#hint { color: #667085; font-size: 11px; }
            QLabel#muted { color: #7b8494; font-size: 11px; }
            QLabel#statusLine { font-size: 12px; color: #344054; }
            QGroupBox {
                border: 1px solid #d7dde7; border-radius: 7px;
                margin-top: 9px; padding: 12px; background: #ffffff;
                font-weight: 600; color: #344054;
            }
            QGroupBox::title {
                subcontrol-origin: margin; left: 12px; padding: 0 4px;
            }
            QLineEdit {
                border: 1px solid #cfd8e6; border-radius: 4px;
                padding: 6px 8px; background: #ffffff; color: #253044;
            }
            QLineEdit:disabled { background: #f4f6f8; color: #abb4c2; }
            QPlainTextEdit#logView {
                background: #fbfcfe; border: 1px solid #dfe5ee;
                border-radius: 4px; padding: 6px; color: #475467;
            }
            QProgressBar {
                border: 1px solid #d7dde7; border-radius: 4px;
                background: #f4f6f8; text-align: center; height: 18px;
            }
            QProgressBar::chunk { background: #1769e0; border-radius: 3px; }
            QPushButton {
                background: #ffffff; border: 1px solid #cfd8e6;
                border-radius: 5px; padding: 7px 13px; color: #253044;
            }
            QPushButton:hover { background: #f0f4fa; }
            QPushButton:disabled {
                color: #abb4c2; background: #f4f6f8; border-color: #e2e7ee;
            }
            QPushButton#primary {
                background: #1769e0; border-color: #1769e0;
                color: #ffffff; font-weight: 600;
            }
            QPushButton#primary:hover { background: #0e57bd; }
            QPushButton#primary:disabled {
                background: #c5d3e8; border-color: #c5d3e8; color: #ffffff;
            }
            """
        )

    # ---- state --------------------------------------------------------
    def _refresh_model_state(self) -> None:
        if model_looks_ready():
            self.ready_label.setText(
                "● 已检测到完整模型文件。可以再次点击下载按钮进行 SHA256 校验。"
            )
            self.ready_label.setStyleSheet("color:#0b8a4b; font-size:12px;")
            self.launch_button.setEnabled(True)
            self.progress.setValue(100)
            self.status_label.setText("模型已就绪，可以启动主程序。")
        else:
            self.ready_label.setText(
                "○ 尚未检测到完整模型。首次使用需要下载约 1.62 GB。"
            )
            self.ready_label.setStyleSheet("color:#b25e09; font-size:12px;")
            self.launch_button.setEnabled(False)

    def _append_log(self, message: str) -> None:
        if not message:
            return
        prefix = datetime.now().strftime("%H:%M:%S")
        lines = [line for line in str(message).splitlines() if line.strip()]
        for line in lines:
            self.log_view.appendPlainText(f"[{prefix}] {line}")
        # Avoid unbounded log growth during very long retries.
        if self.log_view.blockCount() > 800:
            cursor = self.log_view.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            cursor.movePosition(
                QTextCursor.MoveOperation.Down,
                QTextCursor.MoveMode.KeepAnchor,
                300,
            )
            cursor.removeSelectedText()
            cursor.deleteChar()

    def _on_proxy_toggled(self, checked: bool) -> None:
        self.proxy_edit.setEnabled(checked)

    def _validated_proxy(self) -> str | None:
        if not self.proxy_checkbox.isChecked():
            return ""
        if not self.proxy_edit.text().strip():
            QMessageBox.warning(self, "代理为空", "请填写 SOCKS5 代理地址，或取消勾选代理。")
            return None
        try:
            return normalize_proxy(self.proxy_edit.text())
        except ValueError as exc:
            QMessageBox.warning(self, "代理格式错误", str(exc))
            return None

    def _save_proxy(self, proxy: str) -> None:
        try:
            set_proxy(proxy)
        except Exception as exc:
            self._append_log(f"保存代理设置失败：{exc}")

    # ---- proxy test ---------------------------------------------------
    def _test_proxy(self) -> None:
        if self._proxy_worker is not None and self._proxy_worker.isRunning():
            return
        proxy = self._validated_proxy()
        if proxy is None:
            return
        self._save_proxy(proxy)
        self.test_proxy_button.setEnabled(False)
        self.download_button.setEnabled(False)
        self.proxy_status.setText("正在测试网络连接…")
        self.proxy_status.setStyleSheet("color:#b25e09; font-size:11px;")
        worker = ProxyTestWorker(proxy, self)
        worker.succeeded.connect(self._on_proxy_ok)
        worker.failed.connect(self._on_proxy_failed)
        worker.finished.connect(self._on_proxy_finished)
        self._proxy_worker = worker
        worker.start()

    def _on_proxy_ok(self, message: str) -> None:
        self.proxy_status.setText(f"● {message}")
        self.proxy_status.setStyleSheet("color:#0b8a4b; font-size:11px;")

    def _on_proxy_failed(self, message: str) -> None:
        self.proxy_status.setText(f"● 连接失败：{message}")
        self.proxy_status.setStyleSheet("color:#c4320a; font-size:11px;")

    def _on_proxy_finished(self) -> None:
        worker = self._proxy_worker
        self._proxy_worker = None
        if worker is not None:
            worker.deleteLater()
        self.test_proxy_button.setEnabled(True)
        if self._download_worker is None:
            self.download_button.setEnabled(True)

    # ---- model download ----------------------------------------------
    def _start_download(self) -> None:
        if self._download_worker is not None and self._download_worker.isRunning():
            return
        if self._proxy_worker is not None and self._proxy_worker.isRunning():
            QMessageBox.information(self, "请稍候", "代理网络测试仍在进行，请稍候再开始下载。")
            return
        proxy = self._validated_proxy()
        if proxy is None:
            return
        free = shutil.disk_usage(DEFAULT_MODEL_DIR.parent).free
        if free < 2_000_000_000:
            QMessageBox.warning(
                self,
                "磁盘空间不足",
                f"模型目录所在磁盘只剩 {free / (1024**3):.1f} GB，"
                "至少需要约 2 GB 可用空间。",
            )
            return
        self._save_proxy(proxy)
        self._append_log("开始初始化模型下载。")
        self._append_log(
            f"网络：{'SOCKS5 代理' if proxy else '直连'}；目标：{DEFAULT_MODEL_DIR}"
        )
        self.progress.setValue(0)
        self.status_label.setText("正在连接 ModelScope…")
        self.download_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.test_proxy_button.setEnabled(False)
        self.proxy_checkbox.setEnabled(False)
        self.proxy_edit.setEnabled(False)
        self.launch_button.setEnabled(False)
        self.close_button.setEnabled(False)

        worker = DownloadWorker(proxy, DEFAULT_MODEL_DIR, self)
        worker.progress.connect(self._on_progress)
        worker.log.connect(self._append_log)
        worker.succeeded.connect(self._on_download_success)
        worker.failed.connect(self._on_download_failed)
        worker.cancelled.connect(self._on_download_cancelled)
        worker.finished.connect(self._on_download_finished)
        self._download_worker = worker
        worker.start()

    def _on_progress(self, percent: int, message: str) -> None:
        self.progress.setValue(max(0, min(100, percent)))
        self.status_label.setText(message)

    def _on_download_success(self) -> None:
        self.progress.setValue(100)
        self.status_label.setText("全部模型文件已下载并通过 SHA256 校验。")
        self.ready_label.setText("● 模型已就绪。可以启动主程序。")
        self.ready_label.setStyleSheet("color:#0b8a4b; font-size:12px;")
        self.launch_button.setEnabled(True)
        self._append_log("初始化完成：模型已就绪。")
        QMessageBox.information(
            self,
            "初始化完成",
            "模型已下载并通过 SHA256 校验。\n\n"
            "现在可以关闭本向导，双击 run.bat 启动主程序；"
            "也可以直接点击“启动主程序”。",
        )

    def _on_download_failed(self, message: str) -> None:
        self.status_label.setText(f"下载失败：{message}")
        self.status_label.setStyleSheet("color:#c4320a;")
        self._append_log(f"下载失败：{message}")
        QMessageBox.critical(
            self,
            "下载失败",
            f"{message}\n\n已下载的 .part 文件会保留，修复网络后可续传。",
        )

    def _on_download_cancelled(self) -> None:
        self.status_label.setText("已取消。已下载的 .part 文件会保留，可稍后续传。")
        self._append_log("用户取消了下载。")
        self._refresh_model_state()

    def _on_download_finished(self) -> None:
        worker = self._download_worker
        self._download_worker = None
        if worker is not None:
            worker.deleteLater()
        self.download_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.test_proxy_button.setEnabled(True)
        self.proxy_checkbox.setEnabled(True)
        self.proxy_edit.setEnabled(self.proxy_checkbox.isChecked())
        self.launch_button.setEnabled(model_looks_ready())
        self.close_button.setEnabled(True)
        self._refresh_model_state()

    def _cancel_download(self) -> None:
        worker = self._download_worker
        if worker is None:
            return
        worker.request_cancel()
        self.cancel_button.setEnabled(False)
        self.status_label.setText("正在取消，请稍候…")

    # ---- other actions ------------------------------------------------
    def _open_model_dir(self) -> None:
        DEFAULT_MODEL_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(DEFAULT_MODEL_DIR)))

    def _launch_app(self) -> None:
        if not model_looks_ready():
            QMessageBox.warning(self, "模型未就绪", "请先完成模型下载。")
            return
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        executable = pythonw if pythonw.is_file() else Path(sys.executable)
        try:
            subprocess.Popen(
                [str(executable), str(BASE_DIR / "app.py")],
                cwd=str(BASE_DIR),
            )
        except OSError as exc:
            QMessageBox.critical(self, "启动失败", f"无法启动主程序：\n{exc}")
            return
        self.status_label.setText("已启动主程序（独立进程）。可以关闭本向导。")

    def closeEvent(self, event) -> None:
        worker = self._download_worker
        if worker is not None and worker.isRunning():
            answer = QMessageBox.question(
                self,
                "下载进行中",
                "模型仍在下载。关闭会取消本次下载（.part 文件保留，可续传）。\n\n"
                "确定关闭吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            worker.request_cancel()
            if not worker.wait(5000):
                QMessageBox.information(
                    self,
                    "正在取消",
                    "网络请求尚未结束，已下载的 .part 文件会保留。请稍候再关闭。",
                )
                event.ignore()
                return
        proxy_worker = self._proxy_worker
        if proxy_worker is not None and proxy_worker.isRunning():
            if not proxy_worker.wait(10000):
                QMessageBox.information(
                    self,
                    "网络测试进行中",
                    "代理测试尚未结束，请稍候再关闭。",
                )
                event.ignore()
                return
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("LocalASRV2 Setup")
    app.setOrganizationName("LocalASRV2")
    app.setStyle("Fusion")
    window = SetupWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
