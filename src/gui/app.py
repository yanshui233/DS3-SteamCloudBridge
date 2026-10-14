"""图形向导主窗口.

五步: 检查环境 -> 备份存档 -> 每次玩之前 -> 遇到问题 -> 魂2 存档.

前四步针对**黑魂3** (它支持 Steam 云, 所以能"点亮云通道"); 第五步是**魂二**,
魂二**没有** Steam 云存档, 所以那一步不点亮任何东西, 改用"你指定的文件夹"
当传输层 (网盘同步目录 / U 盘). 两者不是同一套机制, 因此**并列而不是并入**.

设计上的两条硬规矩:
1. **界面线程只做渲染**. 任何可能超过几十毫秒的动作 (扫 Steam、复制 27 MB、
   等控制台回执) 都丢到后台线程, 结果用队列回主线程 —— 否则窗口会假死,
   用户以为"点了没反应"就又点一次.
2. **有副作用的动作全部委托给 `service`**. 本文件不自己判断"算不算点亮了"
   这类业务结论, 只说"往界面上写什么字". 判据一旦分叉, GUI 与 CLI 就会给出
   不同的答案, 那是这个项目最不能接受的事.
"""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, Callable

from ..version import VERSION
from . import service, theme, widgets
from .theme import C

WINDOW_W, WINDOW_H = 1060, 740  # 96dpi 下的基准尺寸, 实际会按屏幕 DPI 放大
MIN_W, MIN_H = 940, 600

STEPS = (
    ("检查环境", "看看这台机器现在的状态"),
    ("备份存档", "先把存档完整复制一份"),
    ("每次玩之前", "点亮云通道, 然后开游戏"),
    ("遇到问题", "常见故障与出口"),
    ("魂2 存档", "没有云，用文件夹搬"),
)


# ---------------------------------------------------------------- 主窗口


