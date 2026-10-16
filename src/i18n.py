"""多语言文案层 (界面用).

设计: **中文原文就是 key** —— 与 gettext 拿源语言字符串当 `msgid` 是同一套思路.
三个直接后果, 都是我们要的:

1. **中文模式下 `t()` 原样返回** ⇒ 既有输出 (命令行版的中文提示、单测里断言的
   中文) 一个字节都不变 —— 引入 i18n 不会碰坏任何既有契约;
2. 英文表**缺条目时退回中文** ⇒ 最坏情况是界面上冒一句中文, 而不是白屏或抛异常;
3. 源码里写着 `i18n.t("存档位置")`, 自己就是可读的, 不需要再维护一份 key 表.

缺条目这件事不靠自觉, 由 `tests/test_i18n.py` 的 **AST 全量扫描**兜住:
源码里每一个 `t("…")` 的 key 都必须在 `CATALOG` 里有对应英文, 漏一个就红.

**语言是界面层的事**: 这里只存一个模块级状态, 默认 `zh`, 且**谁都不许在 import 时
改它**. 只有图形向导入口会 `set_lang(detect())`. 命令行版因此永远保持中文 ——
否则用户存了英文, 再跑命令行会看到"一半英文一半中文"的混合输出.

**不许翻译的东西**: 目录名 / 文件名 / schema / 环境变量名 / 命令名. 那些是**契约**
(例如 `savepack.json`、`files/`、`save-backup`), 跟着界面语言变会让两台机器对不上.
本期已用 AST 全量核对过: 这几个文件里没有把中文当逻辑用
(比较 / 下标 / 字典键 / `get`) —— 见 `tests/test_i18n.py::TestNoChineseInLogic`.

唯一的例外是魂二存档包里那份**给人读的说明文件** (`ds2.note_name()`): 程序从不按
名字读它, 导入端只认 `savepack.json` + `files/`, 所以让它跟着界面语言走是安全的
(英文界面下叫 `READ-ME-FIRST.txt`), 免得海外用户对着一个中文文件名发愣.
"""

from __future__ import annotations

import json
import locale
import os
import sys
import warnings
from pathlib import Path

# ---------------------------------------------------------------- 语言

LANGS: tuple[str, ...] = ("zh", "en")
"""支持的语言. 第一个是默认语言."""

DEFAULT_LANG = LANGS[0]

LANG_LABEL: dict[str, str] = {
    "zh": "中文",
    "en": "English",
}
"""语言选择器上显示的名字. **故意不翻译** —— 否则找不到自己母语的人会更懵."""

_ENV_LANG = "SCB_LANG"
"""环境变量: 强制指定界面语言 (`zh` / `en`). 优先级高于存下来的设置."""

_ENV_SETTINGS = "SCB_SETTINGS"
"""环境变量: 覆盖设置文件路径 (测试用). 见 `settings_path`."""

_lang: str = DEFAULT_LANG


def is_supported(code: str | None) -> bool:
    return bool(code) and str(code).strip().lower() in LANGS


def current() -> str:
    """当前语言代码."""
    return _lang


def set_lang(code: str) -> str:
    """切换语言; 返回生效后的代码.

    只认 `LANGS` 里的值 —— 未知语言**报错**而不是悄悄退回默认: 界面上的语言
    选择器不可能传出别的值, 真传出来了说明代码写错了, 应该当场响.
    """
    c = str(code).strip().lower()
    if c not in LANGS:
        raise ValueError(f"不支持的语言: {code!r} (可选: {', '.join(LANGS)})")
    global _lang
    _lang = c
    return _lang


def t(src: str) -> str:
    """把中文原文翻成当前语言.

    `zh` 模式**原样返回** (这是"既有输出不变"的全部秘密); `en` 模式查表,
    查不到也原样返回.
    """
    if _lang == DEFAULT_LANG:
        return src
    return CATALOG.get(src, src)


def keys() -> list[str]:
    """英文表里已有的 key (测试与自检用)."""
    return sorted(CATALOG)


# ---------------------------------------------------------------- 设置落盘


def settings_path() -> Path:
    """设置文件放哪: `%APPDATA%\\SteamCloudBridge\\settings.json`.

    为什么是这里而不是程序旁边: 包可能被解压到 `Program Files` 这类只读位置,
    "顺手在 exe 旁边写个文件"会直接失败. 用户目录永远可写.
    `SCB_SETTINGS` 可以把整条路径换掉 (测试用, 免得动到真实用户目录).
    """
    override = os.environ.get(_ENV_SETTINGS, "").strip()
    if override:
        return Path(override)
    base = os.environ.get("APPDATA", "").strip()
    root = Path(base) / "SteamCloudBridge" if base else Path.home() / ".steamcloudbridge"
    return root / "settings.json"


