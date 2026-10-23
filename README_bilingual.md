# LocalASR V2

[中文 README（跳转至中文版）](#中文说明)

A Windows real-time local English speech transcription tool. It continuously captures system playback and performs near-real-time recognition on an NVIDIA GPU using **faster-whisper + Whisper large-v3-turbo**. It runs **completely offline**, with no cloud ASR API or API key required.

This is V2. It fixes the clipboard failure and TXT-saving freeze found in V1, and adds a standalone setup wizard with SOCKS5 proxy support and model-download progress. V2 uses its own `LocalASRV2` data directory and does not read or modify data from the old `LocalASRCaptioner` version.

![Main window](docs/main-window.png)

![Setup wizard](docs/setup-screenshot.png)

## Features

- Captures system audio from the default playback device via Windows WASAPI Loopback; no microphone, OBS, or virtual sound card is required.
- Runs local inference with faster-whisper large-v3-turbo + CTranslate2 CUDA float16.
- Continuous capture, WebRTC VAD segmentation, separate interim and final results, and deduplication by utterance ID.
- Live scrolling transcript with text selection, `Ctrl+C`, a right-click context menu, Copy All, and Copy Selected.
- Saves TXT files using Qt's non-native file dialog; automatic logging and manual export are kept separate.
- Unloads the model and releases VRAM when listening stops.
- Standalone setup wizard: configure and test a SOCKS5 proxy, download the model, view overall progress, and verify SHA256 hashes.
- Resumable model downloads with pinned-hash verification.
- No database, server, or cloud-based recognition.

## Technology choices

- **faster-whisper + CTranslate2 + Whisper large-v3-turbo**: CTranslate2 runs the converted FP16 model directly, which fits comfortably in 8 GB of VRAM on an RTX 5060. In testing, 11 seconds of English audio took about 0.3 seconds to process (35–40× real time).
- PyTorch and the OpenAI Whisper runtime are not used: this task does not require training or the PyTorch ecosystem, and CTranslate2 has lower startup and VRAM overhead.
- `whisper.cpp` is not used: GPU integration and the Python segmentation pipeline would be more complicated, with no performance advantage on the test machine.
- Distil/small models are not used: `large-v3-turbo` has already been verified to work with 8 GB of VRAM, so sacrificing accuracy to save memory is unnecessary.
- The GPU uses `float16` exclusively. On RTX 50-series GPUs (Blackwell / `sm_120`), avoid `int8_float16`; some CTranslate2 versions may report `CUBLAS_STATUS_NOT_SUPPORTED`.
- The model is not truly streaming. Near-real-time behavior is achieved with WebRTC VAD segmentation, interim previews, and final confirmation; the project does not claim word-by-word streaming.

## Quick start

Requirements: Windows 10/11 x64, Python 3.10 x64, and an NVIDIA driver. Model loading requires an NVIDIA GPU with CUDA support; the test machine uses an RTX 5060 with 8 GB of VRAM.

### 1. Install dependencies

Double-click `install.bat`, or run:

```powershell
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

You do not need to install the full CUDA Toolkit. `requirements.txt` includes the CUDA 12.8 cuBLAS / cuDNN / NVRTC runtime libraries. PyTorch is not required either.

### 2. Initialize and download the model

Double-click `init.bat`, or run:

```powershell
python setup_wizard.py
```

In the standalone wizard:

1. If needed, enable **Download through SOCKS5 proxy** and enter an address such as `socks5h://127.0.0.1:7897`. You can click **Test connection** first.
2. Click **Start download / verify model**. The first download is approximately 1.62 GB.
3. Once the wizard reports **All model files downloaded and verified with SHA256**, you can close it.

You can also use the command-line downloader:

```powershell
python download_model.py
python download_model.py --proxy socks5h://127.0.0.1:7897
```

`init.bat` only launches the setup wizard; it does not load the recognition model. `setup_wizard.py` is completely separate from the main program, `app.py`.

### 3. Launch the application

Double-click `run.bat`, or run:

```powershell
python app.py
```

The main window opens directly. Click **Start Listening** to load and warm up the model. Begin playing English audio after the status changes to **Listening**.

## Setup wizard and proxy

**Wizard:** [setup_wizard.py](setup_wizard.py)  
**Launcher:** [init.bat](init.bat)

The following proxy formats are supported:

```text
127.0.0.1:7897
socks5://127.0.0.1:7897
socks5h://127.0.0.1:7897
socks5h://user:password@127.0.0.1:7897
```

- `socks5h` routes DNS resolution through the proxy as well, which is usually more suitable for downloads from overseas.
- Only SOCKS5 is supported; HTTP proxies are not supported.
- The proxy is used only for setup downloads. Recognition does not access the network and does not require a proxy.
- The proxy address is stored locally at `%APPDATA%\LocalASRV2\config.json`, and is not included in the repository. If the proxy contains a username and password, they are stored in plain text under the current Windows user's profile. Do not share or commit this file.
- The downloader explicitly sets `trust_env=False`, so it will not silently use proxy settings from the system environment. Only a proxy entered and enabled in the wizard takes effect.

## Model and integrity

- Model repository: [ModelScope · pengzhendong/faster-whisper-large-v3-turbo](https://www.modelscope.cn/models/pengzhendong/faster-whisper-large-v3-turbo)
- Model directory: `models/faster-whisper-large-v3-turbo/`
- Pinned SHA256 for `model.bin`:

```text
e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da
```

Pinned hashes for all files are defined in `PINNED_SHA256` in `model_downloader.py`; the downloader does not simply trust the hash returned by the server. It verifies each file's SHA256 before moving it to its final filename. If a download is interrupted, the `.part` file is retained so the download can resume later.

Model weights are large and **are not committed to GitHub** (`models/*` is excluded by `.gitignore`). Users download them the first time they run the setup wizard. The model is the CTranslate2 FP16 conversion of `large-v3-turbo`.

If ModelScope is unavailable, you can also download files from Hugging Face's `Systran/faster-whisper-large-v3-turbo` repository, then rerun the wizard for verification. Example using the local SOCKS5 proxy:

```powershell
curl.exe -L --proxy socks5h://127.0.0.1:7897 `
  -o models\faster-whisper-large-v3-turbo\model.bin `
  https://huggingface.co/Systran/faster-whisper-large-v3-turbo/resolve/main/model.bin
```

You must also download `config.json`, `preprocessor_config.json`, `tokenizer.json`, and `vocabulary.json` into the same directory.

## Usage

1. Play English audio through the **Windows default playback device**.
2. Click **Start Listening**.
3. The **Current utterance** area shows interim results. Final results are appended to **Confirmed transcript** above and immediately written to the day's automatic log.
4. **Pause Listening** stops capture while keeping the model loaded. **Resume Listening** reopens the default output device.
5. **Stop Listening** stops capture, processes queued results, and unloads the model to release VRAM.
6. **Copy All** copies all confirmed text; **Copy Selected** copies the selected text. You can also use `Ctrl+C` or the right-click context menu.
7. **Save TXT** opens Qt's built-in non-native file dialog, allowing you to save to any path. The default directory is `Documents\LocalASRV2\exports\`.
8. **Clear Display** clears only the UI and does not delete automatic logs. A confirmation dialog explains this.

Automatic recovery: on startup, the most recent automatic log is loaded into the interface.

## Data and privacy

- Automatic transcripts: `%USERPROFILE%\Documents\LocalASRV2\transcripts\transcripts-YYYY-MM-DD.txt`
- Manual exports: default `%USERPROFILE%\Documents\LocalASRV2\exports\`; users can choose another directory.
- Application log: `%LOCALAPPDATA%\LocalASRV2\logs\app.log` (does not contain the full recognized transcript).
- Setup configuration: `%APPDATA%\LocalASRV2\config.json` (proxy settings and schema version only).

Recognition runs entirely on the local machine; audio is never sent to the cloud. After the initial model download, `app.py` does not need a network connection. V2's data directory is separate from the old `LocalASRCaptioner` directory.

## Fixes in this release

### Clipboard operations failing

The root cause was not the buttons themselves. In V1, `soundcard` was imported at the top level on the GUI thread. During import, SoundCard initialized the current thread's COM apartment as **MTA**, while the Qt/Windows clipboard uses OLE and requires the GUI thread to be **STA**. As a result, `Qt`'s `OleInitialize()` failed, breaking the right-click Copy command, `Ctrl+C`, and the Copy buttons.

V2 fixes this by:

- Never importing SoundCard on the GUI thread. It is imported lazily inside the capture thread, which manages its own COM lifecycle.
- Adding `clipboard_utils.py`, which reads the clipboard back through Qt to verify writes and falls back to Win32 `CF_UNICODETEXT` if needed.
- Using a custom `CopyablePlainTextEdit` so `Ctrl+C`, the right-click **Copy selected text** command, and the Copy buttons all use the same reliable path.
- Verifying clipboard writes by reading them from an external process for the buttons and `Ctrl+C`, and by reading them back locally for the right-click menu.

### TXT save dialog freezing

The same COM issue can affect the native Windows file dialog. V2 uses Qt's dialog with `QFileDialog.Option.DontUseNativeDialog`. Saving now uses local UTF-8 writes and atomic replacement, without relying on shell COM.

In testing, the save dialog opened and completed a save in about 0.5 seconds, with the correct TXT contents.

## How it works

```text
Windows default playback device
  → SoundCard / WASAPI Loopback (16 kHz, mono, float32)
  → Capture thread reads a chunk every 100 ms (device remains open)
  → 20 ms frames + WebRTC VAD
  → Speech segment (ends after 700 ms of silence; maximum 12 s)
  → AsrWorker QThread: faster-whisper / CUDA float16
  → Qt queued signals → update UI / append TXT on the main thread
```

- Two-stage output: while speech is ongoing, interim text is updated about every 1.6 seconds using `beam_size=1`; when a segment ends, final text is generated using `beam_size=5`.
- Final results are deduplicated using globally unique utterance IDs. Interim previews are not written to automatic logs.
- Audio segments do not overlap, preventing duplicate text across segments; the trade-off is that an unusually long pause may split a sentence.
- Capture, inference, and GUI operations run on separate threads, so the GUI does not wait for GPU inference to finish.
- Stop and pause operations end the capture thread normally and submit the final segment for processing.

## Benchmark results

Test environment: Windows 11 22H2 (build 22621) · Python 3.10.11 · RTX 5060 8 GB · driver 610.88 · CTranslate2 4.8.3 · faster-whisper 1.2.1.

| Test | Result |
|---|---|
| Model load onto GPU | About 3 seconds |
| 11-second JFK sample after warm-up | 0.27–0.31 seconds, about 35–40× real time |
| 13.3-second TTS system-loopback test | 3 correct segments; 3 lines in the automatic log |
| VRAM usage | About 3.3–3.4 GB for the whole GPU; model adds about 2.1–2.3 GB |
| After stopping listening | Total GPU memory use drops to about 1.1–1.3 GB |
| Clipboard | Button and `Ctrl+C` verified by reading from an external process; right-click copy verified by local read-back |
| Save TXT | Qt non-native dialog; completes in about 0.5 seconds |
| Setup wizard | Both direct connection and `socks5h://127.0.0.1:7897` succeeded; model verification reached 100% |
| Pause / resume / stop / restart | Passed |
| Offline operation | Model loading and inference passed with an invalid HTTP(S) proxy and `HF_HUB_OFFLINE=1` |

## GPU verification

Run:

```powershell
python diagnose.py --audio samples\jfk.wav
```

This checks the NVIDIA driver, CUDA device, CTranslate2 version, VRAM, and model SHA256, then performs an actual recognition run with `device="cuda"`, `float16`, and `local_files_only=True`.

If the GPU is unavailable, the main program falls back to CPU `int8` and displays a yellow warning explicitly identifying **CPU mode**. It will not pretend that the GPU is enabled.

## Tests

Unit tests that do not need to load the model or an audio device:

```powershell
python -m unittest discover -s tests -v
```

Coverage includes proxy formats and configuration read/write, log append and recovery, atomic TXT export, the pinned model-hash set, and VAD segmentation of the JFK sample.

## Project structure

```text
LocalASRV2/
├─ app.py                 # Main app: GUI, capture, VAD, inference thread, persistence
├─ setup_wizard.py        # Standalone setup GUI: proxy test and model-download progress
├─ model_downloader.py    # Shared downloader: direct/SOCKS5, resume, progress, SHA256
├─ app_config.py          # Local config (%APPDATA%\LocalASRV2\config.json)
├─ clipboard_utils.py     # Reliable Qt + Win32 clipboard writes
├─ runtime_setup.py       # Paths, CUDA DLLs, VRAM query
├─ diagnose.py            # GPU / offline inference verification
├─ download_model.py      # Command-line downloader (advanced)
├─ requirements.txt
├─ install.bat            # Install dependencies, then open setup wizard
├─ init.bat               # Open setup wizard only
├─ run.bat                # Launch main app (without console)
├─ run_debug.bat          # Launch with console for troubleshooting
├─ docs/                  # Screenshots
├─ samples/jfk.wav        # English test audio
├─ samples/tts_en.wav     # English TTS test audio
├─ tests/test_core.py     # Fast unit tests
├─ models/                # Model directory (weights are not committed)
├─ LICENSE
└─ README.md
```

## Dependencies

See [requirements.txt](requirements.txt). Key versions:

- PySide6 6.12.0
- SoundCard 0.4.6
- numpy 2.2.6
- faster-whisper 1.2.1
- ctranslate2 4.8.3
- webrtcvad-wheels 2.0.14.post1
- requests 2.32.5
- PySocks 1.7.1
- nvidia-cublas-cu12 12.8.5.5 / nvidia-cudnn-cu12 9.10.2.21 / nvidia-cuda-nvrtc-cu12 12.9.86

## Known limitations

- English only; `language="en"` is fixed.
- This is not word-by-word streaming inference. Results are produced per VAD speech segment, up to 12 seconds long. For continuous speech, final-result latency can approach 12 seconds.
- The fixed silence threshold may split one sentence into two segments. The words are usually preserved, but punctuation and sentence boundaries may be unnatural.
- Only the Windows default playback device is monitored; audio from players using exclusive mode may not be captured.
- Background music, heavy noise, or low volume can reduce VAD segmentation and recognition quality.
- Stopping listening unloads the model; starting again requires a reload that takes several seconds.
- The first CUDA inference may be affected by driver or shader-cache initialization; the UI displays the warm-up status.
- Testing was performed on Windows 11 22H2. Windows 10 uses the same WASAPI/Qt interfaces, but has not been re-verified on physical Windows 10 hardware.
- Only SOCKS5 proxies are supported. Proxy settings are stored in plain text under the current Windows user's profile.

## Frequently asked questions

**Copy does nothing**

Make sure you are running V2 (`app.py` should not import `soundcard` on the main thread), then check `%LOCALAPPDATA%\LocalASRV2\logs\app.log`. V2 displays a red error message if copying fails instead of failing silently.

**The save dialog will not open or freezes**

V2 forces the Qt non-native file dialog. If system security software blocks file dialogs, check in File Explorer that `Documents\LocalASRV2\exports` is writable. A save failure triggers an error dialog.

**The model is missing**

Run `init.bat` and download it through the wizard. You can also run `python download_model.py`.

**GPU warning / CPU mode**

Check `nvidia-smi` and the dependency versions. The GPU uses `float16` exclusively. Do not change it to `int8_float16`; some CTranslate2 versions on RTX 50-series GPUs may report `CUBLAS_STATUS_NOT_SUPPORTED`.

**Proxy test fails**

Check the SOCKS5 address and port, for example `socks5h://127.0.0.1:7897`. The proxy affects setup downloads only and does not affect offline recognition.

## Publishing to GitHub

Model weights, configuration, logs, and `.part` files are excluded by `.gitignore`. Recommended commands:

```powershell
cd LocalASRV2
git init
git add .
git status          # Make sure models/ and config files are not staged
git commit -m "LocalASR V2: local English captions with setup wizard"
git branch -M main
git remote add origin https://github.com/<your-name>/<repo>.git
git push -u origin main
```

The repository should include the `LICENSE`, README, source code, launch scripts, and screenshots. Do not commit `model.bin`.

## Third-party components and license

The project source code is licensed under the MIT License; see [LICENSE](LICENSE).

Third-party components:

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (MIT)
- [CTranslate2](https://github.com/OpenNMT/CTranslate2) (MIT)
- [SoundCard](https://github.com/bastibe/SoundCard) (MIT)
- The Whisper `large-v3-turbo` model and converted weights follow their upstream license (MIT); see the ModelScope link above for the model source.
- `samples/jfk.wav` comes from the `whisper.cpp` example audio and is used for local testing.

---

<a id="中文说明"></a>
# 中文 README

Windows 本地实时英文语音转写工具。持续采集系统播放的声音，通过 **faster-whisper + Whisper large-v3-turbo** 在 NVIDIA GPU 上近实时识别，**完全离线运行，不需要任何云端 ASR API 或 API Key**。

这是 V2 版本，修复了 V1 的剪贴板失效和保存 TXT 卡死问题，并加入独立的初始化向导（SOCKS5 代理 + 模型下载进度）。V2 使用独立的 `LocalASRV2` 数据目录，不会读取或修改旧版 `LocalASRCaptioner` 的数据。

![主界面](docs/main-window.png)

![初始化向导](docs/setup-screenshot.png)

## 功能

- Windows WASAPI Loopback 采集默认播放设备的系统音频，不使用麦克风、OBS 或虚拟声卡
- faster-whisper large-v3-turbo + CTranslate2 CUDA float16 本地推理
- 连续采集、WebRTC VAD 分段、临时结果与最终结果分离、按 utterance id 去重
- 实时文本滚动显示，支持鼠标选择、`Ctrl+C`、右键菜单、复制全部、复制选中
- TXT 保存使用 Qt 非原生文件对话框，自动日志与手动导出分离
- 停止监听时卸载模型并释放显存
- 独立的初始化向导：可填写 SOCKS5 代理、测试代理、下载模型、显示整体进度、校验 SHA256
- 模型下载支持断点续传和固定哈希校验
- 无数据库、无服务端、无云端识别

## 技术选型

- **faster-whisper + CTranslate2 + Whisper large-v3-turbo**：CTranslate2 直接运行 FP16 转换模型，RTX 5060 8 GB 可轻松容纳；实测 11 秒英文音频约 0.3 秒完成（35–40× 实时）。
- 不使用 PyTorch/OpenAI whisper 运行时：本任务不需要训练或 PyTorch 生态，CTranslate2 启动和显存开销更小。
- 不使用 whisper.cpp：GPU 集成和 Python 分段管线更复杂，本机没有性能优势。
- 不使用 distil/small 模型：large-v3-turbo 在 8 GB 显存下已验证可用，没必要为省显存牺牲准确率。
- GPU 固定使用 `float16`。RTX 50 系（Blackwell/sm_120）不要使用 `int8_float16`，部分 CTranslate2 版本会报 `CUBLAS_STATUS_NOT_SUPPORTED`。
- 模型本身不是真正流式模型；这里通过 WebRTC VAD 分段、临时预览和最终确认实现近实时，不宣称逐词流式。

## 快速开始

环境：Windows 10/11 x64、Python 3.10 x64、NVIDIA 驱动。模型加载需要支持 CUDA 的 NVIDIA GPU；本机为 RTX 5060 8 GB。

### 1. 安装依赖

双击 `install.bat`，或：

```powershell
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

不需要安装完整 CUDA Toolkit，`requirements.txt` 已包含 CUDA 12.8 的 cuBLAS / cuDNN / NVRTC 运行库。也不需要 PyTorch。

### 2. 初始化并下载模型

双击 `init.bat`，或：

```powershell
python setup_wizard.py
```

在独立向导中：

1. 如需要，勾选“通过 SOCKS5 代理下载”，填写例如 `socks5h://127.0.0.1:7897`，可先点“测试连接”。
2. 点击“开始下载 / 校验模型”。首次需要下载约 1.62 GB。
3. 看到“全部模型文件已下载并通过 SHA256 校验”后，可以关闭向导。

也可以使用命令行下载器：

```powershell
python download_model.py
python download_model.py --proxy socks5h://127.0.0.1:7897
```

`init.bat` 只启动初始化向导，不会加载识别模型；`setup_wizard.py` 与主程序 `app.py` 完全独立。

### 3. 启动主程序

```text
双击 run.bat
```

或：

```powershell
python app.py
```

程序直接显示主界面。点击“开始监听”后会加载并预热模型；状态变为“正在监听”后再播放英语音频。

## 初始化向导与代理

**向导文件：** [setup_wizard.py](setup_wizard.py)  
**启动脚本：** [init.bat](init.bat)

支持以下代理写法：

```text
127.0.0.1:7897
socks5://127.0.0.1:7897
socks5h://127.0.0.1:7897
socks5h://user:password@127.0.0.1:7897
```

- `socks5h` 会让 DNS 也通过代理，通常更适合海外下载。
- 只支持 SOCKS5；不支持 HTTP 代理。
- 代理仅用于初始化下载。识别运行时不会联网，也不需要代理。
- 代理地址保存在本机 `%APPDATA%\LocalASRV2\config.json`，不会进入仓库。如果代理包含账号密码，该文件会以明文保存在当前 Windows 用户目录；请勿分享或提交该文件。
- 下载器显式设置 `trust_env=False`，不会悄悄使用系统环境里的代理；只有界面上填写并启用的代理才会生效。

## 模型与完整性

- 模型仓库：[ModelScope · pengzhendong/faster-whisper-large-v3-turbo](https://www.modelscope.cn/models/pengzhendong/faster-whisper-large-v3-turbo)
- 模型目录：`models/faster-whisper-large-v3-turbo/`
- `model.bin` 固定 SHA256：

```text
e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da
```

所有文件的固定哈希都在 `model_downloader.py` 的 `PINNED_SHA256` 中，不只听信服务端返回值。下载器每完成一个文件就校验 SHA256，校验通过后才移动到正式文件名；中断会保留 `.part` 文件，下次继续。

模型权重体积大，**不会提交到 GitHub**（`.gitignore` 已排除 `models/*`）。用户首次运行初始化向导时下载。模型是 `large-v3-turbo` 的 CTranslate2 FP16 转换版。

如 ModelScope 不可用，也可以从 Hugging Face 下载 `Systran/faster-whisper-large-v3-turbo` 的文件，然后重新运行向导校验；使用本机 SOCKS5 的示例：

```powershell
curl.exe -L --proxy socks5h://127.0.0.1:7897 `
  -o models\faster-whisper-large-v3-turbo\model.bin `
  https://huggingface.co/Systran/faster-whisper-large-v3-turbo/resolve/main/model.bin
```

需要同时下载 `config.json`、`preprocessor_config.json`、`tokenizer.json`、`vocabulary.json` 到同一个目录。

## 使用

1. 让英语音频从 **Windows 默认播放设备** 播放。
2. 点击“开始监听”。
3. “当前句”区域显示临时结果；最终结果追加到上方“已确认文本”，并立即写入当天的自动日志。
4. “暂停监听”停止采集但保留已加载的模型；“继续监听”会重新打开默认输出设备。
5. “停止监听”停止采集、处理完已排队结果，并卸载模型释放显存。
6. “复制全部”复制所有已确认文本；“复制选中”复制当前选中的文字。也可以使用 `Ctrl+C` 或右键菜单。
7. “保存 TXT”打开 Qt 自带（非原生）文件对话框，可选择任意路径保存。默认目录为 `Documents\LocalASRV2\exports\`。
8. “清空显示”只清空界面，不删除自动日志；确认框会明确提示。

自动恢复：启动时会载入最近一份自动日志到界面。

## 数据与隐私

- 自动日志：`%USERPROFILE%\Documents\LocalASRV2\transcripts\transcripts-YYYY-MM-DD.txt`
- 手动导出：默认 `%USERPROFILE%\Documents\LocalASRV2\exports\`，可由用户选择其他目录
- 应用日志：`%LOCALAPPDATA%\LocalASRV2\logs\app.log`（不含完整识别文本）
- 初始化配置：`%APPDATA%\LocalASRV2\config.json`（仅代理和 schema 版本）

识别完全在本机进行；音频不会发往云端。初始化下载完成之后，运行 `app.py` 不需要网络。V2 的数据目录与旧版 `LocalASRCaptioner` 分离。

## 本次修复说明

### 剪贴板全部失效

根因并不在按钮本身：V1 在 GUI 主线程顶层 `import soundcard`。SoundCard 在导入时会把当前线程的 COM 初始化为 **MTA**，而 Qt/Windows 的剪贴板使用 OLE，要求 GUI 线程是 **STA**。结果 Qt 的 `OleInitialize()` 失败，右键复制、`Ctrl+C` 和复制按钮全部失效。

V2 的修复：

- 绝不在 GUI 线程导入 SoundCard；改为在采集线程内延迟导入，并由采集线程自己管理 COM 生命周期。
- 新增 `clipboard_utils.py`，写入后用 Qt 读回验证；失败时回退到 Win32 `CF_UNICODETEXT`。
- 文本区域使用自定义 `CopyablePlainTextEdit`：`Ctrl+C`、右键菜单“复制选中内容”、复制按钮走同一条可靠路径。
- 验证结果：按钮和 `Ctrl+C` 通过外部进程读取剪贴板确认；右键菜单“复制选中内容”通过本地剪贴板读回确认。

### 保存 TXT 卡死

同一 COM 问题会影响 Windows 原生文件对话框。V2 使用 `QFileDialog.Option.DontUseNativeDialog` 的 Qt 对话框，保存时只进行本地 UTF-8 写入和原子替换，不再依赖 shell COM。

实测保存对话框打开并确认约 0.5 秒完成，TXT 内容正确。

## 工作原理

```text
Windows 默认播放设备
  → SoundCard / WASAPI Loopback（16 kHz、单声道、float32）
  → 采集线程每 100 ms 读取一块（设备持续打开）
  → 20 ms 帧 + WebRTC VAD
  → 语音段（静音 700 ms 结束，最长 12 s）
  → AsrWorker QThread：faster-whisper / CUDA float16
  → Qt queued signals → 主线程更新界面 / 追加 TXT
```

- 两阶段输出：语音进行中约每 1.6 秒用 `beam_size=1` 更新临时句；段结束时用 `beam_size=5` 生成最终文本。
- 最终结果按全局唯一的 utterance id 去重，预览结果不会写入自动日志。
- 音频段之间不重叠，因此不会产生重叠文本重复；代价是极端停顿处可能切断句子。
- 采集、推理、GUI 分属不同线程；GUI 不等待 GPU 推理完成。
- 停止/暂停通过正常结束采集线程并提交最后一段处理。

## 实测结果

测试环境：Windows 11 22H2（build 22621）· Python 3.10.11 · RTX 5060 8 GB · 驱动 610.88 · CTranslate2 4.8.3 · faster-whisper 1.2.1。

| 项目 | 结果 |
|---|---|
| 模型加载到 GPU | 约 3 秒 |
| 预热后 11 秒 JFK 推理 | 0.27–0.31 秒，约 35–40× 实时 |
| 13.3 秒 TTS 系统回环测试 | 3 段正确结果，自动日志 3 行 |
| 显存 | 整卡约 3.3–3.4 GB；模型额外约 2.1–2.3 GB |
| 停止监听 | 整卡显存回落到约 1.1–1.3 GB |
| 剪贴板 | 按钮和 `Ctrl+C` 经外部进程读回验证；右键菜单复制通过本地读回验证 |
| 保存 TXT | Qt 非原生对话框，约 0.5 秒完成 |
| 初始化向导 | 直连和 `socks5h://127.0.0.1:7897` 测试均成功；模型校验 100% |
| 暂停/继续/停止/重新开始 | 通过 |
| 离线 | 无效 HTTP(S) 代理 + `HF_HUB_OFFLINE=1` 下模型加载和推理仍通过 |

## GPU 验证

```powershell
python diagnose.py --audio samples\jfk.wav
```

会检查 NVIDIA 驱动、CUDA 设备、CTranslate2 版本、显存、模型 SHA256，并以 `device="cuda"`、`float16`、`local_files_only=True` 完成一次实际识别。

如果 GPU 不可用，主程序会切换到 CPU `int8`，并在界面显示黄色警告，明确标注“CPU 模式”；不会假装 GPU 已启用。

## 测试

不需要加载模型或音频设备的单元测试：

```powershell
python -m unittest discover -s tests -v
```

覆盖代理格式/配置读写、日志追加与恢复、原子 TXT 导出、固定模型哈希集合，以及 JFK 样本的 VAD 分段。

## 项目结构

```text
LocalASRV2/
├─ app.py                 # 主程序：GUI、采集、VAD、推理线程、持久化
├─ setup_wizard.py        # 独立初始化 GUI：代理测试、模型下载进度
├─ model_downloader.py    # 共享下载器：直连/SOCKS5、续传、进度、SHA256
├─ app_config.py          # 本地配置 (%APPDATA%\LocalASRV2\config.json)
├─ clipboard_utils.py     # Qt + Win32 剪贴板可靠写入
├─ runtime_setup.py       # 路径、CUDA DLL、显存查询
├─ diagnose.py            # GPU/离线推理验证
├─ download_model.py      # 命令行下载器（高级）
├─ requirements.txt
├─ install.bat            # 安装依赖后打开初始化向导
├─ init.bat               # 只打开初始化向导
├─ run.bat                # 启动主程序（无控制台）
├─ run_debug.bat          # 带控制台启动，排错
├─ docs/                  # 截图
├─ samples/jfk.wav        # 英语测试音频
├─ samples/tts_en.wav     # 英语 TTS 测试音频
├─ tests/test_core.py     # 快速单元测试
├─ models/                # 模型目录（不提交权重）
├─ LICENSE
└─ README.md
```

## 依赖

见 [requirements.txt](requirements.txt)。关键版本：

- PySide6 6.12.0
- SoundCard 0.4.6
- numpy 2.2.6
- faster-whisper 1.2.1
- ctranslate2 4.8.3
- webrtcvad-wheels 2.0.14.post1
- requests 2.32.5
- PySocks 1.7.1
- nvidia-cublas-cu12 12.8.5.5 / nvidia-cudnn-cu12 9.10.2.21 / nvidia-cuda-nvrtc-cu12 12.9.86

## 已知限制

- 仅支持英语，固定 `language="en"`。
- 不是逐词流式推理；结果以 VAD 语音段为单位，最长 12 秒。连续讲话的最终结果延迟可能接近 12 秒。
- 固定静音阈值可能把一句话切成两段，词内容通常完整但标点/断句可能不自然。
- 仅监听 Windows 默认播放设备；独占模式播放器可能无法采集。
- 有背景音乐、强噪声或音量过低时，VAD 分段和识别质量会下降。
- 停止监听会卸载模型；再次开始需重新加载数秒。
- 首次 CUDA 推理可能受驱动/着色器缓存影响，界面会显示预热状态。
- 实测为 Windows 11 22H2；Windows 10 使用同样的 WASAPI/Qt 接口，但未在实体 Windows 10 上重复验证。
- 代理仅支持 SOCKS5，代理配置以明文保存在当前 Windows 用户目录。

## 常见问题

**复制还是没有反应**  
确认运行的是 V2（`app.py` 不应在主线程 import soundcard），并查看 `%LOCALAPPDATA%\LocalASRV2\logs\app.log`。V2 的复制路径会在失败时显示红色错误提示，而不是静默失败。

**保存窗口打不开/卡住**  
V2 强制使用 Qt 非原生文件对话框。如果系统级安全软件拦截文件对话框，可先在资源管理器确认 `Documents\LocalASRV2\exports` 可写。保存失败会弹出错误对话框。

**模型缺失**  
运行 `init.bat`，在向导中下载。也可以执行 `python download_model.py`。

**GPU 警告 / CPU 模式**  
检查 `nvidia-smi` 和依赖版本。GPU 固定使用 float16；不要改成 `int8_float16`，RTX 50 系在部分 CTranslate2 版本上会报 `CUBLAS_STATUS_NOT_SUPPORTED`。

**代理测试失败**  
检查 SOCKS5 地址和端口，例如 `socks5h://127.0.0.1:7897`。代理只影响初始化下载，不影响离线识别。

## 发布到 GitHub

模型权重、配置、日志和 `.part` 文件已被 `.gitignore` 排除。建议：

```powershell
cd LocalASRV2
git init
git add .
git status          # 确认 models/ 和 config 未被加入
git commit -m "LocalASR V2: local English captions with setup wizard"
git branch -M main
git remote add origin https://github.com/<your-name>/<repo>.git
git push -u origin main
```

仓库应包含 `LICENSE`、README、源代码、启动脚本和截图；不要提交 `model.bin`。

## 第三方与许可

本项目源代码使用 MIT License，见 [LICENSE](LICENSE)。

第三方组件：

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)（MIT）
- [CTranslate2](https://github.com/OpenNMT/CTranslate2)（MIT）
- [SoundCard](https://github.com/bastibe/SoundCard)（MIT）
- Whisper `large-v3-turbo` 模型及转换权重遵循其上游许可（MIT）；模型来源见上文 ModelScope 链接。
- `samples/jfk.wav` 来自 whisper.cpp 示例音频，用于本地测试。