class App(tk.Tk):
    def __init__(self, *, appid: str | None = None, smoke: bool = False) -> None:
        super().__init__()
        self.withdraw()  # 先把窗口藏起来, 等布局算完再显示 —— 避免开局闪一下白框
        self.title("SteamCloudBridge · 黑魂存档同步")
        self.configure(bg=C["bg"])
        theme.init(self)

        # 先把 pt->px 的换算基准对齐真实 DPI, 再按同一比例定窗口尺寸 ——
        # 顺序不能反: 尺寸是按物理像素给的, 字体是按 pt 自动换算的.
        self.scale = theme.screen_scale(self)
        self.geometry(f"{int(WINDOW_W * self.scale)}x{int(WINDOW_H * self.scale)}")
        self.minsize(int(MIN_W * self.scale), int(MIN_H * self.scale))

        self.appid = str(appid or service.DEFAULT_APPID)
        self.env: service.Environment | None = None
        self._busy = False
        self._cancel = threading.Event()
        self._watch_started = 0.0
        self._watch_timer: str | None = None

        self._build_chrome()
        # 顺序有讲究: pack 是**按调用顺序**分空间的, `body` 带 expand 会吃掉
        # 所有剩余高度. 所以先摆底栏, 再让 body 去填中间 —— 反过来的话底栏
        # 会被挤到窗口外面 (实测: 按钮栏整个不见了).
        self._build_footer()
        self._build_body()
        self._install_steps()
        self.go(0)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.deiconify()
        self.after(80, self.refresh_env)

        if smoke:
            # 自检模式: 建完界面就退, 用来验证"打包后 tkinter 还能用".
            self.after(400, self.destroy)

    # ------------------------------------------------------------ 布局

    def _build_chrome(self) -> None:
        bar = tk.Frame(self, bg=C["surface"], height=58)
        bar.pack(side="top", fill="x")
        bar.pack_propagate(False)

        inner = tk.Frame(bar, bg=C["surface"])
        inner.pack(fill="both", expand=True, padx=22)
        tk.Label(
            inner, text="SteamCloudBridge", font=theme.fonts().brand,
            bg=C["surface"], fg=C["text"],
        ).pack(side="left", pady=16)
        tk.Label(
            inner, text=f"黑魂存档同步助手   v{VERSION}",
            font=theme.fonts().small, bg=C["surface"], fg=C["text_faint"],
        ).pack(side="left", padx=(12, 0), pady=16)

        self.badge = tk.Label(
            inner, text="", font=theme.fonts().small_bold,
            bg=C["neutral_soft"], fg=C["neutral"], padx=10, pady=4,
        )
        self.badge.pack(side="right", pady=16)
        self._set_badge("未检查", "neutral")

        widgets.hline(self).pack(side="top", fill="x")

    def _set_badge(self, text: str, kind: str) -> None:
        bg, fg = {
            "ok": (C["ok_soft"], C["ok"]),
            "warn": (C["warn_soft"], C["warn"]),
            "bad": (C["bad_soft"], C["bad"]),
            "neutral": (C["neutral_soft"], C["neutral"]),
        }[kind]
        self.badge.configure(text=text, bg=bg, fg=fg)

    def _build_body(self) -> None:
        body = tk.Frame(self, bg=C["bg"])
        body.pack(side="top", fill="both", expand=True)

        side = tk.Frame(body, bg=C["sidebar"], width=228)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        self.steps_host = tk.Frame(side, bg=C["sidebar"])
        self.steps_host.pack(fill="x", pady=(16, 0))

        sep = tk.Frame(body, bg=C["border"], width=1)
        sep.pack(side="left", fill="y")

        self.content = tk.Frame(body, bg=C["bg"])
        self.content.pack(side="left", fill="both", expand=True)

    def _build_footer(self) -> None:
        # 先摆按钮条、再摆它上面那条分隔线 —— 同为 side="bottom" 时,
        # **先 pack 的在最底下**, 顺序写反会让分隔线跑到窗口最底边.
        foot = tk.Frame(self, bg=C["surface"], height=62)
        foot.pack(side="bottom", fill="x")
        foot.pack_propagate(False)
        widgets.hline(self).pack(side="bottom", fill="x")

        inner = tk.Frame(foot, bg=C["surface"])
        inner.pack(fill="both", expand=True, padx=22, pady=11)

        self.btn_prev = widgets.ghost_button(inner, "上一步", lambda: self.go(self.page - 1))
        self.btn_prev.pack(side="left")

        self.hint = tk.Label(
            inner, text="", font=theme.fonts().small, bg=C["surface"], fg=C["text_faint"],
        )
        self.hint.pack(side="left", padx=16)

        self.btn_next = widgets.primary_button(inner, "下一步", lambda: self.go(self.page + 1))
        self.btn_next.pack(side="right")

    def _install_steps(self) -> None:
        self.step_items: list[widgets.StepItem] = []
        for i, (title, _sub) in enumerate(STEPS):
            item = widgets.StepItem(
                self.steps_host, i + 1, title, (lambda k=i: self.go(k)),
                bg=C["sidebar"],
            )
            item.pack(fill="x")
            self.step_items.append(item)

    # ------------------------------------------------------------ 页面切换

    @property
    def page(self) -> int:
        return getattr(self, "_page", 0)

    def go(self, index: int) -> None:
        pages = getattr(self, "_pages", None)
        if pages is None:
            return
        index = max(0, min(index, len(pages) - 1))
        self._page = index
        for i, p in enumerate(pages):
            if i == index:
                p.pack(fill="both", expand=True)
            else:
                p.pack_forget()
        for i, item in enumerate(self.step_items):
            item.set_state("current" if i == index else ("done" if i < index else "idle"))
        self.btn_prev.configure(state="normal" if index > 0 else "disabled")
        last = index == len(pages) - 1
        self.btn_next.configure(
            state="disabled" if last else "normal",
            text="完成" if last else "下一步",
        )
        pages[index].on_show()

    def set_hint(self, text: str) -> None:
        self.hint.configure(text=text)

    # ------------------------------------------------------------ 异步

    def run_async(self, work: Callable[[], Any], done: Callable[[Any, Exception | None], None]) -> None:
        """后台跑 `work`, 回主线程调 `done(结果, 异常)`."""
        if self._busy:
            return
        self._busy = True
        q: queue.Queue[tuple[str, Any]] = queue.Queue()

        def worker() -> None:
            try:
                q.put(("ok", work()))
            except Exception as exc:  # noqa: BLE001 - 后台线程里的异常必须带回界面
                q.put(("err", exc))

        threading.Thread(target=worker, daemon=True).start()

        def poll() -> None:
            try:
                kind, payload = q.get_nowait()
            except queue.Empty:
                self.after(60, poll)
                return
            self._busy = False
            done(payload, None if kind == "ok" else payload)

        self.after(60, poll)

    def _on_close(self) -> None:
        self._cancel.set()
        self.destroy()

    # ------------------------------------------------------------ 体检

    def refresh_env(self, on_done: Callable[[], None] | None = None) -> None:
        self._set_badge("检查中…", "neutral")
        home = self._pages[0]
        home.set_loading(True)

        def work() -> service.Environment:
            return service.inspect(self.appid)

        def done(result: Any, exc: Exception | None) -> None:
            home.set_loading(False)
            if exc is not None:
                self._set_badge("检查失败", "bad")
                home.banner.show("bad", f"检查失败: {exc}")
                return
            env: service.Environment = result
            self.env = env
            for page in self._pages:
                page.env = env
            self._apply_env(env)
            home.render(env)
            # 体检结果会改变别的页面的文案 (第 3 页的"Steam 没开/通道未亮"结论就靠它).
            # 而各页的 on_show 在切过去时才会跑 —— 启动时 env 还是空的, 那次调用会
            # 直接早退. 所以拿到结果后必须把当前页重画一次, 否则它会停在初始文案上.
            self._pages[self.page].on_show()
            if on_done is not None:
                on_done()

        self.run_async(work, done)

    def _apply_env(self, env: service.Environment) -> None:
        if env.error:
            self._set_badge("无法检查", "bad")
            return
        if env.channel_lit is True:
            self._set_badge("云通道已点亮", "ok")
        elif env.game_running:
            self._set_badge("游戏正在运行", "warn")
        elif env.channel_lit is False:
            self._set_badge("云通道未点亮", "warn")
        else:
            self._set_badge("已检查", "neutral")


# ---------------------------------------------------------------- 页面基类