def load_saved() -> str | None:
    """读回上次选的语言; 没有 / 坏了 / 不是支持的值都返回 `None`."""
    try:
        raw = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    lang = raw.get("lang")
    return str(lang).lower() if is_supported(lang) else None


def save_lang(code: str) -> bool:
    """把语言记下来. **尽力而为** —— 写不进去也不该让界面崩掉, 所以返回布尔."""
    if not is_supported(code):
        return False
    path = settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"lang": str(code).lower()}
        old = {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                old = loaded
        except (OSError, ValueError):
            pass
        old.update(payload)
        # 先写临时文件再替换: 中途断电也不会留下半个文件把设置读坏.
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- 探测


def system_lang() -> str | None:
    """按操作系统语言给个建议值; 认不出来返回 `None` (调用方会退回默认).

    只做"是不是中文"这一档判断: 中文系统给中文, 其它一律给英文 —— 这是**面向
    海外**的默认, 比"非中文就退回中文"合理得多.
    """
    candidates: list[str] = []
    # `locale.getdefaultlocale` 在 3.13 起已废弃, 未来会被删 —— 用 getattr 取,
    # 取不到就跳过, 别让一个"读系统语言"的辅助函数把整个界面拖下水.
    for name in ("getlocale", "getdefaultlocale"):
        getter = getattr(locale, name, None)
        if getter is None:
            continue
        try:
            with warnings.catch_warnings():
                # 它的废弃警告只在这里是噪音: 它已经是最后一档兜底, 拿不到就算了.
                warnings.simplefilter("ignore", DeprecationWarning)
                pair = getter()
        except Exception:  # noqa: BLE001 - 见上: 系统语言读不到不算致命
            continue
        if pair and pair[0]:
            candidates.append(str(pair[0]))
    # Windows 上 getdefaultlocale 在部分区域会抛, 再补一条系统 API 兜底.
    if sys.platform == "win32" and not candidates:
        try:
            import ctypes

            buf = ctypes.create_unicode_buffer(85)
            if ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, 85):
                candidates.append(buf.value)
        except Exception:  # noqa: BLE001
            pass

    for name in candidates:
        if name.lower().startswith("zh"):
            return "zh"
    return "en" if candidates else None


def detect(explicit: str | None = None) -> str:
    """按优先级定语言: 显式参数 -> 环境变量 -> 存下来的设置 -> 系统语言 -> 默认.

    显式参数和环境变量**都不写盘** —— 它们表达的是"这一次这么跑", 不是"以后都这样".
    """
    if is_supported(explicit):
        return str(explicit).strip().lower()
    env = os.environ.get(_ENV_LANG, "").strip()
    if is_supported(env):
        return env.lower()
    saved = load_saved()
    if saved:
        return saved
    return system_lang() or DEFAULT_LANG


# ---------------------------------------------------------------- 英文表

"""`中文原文 -> 英文`. 由 `tests/test_i18n.py` 保证与源码里的 `t()` 调用一一对应.

放最后是故意的: 读源码的人先看到机制, 再看到数据 —— 这张表很长, 放前面会
把 API 挤到两千行之外.
"""

