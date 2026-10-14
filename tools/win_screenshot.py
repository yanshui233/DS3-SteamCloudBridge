# -*- coding: utf-8 -*-
"""win_screenshot.py —— 在沙箱里给 Windows 窗口/屏幕截图 (纯 ctypes, 零依赖).

为什么要自己写
==============
本环境的 PowerShell 拦了 `Add-Type` (不能运行时编译 .NET), 所以 `System.Drawing`
那套截图方法用不了; pip 装 Pillow 又不通. 于是直接用 ctypes 调 GDI:
`PrintWindow` (CEF/Chrome 系窗口需要 `PW_RENDERFULLCONTENT=2`) -> `GetDIBits`
取 BGRA 位图 -> 手写 PNG 编码 (zlib + struct, 都是 stdlib).

用法
====
    python win_screenshot.py --title Steam --out shot.png --max-width 1280
    python win_screenshot.py --hwnd 132702 --out shot.png
    python win_screenshot.py --screen --out full.png        # 整个虚拟屏幕
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import struct
import sys
import zlib
from pathlib import Path

import win_dpi  # noqa: F401  import 即开启 DPI 感知 (截图坐标必须是物理像素)

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

BI_RGB = 0
DIB_RGB_COLORS = 0
PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", wt.LONG),
        ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


def find_window_by_title(substr: str) -> int:
    EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    hits: list[tuple[int, str]] = []

    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if substr.lower() in buf.value.lower():
            hits.append((int(hwnd), buf.value))
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return hits[0][0] if hits else 0


def grab(hwnd: int, *, full_content: bool = True) -> tuple[int, int, bytes]:
    """抓一个窗口 -> (width, height, BGRA 自顶向下)."""
    rect = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError("GetWindowRect 失败")
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise OSError(f"窗口尺寸非法: {w}x{h}")

    hdc = user32.GetWindowDC(hwnd)
    memdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(memdc, bmp)

    flag = PW_RENDERFULLCONTENT if full_content else 0
    ok = user32.PrintWindow(hwnd, memdc, flag)
    if not ok:
        gdi32.BitBlt(memdc, 0, 0, w, h, hdc, 0, 0, SRCCOPY)

    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h  # 负值 = 自顶向下
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = BI_RGB

    buf = ctypes.create_string_buffer(w * h * 4)
    got = gdi32.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(bi), DIB_RGB_COLORS)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(hwnd, hdc)

    if got == 0:
        raise OSError("GetDIBits 失败")
    return w, h, buf.raw


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def encode_png(width: int, height: int, rgb: bytes) -> bytes:
    """把**紧凑 RGB** 缓冲编成 PNG (8bit RGB, 每行 filter=0)."""
    stride = width * 3
    rows = bytearray()
    for y in range(height):
        rows.append(0)  # filter type 0
        rows += rgb[y * stride : (y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(rows), 6))
        + _png_chunk(b"IEND", b"")
    )


def downscale_rgb(w: int, h: int, rgb: bytes, max_w: int) -> tuple[int, int, bytes]:
    """最近邻缩放到宽度 <= max_w (输入是紧凑 RGB)."""
    if w <= max_w:
        return w, h, rgb
    ratio = max_w / w
    nw, nh = max_w, max(1, int(h * ratio))
    out = bytearray(nw * nh * 3)
    for y in range(nh):
        sy = min(h - 1, int(y / ratio))
        for x in range(nw):
            sx = min(w - 1, int(x / ratio))
            si = (sy * w + sx) * 3
            di = (y * nw + x) * 3
            out[di : di + 3] = rgb[si : si + 3]
    return nw, nh, bytes(out)


def _bgra_to_rgb(w: int, h: int, bgra: bytes) -> bytes:
    out = bytearray(w * h * 3)
    for i in range(w * h):
        s = i * 4
        d = i * 3
        out[d] = bgra[s + 2]
        out[d + 1] = bgra[s + 1]
        out[d + 2] = bgra[s]
    return bytes(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="win_screenshot")
    ap.add_argument("--title", default="", help="按标题子串找窗口")
    ap.add_argument("--hwnd", type=int, default=0)
    ap.add_argument("--screen", action="store_true", help="整个虚拟屏幕")
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-width", type=int, default=1280)
    a = ap.parse_args(argv[1:])

    if a.screen:
        hwnd = user32.GetDesktopWindow()
    elif a.hwnd:
        hwnd = a.hwnd
    elif a.title:
        hwnd = find_window_by_title(a.title)
        if not hwnd:
            print(f"✗ 没找到标题含 {a.title!r} 的窗口")
            return 3
    else:
        print("需要 --title / --hwnd / --screen 之一")
        return 2

    w, h, bgra = grab(hwnd)
    nw, nh, rgb = downscale_rgb(w, h, _bgra_to_rgb(w, h, bgra), a.max_width)
    png = encode_png(nw, nh, rgb)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png)
    print(f"已写出 {out}  ({nw}x{nh}, {len(png):,} B)  [原窗口 {w}x{h}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