class Page(tk.Frame):
    def __init__(self, parent: tk.Misc, app: App, index: int) -> None:
        super().__init__(parent, bg=C["bg"])
        self.app = app
        self.index = index
        self.env: service.Environment | None = None

        head = tk.Frame(self, bg=C["bg"])
        head.pack(fill="x", padx=30, pady=(26, 0))
        tk.Label(
            head, text=STEPS[index][0], font=theme.fonts().title,
            bg=C["bg"], fg=C["text"], anchor="w",
        ).pack(anchor="w")
        tk.Label(
            head, text=STEPS[index][1], font=theme.fonts().subtitle,
            bg=C["bg"], fg=C["text_dim"], anchor="w",
        ).pack(anchor="w", pady=(4, 0))

        # 正文放进滚动区: 状态卡片的行数跟着机器走, 靠"把窗口开大"兜不住
        self.scroll = widgets.ScrollArea(self, bg=C["bg"])
        self.scroll.pack(fill="both", expand=True, padx=30, pady=(18, 22))
        self.body = self.scroll.inner

        # 提示条按需出现, 位置固定在正文上方 (必须给 `before`, 否则会掉到页面底部)
        self.banner = widgets.Banner(self, before=self.scroll)
        self.banner.hide()

    def on_show(self) -> None:  # pragma: no cover - 子类覆盖
        pass


# ---------------------------------------------------------------- 第 1 步


class HomePage(Page):
    def __init__(self, parent: tk.Misc, app: App) -> None:
        super().__init__(parent, app, 0)

        top = tk.Frame(self.body, bg=C["bg"])
        top.pack(fill="x")
        self.status_label = tk.Label(
            top, text="正在检查…", font=theme.fonts().small,
            bg=C["bg"], fg=C["text_dim"],
        )
        self.status_label.pack(side="left")
        widgets.ghost_button(top, "重新检查", self.app.refresh_env, small=True).pack(side="right")

        card = widgets.Card(self.body, padding=20)
        card.pack(fill="both", expand=True, pady=(12, 0))

        widgets.section_title(card.body, "本机状况").pack(anchor="w")
        widgets.paragraph(
            card.body, "这一步只看不改，不会动任何文件。", bg=C["surface"],
        ).pack(anchor="w", pady=(3, 14))
        widgets.hline(card.body).pack(fill="x", pady=(0, 12))

        self.rows: list[widgets.StatusRow] = []
        for _ in range(8):
            row = widgets.StatusRow(card.body, bg=C["surface"])
            self.rows.append(row)  # 先不 pack —— render() 里按实际条数摆

        self.more = tk.Frame(card.body, bg=C["surface"])
        self.more.pack(fill="x", pady=(14, 0))
        self.notes_label = tk.Label(
            self.more, text="", font=theme.fonts().small, bg=C["surface"],
            fg=C["text_faint"], anchor="w", justify="left", wraplength=theme.px(700),
        )
        self.notes_label.pack(anchor="w")

    def set_loading(self, busy: bool) -> None:
        self.status_label.configure(text="正在检查…" if busy else "检查完成")

    def render(self, env: service.Environment) -> None:
        self.banner.hide()
        for i, row in enumerate(self.rows):
            if i < len(env.checks):
                c = env.checks[i]
                row.set(c.label, c.state, c.detail, c.fix)
                row.pack(fill="x", pady=5)
            else:
                row.pack_forget()

        summary = []
        if env.save_path:
            summary.append(f"存档: {service.fmt_size(env.save_size)}")
        if env.cloud_change_number:
            summary.append(f"云端版本号: {env.cloud_change_number}")
        if env.remote_remotetime:
            summary.append("云端已有一份副本")
        self.notes_label.configure(text="  ·  ".join(summary) if summary else "")

        if env.error:
            self.banner.show("bad", env.error)
            return
        if env.save_exists and env.channel_lit is False:
            self.banner.show(
                "info",
                "状态正常。云通道每次启动 Steam 都会关掉，所以下一步先备份，"
                "再进入第三步点亮它。",
            )

    def on_show(self) -> None:
        if self.env is None and not self.app._busy:
            self.app.refresh_env()


# ---------------------------------------------------------------- 第 2 步


