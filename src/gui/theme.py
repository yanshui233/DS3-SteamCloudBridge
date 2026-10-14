"""GUI 主题层 —— 色值 / 字体 / 圆角卡片绘制.

**界面代码里不出现任何颜色字面量**: 换一次配色只改这一个文件.
(`app.py` / `widgets.py` 里出现 `#RRGGBB` 就算违规, 有测试盯着.)

配色是浅色的: 目标用户拿到的是一个"软件", 浅色在白天更常见, 也更接近
Windows 自己的设置界面. 所有前景/背景都是**成对**定义的 (`x` 配 `x_soft`),
避免出现"深底深字"那种看不清的组合.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont

# ---------------------------------------------------------------- 配色

C: dict[str, str] = {
    # 表面
    "bg": "#F2F4F8",  # 窗口底
    "surface": "#FFFFFF",  # 卡片
    "surface_alt": "#F7F9FC",  # 次级面 (代码框 / 表头)
    "border": "#E1E6EF",
    "border_soft": "#EDF1F7",
    # 主色
    "accent": "#2F6FED",
    "accent_hover": "#1E5CE0",
    "accent_active": "#1A4FC4",
    "accent_soft": "#EAF1FE",
    "accent_ink": "#1B4DB1",
    # 文字
    "text": "#0F172A",
    "text_dim": "#5B6B84",
    "text_faint": "#93A0B4",
    "on_accent": "#FFFFFF",
    # 语义色 (前景 + 浅底, 成对使用)
    "ok": "#0F9D6B",
    "ok_soft": "#E7F7F0",
    "warn": "#B8760A",
    "warn_soft": "#FDF3E2",
    "bad": "#D64545",
    "bad_soft": "#FDECEC",
    "neutral": "#5B6B84",
    "neutral_soft": "#F1F5F9",
    # 侧栏
    "sidebar": "#FFFFFF",
    "sidebar_hover": "#F5F8FF",
    "sidebar_sel": "#EAF1FE",
}


# ---------------------------------------------------------------- 字体

_UI_CANDIDATES = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "PingFang SC",
    "Segoe UI",
    "SimHei",
    "TkDefaultFont",
)
_MONO_CANDIDATES = ("Consolas", "Cascadia Mono", "Courier New", "TkFixedFont")


class Fonts:
    """一套字体. 用 `init(root)` 建好后全局取用."""

    def __init__(self, ui: str, mono: str) -> None:
        self.ui_family = ui
        self.mono_family = mono
        self.brand = (ui, 15, "bold")
        self.title = (ui, 17, "bold")
        self.subtitle = (ui, 11)
        self.h2 = (ui, 12, "bold")
        self.body = (ui, 11)
        self.body_bold = (ui, 11, "bold")
        self.small = (ui, 10)
        self.small_bold = (ui, 10, "bold")
        self.tiny = (ui, 9)
        self.step = (ui, 11)
        self.step_bold = (ui, 11, "bold")
        self.badge = (ui, 9, "bold")
        self.btn = (ui, 11, "bold")
        self.btn_sm = (ui, 10, "bold")
        self.mono = (mono, 13, "bold")
        self.mono_sm = (mono, 10)
        self.mono_xs = (mono, 9)


_F: Fonts | None = None
_SCALE: float = 1.0  # 屏幕 DPI 缩放比, 由 `screen_scale()` 写入


def init(root: tk.Misc) -> Fonts:
    """探测系统里实际可用的中文字体与等宽字体, 建立全局字体表."""
    global _F
    if _F is not None:
        return _F
    try:
        families = set(tkfont.families(root))
    except Exception:  # pragma: no cover - 极端环境下取不到字体表
        families = set()

    def pick(cands: tuple[str, ...], fallback: str) -> str:
        for name in cands:
            if name in families:
                return name
        return fallback

    _F = Fonts(pick(_UI_CANDIDATES, "TkDefaultFont"), pick(_MONO_CANDIDATES, "TkFixedFont"))
    return _F


def fonts() -> Fonts:
    """取全局字体表 (`init` 之前调用会报错 —— 属于编程错误, 让它响)."""
    if _F is None:
        raise RuntimeError("theme.init(root) 必须先调用")
    return _F


def screen_scale(root: tk.Misc) -> float:
    """把 `tk scaling` 按真实 DPI 调好, 并返回缩放比 (1.0 = 96dpi).

    为什么必须显式做: 进程若不声明 DPI 感知, Windows 会把整个窗口**位图拉伸**
    1.25 倍 —— 看起来字是糊的, 而且 `GetWindowRect` 拿到物理像素、tk 按逻辑像素
    绘制, 两套坐标系错位 (实测窗口底部的按钮栏会被裁掉). 入口处 `import win_dpi`
    已经声明了感知; 这里再把 pt->px 的换算基准对齐, 并让调用方按同一比例放大窗口.

    返回值只用于窗口尺寸: 字号是 pt, 会随 `tk scaling` 自动变大, 不要重复乘.
    """
    global _SCALE
    dpi = 96
    try:
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        hdc = user32.GetDC(0)
        got = int(gdi32.GetDeviceCaps(hdc, 88))  # LOGPIXELSX
        user32.ReleaseDC(0, hdc)
        if got > 0:
            dpi = got
    except Exception:  # pragma: no cover - 非 Windows / 取不到就按 96 走
        dpi = 96
    try:
        root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:  # pragma: no cover
        pass
    _SCALE = dpi / 96.0
    return _SCALE


def px(value: float) -> int:
    """把"设计值"换算成实际像素.

    只用于 `wraplength` 这类**必须按像素给**的渲染参数 —— 字号走 pt 会自动缩放,
    这些不走, 不换算的话在 125% 屏上文字会换行换得莫名其妙地早.
    """
    return max(1, int(round(value * _SCALE)))



# ---------------------------------------------------------------- 绘制辅助


def hline(parent: tk.Misc, *, bg: str | None = None) -> tk.Frame:
    """1px 水平分隔线."""
    return tk.Frame(parent, bg=bg or C["border_soft"], height=1, bd=0)


def dot(parent: tk.Misc, color: str, size: int = 8) -> tk.Canvas:
    """一个实心小圆点 (状态指示用). Canvas 比画字符的基线稳."""
    cv = tk.Canvas(
        parent, width=size, height=size, bg=parent.cget("bg"),
        highlightthickness=0, bd=0,
    )
    cv.create_oval(0, 0, size - 1, size - 1, fill=color, outline=color)
    return cv


__all__ = ["C", "Fonts", "init", "fonts", "screen_scale", "px", "hline", "dot"]

