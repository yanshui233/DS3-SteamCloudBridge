"""向导用到的控件.

全部是 tk 原生控件的组合 (不用 ttk): ttk 在 Windows 上会把背景色交给系统主题,
想做出"卡片 + 主色按钮"这类外观反而要跟主题对抗. tk 的 `bg`/`activebackground`
是直接生效的, 代价是要自己绑 hover —— 就几十行的事.

这些控件不含任何业务判断: 状态文案与颜色都由调用方 (`app.py`) 传进来.
"""

from __future__ import annotations
from .. import i18n

from typing import Callable

import tkinter as tk

from . import theme
from .theme import C
from .theme import hline  # noqa: F401  —— 分隔线是"控件级"的东西, 在界面层按 widgets.hline 取用


# ---------------------------------------------------------------- 按钮


def _bind_hover(widget: tk.Widget, normal: str, hover: str) -> None:
    def on_enter(_e: tk.Event) -> None:
        if str(widget.cget("state")) != "disabled":
            widget.configure(bg=hover)

    def on_leave(_e: tk.Event) -> None:
        widget.configure(bg=normal if str(widget.cget("state")) != "disabled" else C["border_soft"])

    widget.bind("<Enter>", on_enter, add="+")
    widget.bind("<Leave>", on_leave, add="+")


def primary_button(
    parent: tk.Misc, text: str, command: Callable[[], None] | None = None,
    *, small: bool = False, width: int | None = None,
) -> tk.Button:
    """实心主色按钮 —— 一屏里最多一个, 用来表示"该点这个"."""
    f = theme.fonts()
    b = tk.Button(
        parent, text=text, command=command,
        font=(f.btn_sm if small else f.btn),
        bg=C["accent"], fg=C["on_accent"],
        activebackground=C["accent_active"], activeforeground=C["on_accent"],
        disabledforeground="#B9C6DC",
        relief="flat", bd=0, highlightthickness=0, cursor="hand2",
        padx=14 if small else 22, pady=6 if small else 10,
    )
    if width:
        b.configure(width=width)
    _bind_hover(b, C["accent"], C["accent_hover"])
    return b


def ghost_button(
    parent: tk.Misc, text: str, command: Callable[[], None] | None = None,
    *, small: bool = False, width: int | None = None,
) -> tk.Button:
    """描边按钮 —— 次要动作 (刷新/打开目录/上一步)."""
    f = theme.fonts()
    b = tk.Button(
        parent, text=text, command=command,
        font=(f.btn_sm if small else f.btn),
        bg=C["surface"], fg=C["text"],
        activebackground=C["neutral_soft"], activeforeground=C["text"],
        disabledforeground=C["text_faint"],
        relief="flat", bd=0, highlightthickness=1,
        highlightbackground=C["border"], highlightcolor=C["border"],
        cursor="hand2",
        padx=12 if small else 18, pady=5 if small else 9,
    )
    if width:
        b.configure(width=width)
    _bind_hover(b, C["surface"], C["sidebar_hover"])
    return b


def link_button(parent: tk.Misc, text: str, command: Callable[[], None]) -> tk.Button:
    """看起来像链接的按钮 (用于"打开文件夹""看说明")."""
    f = theme.fonts()
    b = tk.Button(
        parent, text=text, command=command,
        font=f.small, bg=parent.cget("bg"), fg=C["accent"],
        activebackground=parent.cget("bg"), activeforeground=C["accent_active"],
        relief="flat", bd=0, highlightthickness=0, cursor="hand2", padx=0, pady=0,
    )
    b.bind("<Enter>", lambda _e: b.configure(fg=C["accent_hover"]), add="+")
    b.bind("<Leave>", lambda _e: b.configure(fg=C["accent"]), add="+")
    return b


# ---------------------------------------------------------------- 卡片


class Card(tk.Frame):
    """白底 1px 描边卡片, 内部用 `body` 放内容."""

    def __init__(self, parent: tk.Misc, *, padding: int = 18, bg: str | None = None) -> None:
        super().__init__(
            parent, bg=bg or C["surface"],
            highlightbackground=C["border"], highlightcolor=C["border"],
            highlightthickness=1, bd=0,
        )
        self.body = tk.Frame(self, bg=bg or C["surface"])
        self.body.pack(fill="both", expand=True, padx=padding, pady=padding)