class BackupPage(Page):
    def __init__(self, parent: tk.Misc, app: App) -> None:
        super().__init__(parent, app, 1)

        card = widgets.Card(self.body, padding=20)
        card.pack(fill="x")

        widgets.section_title(card.body, "备份存档").pack(anchor="w")
        widgets.paragraph(
            card.body,
            "把整个存档目录（含所有角色进度与设置）完整复制一份到程序旁边。\n"
            "只会复制，不会移动或删除任何东西。复制完会逐个文件核对校验值。",
            bg=C["surface"],
        ).pack(anchor="w", pady=(4, 6))

        self.where = tk.Label(
            card.body, text="", font=theme.fonts().small, bg=C["surface"],
            fg=C["text_faint"], anchor="w", justify="left", wraplength=theme.px(700),
        )
        self.where.pack(anchor="w", pady=(0, 14))

        row = tk.Frame(card.body, bg=C["surface"])
        row.pack(fill="x")
        self.btn_backup = widgets.primary_button(row, "立即备份", self.start_backup)
        self.btn_backup.pack(side="left")
        self.btn_open = widgets.ghost_button(row, "打开备份文件夹", self.open_folder)
        self.btn_open.pack(side="left", padx=(10, 0))
        self.btn_open.configure(state="disabled")

        self.progress = tk.Label(
            card.body, text="", font=theme.fonts().small, bg=C["surface"], fg=C["text_dim"],
        )
        self.progress.pack(anchor="w", pady=(12, 0))

        hist = widgets.Card(self.body, padding=20)
        hist.pack(fill="both", expand=True, pady=(16, 0))
        widgets.section_title(hist.body, "已有备份").pack(anchor="w")
        self.hist_box = tk.Frame(hist.body, bg=C["surface"])
        self.hist_box.pack(fill="both", expand=True, pady=(12, 0))

    def on_show(self) -> None:
        env = self.env
        if env is not None and env.cloud_root:
            self.where.configure(text=f"来源: {env.cloud_root}     ({env.cloud_root_source})")
            self.btn_backup.configure(state="normal")
        elif env is not None:
            self.where.configure(text="还没定位到存档目录 —— 回到第一步点一次“重新检查”。")
            self.btn_backup.configure(state="disabled")
        self.render_history()

    def render_history(self) -> None:
        for w in self.hist_box.winfo_children():
            w.destroy()
        items = service.list_backups()
        if not items:
            tk.Label(
                self.hist_box, text="还没有备份。", font=theme.fonts().small,
                bg=C["surface"], fg=C["text_faint"],
            ).pack(anchor="w")
            self.btn_open.configure(state="disabled")
            return
        self.btn_open.configure(state="normal")
        for b in items[:6]:
            line = tk.Frame(self.hist_box, bg=C["surface"])
            line.pack(fill="x", pady=3)
            tk.Label(
                line, text="●", font=theme.fonts().small, bg=C["surface"], fg=C["ok"],
            ).pack(side="left", padx=(0, 8))
            tk.Label(
                line, text=b["when"], font=theme.fonts().small_bold,
                bg=C["surface"], fg=C["text"],
            ).pack(side="left")
            tk.Label(
                line,
                text=f"{b['count']} 个文件 · {service.fmt_size(b['bytes'])}",
                font=theme.fonts().small, bg=C["surface"], fg=C["text_dim"],
            ).pack(side="left", padx=(10, 0))

    def open_folder(self) -> None:
        items = service.list_backups()
        target = items[0]["dir"].parent if items else service.default_backup_root()
        if not service.open_path(target):
            self.banner.show("warn", f"打不开: {target}")

    def start_backup(self) -> None:
        env = self.env
        if env is None or not env.can_backup:
            self.banner.show("bad", "还没定位到存档目录，先回到第一步重新检查。")
            return
        self.banner.hide()
        self.btn_backup.configure(state="disabled")
        self.progress.configure(text="正在复制并校验（约 27 MB，几秒钟）…")

        def work() -> service.BackupResult:
            return service.backup_saves(env)

        def done(result: Any, exc: Exception | None) -> None:
            self.btn_backup.configure(state="normal")
            if exc is not None:
                self.progress.configure(text="")
                self.banner.show("bad", f"备份失败: {exc}")
                return
            res: service.BackupResult = result
            if not res.ok:
                self.progress.configure(text="")
                self.banner.show("bad", f"备份失败: {res.error}")
                return
            self.progress.configure(
                text=f"完成: {len(res.files)} 个文件，{service.fmt_size(res.total_bytes)}，"
                f"已逐个核对校验值"
            )
            self.banner.show(
                "ok",
                f"备份成功，已核对无误。位置: {res.dest}",
            )
            self.btn_open.configure(state="normal")
            self.render_history()

        self.app.run_async(work, done)


# ---------------------------------------------------------------- 第 3 步