CATALOG: dict[str, str] = {
    '      否则刚放回去的存档会被云端旧档覆盖。': '       or the save you just restored gets overwritten by the older cloud copy.',
    '    {0} 字节': '    {0} bytes',
    '  * 只适用于同一 Steam 账号; 换账号导入会被工具拒绝': '  * Only works between the same Steam account; importing into another account is refused.',
    '  * 导入时请务必先退出游戏 (工具也会检查)': '  * Fully quit the game before importing (the tool checks this too).',
    '  * 魂二没有 Steam 云存档, 这个包就是你的"云"': '  * Dark Souls II has no Steam Cloud — this pack is your cloud.',
    '  1. 打开 {0}': '  1. Open {0}',
    '  1. 把这个文件夹整个放到一个能跨机同步的地方 (网盘同步目录 / U 盘)': '  1. Put this whole folder somewhere both machines can reach (cloud-sync folder / USB drive)',
    '  2. 在另一台机器上用工具执行: 从文件夹导入': '  2. On the other machine, use the tool: import from folder',
    '  2. 把里面的存档文件夹改名留底 (例如加上 .old)': '  2. Rename the save folder there as a keepsake (e.g. append .old)',
    '  3. 从本备份里把内容整个复制回去': '  3. Copy everything from this backup back into place',
    '  3. 导入前会自动备份那台机器上的现有存档': '  3. The existing save on that machine is backed up automatically first',
    '  4. 重新启动 Steam 和游戏': '  4. Start Steam and the game again',
    '  files/          存档本体': '   files/          the save files themselves',
    '  savepack.json   清单 (每个文件的 sha256, 导入时逐字节核对; 别改它)': '   savepack.json   manifest (sha256 per file, byte-checked on import; do not edit)',
    ' · 云端有一份 {0}': ' · a {0} copy in the cloud',
    '!  关于魂二：它没有 Steam 云存档，这一点改不了': '!  About Dark Souls II: it has no Steam Cloud — and that cannot be changed',
    '(未知)': '(unknown)',
    '(测试钩子) 已记录 {0}': '(test hook) recorded {0}',
    'Steam 好像没起来，手动开一下再点“重新检查”。': "Steam doesn't seem to have started. Open it yourself, then click “Check again”.",
    'Steam 安装': 'Steam installation',
    'Steam 客户端': 'Steam client',
    'Steam 已启动。': 'Steam has started.',
    'Steam 没在运行，先点上面的“启动它”，再回来敲这一行。': "Steam isn't running. Click “Start it” above first, then come back and type this line.",
    'Steam 没开？启动它': 'Steam not running? Start it',
    'Steam 的云开关分两层：账号那一层是持久的，游戏这一层只在本次开启 Steam 期间有效。\n所以每次开始玩之前，按下面三步走一遍就行（半分钟）。': "Steam's cloud switch has two levels: the account level is permanent, the per-game level only lasts while Steam stays open.\nSo before each session just walk through the three steps below (takes half a minute).",
    'SteamCloudBridge · 黑魂存档同步': 'SteamCloudBridge · Dark Souls save sync',
    'SteamCloudBridge 存档备份': 'SteamCloudBridge save backup',
    'U 盘 / 移动硬盘': 'USB drive / external disk',
    'dry-run: 未写任何文件': 'dry-run: no files were written',
    '{0} 下没有任何账号目录 —— 本机没玩过魂二, 或存档在别处': 'No account folder under {0} — this machine has never run Dark Souls II, or the save is elsewhere',
    '{0} 个文件 · {1}': '{0} files · {1}',
    '{0} 正在运行 —— 退出游戏后再导入': '{0} is running — quit the game before importing',
    '● 未开始': '● Not started',
    '● 正在等待你敲入…': '● Waiting for your input…',
    '● 没等到回执': '● No confirmation seen',
    '● 监听中断': '● Watching stopped',
    '✓ 已点亮': '✓ Cloud on',
    '上一步': 'Back',
    '下一步': 'Next',
    '不是哪一步没做对，也不是设置没开 —— 是这个游戏本身就不提供云存档。\n本机四条证据都对得上：账号配置里没有它的云记录；云日志只有一句\n「Not running AutoCloud. No config data found」；存档目录里没有云配置文件；\n云端用户目录里根本没有它。Steam 商店页也没标它支持云存档。\n所以没有开关可以点亮，工具也变不出一个不存在的功能 —— 这属于游戏方的事，\n第三方替它加不上。\n本工具对魂二的方案是「用文件夹搬」：导出成一个文件夹 → 放进你自己的网盘\n同步目录或 U 盘 → 换台机器再导入回来（左侧第 5 步「魂2 存档」）。\n全程只搬字节，不读、不改存档内容。': "You didn't miss a step, and nothing is switched off — the game itself has no cloud saves at all.\nFour checks on this machine agree: the account config has no cloud entry for it; the cloud log holds a single\nline, “Not running AutoCloud. No config data found”; there is no cloud config file in the save folder;\nand it has no folder at all under the cloud user data. Steam's store page doesn't list Cloud for it either.\nSo there is no switch to turn on, and this tool cannot conjure a feature that doesn't exist — that call\nbelongs to the developer, and no third party can make it.\nWhat this tool does for Dark Souls II is “move the folder”: export to a folder → put it in your own\ncloud-sync folder or USB drive → import it on the other machine (step 5, “Dark Souls II saves”).\nOnly bytes are moved; save contents are never read or altered.",
    '不能。云存档是游戏方接进 Steam 的功能，第三方替它加不上，我们也不去改游戏。本工具能做的是“文件夹搬”：导出 → 放进网盘或 U 盘 → 另一台机器导入，效果上一样是跨机接着玩，只是要你自己点两下。': "No. Cloud saves are a feature the game itself wires into Steam — a third party can't add it, and we won't patch the game. What this tool does instead is “move folders”: export → drop it into your cloud folder or USB drive → import on the other machine. Same result — carry on playing on another PC — you just click twice.",
    '云同步范围': 'Cloud sync scope',
    '云端已有一份副本': 'A copy already exists in the cloud',
    '云端版本号: {0}': 'Cloud revision: {0}',
    '云通道已点亮': 'Cloud sync is on',
    '云通道已点亮。现在可以启动游戏了。': 'Cloud sync is on. You can start the game now.',
    '云通道未点亮': 'Cloud sync is off',
    '云通道每次都要重新点亮': 'Cloud sync must be turned on again every session',
    '云通道状态一直显示未点亮': 'The cloud status never shows as enabled',
    '仅落点所在目录 · 未能确定云同步范围': 'Only the landing folder · sync scope undetermined',
    '从文件夹导入…': 'Import from folder…',
    '会被封号吗': 'Will I get banned?',
    '你指定的是 accountid={0}, 但磁盘上只有一个账号目录 ({1} -> {2}) —— 按磁盘证据优先选用它': 'You specified accountid={0}, but disk has exactly one account folder ({1} -> {2}) — disk evidence wins, using it',
    '先安装并登录 Steam': 'Install Steam and sign in first',
    '先把存档完整复制一份': 'Make a complete copy of the saves first',
    '先按第三步点亮一次云通道, 再回来刷新': 'Turn cloud sync on once from step 3, then come back and refresh',
    '先退出游戏再点亮通道': 'Quit the game before turning cloud sync on',
    '内容不符(sha256): {0}': 'Content mismatch (sha256): {0}',
    '内容与校验 (sha256)': 'Contents and checksums (sha256)',
    '写入后校验不符: {0} —— 目标盘可能有问题': 'Post-write verification failed: {0} — the target drive may be faulty',
    '写入失败 {0}: {1}': 'Write failed {0}: {1}',
    '写清单失败: {0}': 'Writing the manifest failed: {0}',
    '前提: 先彻底退出游戏, 并退出 Steam (右下角托盘也要退干净),': 'Prerequisite: fully quit the game AND Steam (exit it from the tray too),',
    '包校验未通过, 拒绝导入': 'Pack verification failed; import refused',
    '包清单里没有 accountid (老版本导出的包?) —— 无法核对账号归属': 'The pack manifest has no accountid (a pack from an older version?) — cannot verify account ownership',
    '启动一次游戏就会生成': 'Starting the game once will create it',
    '启动失败: {0}': 'Launch failed: {0}',
    '启动游戏': 'Start the game',
    '在“备份存档”一页里点“打开备份文件夹”，里面有带时间戳的完整副本，以及一份写着恢复步骤的说明文件。': 'On the “Back up saves” page click “Open backup folder”: it holds timestamped full copies plus a note file with the restore steps.',
    '备份 {0} 失败: {1}': 'Backing up {0} failed: {1}',
    '备份失败, 已中止 (一个字都没写): {0}': 'Backup failed, aborted (not a byte written): {0}',
    '备份失败: {0}': 'Backup failed: {0}',
    '备份存档': 'Back up saves',
    '备份成功，已核对无误。位置: {0}': 'Backup succeeded and verified. Location: {0}',
    '备份文件夹还不存在（先做一次备份）。': "The backup folder doesn't exist yet (run a backup first).",
    '备份时间  {0}': 'Backed up at  {0}',
    '备份目录  {0}': 'Backup folder  {0}',
    '复制': 'Copy',
    '复制失败 {0}: {1}': 'Copy failed {0}: {1}',
    '复制失败: {0}': 'Copy failed: {0}',
    '大小不符: {0} (清单 {1}, 实际 {2})': 'Size mismatch: {0} (manifest {1}, actual {2})',
    '存档: {0}': 'Saves: {0}',
    '存档与账号绑定: 只搬自己账号的档': 'Saves are tied to an account: only your own saves are moved',
    '存档丢了 / 想回到之前的进度': 'My save is gone / I want to go back to an earlier point',
    '存档位置': 'Save location',
    '存档位置：{0}\n账号：{1}    共 {2} 个文件 · {3}': 'Save location: {0}\nAccount: {1}    {2} files · {3}',
    '存档是在退出游戏时上传的。如果上次是非正常退出（崩溃、强杀进程），Steam 可能没来得及同步 —— 下次正常退出一次即可。': 'Saves upload when you quit the game. After a crash or a force-kill Steam may not have had time to sync — just quit normally once next time and it catches up.',
    '存档根: {0}': 'Save root: {0}',
    '存档根下有 {0} 个账号目录 (accountid={1}), 但无法确定哪个是本机账号 —— 请用 --account <accountid> 指定': 'The save root holds {0} account folders (accountid={1}), but there is no way to tell which one belongs to this machine — pass --account <accountid>',
    '存档目录未定位 (先跑 locate / status)': 'Save folder not located (run locate / status first)',
    '存档目录里没有可导出的文件: {0}': 'No exportable files in the save folder: {0}',
    '完成': 'Done',
    '完成: {0} 个文件，{1}，已逐个核对校验值': 'Done: {0} files, {1}, every checksum verified',
    '定位不到本机的魂二存档目录，也建不出来 —— 请先用命令行加 --account 指定账号。': "Couldn't locate — or create — this machine's Dark Souls II save folder. Run the command-line version with --account to name the account.",
    '导入失败：{0}': 'Import failed: {0}',
    '导出到哪儿？两种都行': 'Where to export? Either works',
    '导出到文件夹…': 'Export to folder…',
    '导出失败：{0}': 'Export failed: {0}',
    '导出时间 : {0}': 'Exported at : {0}',
    '导出目录直接选 U 盘。插到另一台机器上，用下面的“从文件夹导入”。': 'Pick the USB drive as the export folder. Plug it into the other machine and use “Import from folder” below.',
    '导出途中源文件消失: {0}': 'Source file vanished mid-export: {0}',
    '将把 {0} 个文件（{1} 导出，来自 {2}）\n\n导入到：\n{3}\n\n导入前会自动把现有存档备份一份。继续吗？': 'About to import {0} files (exported {1} from {2})\n\nInto:\n{3}\n\nThe current save is backed up automatically first. Continue?',
    '已交给 Steam 启动游戏。玩完正常退出，存档会自动上传到云端。': "Steam is launching the game. Quit normally when you're done and the save uploads to the cloud by itself.",
    '已写入 {0} 个文件{1}': 'Wrote {0} files{1}',
    '已取消': 'Cancelled',
    '已复制': 'Copied',
    '已导出 {0} 个文件（{1}）到 {2}': 'Exported {0} files ({1}) to {2}',
    '已有备份': 'Backups exist',
    '已检查': 'Checked',
    '已经点亮 (本次开 Steam 之后)': 'On (since Steam started this time)',
    '常见故障与出口': 'Common problems and how out',
    '常见问题': 'Common questions',
    '建议: ': 'Suggestion: ',
    '怎么恢复 (万一存档出问题)': 'How to restore (if a save goes wrong)',
    '怎么用:': 'How to use:',
    '打不开: {0}': 'Cannot open: {0}',
    '打不开：{0}': 'Cannot open: {0}',
    '打开 Steam 控制台': 'Open Steam Console',
    '打开备份文件夹': 'Open backup folder',
    '打开存档文件夹': 'Open save folder',
    '找不到 Steam 安装目录': 'Steam installation not found',
    '把整个存档目录（含所有角色进度与设置）完整复制一份到程序旁边。\n只会复制，不会移动或删除任何东西。复制完会逐个文件核对校验值。': "Copies the whole save folder (every character's progress and all settings) next to the program.\nIt only copies — nothing is moved or deleted — and verifies each file's checksum afterwards.",
    '把这一行敲进控制台（或复制粘贴）': 'Type this line into the console (or paste it)',
    '指定的 Steam 目录不存在: {0}': "The Steam folder you specified doesn't exist: {0}",
    '按云同步规则': 'From the sync rule',
    '按第三步走一遍即可': 'Just walk through step 3',
    '按落点反推 · {0} 下第一层': 'Inferred from the landing path · first level under {0}',
    '控制台已打开。⚠ 光打开还不算 —— 先用鼠标点一下最底下那行输入框，再把上面那一行敲进去（或按 Ctrl+V 粘贴）。': 'Console opened. ⚠ Opening it is not enough — click the input line at the bottom first, then type the line above (or press Ctrl+V to paste).',
    '收到点亮回执': 'Confirmation received',
    '敲对了不会有任何成功提示，这是正常的 —— 下面的状态灯会自己变绿。': "Getting it right shows no success message — that's normal. The status light below turns green by itself.",
    '文件     : {0} 个, 共 {1:,} 字节': 'Files    : {0}, {1:,} bytes total',
    '无法创建包目录 {0}: {1}': 'Cannot create pack folder {0}: {1}',
    '无法创建目标目录 {0}: {1}': 'Cannot create target folder {0}: {1}',
    '无法判断游戏是否在运行 (tasklist 不可用) —— 请自行确认已退出游戏': "Can't tell whether the game is running (tasklist unavailable) — please make sure you've quit it",
    '无法检查': 'Cannot check',
    '暂时判断不了': "Can't tell yet",
    '未检查': 'Not checked',
    '未知原因': 'unknown reason',
    '本会话通道已点亮，中途别重启 Steam。': "Cloud sync is on for this session — don't restart Steam.",
    '本地存档': 'Local saves',
    '本机状况': 'This machine',
    '本次云通道': 'Cloud this session',
    '本次云通道已经是亮着的，可以直接启动游戏。': 'Cloud sync is already on for this session — go ahead and start the game.',
    '本次导出备注:': 'Export notes:',
    '来源: {0}     ({1})': 'Source: {0}     ({1})',
    '来源机器 : {0}': 'From machine : {0}',
    '来源目录  {0}': 'Source folder  {0}',
    '校验不一致: {0}': 'Checksum mismatch: {0}',
    '校验读取失败: {0}: {1}': 'Checksum read failed: {0}: {1}',
    '检查中…': 'Checking…',
    '检查失败': 'Check failed',
    '检查失败: {0}': 'Check failed: {0}',
    '检查完成': 'Check complete',
    '检查环境': 'Check setup',
    '正在启动 Steam，请等它登录完成…': 'Starting Steam — wait for it to finish signing in…',
    '正在复制并校验（约 27 MB，几秒钟）…': 'Copying and verifying (~27 MB, a few seconds)…',
    '正在导出 {0} 个文件并计算校验值…': 'Exporting {0} files and computing checksums…',
    '正在忙，等这一步做完再切语言。': 'Busy right now — switch the language once this step finishes.',
    '正在校验并写入（导入前会先备份现有存档）…': 'Verifying and writing (the current save is backed up first)…',
    '正在检查…': 'Checking…',
    '正在读取…': 'Reading…',
    '正在读取魂二存档目录…': 'Reading the Dark Souls II save folder…',
    '正在运行': 'Running',
    '每次玩之前': 'Before each session',
    '没在运行': 'Not running',
    '没找到 appid {0} 的安装清单': 'No install manifest for appid {0}',
    '没找到说明文件。': 'Documentation file not found.',
    '没找到魂二存档目录 —— 这台机器还没玩过魂二。\n（仍然可以“从文件夹导入”：导入时会按你的账号自动建好目录。）': 'No Dark Souls II save folder found — this machine has never run it.\n(You can still “Import from folder”: the folder is created for your account on import.)',
    '没有云，用文件夹搬': 'No cloud — move a folder instead',
    '没有可导出的存档。': 'Nothing to export.',
    '没有在本机找到 Steam': 'Steam not found on this machine',
    '没等到控制台回执。可能是没敲进去，或敲到了别的地方。点一次“打开 Steam 控制台”，用鼠标点一下输入行后重新敲；也可以直接启动游戏，通道通常已经生效。': 'No console confirmation. The line may not have gone in, or it went somewhere else. Click “Open Steam Console” once, click the input line and type it again — or just start the game: sync is usually already active.',
    '没能确定存档在哪儿': "Couldn't determine where the saves are",
    '没能自动打开控制台。可以手动：按 Win+R，输入 steam://open/console 回车。': "Couldn't open the console automatically. Do it manually: press Win+R, type steam://open/console and hit Enter.",
    '注意:': 'Notes:',
    '测试钩子写入失败: {0}': 'Test hook write failed: {0}',
    '清单 schema 不认识: {0} (期望 scb-savepack/…)': 'Unknown manifest schema: {0} (expected scb-savepack/…)',
    '清单格式不对 ({0}): 顶层不是对象': 'Bad manifest format ({0}): the top level is not an object',
    '清单读不出来 ({0}): {1}': 'Cannot read the manifest ({0}): {1}',
    '清单里有可疑文件名, 已中止': 'Suspicious filename in the manifest; aborted',
    '清单里没有任何文件 —— 空包': 'The manifest lists no files — empty pack',
    '清单里的文件名含路径分隔符, 拒绝: {0}': 'Manifest filename contains a path separator; refused: {0}',
    '游戏': 'Game',
    '游戏已经在运行了。': 'The game is already running.',
    '游戏本体': 'Game install',
    '游戏正在运行': 'Game is running',
    '游戏正在运行 —— 请先完全退出游戏，再点亮通道。': 'The game is running — fully quit it before turning cloud sync on.',
    '游戏账号  {0}': 'Account  {0}',
    '游戏退出后云端没更新': "The cloud didn't update after I quit the game",
    '点亮云通道, 然后开游戏': 'Turn cloud sync on, then start the game',
    '点亮云通道需要 Steam 开着, 我可以帮你启动': 'Turning cloud sync on needs Steam running; I can start it for you',
    '点亮命令只在本次开启 Steam 期间有效。重启过 Steam，就要重新敲一次。如果敲了仍然不变，先确认控制台里输入行有焦点（用鼠标点一下那行）。': 'The command only lasts for the current Steam session. Once you restart Steam, type it again. If typing it changes nothing, make sure the console input line has focus (click it once).',
    '点右上角的“中文 / English”就换了，选过就记住（写在 %APPDATA%\\SteamCloudBridge\\settings.json）。第一次打开时按系统语言自动选：中文系统给中文，其余一律给英文。想强制指定，用 SteamCloudBridgeGUI.exe --lang en。命令行版始终是中文，不受影响。': 'Click “中文 / English” in the top-right corner — your choice is remembered (stored in %APPDATA%\\SteamCloudBridge\\settings.json). On first launch it follows your system language: Chinese systems get Chinese, everything else gets English. To force it, run SteamCloudBridgeGUI.exe --lang en. The command-line tools stay Chinese either way.',
    '点右上角的“打开 Steam 控制台”，然后把下面这一行敲进控制台。': 'Click “Open Steam Console” at the top right, then type the line below into the console.',
    '状态正常。云通道每次启动 Steam 都会关掉，所以下一步先备份，再进入第三步点亮它。': 'All good. Cloud sync switches itself off every time Steam restarts, so next: back up, then go to step 3 and turn it on.',
    '玩完正常退出游戏就行，Steam 会把存档同步到云端。': "Just quit the game normally when you're done — Steam syncs the save to the cloud.",
    '界面怎么换成英文': 'How do I switch the interface to English?',
    '目录内 {0} 个文件计入 (include_all={1})': '{0} files included from the folder (include_all={1})',
    '目标存档目录未定位': 'Target save folder not located',
    '目标目录本来就没有文件, 无需备份': 'The target folder was empty; no backup needed',
    '看使用说明': 'Read the manual',
    '看看这台机器现在的状态': 'See what this machine looks like right now',
    '确认导入': 'Confirm import',
    '确认游戏装在这台机器上': 'Make sure the game is installed on this machine',
    '磁盘上找到的账号目录: ': 'Account folders found on disk: ',
    '磁盘上没有账号目录, 按已验证规则(SteamID64 十六进制)生成: {0}': 'No account folder on disk; generated from the verified rule (SteamID64 hex): {0}',
    '磁盘上没有这个文件': "This file isn't on disk",
    '磁盘上的账号目录不足以判定, 取本机当前账号 accountid={0}': "Disk folders are inconclusive; using this machine's account accountid={0}",
    '磁盘上的账号目录没有一个属于 accountid={0}; 找到的是 {1} —— 拒绝猜哪个是你': 'None of the account folders on disk belongs to accountid={0}; found {1} — refusing to guess which one is yours',
    '立即备份': 'Back up now',
    '等待控制台回执中（最多 3 分钟）': 'Waiting for the console reply (up to 3 minutes)',
    '等待超时, 没看到回执': 'Timed out; no reply seen',
    '等确认': 'Waiting',
    '缺少文件: {0}': 'Missing file: {0}',
    '网盘同步目录': 'Cloud-sync folder',
    '该目录本次是新按规则确定的, 不是磁盘上实测到的 —— 请确认账号无误': 'This folder was derived from the rule, not observed on disk — please confirm the account is correct',
    '读不到 %APPDATA%, 无法定位存档目录': 'Cannot read %APPDATA%; the save folder cannot be located',
    '读取存档目录失败：{0}': 'Reading the save folder failed: {0}',
    '账号     : accountid={0}  (SteamID64={1})': 'Account  : accountid={0}  (SteamID64={1})',
    '账号目录存在, 但里面没有 .sl2 / .co2 存档 —— 导出会得到空包': 'The account folder exists but holds no .sl2 / .co2 save — the export would be empty',
    '跨账号: 包属于 accountid={0}, 本机目标目录属于 accountid={1} —— 拒绝导入': "Cross-account: the pack belongs to accountid={0}, this machine's target folder to accountid={1} — import refused",
    '还没定位到存档目录 —— 回到第一步点一次“重新检查”。': 'Save folder not located yet — go back to step 1 and click “Check again”.',
    '还没定位到存档目录, 无法备份': 'Save folder not located yet; cannot back up',
    '还没定位到存档目录。': 'Save folder not located yet.',
    '还没定位到存档目录，先回到第一步重新检查。': 'Save folder not located yet — go back to step 1 and check again.',
    '还没有备份。': 'No backups yet.',
    '还没检查过环境，先回到第一步。': "Setup hasn't been checked yet — go back to step 1.",
    '还没点亮': 'Not on',
    '这一步只看不改，不会动任何文件。': 'This step only looks — it never touches a single file.',
    '这不是一个完整的存档包: 找不到 {0} ({1}). 多半是导出没跑完, 或把 files/ 里的文件单独拷了出来.': "This isn't a complete save pack: {0} not found ({1}). Most likely the export didn't finish, or the files inside files/ were copied out separately.",
    '这个包不完整或已损坏：{0}': 'This pack is incomplete or damaged: {0}',
    '这个包是 appid={0} 的, 不是魂二 ({1})': 'This pack is for appid={0}, not Dark Souls II ({1})',
    '这个工具只在你自己账号的存档目录里搬字节，不修改游戏、不注入、不联机作弊。黑魂3 没有 VAC；封号针对的是上传非法内容存档，与本工具无关。': "This tool only moves bytes inside your own account's save folder. It doesn't modify the game, inject anything, or cheat online. Dark Souls III has no VAC; bans target uploaded illegal saves, which has nothing to do with this tool.",
    '进入第三步, 敲一行就亮了': 'Go to step 3 — one line lights it up',
    '选一个文件夹来放存档包（网盘同步目录 / U 盘都可以）': 'Pick a folder for the save pack (a cloud-sync folder or a USB drive both work)',
    '选中 {0} -> accountid={1}': 'Selected {0} -> accountid={1}',
    '选成 OneDrive／坚果云／Syncthing 这类会自动同步的文件夹。之后在另一台机器上打开同一个文件夹，直接导入即可。': 'Pick a folder that syncs by itself — OneDrive, Dropbox, Syncthing and the like. Then open the same folder on the other machine and import.',
    '选择存档包文件夹（里面应有 savepack.json）': 'Choose the save-pack folder (it should contain savepack.json)',
    '遇到问题': 'Troubleshooting',
    '里面是什么:': "What's inside:",
    '重新检查': 'Check again',
    '魂 2 存档': 'Dark Souls II saves',
    '魂2 存档': 'Dark Souls II saves',
    '魂二为什么没有“点亮云通道”这一步': 'Why is there no “turn on cloud sync” step for Dark Souls II?',
    '魂二存档包 —— {0}': 'Dark Souls II save pack — {0}',
    '魂二本身不支持 Steam 云存档 —— 不是设置没开，是游戏没有这个功能，所以没有开关可点。它改用“魂2 存档”那一页：导出成一个文件夹，放进你的网盘同步目录或 U 盘，换台机器再导入回来。': "Dark Souls II itself doesn't support Steam Cloud — it's not a setting you missed, the game simply lacks the feature, so there's no switch to flip. Use the “Dark Souls II saves” page instead: export to a folder, drop it into your cloud-sync folder or USB drive, then import it on the other machine.",
    '魂二没有 Steam 云存档, 跨机搬运靠你指定的目录 (同步盘 / U 盘)': 'Dark Souls II has no Steam Cloud; moving saves between machines relies on a folder you choose (cloud folder / USB drive)',
    '魂二能不能也搞成云存档': 'Can Dark Souls II be made to use cloud saves?',
    '魂二这个游戏，Steam 本身不提供云存档 —— 不是设置没开，是它没有这个功能，\n所以也没有开关可以点亮。\n替代办法：把存档导出成一个文件夹，放进你自己的网盘同步目录（或 U 盘）；\n换台机器再导入回来。工具只搬字节，不读取也不修改存档内容。': "Steam doesn't offer cloud saves for Dark Souls II — it's not a setting you missed, the game has no such feature,\nso there is nothing to switch on.\nInstead: export the save to a folder, put it in your own cloud-sync folder (or a USB drive),\nthen import it on the other machine. The tool only moves bytes; it never reads or alters save contents.",
    '黑魂存档同步助手   v{0}': 'Dark Souls save sync   v{0}',
    '（已备份到 {0}）': ' (backed up to {0})',
}