def section_title(parent: tk.Misc, text: str, *, bg: str | None = None) -> tk.Label:
    return tk.Label(
        parent, text=text, font=theme.fonts().h2,
        bg=bg or C["surface"], fg=C["text"], anchor="w",
    )


def paragraph(parent: tk.Misc, text: str, *, bg: str | None = None, dim: bool = True) -> tk.Label:
    return tk.Label(
        parent, text=text, font=theme.fonts().body,
        bg=bg or C["surface"], fg=C["text_dim"] if dim else C["text"],
        anchor="w", justify="left", wraplength=theme.px(620),
    )


# ---------------------------------------------------------------- 语言选择


def lang_switch(
    parent: tk.Misc,
    current: str,
    on_pick: Callable[[str], None],
    *,
    bg: str | None = None,
) -> tk.Frame:
    """语言选择器: 几个并列的小标签, 当前语言高亮.

    为什么是两个并列的标签而不是下拉菜单: 只有两三种语言时, 下拉要多点一次才能看到
    选项 —— 而"这软件能不能切语言"恰恰是第一次打开就该被看见的事. 并列写法一眼就懂,
    也不用碰 ttk (ttk 会把配色交给系统主题, 见本模块开头那段).
    """
    host = tk.Frame(parent, bg=bg or C["surface"])
    base = bg or C["surface"]
    for i, code in enumerate(i18n.LANGS):
        active = code == current
        label = tk.Label(
            host,
            text=i18n.LANG_LABEL.get(code, code),
            font=theme.fonts().small_bold if active else theme.fonts().small,
            bg=C["accent_soft"] if active else base,
            fg=C["accent_ink"] if active else C["text_faint"],
            padx=10, pady=3, cursor="hand2" if not active else "arrow",
        )
        label.pack(side="left", padx=(0 if i == 0 else 2, 0))
        if not active:
            # 悬停后要回到**本来的底色**, 不是回到某个"正常色"常量 —— 这条选择器
            # 会出现在顶栏 (surface) 上, 底色是调用方给的.
            _bind_hover(label, base, C["sidebar_hover"])
            label.bind("<Button-1>", lambda _e, c=code: on_pick(c))
    return host


# ---------------------------------------------------------------- 状态色


_STATE_STYLE: dict[str, tuple[str, str]] = {
    "ok": (C["ok"], "正常"),
    "warn": (C["warn"], "注意"),
    "bad": (C["bad"], "需处理"),
    "info": (C["neutral"], "说明"),
}


class StatusRow(tk.Frame):
    """一行检查项: 色点 + 名称 + 详情 (+ 建议)."""

    def __init__(self, parent: tk.Misc, *, bg: str | None = None) -> None:
        super().__init__(parent, bg=bg or C["surface"])
        self._bg = bg or C["surface"]
        self.columnconfigure(2, weight=1)

        self._dot_host = tk.Frame(self, bg=self._bg, width=14, height=20)
        self._dot_host.grid(row=0, column=0, sticky="nw", pady=(3, 0))
        self._dot_host.grid_propagate(False)
        self._dot: tk.Canvas | None = None

        self.name = tk.Label(
            self, text="", font=theme.fonts().body_bold, bg=self._bg, fg=C["text"],
            anchor="w", width=0,
        )
        self.name.grid(row=0, column=1, sticky="w", padx=(8, 12))

        self.detail = tk.Label(
            self, text="", font=theme.fonts().body, bg=self._bg, fg=C["text_dim"],
            anchor="w", justify="left", wraplength=theme.px(520),
        )
        self.detail.grid(row=0, column=2, sticky="w")

        self.fix = tk.Label(
            self, text="", font=theme.fonts().small, bg=self._bg, fg=C["warn"],
            anchor="w", justify="left", wraplength=theme.px(620),
        )
        self.fix.grid(row=1, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=(2, 0))

    def set(self, label: str, state: str, detail: str = "", fix: str = "") -> None:
        color, _word = _STATE_STYLE.get(state, (C["neutral"], ""))
        if self._dot is not None:
            self._dot.destroy()
        self._dot = theme.dot(self._dot_host, color, 9)
        self._dot.pack(anchor="n", pady=(6, 0))
        self.name.configure(text=label)
        self.detail.configure(text=detail)
        if fix:
            self.fix.configure(text=i18n.t("建议: ") + fix)
            self.fix.grid()
        else:
            self.fix.grid_remove()


# ---------------------------------------------------------------- 命令框