class PlayPage(Page):
    def __init__(self, parent: tk.Misc, app: App) -> None:
        super().__init__(parent, app, 2)
        self._watching = False

        card = widgets.Card(self.body, padding=20)
        card.pack(fill="both", expand=True)

        widgets.section_title(card.body, "云通道每次都要重新点亮").pack(anchor="w")
        widgets.paragraph(
            card.body,
            "Steam 的云开关分两层：账号那一层是持久的，游戏这一层只在本次开启 Steam 期间有效。\n"
            "所以每次开始玩之前，按下面三步走一遍就行（半分钟）。",
            bg=C["surface"],
        ).pack(anchor="w", pady=(4, 14))

        # --- 1 打开控制台
        step1 = tk.Frame(card.body, bg=C["surface"])
        step1.pack(fill="x", pady=(0, 12))
        self._step_label(step1, "1", "打开 Steam 控制台").pack(side="left")
        self.btn_console = widgets.ghost_button(
            step1, "打开 Steam 控制台", self.do_open_console, small=True,
        )
        self.btn_console.pack(side="right")
        self.btn_steam = widgets.ghost_button(
            step1, "Steam 没开？启动它", self.do_start_steam, small=True,
        )
        self.btn_steam.pack(side="right", padx=(0, 8))
        self.btn_steam.configure(state="disabled")  # 体检结论出来前不知道自己该不该亮

        # --- 2 敲一行
        step2 = tk.Frame(card.body, bg=C["surface"])
        step2.pack(fill="x", pady=(0, 6))
        self._step_label(step2, "2", "把这一行敲进控制台（或复制粘贴）").pack(anchor="w")
        self.cmd = widgets.CommandBox(card.body, bg=C["surface"])
        self.cmd.pack(fill="x", pady=(8, 4))
        tk.Label(
            card.body,
            text="敲对了不会有任何成功提示，这是正常的 —— 下面的状态灯会自己变绿。",
            font=theme.fonts().small, bg=C["surface"], fg=C["text_faint"],
            anchor="w", justify="left", wraplength=theme.px(700),
        ).pack(anchor="w", pady=(4, 14))

        # --- 3 等确认
        step3 = tk.Frame(card.body, bg=C["surface"])
        step3.pack(fill="x")
        self._step_label(step3, "3", "等确认").pack(side="left")
        self.watch_state = tk.Label(
            step3, text="● 未开始", font=theme.fonts().small_bold,
            bg=C["surface"], fg=C["text_faint"],
        )
        self.watch_state.pack(side="right")

        widgets.hline(card.body).pack(fill="x", pady=(16, 14))

        launch_row = tk.Frame(card.body, bg=C["surface"])
        launch_row.pack(fill="x")
        self.btn_play = widgets.primary_button(launch_row, "启动游戏", self.do_launch)
        self.btn_play.pack(side="left")
        self.play_hint = tk.Label(
            launch_row,
            text="玩完正常退出游戏就行，Steam 会把存档同步到云端。",
            font=theme.fonts().small, bg=C["surface"], fg=C["text_dim"],
        )
        self.play_hint.pack(side="left", padx=(14, 0))

    def _step_label(self, parent: tk.Misc, num: str, text: str) -> tk.Frame:
        box = tk.Frame(parent, bg=C["surface"])
        cv = tk.Canvas(box, width=20, height=20, bg=C["surface"], highlightthickness=0, bd=0)
        cv.create_oval(0, 0, 19, 19, fill=C["accent_soft"], outline=C["accent_soft"])
        cv.create_text(10, 10, text=num, fill=C["accent_ink"], font=theme.fonts().badge)
        cv.pack(side="left")
        tk.Label(
            box, text=text, font=theme.fonts().body_bold, bg=C["surface"], fg=C["text"],
        ).pack(side="left", padx=(9, 0))
        return box

    # -------------------------------------------------- 展示

    def on_show(self) -> None:
        self.cmd.set_text(service.enable_command(self.app.appid))
        env = self.env
        if env is None:
            return
        self.btn_steam.configure(state="disabled" if env.steam_running else "normal")

        if env.channel_lit is True:
            self._set_watch("ok", "✓ 已点亮")
            self.banner.show("ok", "本次云通道已经是亮着的，可以直接启动游戏。")
            return
        if env.game_running:
            self.banner.show("warn", "游戏正在运行 —— 请先完全退出游戏，再点亮通道。")
            return
        if env.steam_running is False:
            self.banner.show("warn", "Steam 没在运行，先点上面的“启动它”，再回来敲这一行。")
            return
        # 监听**不在这里**自动挂上: 用户可能只是先看一眼这页, 挂上去就白等三分钟.
        # 等他点了"打开 Steam 控制台"再开始盯日志.
        self._set_watch("idle", "● 未开始")
        self.banner.show(
            "info", "点右上角的“打开 Steam 控制台”，然后把下面这一行敲进控制台。"
        )

    def _set_watch(self, kind: str, text: str) -> None:
        color = {"ok": C["ok"], "warn": C["warn"], "idle": C["text_faint"]}[kind]
        self.watch_state.configure(text=text, fg=color)

    # -------------------------------------------------- 动作

    def do_open_console(self) -> None:
        if service.open_console():
            self.banner.show(
                "info",
                "控制台已打开。⚠ 光打开还不算 —— 先用鼠标点一下最底下那行输入框，"
                "再把上面那一行敲进去（或按 Ctrl+V 粘贴）。",
            )
            self.start_watch()
        else:
            self.banner.show(
                "warn",
                "没能自动打开控制台。可以手动：按 Win+R，输入 steam://open/console 回车。",
            )

    def do_start_steam(self) -> None:
        env = self.env
        if env is None or env.install is None:
            self.banner.show("bad", "还没检查过环境，先回到第一步。")
            return
        self.banner.show("info", "正在启动 Steam，请等它登录完成…")

        def work() -> bool:
            return service.start_steam(env.install)

        def done(result: Any, exc: Exception | None) -> None:
            if exc is not None or not result:
                self.banner.show("warn", "Steam 好像没起来，手动开一下再点“重新检查”。")
                return
            self.banner.show("ok", "Steam 已启动。")
            self.app.refresh_env()

        self.app.run_async(work, done)

    def do_launch(self) -> None:
        env = self.env
        if env is not None and env.game_running:
            self.banner.show("warn", "游戏已经在运行了。")
            return
        ok, detail = service.launch_game(self.app.appid)
        if ok:
            self.banner.show(
                "ok",
                "已交给 Steam 启动游戏。玩完正常退出，存档会自动上传到云端。",
            )
            self.play_hint.configure(text="本会话通道已点亮，中途别重启 Steam。")
        else:
            self.banner.show("bad", f"启动失败: {detail}")

    def start_watch(self) -> None:
        """开始盯控制台日志, 等用户敲进去的回执. 幂等 —— 重复调用不会叠加线程."""
        env = self.env
        if env is None or env.install is None or self._watching:
            return
        self._watching = True
        self._set_watch("warn", "● 正在等待你敲入…")
        self.app.set_hint("等待控制台回执中（最多 3 分钟）")

        def work() -> service.EnableOutcome:
            return service.watch_enable(
                env.install,
                self.app.appid,
                wait_s=180.0,
                poll=1.5,
                should_stop=self.app._cancel.is_set,
            )

        def done(result: Any, exc: Exception | None) -> None:
            self._watching = False
            self.app.set_hint("")
            if exc is not None:
                self._set_watch("idle", "● 监听中断")
                return
            out: service.EnableOutcome = result
            if out.ok:
                self._set_watch("ok", "✓ 已点亮")
                self.banner.show("ok", "云通道已点亮。现在可以启动游戏了。")
                self.app._set_badge("云通道已点亮", "ok")
            else:
                self._set_watch("idle", "● 没等到回执")
                self.banner.show(
                    "warn",
                    "没等到控制台回执。可能是没敲进去，或敲到了别的地方。"
                    "点一次“打开 Steam 控制台”，用鼠标点一下输入行后重新敲；"
                    "也可以直接启动游戏，通道通常已经生效。",
                )

        self.app.run_async(work, done)


