"""Clipboard helpers that work reliably in a Qt GUI application.

Qt normally does the right thing on Windows, but the Windows clipboard is an
OLE feature: it requires the GUI thread to be an STA (single-threaded
apartment).  Some audio libraries initialize COM as MTA in the main thread,
after which Qt can no longer use OLE clipboard or native file dialogs.  This
module (a) keeps clipboard access in one place and (b) provides a direct
Win32 fallback, so Ctrl+C, context menus, and the copy buttons are not left
silently broken.

Only plain text is supported.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from PySide6.QtWidgets import QApplication

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _win32_clipboard_set(text: str) -> tuple[bool, str]:
    """Set CF_UNICODETEXT with the Win32 API.  Returns (ok, error)."""

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL

    opened = False
    for _ in range(8):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.04)
    if not opened:
        return False, "剪贴板正被其他程序占用，请稍后重试"

    handle = None
    try:
        if not user32.EmptyClipboard():
            return False, f"清空剪贴板失败（Win32 错误 {ctypes.get_last_error()}）"
        # Null-terminated UTF-16LE, as required by CF_UNICODETEXT.
        data = text.encode("utf-16-le") + b"\x00\x00"
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return False, f"分配剪贴板内存失败（Win32 错误 {ctypes.get_last_error()}）"
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return False, f"锁定剪贴板内存失败（Win32 错误 {ctypes.get_last_error()}）"
        try:
            ctypes.memmove(pointer, data, len(data))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            return False, f"写入剪贴板失败（Win32 错误 {ctypes.get_last_error()}）"
        handle = None  # Ownership transferred to the clipboard.
        return True, ""
    finally:
        if handle:
            kernel32.GlobalFree(handle)
        user32.CloseClipboard()


def copy_text(text: str) -> tuple[bool, str]:
    """Copy text to the Windows clipboard and verify it can be read back.

    Returns (ok, error_message).  It first uses Qt, then falls back to
    Win32 CF_UNICODETEXT so the copy controls keep working even if the Qt/OLE
    path is unavailable.
    """

    if text is None or text == "":
        return False, "没有可复制的内容"

    qt_error = ""
    try:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)
            QApplication.processEvents()
            if clipboard.text() == text:
                return True, ""
            qt_error = "Qt 剪贴板写入后读回内容不一致"
    except Exception as exc:  # Clipboard backends can fail in several ways.
        qt_error = f"Qt 剪贴板错误：{exc}"

    ok, win_error = _win32_clipboard_set(text)
    if ok:
        return True, ""
    detail = "；".join(part for part in (qt_error, win_error) if part)
    return False, detail or "未知剪贴板错误"