class CommandBox(tk.Frame):
    """一行要用户手敲的命令: 大字等宽 + 复制按钮."""

    def __init__(self, parent: tk.Misc, *, bg: str | None = None) -> None:
        super().__init__(parent, bg=bg or C["surface"])
        self._bg = bg or C["surface"]
        self.columnconfigure(0, weight=1)

        holder = tk.Frame(
            self, bg=C["surface_alt"],
            highlightbackground=C["border"], highlightcolor=C["border"],
            highlightthickness=1, bd=0,
        )
        holder.grid(row=0, column=0, sticky="ew")
        holder.columnconfigure(0, weight=1)

        self.var = tk.StringVar(value="")
        self.entry = tk.Entry(
            holder, textvariable=self.var, font=theme.fonts().mono,
            bg=C["surface_alt"], fg=C["text"], readonlybackground=C["surface_alt"],
            relief="flat", bd=0, highlightthickness=0, justify="center",
        )
        self.entry.configure(state="readonly")
        self.entry.grid(row=0, column=0, sticky="ew", padx=14, pady=12)

        self.copy_btn = ghost_button(self, i18n.t("复制"), self.copy, small=True)
        self.copy_btn.grid(row=0, column=1, sticky="w", padx=(10, 0))

    def set_text(self, text: str) -> None:
        self.var.set(text)

    def copy(self) -> None:
        text = self.var.get()
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.copy_btn.configure(text=i18n.t("已复制"))
            self.after(1400, lambda: self.copy_btn.configure(text=i18n.t("复制")))
        except tk.TclError:
            pass


# ---------------------------------------------------------------- 步骤条


class StepItem(tk.Frame):
    """左侧步骤条的一项. 三态: 未到 / 当前 / 已完成."""

    def __init__(
        self, parent: tk.Misc, index: int, title: str,
        command: Callable[[], None] | None = None, *, bg: str | None = None,
    ) -> None:
        super().__init__(parent, bg=bg or C["sidebar"], cursor="hand2")
        self.index = index
        self._bg = bg or C["sidebar"]
        self._command = command
        self._state = "idle"

        self.marker = tk.Frame(self, bg=self._bg, width=3)
        self.marker.pack(side="left", fill="y")

        self.inner = tk.Frame(self, bg=self._bg)
        self.inner.pack(side="left", fill="both", expand=True, padx=(13, 10), pady=9)

        self.badge = tk.Canvas(
            self.inner, width=22, height=22, bg=self._bg, highlightthickness=0, bd=0,
        )
        self.badge.pack(side="left")

        self.label = tk.Label(
            self.inner, text=title, font=theme.fonts().step, bg=self._bg,
            fg=C["text_dim"], anchor="w",
        )
        self.label.pack(side="left", padx=(11, 0))

        for w in (self, self.inner, self.label, self.badge):
            w.bind("<Button-1>", self._on_click)
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)

    def _on_click(self, _e: tk.Event) -> None:
        if self._command is not None:
            self._command()

    def _on_enter(self, _e: tk.Event) -> None:
        if self._state != "current":
            self._paint_bg(C["sidebar_hover"])

    def _on_leave(self, _e: tk.Event) -> None:
        if self._state != "current":
            self._paint_bg(C["sidebar"])

    def _paint_bg(self, color: str) -> None:
        self._bg = color
        for w in (self, self.inner, self.badge, self.label):
            try:
                w.configure(bg=color)
            except tk.TclError:
                pass
        self._draw()

    def set_state(self, state: str) -> None:
        self._state = state
        if state == "current":
            self.marker.configure(bg=C["accent"])
            self._paint_bg(C["sidebar_sel"])
            self.label.configure(fg=C["accent_ink"], font=theme.fonts().step_bold)
        else:
            self.marker.configure(bg=self._bg)
            self._paint_bg(C["sidebar"])
            self.label.configure(fg=C["text_dim"], font=theme.fonts().step)
        self._draw()

    def _draw(self) -> None:
        self.badge.delete("all")
        cnt = self.badge
        if self._state == "done":
            cnt.create_oval(1, 1, 21, 21, fill=C["ok"], outline=C["ok"])
            cnt.create_text(11, 11, text="✓", fill="#FFFFFF", font=theme.fonts().badge)
        elif self._state == "current":
            cnt.create_oval(1, 1, 21, 21, fill=C["accent"], outline=C["accent"])
            cnt.create_text(11, 11, text=str(self.index), fill="#FFFFFF", font=theme.fonts().badge)
        else:
            outline = C["text_faint"]
            cnt.create_oval(1, 1, 21, 21, outline=outline, width=1)
            cnt.create_text(11, 11, text=str(self.index), fill=outline, font=theme.fonts().badge)