# ---------------------------------------------------------------- 第 4 步


FAQ: tuple[tuple[str, str], ...] = (
    (
        "云通道状态一直显示未点亮",
        "点亮命令只在本次开启 Steam 期间有效。重启过 Steam，就要重新敲一次。"
        "如果敲了仍然不变，先确认控制台里输入行有焦点（用鼠标点一下那行）。",
    ),
    (
        "游戏退出后云端没更新",
        "存档是在退出游戏时上传的。如果上次是非正常退出（崩溃、强杀进程），"
        "Steam 可能没来得及同步 —— 下次正常退出一次即可。",
    ),
    (
        "存档丢了 / 想回到之前的进度",
        "在“备份存档”一页里点“打开备份文件夹”，里面有带时间戳的完整副本，"
        "以及一份写着恢复步骤的说明文件。",
    ),
    (
        "会被封号吗",
        "这个工具只在你自己账号的存档目录里搬字节，不修改游戏、不注入、不联机作弊。"
        "黑魂3 没有 VAC；封号针对的是上传非法内容存档，与本工具无关。",
    ),
    (
        "魂二为什么没有“点亮云通道”这一步",
        "魂二本身不支持 Steam 云存档 —— 不是设置没开，是游戏没有这个功能，"
        "所以没有开关可点。它改用“魂2 存档”那一页：导出成一个文件夹，"
        "放进你的网盘同步目录或 U 盘，换台机器再导入回来。",
    ),
    (
        "魂二能不能也搞成云存档",
        "不能。云存档是游戏方接进 Steam 的功能，第三方替它加不上，我们也不去改游戏。"
        "本工具能做的是“文件夹搬”：导出 → 放进网盘或 U 盘 → 另一台机器导入，"
        "效果上一样是跨机接着玩，只是要你自己点两下。",
    ),
)


class HelpPage(Page):
    def __init__(self, parent: tk.Misc, app: App) -> None:
        super().__init__(parent, app, 3)

        # 「先说清楚」告示 —— 放最上面, 因为这是最容易让人白折腾的一件事:
        # 魂二没有云时, 用户会默认是自己的设置问题, 于是在"点亮云通道"那一页反复试.
        # 所以先把原因和"改不了"讲透, 再谈我们怎么办.
        notice = tk.Frame(
            self.body, bg=C["warn_soft"],
            highlightthickness=1, highlightbackground=C["warn"], highlightcolor=C["warn"],
        )
        notice.pack(fill="x", pady=(0, 16))
        inner = tk.Frame(notice, bg=C["warn_soft"])
        inner.pack(fill="x", padx=18, pady=14)
        tk.Label(
            inner, text="!  关于魂二：它没有 Steam 云存档，这一点改不了",
            font=theme.fonts().body_bold, bg=C["warn_soft"], fg=C["warn"], anchor="w",
        ).pack(anchor="w")
        tk.Label(
            inner,
            text=(
                "不是哪一步没做对，也不是设置没开 —— 是这个游戏本身就不提供云存档。\n"
                "本机四条证据都对得上：账号配置里没有它的云记录；云日志只有一句\n"
                "「Not running AutoCloud. No config data found」；存档目录里没有云配置文件；\n"
                "云端用户目录里根本没有它。Steam 商店页也没标它支持云存档。\n"
                "所以没有开关可以点亮，工具也变不出一个不存在的功能 —— 这属于游戏方的事，\n"
                "第三方替它加不上。\n"
                "本工具对魂二的方案是「用文件夹搬」：导出成一个文件夹 → 放进你自己的网盘\n"
                "同步目录或 U 盘 → 换台机器再导入回来（左侧第 5 步「魂2 存档」）。\n"
                "全程只搬字节，不读、不改存档内容。"
            ),
            font=theme.fonts().small, bg=C["warn_soft"], fg=C["text"],
            anchor="w", justify="left", wraplength=theme.px(680),
        ).pack(anchor="w", pady=(6, 0))

        card = widgets.Card(self.body, padding=20)
        card.pack(fill="both", expand=True)

        widgets.section_title(card.body, "常见问题").pack(anchor="w")
        box = tk.Frame(card.body, bg=C["surface"])
        box.pack(fill="both", expand=True, pady=(12, 0))
        for i, (q, a) in enumerate(FAQ):
            if i:
                widgets.hline(box).pack(fill="x", pady=10)
            tk.Label(
                box, text=q, font=theme.fonts().body_bold, bg=C["surface"],
                fg=C["text"], anchor="w", justify="left",
            ).pack(anchor="w")
            tk.Label(
                box, text=a, font=theme.fonts().small, bg=C["surface"],
                fg=C["text_dim"], anchor="w", justify="left", wraplength=theme.px(700),
            ).pack(anchor="w", pady=(3, 0))

        widgets.hline(card.body).pack(fill="x", pady=(16, 14))
        row = tk.Frame(card.body, bg=C["surface"])
        row.pack(fill="x")
        widgets.ghost_button(row, "打开存档文件夹", self.open_saves, small=True).pack(side="left")
        widgets.ghost_button(row, "打开备份文件夹", self.open_backups, small=True).pack(
            side="left", padx=(10, 0)
        )
        widgets.ghost_button(row, "看使用说明", self.open_readme, small=True).pack(
            side="left", padx=(10, 0)
        )

    def open_saves(self) -> None:
        target = self.env.cloud_root if self.env else None
        if not service.open_path(target):
            self.banner.show("warn", "还没定位到存档目录。")

    def open_backups(self) -> None:
        if not service.open_path(service.default_backup_root()):
            self.banner.show("warn", "备份文件夹还不存在（先做一次备份）。")

    def open_readme(self) -> None:
        here = Path(__file__).resolve()
        for cand in (
            here.parents[2] / "packaging" / "README.md",
            here.parents[2] / "README.md",
        ):
            if cand.is_file():
                if service.open_path(cand):
                    return
        self.banner.show("warn", "没找到说明文件。")


# ---------------------------------------------------------------- 第 5 步: 魂二


class Ds2Page(Page):
    """魂二 —— Steam 不提供云存档, 改用"你指定的文件夹"当传输层.

    为什么单开一页, 而不是并进前三步: 魂二**没有云通道可以点亮**, "每次玩之前"
    那一套对它根本不适用. 硬塞进同一套步骤, 用户只会得出"是不是我哪一步没做对"
    的结论 —— 而他其实什么都做对了, 是这游戏就没有这个功能.
    """

    def __init__(self, parent: tk.Misc, app: App) -> None:
        super().__init__(parent, app, 4)
        self._loc: Any = None

        card = widgets.Card(self.body, padding=20)
        card.pack(fill="x")

        widgets.section_title(card.body, "魂 2 存档").pack(anchor="w")
        widgets.paragraph(
            card.body,
            "魂二这个游戏，Steam 本身不提供云存档 —— 不是设置没开，是它没有这个功能，\n"
            "所以也没有开关可以点亮。\n"
            "替代办法：把存档导出成一个文件夹，放进你自己的网盘同步目录（或 U 盘）；\n"
            "换台机器再导入回来。工具只搬字节，不读取也不修改存档内容。",
            bg=C["surface"],
        ).pack(anchor="w", pady=(4, 10))

        self.where = tk.Label(
            card.body, text="正在读取…", font=theme.fonts().small, bg=C["surface"],
            fg=C["text_dim"], anchor="w", justify="left", wraplength=theme.px(700),
        )
        self.where.pack(anchor="w", pady=(0, 14))

        row = tk.Frame(card.body, bg=C["surface"])
        row.pack(fill="x")
        self.btn_out = widgets.primary_button(row, "导出到文件夹…", self.do_export)
        self.btn_out.pack(side="left")
        self.btn_in = widgets.ghost_button(row, "从文件夹导入…", self.do_import)
        self.btn_in.pack(side="left", padx=(10, 0))
        self.btn_open = widgets.ghost_button(row, "打开存档文件夹", self.open_saves)
        self.btn_open.pack(side="left", padx=(10, 0))

        self.progress = tk.Label(
            card.body, text="", font=theme.fonts().small, bg=C["surface"],
            fg=C["text_faint"], anchor="w", justify="left", wraplength=theme.px(700),
        )
        self.progress.pack(anchor="w", pady=(12, 0))

        tip = widgets.Card(self.body, padding=20)
        tip.pack(fill="both", expand=True, pady=(16, 0))
        widgets.section_title(tip.body, "导出到哪儿？两种都行").pack(anchor="w")
        for name, desc in (
            (
                "网盘同步目录",
                "选成 OneDrive／坚果云／Syncthing 这类会自动同步的文件夹。"
                "之后在另一台机器上打开同一个文件夹，直接导入即可。",
            ),
            (
                "U 盘 / 移动硬盘",
                "导出目录直接选 U 盘。插到另一台机器上，用下面的“从文件夹导入”。",
            ),
        ):
            line = tk.Frame(tip.body, bg=C["surface"])
            line.pack(fill="x", pady=(10, 0))
            tk.Label(
                line, text=name, font=theme.fonts().small_bold, bg=C["surface"], fg=C["text"],
            ).pack(anchor="w")
            tk.Label(
                line, text=desc, font=theme.fonts().small, bg=C["surface"], fg=C["text_dim"],
                anchor="w", justify="left", wraplength=theme.px(660),
            ).pack(anchor="w", pady=(3, 0))

    # ------------------------------------------------------------ 渲染

    def on_show(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        self.progress.configure(text="正在读取魂二存档目录…")

        def work() -> Any:
            return service.ds2_locate()

        def done(loc: Any, err: Exception | None) -> None:
            if err is not None:
                self.progress.configure(text="")
                self.banner.show("bad", f"读取存档目录失败：{err}")
                return
            self._loc = loc
            self.render(loc)

        self.app.run_async(work, done)

    def render(self, loc: Any) -> None:
        self.progress.configure(text="")
        if loc.dir is None:
            self.where.configure(
                text="没找到魂二存档目录 —— 这台机器还没玩过魂二。\n"
                "（仍然可以“从文件夹导入”：导入时会按你的账号自动建好目录。）"
            )
            self.btn_out.configure(state="disabled")
            self.btn_open.configure(state="disabled")
            self.btn_in.configure(state="normal")
            return
        total = sum(f.size for f in loc.files)
        self.where.configure(
            text=f"存档位置：{loc.dir}\n"
            f"账号：{loc.accountid}    共 {len(loc.files)} 个文件 · {service.fmt_size(total)}"
        )
        self.btn_out.configure(state="normal" if loc.files else "disabled")
        self.btn_in.configure(state="normal")
        self.btn_open.configure(state="normal")

    # ------------------------------------------------------------ 动作

    def open_saves(self) -> None:
        loc = self._loc
        if loc is None or loc.dir is None:
            return
        if not service.open_path(loc.dir):
            self.banner.show("warn", f"打不开：{loc.dir}")

    def do_export(self) -> None:
        loc = self._loc
        if loc is None or loc.dir is None or not loc.files:
            self.banner.show("bad", "没有可导出的存档。")
            return
        dest = filedialog.askdirectory(
            title="选一个文件夹来放存档包（网盘同步目录 / U 盘都可以）",
        )
        if not dest:
            return
        self.banner.hide()
        self.btn_out.configure(state="disabled")
        self.progress.configure(text=f"正在导出 {len(loc.files)} 个文件并计算校验值…")

        def work() -> Any:
            return service.ds2_export(loc, dest, label="gui")

        def done(res: Any, err: Exception | None) -> None:
            self.btn_out.configure(state="normal")
            self.progress.configure(text="")
            if err is not None or not res.ok:
                self.banner.show("bad", f"导出失败：{err or res.error}")
                return
            self.banner.show(
                "ok",
                f"已导出 {res.file_count} 个文件（{service.fmt_size(res.total_size)}）"
                f"到 {res.pack_dir}",
            )

        self.app.run_async(work, done)

    def do_import(self) -> None:
        pack_dir = filedialog.askdirectory(title="选择存档包文件夹（里面应有 savepack.json）")
        if not pack_dir:
            return
        ok, problems = service.ds2_verify_pack(pack_dir)
        if not ok:
            self.banner.show(
                "bad",
                f"这个包不完整或已损坏：{problems[0] if problems else '未知原因'}",
            )
            return

        manifest = service.ds2_read_pack(pack_dir)
        target = service.ds2_locate_for_import()
        if target.dir is None:
            self.banner.show(
                "bad",
                "定位不到本机的魂二存档目录，也建不出来 —— 请先用命令行加 --account 指定账号。",
            )
            return

        if not messagebox.askyesno(
            "确认导入",
            f"将把 {manifest.get('file_count')} 个文件"
            f"（{manifest.get('exported_at')} 导出，来自 {manifest.get('host')}）\n\n"
            f"导入到：\n{target.dir}\n\n"
            f"导入前会自动把现有存档备份一份。继续吗？",
        ):
            return

        self.banner.hide()
        self.btn_in.configure(state="disabled")
        self.progress.configure(text="正在校验并写入（导入前会先备份现有存档）…")

        def work() -> Any:
            return service.ds2_import(pack_dir, target)

        def done(res: Any, err: Exception | None) -> None:
            self.btn_in.configure(state="normal")
            self.progress.configure(text="")
            if err is not None or not res.ok:
                self.banner.show("bad", f"导入失败：{err or res.error}")
                return
            backup = f"（已备份到 {res.backup_dir}）" if res.backup_dir else ""
            self.banner.show("ok", f"已写入 {len(res.written)} 个文件{backup}")
            self.refresh()

        self.app.run_async(work, done)


# ---------------------------------------------------------------- 入口


def build_app(*, appid: str | None = None, smoke: bool = False, start_page: int = 0) -> App:
    """建好窗口但不进入事件循环 (测试 / 自检用)."""
    app = App(appid=appid, smoke=smoke)
    pages = [HomePage(app.content, app), BackupPage(app.content, app),
             PlayPage(app.content, app), HelpPage(app.content, app),
             Ds2Page(app.content, app)]
    app._pages = pages
    app.go(max(0, min(start_page, len(pages) - 1)))
    return app


def run(argv: list[str] | None = None) -> int:
    """打开向导并进入事件循环.

    `argv` 只是兜底: 命令行解析在入口 `tools/bridge_gui.py` 里已经做完了.
    """
    appid, smoke, page = None, False, 0
    args = list(argv or [])
    for i, a in enumerate(args):
        if a == "--appid" and i + 1 < len(args):
            appid = args[i + 1]
        elif a == "--page" and i + 1 < len(args):
            try:
                page = int(args[i + 1])
            except ValueError:
                page = 0
        elif a == "--smoke":
            smoke = True

    try:
        app = build_app(appid=appid, smoke=smoke, start_page=page)
    except tk.TclError:  # 没有图形环境 (远程会话 / 精简系统)
        return 2
    app.mainloop()
    return 0