# ---------------------------------------------------------------- 提示条


class ScrollArea(tk.Frame):
    """可纵向滚动的容器 —— 内容放进 `.inner`.

    为什么值得单独做一个: 状态卡片的行数是**跟着机器变的** (8 条检查在 125% 缩放下
    就会顶出窗口), 不能靠"把窗口开大点"兜住. 滚动条只在内容真的超出时才出现,
    免得平时白占一条灰边.

    鼠标滚轮: 用 `winfo_containing` 判断指针是否落在本区域内再滚 —— 直接
    `bind_all` 会让编辑框里的滚动也带着整页跑.
    """

    def __init__(self, parent: tk.Misc, *, bg: str | None = None) -> None:
        super().__init__(parent, bg=bg or C["bg"])
        self._bg = bg or C["bg"]

        self.vbar = tk.Scrollbar(
            self, orient="vertical", width=10, bd=0,
            troughcolor=self._bg, bg=C["border"], activebackground=C["text_faint"],
            highlightthickness=0, relief="flat",
        )
        self.canvas = tk.Canvas(self, bg=self._bg, highlightthickness=0, bd=0)
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.vbar.configure(command=self.canvas.yview)
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner = tk.Frame(self.canvas, bg=self._bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self.inner.bind("<Configure>", lambda _e: self._sync())
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind_all("<MouseWheel>", self._wheel, add="+")

    def _on_canvas(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self._win, width=event.width)
        self._sync()

    def _on_scroll(self, first: str, last: str) -> None:
        need = not (float(first) <= 0.0 and float(last) >= 1.0)
        if need and not self.vbar.winfo_ismapped():
            self.vbar.pack(side="right", fill="y")
        elif not need and self.vbar.winfo_ismapped():
            self.vbar.pack_forget()
        self.vbar.set(first, last)

    def _sync(self) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _wheel(self, event: tk.Event) -> None:
        try:
            widget = self.winfo_containing(event.x_root, event.y_root)
        except tk.TclError:
            return
        node = widget
        while node is not None:
            if node is self:
                break
            node = getattr(node, "master", None)
        else:
            return  # 指针不在本区域内
        delta = getattr(event, "delta", 0)
        if delta:
            self.canvas.yview_scroll(-1 * int(delta / 120), "units")

    def scroll_to_top(self) -> None:
        self.canvas.yview_moveto(0.0)


class Banner(tk.Frame):
    """页面顶部的整条提示 (成功/注意/错误). 用 `show` 切换, `hide` 收起.

    `before` 指定它显示在哪个控件之前. 必须显式给: tk 的 pack 是**按调用顺序**
    排位置的, 后 pack 的会跑到最下面 —— 等到出提示时才 `pack`, 它就会出现在
    页面底部而不是标题下面.
    """

    _STYLE = {
        "ok": (C["ok_soft"], C["ok"], "✓"),
        "warn": (C["warn_soft"], C["warn"], "!"),
        "bad": (C["bad_soft"], C["bad"], "×"),
        "info": (C["accent_soft"], C["accent_ink"], "i"),
    }

    def __init__(self, parent: tk.Misc, *, before: tk.Misc | None = None) -> None:
        super().__init__(parent, bg=C["accent_soft"])
        self._before = before
        self._icon = tk.Label(self, text="i", font=theme.fonts().body_bold, bg=C["accent_soft"])
        self._icon.pack(side="left", padx=(14, 8), pady=10)
        self._text = tk.Label(
            self, text="", font=theme.fonts().body, bg=C["accent_soft"],
            fg=C["text"], anchor="w", justify="left", wraplength=theme.px(700),
        )
        self._text.pack(side="left", fill="x", expand=True, padx=(0, 14), pady=10)

    def show(self, kind: str, text: str) -> None:
        bg, fg, icon = self._STYLE.get(kind, self._STYLE["info"])
        for w in (self, self._icon, self._text):
            w.configure(bg=bg)
        self._icon.configure(text=icon, fg=fg)
        self._text.configure(text=text)
        try:
            if self._before is not None:
                self.pack(fill="x", padx=30, pady=(14, 0), before=self._before)
            else:
                self.pack(fill="x")
        except tk.TclError:
            self.pack(fill="x")

    def hide(self) -> None:
        self.pack_forget()

