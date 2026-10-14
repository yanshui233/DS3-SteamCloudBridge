#!/usr/bin/env python3
"""SteamCloudBridge 机器探测器 (portable machine probe).

目的: 在**任意一台** Windows/Linux/macOS 机器上, 一条命令扫出"这台机器的 Steam
云存全貌 + 目标游戏的存档落点 + 注入点", 并且把结果变成

  1. `machine_profile.json`        —— 完整档, 只留本机
  2. `machine_profile.share.json`  —— **脱敏档**, 可直接贴进 GitHub Issue
  3. `machine_profile.md`          —— 人读摘要 (基于脱敏档)
  4. `machine_profile_bundle.zip`  —— 上面三个打成一个包 (可选 `--zip`)

设计红线 (与 plan-v3 一致):
  * **纯只读**: 只 stat/open 读取, 绝不写入任何 Steam / 游戏目录.
  * **零第三方依赖**: 只用标准库, 用户机不需要 pip install.
  * **不硬编码盘符/账号/目录名**: 全部运行时探测.
  * **默认产出脱敏档**: 账号 ID 与用户名不进共享文件.

用法:
    python tools/machine_probe.py                    # 自动选目标游戏
    python tools/machine_probe.py --appid 374320     # 指定游戏
    python tools/machine_probe.py --list             # 只列可探测的 appid
    python tools/machine_probe.py --zip              # 额外打包成 zip
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from src.console import harden_console as _harden_console  # noqa: E402
from src.steam import accounts, cloud_log, discovery, localconfig, remotecache  # noqa: E402
from src.transport.cloudchannel import (  # noqa: E402
    CONSOLE_SESSION_MARKER as _CONSOLE_SESSION_MARKER,
    console_ack as _console_ack,
    console_var_value as _console_var_value,
)
from src.version import VERSION  # noqa: E402  # 版本号唯一来源 (src/version.py)


def _default_out_dir() -> Path:
    """默认输出目录.

    三种布局, 报告都必须落在**用户看得见、且不会被清理**的地方:

    1. **PyInstaller onefile exe** —— `__file__` 指向临时解压目录
       (`%TEMP%\\_MEIxxxx\\SteamCloudBridgeProbe.py`), 照抄它会把报告写进临时目录,
       程序一退出就被删掉, 用户看到的是"跑完了但什么都没生成".
       所以 frozen 状态下**必须**认 `sys.executable` 所在目录.
    2. **单文件发行版 .py** —— `__file__` 就是那一个 .py, 落在它同级.
    3. **仓库版** `tools/machine_probe.py` —— 落在 `<repo>/reports`.
    """
    if getattr(sys, "frozen", False):  # PyInstaller 打包产物
        return Path(sys.executable).resolve().parent / "reports"
    here = Path(__file__).resolve()
    if here.parent.name == "tools":  # 仓库布局: <repo>/tools/machine_probe.py
        return here.parent.parent / "reports"
    return here.parent / "reports"  # 单文件发行版: .py 同级


SCHEMA = "steamcloudbridge.machine_profile/1"
TOOL = "machine_probe"

# 存档目录里最多列几个文件名 (避免报告爆掉)
MAX_FILE_LIST = 40


# ---------------------------------------------------------------------------
# 只读采集原语
# ---------------------------------------------------------------------------


def scan_userdata_apps(userdata_dir: Path, accountid: str) -> dict[str, dict[str, Any]]:
    """扫描 `userdata\\<accountid>\\<appid>\\` 下每个 appid 的云存目录形态.

    判定:
      * 有 `remote\\` 目录 -> API 型 (ISteamRemoteStorage), 文件实体在本地
      * 只有 `ac\\` 目录   -> Auto-Cloud (UFS), `ac\\` 多数是空壳
    """
    result: dict[str, dict[str, Any]] = {}
    acc_dir = userdata_dir / accountid
    if not acc_dir.is_dir():
        return result

    for app_dir in acc_dir.iterdir():
        if not app_dir.is_dir() or not app_dir.name.isdigit():
            continue
        appid = app_dir.name
        remote_dir = app_dir / "remote"
        ac_dir = app_dir / "ac"
        rc_path = app_dir / "remotecache.vdf"

        info: dict[str, Any] = {
            "appid": appid,
            "path": str(app_dir),
            "has_remote_dir": remote_dir.is_dir(),
            "has_ac_dir": ac_dir.is_dir(),
            "has_remotecache": rc_path.is_file(),
            "remote_files": 0,
            "remote_bytes": 0,
            "ac_top_dirs": [],
            "cache": None,
        }

        if info["has_remote_dir"]:
            count = 0
            total = 0
            try:
                for p in remote_dir.rglob("*"):
                    if p.is_file():
                        count += 1
                        try:
                            total += p.stat().st_size
                        except OSError:
                            pass
            except OSError:
                pass
            info["remote_files"] = count
            info["remote_bytes"] = total

        if info["has_ac_dir"]:
            try:
                info["ac_top_dirs"] = sorted(d.name for d in ac_dir.iterdir() if d.is_dir())
            except OSError:
                pass

        if info["has_remotecache"]:
            try:
                info["cache"] = remotecache.parse_remotecache(rc_path, appid=appid).to_dict()
            except Exception:  # noqa: BLE001 - 单个 appid 解析失败不应中断整轮
                info["cache"] = None

        if info["has_remote_dir"] or info["has_ac_dir"] or info["has_remotecache"]:
            result[appid] = info
    return result


def appid_from_path(path: Path, manifests: dict[str, discovery.AppManifest]) -> str:
    """从 `steam_autocloud.vdf` 的绝对路径反推 appid (按 installdir 前缀最长匹配).

    不限定在同库内匹配 —— 跨库 (C 盘客户端 + D 盘库) 也要能归属.

    匹配必须落在**路径分隔符边界**上: 早先只做字符串前缀比较, 于是
    `common/Half-Life 2/hl2/save/...` 会被误判成 install-dir 为 `Half-Life` 的
    另一个 appid (字符串前缀确实命中), 把别人的存档目录算到它头上.
    """
    s = str(path).lower().replace("\\", "/")
    best = ""
    best_len = 0
    for appid, m in manifests.items():
        if not m.installdir or not m.library:
            continue
        prefix = str(m.library / "steamapps" / "common" / m.installdir)
        prefix = prefix.lower().replace("\\", "/")
        if (s == prefix or s.startswith(prefix + "/")) and len(prefix) > best_len:
            best, best_len = appid, len(prefix)
    return best or "unknown"


def probe_local_frame(install_dir: Path | None, max_depth: int = 3) -> dict[str, Any]:
    """探测游戏安装目录里的 mod 加载框架 / 启动器 (只读, 不假定游戏).

    返回的是**运行时发现**结果: 别人机器上没有这些目录时, 相关能力自动关闭,
    主流程不受影响 (plan-v3 §C 的运行时发现要求).
    """
    out: dict[str, Any] = {
        "install_dir": str(install_dir) if install_dir else None,
        # loader_dirs  = 真正的**注入点** (DLL 自动加载目录, 例如 Mod Engine 的 dllMods\)
        # mod_dirs     = 普通 mod 资源目录 (mods\ / plugins\), **不构成注入能力**
        #                例如 Unity 的 `<Game>_Data\Plugins` 遍地都是, 误判会给出假的注入点
        "loader_dirs": [],
        "mod_dirs": [],
        "config_files": [],
        "launchers": [],
        "steam_appid_txt": None,
        "injection_available": False,
    }
    if install_dir is None or not install_dir.is_dir():
        return out

    base_depth = len(install_dir.parts)
    dll_loader_names = {"dllmods"}
    mod_names = {"mods", "plugins"}

    for dirpath, dirnames, filenames in os.walk(install_dir):
        current = Path(dirpath)
        depth = len(current.parts) - base_depth
        if depth >= max_depth:
            dirnames[:] = []
        for d in list(dirnames):
            low = d.lower()
            if low in dll_loader_names:
                out["loader_dirs"].append(str(current / d))
            elif low in mod_names:
                out["mod_dirs"].append(str(current / d))
        if depth <= 1:
            for f in filenames:
                low = f.lower()
                if low.startswith("modengine") and low.endswith(".ini"):
                    out["config_files"].append(str(current / f))
                elif low == "steam_appid.txt":
                    try:
                        out["steam_appid_txt"] = (current / f).read_text(
                            encoding="utf-8", errors="replace"
                        ).strip()
                    except OSError:
                        out["steam_appid_txt"] = None
                elif low.endswith(".exe") and "launcher" in low:
                    out["launchers"].append(str(current / f))

    out["loader_dirs"] = sorted(set(out["loader_dirs"]))
    out["mod_dirs"] = sorted(set(out["mod_dirs"]))
    out["config_files"] = sorted(set(out["config_files"]))
    out["launchers"] = sorted(set(out["launchers"]))
    # 有加载器配置 (modengine.ini) 但没找到 dllMods 目录时, 也认为具备注入能力
    out["injection_available"] = bool(out["loader_dirs"] or out["config_files"])
    return out


def _list_dir_snapshot(directory: Path, limit: int = MAX_FILE_LIST) -> dict[str, Any]:
    """列出目录内容摘要 (文件名 + 扩展名直方图), 只 stat 不读内容."""
    out: dict[str, Any] = {
        "exists": directory.is_dir(),
        "file_count": 0,
        "bytes": 0,
        "files": [],
        "extensions": {},
    }
    if not directory.is_dir():
        return out

    exts: dict[str, int] = {}
    files: list[str] = []
    count = 0
    total = 0
    try:
        for p in sorted(directory.rglob("*")):
            if not p.is_file():
                continue
            count += 1
            try:
                total += p.stat().st_size
            except OSError:
                pass
            ext = p.suffix.lower() or "(no-ext)"
            exts[ext] = exts.get(ext, 0) + 1
            if len(files) < limit:
                files.append(str(p.relative_to(directory)))
    except OSError:
        pass

    out["file_count"] = count
    out["bytes"] = total
    out["files"] = files
    out["extensions"] = dict(sorted(exts.items(), key=lambda kv: (-kv[1], kv[0])))
    return out


# ---------------------------------------------------------------------------
# 单游戏探测
# ---------------------------------------------------------------------------


def _classify_mechanism(
    ud: dict[str, Any] | None,
    entry: Any,
    markers: list[str] | None = None,
) -> tuple[str, list[str]]:
    """判定机制. 返回 `(主机制, 全部机制)`. 日志证据优先于目录证据."""
    found: list[str] = []
    if entry is not None:
        if entry.watching_files or getattr(entry, "rules", None):
            found.append("auto-cloud")
        elif entry.mechanism == "auto-cloud":
            found.append("auto-cloud")
    if ud:
        if ud["has_ac_dir"]:
            found.append("auto-cloud")
        if ud["has_remote_dir"]:
            found.append("remote-storage-api")

    order = ["auto-cloud", "remote-storage-api"]
    found = sorted(set(found), key=lambda m: order.index(m) if m in order else 99)
    primary = found[0] if found else "unknown"

    # 日志里有 Watching 记录或 Auto-Cloud 规则评估时, Auto-Cloud 是**实锤**, 压过目录推断
    if entry is not None and (entry.watching_files or getattr(entry, "rules", None)):
        primary = "auto-cloud"

    # 兜底**补位** (只在完全判不出机制时生效): 安装目录里的 `steam_autocloud.vdf`
    # 是 Steam 自己落下的 Auto-Cloud 声明文件, 足够证明这游戏走 Auto-Cloud.
    # 刻意"不夺权" —— 已判出 remote-storage-api 的游戏不因 marker 改判, 避免把
    # 同时有 remote 目录与 marker 的 API 型游戏 (220/620 等) 误标成 auto-cloud.
    if not found and markers:
        found = ["auto-cloud"]
        primary = "auto-cloud"
    return primary, found


def _account_form(name: str, forms: dict[str, str]) -> str | None:
    return forms.get(name.lower())


def probe_game(
    appid: str,
    manifest: discovery.AppManifest | None,
    ud: dict[str, Any] | None,
    entry: Any,
    cfg: localconfig.AppCloudSetting | None,
    ident_strings: list[str],
    account_forms: dict[str, str],
    root_base_paths: dict[str, Path] | None = None,
    markers: list[str] | None = None,
    console_lines: list[str] | None = None,
) -> dict[str, Any]:
    """完整探测一款游戏 (只读).

    `root_base_paths` 是**整机标定**出来的 root 枚举 -> 全局基目录映射, 缺了它
    非 0/1 号 root 的 remotecache 条目就无法解析 (见 `calibrate_roots`).
    `markers` 是这台机器上属于本 appid 的 `steam_autocloud.vdf` 绝对路径.
    `console_lines` 是 `console_log.txt` 的全文行. 用途单一但关键:
    **点亮命令 `testappcloudpaths <appid>` 的执行回执只写在这里** (它是控制台变量
    赋值, 不会立刻产生 `cloud_log` 活动). 没有它, 报告只能说"云被禁用",
    无法回答"用户到底执行过点亮命令没有" —— 2026-10-13 实测就卡在这个盲区上.
    """
    rec: dict[str, Any] = {
        "appid": appid,
        "name": (manifest.name if manifest else "") or "",
        "installed": manifest is not None,
        "install_dir": str(manifest.install_dir) if manifest and manifest.install_dir else None,
        "mechanism": "unknown",
        "mechanisms": [],
        "cloud_state": "unknown",
        "cloud_last_sync_state": "",
        "watching_files": [],
        "watching_count_reported": None,
        "cloud_rules": [],
        "rule_patterns": [],
        "rule_file_names": [],
        "cloud_rel_paths": [],
        "persisted_files": [],
        "synced_change_number": None,
        "cloud_roundtrip_ok": None,
        "queued_not_uploaded": False,
        "remote_upload": None,
        "upload_initiated": False,
        "last_upload_change_number": None,
        "upload_complete_count": 0,
        "uploaded_files": [],
        "deleted_files": [],
        "upload_result_ok_count": 0,
        "upload_confirmed": False,
        "cloud_deleted": False,
        "change_numbers": {"local": None, "global": None},
        "ever_advanced": None,
        "never_synced_strong": False,
        "no_change_advance": False,
        "cloud_toggle_console": None,
        "incomplete_sync_count": 0,
        "userdata": None,
        "ac_root_map": [],
        "save_candidates": [],
        "save_candidates_raw_count": 0,
        "autocloud_marker_dirs": [],
        "save_dir_rejected": None,
        "save_dir_resolved": None,
        "save_dir_confident": False,
        "save_dir_source": None,
        "watched_file_names": [],
        "watched_save_dirs": [],
        "watched_file_state": None,
        "account_dir_form": None,
        "save_snapshot": None,
        "cloud_toggle": {"needed": False, "command": None, "reset_command": None},
        "cloud_toggle_effective": None,
        "cloud_toggle_ever_disabled": False,
        "local_frame": None,
        "verdict": {"bridgeable": False, "reasons": []},
    }

    # --- 机制 ---
    rec["mechanism"], rec["mechanisms"] = _classify_mechanism(ud, entry, markers)

    # --- 云状态 / 同步进度 ---
    if cfg is not None:
        rec["cloud_last_sync_state"] = cfg.last_sync_state
    if entry is not None:
        rec["cloud_state"] = entry.cloud_state
        rec["watching_files"] = list(entry.watching_files)
        rec["watching_count_reported"] = entry.watching_count
        rec["cloud_rules"] = [dict(r) for r in getattr(entry, "rules", [])]
        rec["rule_patterns"] = list(getattr(entry, "rule_patterns", []))
        rec["rule_file_names"] = list(getattr(entry, "rule_file_names", []))
        rec["cloud_rel_paths"] = list(getattr(entry, "cloud_rel_paths", []))
        rec["persisted_files"] = list(getattr(entry, "persisted_files", []))
        rec["synced_change_number"] = getattr(entry, "synced_change_number", None)
        rec["cloud_roundtrip_ok"] = getattr(entry, "cloud_roundtrip_ok", None)
        rec["queued_not_uploaded"] = bool(getattr(entry, "queued_not_uploaded", False))
        rec["cloud_toggle_effective"] = bool(getattr(entry, "toggle_effective", False))
        rec["cloud_toggle_ever_disabled"] = entry.disabled_count > 0
        rec["change_numbers"] = {"local": entry.local_cn, "global": entry.global_cn}
        rec["ever_advanced"] = entry.ever_advanced
        rec["never_synced_strong"] = entry.never_synced
        rec["no_change_advance"] = entry.no_change_advance
        rec["incomplete_sync_count"] = entry.incomplete_sync_count
    if rec["cloud_state"] == "unknown" and rec["cloud_last_sync_state"]:
        rec["cloud_state"] = "state-field-only"

    if ud is not None:
        rec["userdata"] = {
            "path": ud["path"],
            "has_remote_dir": ud["has_remote_dir"],
            "has_ac_dir": ud["has_ac_dir"],
            "has_remotecache": ud["has_remotecache"],
            "remote_files": ud["remote_files"],
            "remote_bytes": ud["remote_bytes"],
            "ac_top_dirs": ud["ac_top_dirs"],
            "cache_roots": sorted(
                {e["root"] for e in (ud["cache"] or {}).get("entries", [])}
            ),
        }

    # --- 上传往返的**真实判据**: remotecache.vdf 的 remotetime ---
    #
    # 实测结论: cloud_log 的 `Successfully synced to ChangeNumber` 是**通用同步
    # 完成语句**, 上传路径里它出现在 `Upload batch initiated` 之前 (实测
    # 3590/105600/219740/431960), Auto-Cloud 型 (DS3) 则从不打印它.
    # 所以"文件到底有没有落到 Valve 服务器"只能看 remotecache:
    #   remotetime > 0  = 云端有副本 (上传曾成功)
    #   remotetime == 0 = 仅本地登记, 从未上云
    # 而"有没有动手传"看 cloud_log 的 `Upload batch initiated` (跨机制通用).
    # remotecache 是**权威**, 一旦有条目就覆盖日志层面的判定.
    _cache = (ud or {}).get("cache") or {}
    _cache_entries = _cache.get("entries", [])
    if _cache_entries:
        _verified = bool(_cache.get("upload_verified"))
        _pending = list(_cache.get("pending_upload") or [])
        rec["remote_upload"] = {
            "verified": _verified,
            "max_remotetime": _cache.get("max_remotetime", 0),
            "pending_files": _pending,
            "file_count": _cache.get("file_count", len(_cache_entries)),
            "change_number": _cache.get("change_number", 0),
            "source": "remotecache.vdf",
        }
        rec["cloud_roundtrip_ok"] = _verified
        rec["queued_not_uploaded"] = (not _verified) and bool(_pending)
        # remotecache 是**权威且更晚**: 它落盘在上传**之后**, 而 cloud_log 的
        # `Need to sync ... 'X' to 'Y'` 行打印在上传**之前**. 所以"刚上传成功"
        # 的游戏会被日志层误报成 `曾推进过=False` —— 用 remotecache 覆盖它.
        if _cache.get("ever_advanced"):
            rec["ever_advanced"] = True
            rec["no_change_advance"] = False
            rec["never_synced_strong"] = False

    # 日志层面"上传有没有真的发起". `Upload batch initiated` 是 Steam 向服务器
    # 提交上传批次的行, 比 remotetime 更早可见, 且**跨机制通用**:
    # 云同步被禁用的 app 从不打印它 (即使文件已 Persisting 到本地缓存).
    # 2026-10-05 实测: 730(账号 987654321) 有, DS3(账号 1234567890) 一次都没有.
    if entry is not None:
        rec["upload_initiated"] = bool(getattr(entry, "upload_initiated", False))
        rec["last_upload_change_number"] = getattr(entry, "last_upload_change_number", None)
        rec["upload_complete_count"] = int(getattr(entry, "upload_complete_count", 0) or 0)
        rec["uploaded_files"] = list(getattr(entry, "uploaded_files", []) or [])
        rec["deleted_files"] = list(getattr(entry, "deleted_files", []) or [])
        rec["upload_result_ok_count"] = int(
            getattr(entry, "upload_result_ok_count", 0) or 0
        )
        rec["upload_confirmed"] = bool(getattr(entry, "upload_confirmed", False))
        rec["cloud_deleted"] = bool(getattr(entry, "cloud_deleted", False))

    # --- Auto-Cloud 根语义 + 存档目录定位 ---
    install_dir = manifest.install_dir if manifest and manifest.install_dir else None
    raw_candidates: list[str] = []
    ac_map: list[dict[str, Any]] = []
    for ac_name in (ud or {}).get("ac_top_dirs", []):
        base = discovery.resolve_ac_root_dir(ac_name, install_dir)
        ac_map.append(
            {
                "ac_dir": ac_name,
                "base": str(base) if base else None,
                "base_exists": bool(base and base.is_dir()),
                "resolved": base is not None,
            }
        )
        if base and base.is_dir():
            raw_candidates.extend(
                str(p) for p in discovery.find_account_dirs(base, ident_strings)
            )

    # API 型: 文件实体就在 userdata 的 remote 目录
    if ud and ud["has_remote_dir"]:
        raw_candidates.append(ud["path"] + os.sep + "remote")

    # remotecache.vdf 的 `root` 枚举 + 相对路径 -> 直接算出存档文件落点.
    # **这份 remotecache 属于这一个 appid**, 所以据此推出且实际存在的路径天然可信
    # (不需要再靠目录名猜). root=0/1 是 per-appid 语义, 用本 appid 的 remote /
    # 安装目录; 其余 root 用整机标定出来的全局基目录.
    derived: list[tuple[str, str]] = []
    per_appid_roots: dict[str, Path] = {}
    if ud and ud["has_remote_dir"]:
        rd = Path(ud["path"]) / "remote"
        if rd.is_dir():
            per_appid_roots["0"] = rd
    if install_dir and install_dir.is_dir():
        per_appid_roots["1"] = install_dir
    cache_entries = ((ud or {}).get("cache") or {}).get("entries", []) if ud else []
    for e in cache_entries:
        root = str(e.get("root"))
        name = str(e.get("name") or "")
        if not name:
            continue
        base = per_appid_roots.get(root) or (root_base_paths or {}).get(root)
        if base is None:
            continue
        rel = name.replace("\\", os.sep).replace("/", os.sep)
        if rel.startswith(os.sep) or ".." in rel.split(os.sep):
            continue
        cand = base / rel
        if cand.exists():
            derived.append((str(cand.parent if cand.is_file() else cand), root))

    # `steam_autocloud.vdf` 是 Steam 自己落在**存档目录**里的 Auto-Cloud 声明文件.
    # 它的所在目录就是该规则实际同步的目录 —— 对"既无 ac 目录、又无 remote 目录、
    # 日志也没有记录"的游戏是唯一可用线索. 实测 692850 Bloodstained 明明有
    # `<GameDir>\BloodstainedRotN\Saved\SaveGames\steam_autocloud.vdf`,
    # 旧代码却因为这条线索只用于选目标而判"未能定位存档目录".
    marker_dirs: list[tuple[str, str]] = []
    if install_dir:
        # 归属护栏: marker 必须落在本游戏自己的安装目录内. 归属函数对
        # `Half-Life 2\...` 这类"另一个游戏的 installdir 是它的字符串前缀"的情况
        # 曾误判 (见 `appid_from_path` 的说明), 这里再做一次路径级校验, 避免把别的
        # 游戏的存档目录算到本 appid 头上.
        root_norm = os.path.normcase(os.path.abspath(str(install_dir)))
        for mk in markers or []:
            mp = Path(mk)
            d = mp.parent if mp.suffix else mp
            dn = os.path.normcase(os.path.abspath(str(d)))
            if not (dn == root_norm or dn.startswith(root_norm + os.sep)):
                continue
            if d.is_dir():
                marker_dirs.append((str(d), str(mp)))
    else:
        # 游戏已卸载 (无安装目录) 时无从校验归属, 仍按最佳努力采信
        for mk in markers or []:
            mp = Path(mk)
            d = mp.parent if mp.suffix else mp
            if d.is_dir():
                marker_dirs.append((str(d), str(mp)))

    rec["ac_root_map"] = ac_map
    rec["save_candidates_raw_count"] = (
        len(set(raw_candidates)) + len(set(derived)) + len({d for d, _ in marker_dirs})
    )
    rec["autocloud_marker_dirs"] = sorted({d for d, _ in marker_dirs})

    # cloud_log 的 `Watching file <绝对路径>` 是**最强证据**: 直接告诉我们 Steam
    # 实际同步哪些文件. 它比 ac 根语义硬, 而且游戏已卸载 (无 appmanifest) 时依然有效.
    #
    # 但 2026-10-05 实测发现 `Watching file` 只在"本机已有云端缓存"时才打印:
    # DS3 这类云从未通过的游戏启动阶段只报 `Watching 0 files`, 真相要从
    # `Evaluating rule` + `Persisting file` 两条线取. 因此这里做三级降级.
    watched = [str(f) for f in (entry.watching_files if entry is not None else [])]
    if not watched and entry is not None:
        watched = [str(f) for f in getattr(entry, "persisted_files", [])]
    watched_dirs = sorted({str(Path(f).parent) for f in watched if f})
    if not watched_dirs and entry is not None:
        derived_dirs: set[str] = set()
        for rule in getattr(entry, "rules", []):
            for hit in rule.get("hits") or []:
                glob = str(hit.get("glob") or "")
                if hit.get("count") and glob:
                    derived_dirs.add(str(Path(glob).parent))
        watched_dirs = sorted(derived_dirs)

    rec["watched_file_names"] = sorted({Path(f).name for f in watched if f})
    if not rec["watched_file_names"] and entry is not None:
        rec["watched_file_names"] = list(getattr(entry, "rule_file_names", []))
    rec["watched_save_dirs"] = watched_dirs

    if raw_candidates or watched_dirs or derived or marker_dirs:
        game_names = [
            n
            for n in [(manifest.name if manifest else ""), (manifest.installdir if manifest else "")]
            if n
        ]
        scored: list[dict[str, Any]] = []

        for raw in sorted(set(raw_candidates)):
            p = Path(raw)
            level, reason = discovery.match_game_name(p, game_names)
            form = _account_form(p.name, account_forms)
            is_current = form is not None
            if p.name.lower() == "remote":
                reason = "Steam 云端映射目录 (remote)"
                level = "exact"
                is_current = True
            scored.append(
                {
                    "path": raw,
                    "source": "remote-dir" if p.name.lower() == "remote" else "ac-root",
                    "match_level": level,
                    "match_reason": reason,
                    "is_current_account": is_current,
                    "account_dir_form": form,
                }
            )

        # 注意: `is_current_account` 只是**候选生成器**, 不是置信来源 ——
        # %APPDATA% 下会有十几个游戏目录都叫同一个账号名 (DS3/DS2/EldenRing/...),
        # 拿它当置信度会把 DS3 的目录错认成 Stardew 的存档目录.
        for path_str, root in sorted(set(derived)):
            scored.append(
                {
                    "path": path_str,
                    "source": "remotecache",
                    "match_level": "exact",
                    "match_reason": f"该游戏自己的 remotecache (root={root}) 指出此路径且文件确实存在",
                    "is_current_account": _account_form(Path(path_str).name, account_forms) is not None,
                    "account_dir_form": _account_form(Path(path_str).name, account_forms),
                }
            )

        for wd in watched_dirs:
            p = Path(wd)
            if not p.is_dir():
                continue
            scored.append(
                {
                    "path": wd,
                    "source": "cloud_log",
                    "match_level": "exact",
                    "match_reason": "cloud_log 的 Watching file 指向此目录 (Steam 实际同步目标)",
                    "is_current_account": True,
                    "account_dir_form": _account_form(p.name, account_forms),
                }
            )

        for md, mk in sorted(set(marker_dirs)):
            scored.append(
                {
                    "path": md,
                    "source": "autocloud-marker",
                    "match_level": "exact",
                    "match_reason": f"该目录内有 Steam 自己落下的 Auto-Cloud 声明文件 {Path(mk).name}",
                    "is_current_account": _account_form(Path(md).name, account_forms) is not None,
                    "account_dir_form": _account_form(Path(md).name, account_forms),
                }
            )

        # 优先级: cloud_log 实锤 > 本游戏 remotecache 落点 > remote 目录 >
        #         autocloud marker 所在目录 > ac 目录名匹配.
        # marker 排在 remote-dir **之后**是刻意的: 它对"毫无其他线索"的游戏是唯一
        # 救命线索, 但不该压过 API 型游戏确凿的 remote 目录.
        rank = {
            "cloud_log": 0,
            "remotecache": 1,
            "remote-dir": 2,
            "autocloud-marker": 3,
            "ac-root": 4,
        }
        strong = [
            c
            for c in scored
            if c["source"] in {"cloud_log", "remotecache", "remote-dir", "autocloud-marker"}
        ]
        exact = [c for c in scored if c["match_level"] == "exact" and c["source"] == "ac-root"]
        partial = [c for c in scored if c["match_level"] == "partial"]
        pool = strong or exact or partial or scored

        # 同级候选的偏好顺序: ① 目录名像存档目录的 (save/saves/savedata/...);
        # ② 当前账号; ③ 路径稳定排序.
        # 刻意**不按深度排序**: 实测 API 型游戏 (550/730/105600/219740/563560) 的
        # 正确落点就是 `...\remote` 本身, 按深度会把它错误地推深到
        # `remote\cfg\workshop_saves` 之类的子目录. 旧实现只按路径升序 ⇒ 父目录压过
        # 子目录, 实测 130 Half-Life: Blue Shift 被判成 `...\bshift`(游戏资源目录,
        # 3430 个 .wav/.mdl/.dll) 而不是 `...\bshift\SAVE` —— 这一条由"像存档目录"
        # 偏好修正, 不需要深度参与.
        def _key(c: dict[str, Any]) -> tuple[Any, ...]:
            return (
                rank.get(c["source"], 9),
                0 if _looks_like_save_dir(c["path"]) else 1,
                not c["is_current_account"],
                c["path"],
            )

        pool.sort(key=_key)

        # 硬规则: 存档目录**不可能**是游戏安装目录本身. 候选里出现它, 说明所有证据
        # 都退回到安装根 (实测 113200 / 4659620) —— 宁可报"未定位", 也不能把这个
        # 答案交给 M2, 否则搬运会去复制整个游戏目录.
        install_root = str(install_dir) if install_dir else None
        if install_root:
            kept = [c for c in pool if not _same_path(c["path"], install_root)]
            if kept:
                pool = kept

        rec["save_candidates"] = pool

        # 无任何候选时 (无 remote 目录 / 无 ac 目录 / 无 remotecache / 无日志证据)
        # 保持 save_dir_* 为 None 而不是 IndexError —— 例如只探测到一个空壳 userdata.
        if pool:
            best = pool[0]
            # 只有这几类才算"确认": 日志实锤 / 本游戏 remotecache 命中 / remote 目录 /
            # autocloud marker 命中 / 目录名精确命中
            confident = bool(
                best["source"]
                in {"cloud_log", "remotecache", "remote-dir", "autocloud-marker"}
                or best["match_level"] == "exact"
            )
            if confident and install_root and _same_path(best["path"], install_root):
                confident = False
                rec["save_dir_rejected"] = (
                    "全部候选都指向游戏安装目录本身, 已拒绝 (存档不落在安装根; "
                    "按此目录搬运会复制整个游戏)"
                )
            rec["save_dir_confident"] = confident
            rec["account_dir_form"] = best["account_dir_form"]
            if confident:
                rec["save_dir_resolved"] = best["path"]
                rec["save_dir_source"] = best["source"]
                rec["save_snapshot"] = _list_dir_snapshot(Path(best["path"]))
                if watched:
                    # 只记录被监视文件的**存在性**, 不读内容
                    rec["watched_file_state"] = [
                        {
                            "name": Path(f).name,
                            "exists": Path(f).is_file(),
                            "size": (Path(f).stat().st_size if Path(f).is_file() else None),
                        }
                        for f in watched
                    ]
                elif rec["watched_file_names"] and Path(best["path"]).is_dir():
                    # 只有规则证据时, 用「已定位的存档目录 × 规则声明的文件名」组合出存在性
                    sd = Path(best["path"])
                    rec["watched_file_state"] = [
                        {
                            "name": nm,
                            "exists": (sd / nm).is_file(),
                            "size": ((sd / nm).stat().st_size if (sd / nm).is_file() else None),
                        }
                        for nm in rec["watched_file_names"]
                    ]

    # --- 云开关 ---
    # 点亮命令的执行回执**只在 console_log**: `testappcloudpaths` 只是给一个控制台
    # 变量赋值, 生效不会打印任何 `cloud_log` 行 (要等一次 sync / 进出游戏才见规则行).
    # 记录它, 才能把"未点亮"继续分成两种可操作的情形:
    #   ① 命令从未执行过        -> 让用户去执行
    #   ② 执行过、但之后仍无活动 -> 多半是执行后又重启了 Steam (该开关随重启失效)
    if console_lines is not None:
        rec["cloud_toggle_console"] = _console_ack(
            "testappcloudpaths", appid, console_lines
        ).to_dict()
        # 再加一列**当前值**: `testappcloudpaths` 本质是个控制台变量, 它的值就是
        # "这一会话里当前生效的 appid" (`0` = 未点亮). 本机实测: 执行命令后值变成
        # `374320`, 执行 `testappcloudpaths 0` 或重启 Steam 后回到 `0`.
        # 比"历史上出现过回执"更硬 —— 开关易失, 值会被重启改回去, 而旧回执永远留着.
        # 用户也能自己读它: 控制台执行 `find testappcloudpaths` 会列出这一行.
        _tv = _console_var_value("testappcloudpaths", console_lines)
        rec["cloud_toggle_console"]["value"] = _tv
        rec["cloud_toggle_console"]["value_matches_appid"] = (
            None if _tv is None else (_tv == str(appid))
        )

    if rec["cloud_state"] == "disabled":
        rec["cloud_toggle"] = {
            "needed": True,
            "command": f"testappcloudpaths {appid}",
            "reset_command": "testappcloudpaths 0",
            "note": "Valve 官方开发者测试命令; 零售场景属非预期用途, 且每次重启 Steam 需重做.",
        }
    elif rec["cloud_toggle_ever_disabled"]:
        # 日志里还留着旧的 `AutoCloud is disabled` 行, 但最后一条规则/落盘行在它之后
        # => 命令已经生效. 不能因为"历史上出现过禁用"就继续要求用户再点一次.
        rec["cloud_toggle"] = {
            "needed": False,
            "command": None,
            "reset_command": "testappcloudpaths 0",
            "volatile": True,
            "note": (
                "云通道曾是隐藏关闭状态, 已由开发者测试命令点亮; "
                "但**每次重启 Steam 后会失效** —— 工具每次启动都应重查 cloud_log, "
                "一旦再次出现 `AutoCloud is disabled` 就要重新点亮."
            ),
        }

    # --- 本机注入点 ---
    rec["local_frame"] = probe_local_frame(install_dir)

    # --- 结论 ---
    reasons: list[str] = []
    bridgeable = True
    if manifest is None:
        reasons.append("未安装")
        bridgeable = False
    if rec["mechanism"] == "remote-storage-api" and not rec["mechanisms"].count("auto-cloud"):
        reasons.append("该游戏走 ISteamRemoteStorage 自同步, 无需借道 Auto-Cloud")
    # 云开关这条**必须排在最前**: 它是根因. 如实测 (`374320` DS3) —— 云通道未点亮时
    # 本机连 `userdata\<acct>\<appid>` 目录都不存在, 存档目录的三个来源 (日志 Watching /
    # remotecache / ac 目录) 同时为空. 这时 `--all-accounts` 换账号也扫不出一个**根本
    # 不存在**的目录, 把用户往那条路上引是错的指路.
    if rec["cloud_state"] == "disabled":
        _tack = rec.get("cloud_toggle_console") or {}
        _tv = _tack.get("value")
        if _tv is not None and _tack.get("value_matches_appid"):
            # 变量值 = 本 appid ⇒ 开关**现在就是开的** (本会话生效). 这时绝不能再写
            # "执行后重启过 Steam" —— 那会把用户重新推进"重启"这个坑
            # (重启正是清掉该开关的动作), 让他白白再点一次.
            reasons.append(
                f"云开关当前**已开**: 控制台变量 `testappcloudpaths` 的值 = `{_tv}` (本会话生效), "
                f"但还没产生云活动 ⇒ 直接从库里的游戏条目启动一次 `{appid}`, "
                "进主菜单后正常退出; 中途不要重启 Steam"
            )
        elif _tv is not None:
            reasons.append(
                f"云开关被禁用: 控制台变量 `testappcloudpaths` 的值 = `{_tv}` (≠ `{appid}`) "
                f"⇒ 在**同一个** Steam 会话里先执行 `testappcloudpaths {appid}`, "
                "再从库里的游戏条目启动一次; 中途不要重启 Steam"
            )
        elif _tack.get("found"):
            _when = _tack.get("last_line") or ""
            reasons.append(
                f"云开关被禁用; `console_log` 里**有** `testappcloudpaths {appid}` 的执行回执"
                + (f" (`{_when}`)" if _when else "")
                + " 但之后仍无云活动 ⇒ 多为执行后又重启过 Steam (该开关随重启失效), "
                "或执行后还没进过游戏. 正确顺序: 在**同一个** Steam 会话里 —— "
                f"先 `testappcloudpaths {appid}`, 再从库里的游戏条目启动一次; 中途不要重启 Steam"
            )
        elif _tack:
            reasons.append(
                f"云开关被禁用, 且 `console_log` 里**没有** `testappcloudpaths {appid}` 的执行回执 "
                "⇒ 点亮命令从未在本机执行过. 执行后**不要重启 Steam**, 直接进游戏一次"
            )
        else:
            reasons.append(
                f"云开关被禁用, 需先执行 `testappcloudpaths {appid}` (本机无 console_log 可核对执行记录)"
            )
    if rec["save_dir_resolved"] is None:
        if rec["cloud_state"] == "disabled":
            reasons.append(
                "未能定位存档目录 (因云通道未点亮 ⇒ 本机无任何云证据; "
                f"先 `testappcloudpaths {appid}` 再启动一次游戏, 重跑本工具即可定位)"
            )
        else:
            reasons.append("未能定位存档目录 (需 --all-accounts 或手动指定)")
        bridgeable = False
    elif not rec["save_dir_confident"]:
        reasons.append("存档目录为低置信度推断, 需人工确认")
        bridgeable = False
    if rec["mechanism"] == "unknown":
        reasons.append("机制未知 (无 ac 目录也无 remote 目录, 且日志无记录)")
    if rec["incomplete_sync_count"]:
        reasons.append(f"日志里出现过 {rec['incomplete_sync_count']} 次同步失败")
    frame = rec["local_frame"] or {}
    if frame.get("loader_dirs"):
        reasons.append(f"检测到 DLL 加载目录 {frame['loader_dirs'][0]}, 可用 drop-in DLL 形态")
    elif frame.get("config_files"):
        # 不写死 dllMods —— 那是 Mod Engine 在 DS3 上的目录名, 别把实现细节
        # 当通用名词塞进面客报告 (Sekiro 之类用的是 dinput8 代理加载).
        reasons.append("检测到 mod 加载器配置文件, 推定具备 DLL 注入能力 (未找到自动加载目录)")
    elif frame.get("mod_dirs"):
        reasons.append(f"仅有普通 mod 资源目录 {frame['mod_dirs'][0]}, 不构成 DLL 注入点")
    rec["verdict"] = {"bridgeable": bridgeable, "reasons": reasons}
    return rec


# Steam 自身的 appid. 它的云 (控制器配置/截图) 与「游戏云同步」开关不是同一条链路,
# 账号级判定必须排除, 否则会漏判账号级停摆.
_NON_GAME_APPIDS = frozenset({"7"})

# calibrate_roots 里代表 per-appid 基目录的标签: 它们的 base_path 只是占位说明,
# 不能当成可直接使用的绝对路径回灌给逐游戏探测.
_PER_APPID_ROOT_LABELS = frozenset({"<SteamRemote>", "<GameInstallDir>"})

# **候选池专用**排除名单: Steam 自身的 appid. 它们出现在候选池里只会把报告带偏
# (示例: 241100 = Steam 控制器配置云, 既非游戏也没有存档目录) —— 但它们仍然保留在
# `cloud_log` 段与账号级判据里, 所以诊断信息不丢.
# 刻意与 `_NON_GAME_APPIDS` 分开: 后者参与账号级"游戏云是否活着"的判定, 语义不同,
# 改动它会误伤既有结论.
_POOL_EXCLUDE_APPIDS = frozenset({"7", "241100"})

# 目录名里出现这些词时更可能是"存档目录"而不是游戏资源目录. 仅用于**同级别候选**的
# 排序偏好 (cloud_log / remotecache 这类逐游戏硬证据之间不会互相压过).
_SAVE_DIR_HINTS = frozenset(
    {
        "save", "saves", "saved", "savedata", "savegame", "savegames",
        "savedgames", "saved games", "save data", "save_data",
    }
)


def _looks_like_save_dir(path: str) -> bool:
    return Path(path).name.strip().lower() in _SAVE_DIR_HINTS


def _same_path(a: str | Path, b: str | Path) -> bool:
    """路径比较: 归一化大小写与相对成分 (Windows 盘符大小写不敏感)."""
    try:
        return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(
            os.path.abspath(str(b))
        )
    except OSError:
        return False


def calibrated_root_bases(cal: Mapping[str, Any]) -> dict[str, Path]:
    """把整机 root 标定结果里**全局**基目录转成 `{root: Path}`, 回灌给逐游戏探测.

    早先 `probe_game` 有这个形参却从未在调用点传值, 于是 root ∈ {2,3,4,9,12,...}
    的 remotecache 条目因为拿不到基目录而被**静默丢弃** —— 标定算出来了也没用
    (实测 `692850` Bloodstained: `cache_roots=["3"]`, 而 root 3 已标定为
    `%LOCALAPPDATA%` 79/79, 却仍被判"未能定位存档目录").

    排除两类:
      * `root=0/1` —— per-appid 语义 (标签 `<SteamRemote>`/`<GameInstallDir>`),
        必须由 `probe_game` 按本 appid 的 remote / 安装目录自己解析;
      * 低置信标定 —— 猜出来的基目录不能当事实用.
    """
    out: dict[str, Path] = {}
    for root, rc in cal.items():
        if getattr(rc, "base_label", None) in _PER_APPID_ROOT_LABELS:
            continue
        if getattr(rc, "confidence", None) in (None, "low", "unknown"):
            continue
        base_path = getattr(rc, "base_path", None)
        if not base_path:
            continue
        p = Path(str(base_path))
        if p.is_dir():
            out[str(root)] = p
    return out


def select_targets(
    manifests: Mapping[str, Any],
    ud_all: Mapping[str, Any],
    markers: Mapping[str, list[str]],
    entries: Mapping[str, Any],
    limit: int = 0,
) -> list[str]:
    """自动模式下挑选要逐游戏详查的 appid (按 `_interest` 排序后截断).

    候选来源三条, 缺一不可:

    1. **目录证据** —— 有 `ac` 目录或 `remote` 目录.
    2. **声明文件** —— 安装目录里落了 `steam_autocloud.vdf` (marker 必然在安装目录内,
       所以这一类不需要 appmanifest).
    3. **日志证据** —— 有 Watching 行 / 被禁用过 / 走过 launch 流程, 或
       **云端变更号已推进** (`synced_change_number > 0`).

    第 3 条里"变更号已推进"是后补的: 实测 `1245620` ELDEN RING 有
    `synced_change_number=23`, 但既无 ac/remote 目录、日志里也没有 Watching 行,
    旧判据会把它整只漏掉。这一类**允许已卸载**游戏 —— 与解析器注释
    ("`Watching file` 在游戏卸载后依然有效")保持一致, 选择器不该整体剔除它们。
    """
    dir_cand: set[str] = set()
    for appid in manifests:
        info = ud_all.get(appid)
        if info and (info["has_ac_dir"] or info["has_remote_dir"]):
            dir_cand.add(appid)
    dir_cand |= {a for a in markers if a.isdigit() and a not in _POOL_EXCLUDE_APPIDS}

    log_cand: set[str] = set()
    for appid, e in entries.items():
        if appid in _POOL_EXCLUDE_APPIDS:
            continue
        if (
            e.watching_files
            or e.disabled_count
            or e.on_launch_count
            or (getattr(e, "synced_change_number", None) or 0) > 0
        ):
            log_cand.add(appid)

    cand = dir_cand | log_cand
    ordered = sorted(cand, key=lambda a: _interest(a, ud_all.get(a), entries.get(a)))
    if limit and limit > 0:
        ordered = ordered[:limit]
    return ordered


def machine_upload_initiated_appids(all_entries: Mapping[str, Any]) -> list[str]:
    """整机范围内**发起过上传批次**的游戏 appid (排除 Steam 自身).

    账号级 Steam 云同步是账号属性, 只与"整机有没有游戏传上去过"有关,
    与"本次探测了哪几个游戏"无关, 所以判据一律基于整机 cloud_log 的全部 appid.
    """
    return sorted(
        appid
        for appid, e in all_entries.items()
        if appid not in _NON_GAME_APPIDS
        and (
            bool(getattr(e, "upload_initiated", False))
            or bool(getattr(e, "upload_batches", None))
        )
    )


def compute_cloud_blockers(
    games: list[dict[str, Any]],
    all_entries: Mapping[str, Any] | None = None,
) -> bool:
    """给每个游戏打上「当前被哪一层开关挡住」的诊断, 返回账号级上传是否存活.

    Steam 的游戏云上传受**两层开关**控制, 而两层之间存在时序陷阱:

      第 1 层 (账号级, 持久): 设置 → 云服务 → 「为此账号启用 Steam 云同步」
      第 2 层 (游戏级, 易失): AutoCloud 游戏的 `testappcloudpaths <appid>`,
                              **每次重启 Steam 都会失效**, 必须重做

    陷阱: 修复第 1 层通常要重启 Steam, 而重启会把第 2 层清掉 —— 所以若先点第 2 层、
    再为了改第 1 层重启 Steam, 第 2 层就白点了. 正确顺序是先用「本机是否有任何游戏
    发起过 `Upload batch initiated`」反向确认第 1 层已生效, **再**点第 2 层.

    账号级判据必须是**整机级**的 (Steam 云同步开关是账号属性, 与"本次探测了哪几个
    游戏"无关). 因此传入 `all_entries` (整机 cloud_log 全部 appid 的解析结果) 时,
    就只看整机；否则退回只看 `games` 子集 —— 单游戏探测 (如 `--appid 374320`)
    若不传 `all_entries`, 会把一个健康的账号误判成"账号级停摆".

    `_NON_GAME_APPIDS` 内的 appid (Steam 自身) 不参与账号级判定: 它的云 (控制器
    配置/截图) 与「游戏云同步」开关不是同一条链路, 2026-10-05 实测账号级停摆期间
    AppID 7 仍在上传, 计入会把停摆漏判.

    判定优先级: 游戏级关闭 > 账号级关闭 > 已入队未上云.
    """
    if all_entries is None:
        alive = any(
            g.get("upload_initiated")
            or (g.get("remote_upload") or {}).get("upload_initiated")
            for g in games
        )
    else:
        alive = bool(machine_upload_initiated_appids(all_entries))
    for g in games:
        blocker: dict[str, Any] | None = None
        if (
            "auto-cloud" in (g.get("mechanisms") or []) or g.get("mechanism") == "auto-cloud"
        ) and g.get("cloud_state") == "disabled":
            blocker = {
                "layer": "game",
                "code": "game-toggle-off",
                "detail": "该游戏的 AutoCloud 隐藏开关未点亮; 每次重启 Steam 都会失效.",
                "fix": (g.get("cloud_toggle") or {}).get("command"),
            }
        elif g.get("queued_not_uploaded") and not alive:
            blocker = {
                "layer": "account",
                "code": "account-cloud-off",
                "detail": "整机没有任何游戏发起过上传批次 ⇒ 账号级 Steam 云同步未启用.",
                "fix": "设置 → 云服务 → 勾选「为此账号启用 Steam 云同步」, 然后重启 Steam.",
            }
        elif g.get("queued_not_uploaded"):
            blocker = {
                "layer": "game",
                "code": "pending-full-sync",
                "detail": "文件已入队但云端仍无副本; 保持开关点亮并让文件 mtime 真正变化后进出游戏.",
                "fix": None,
            }
        g["cloud_blocker"] = blocker
    return alive


# ---------------------------------------------------------------------------
# 主采集
# ---------------------------------------------------------------------------


def _dedupe_libraries(installs: list[discovery.SteamInstall]) -> list[discovery.Library]:
    out: list[discovery.Library] = []
    seen: set[str] = set()
    for inst in installs:
        for lib in discovery.load_libraries(inst):
            key = os.path.normcase(str(lib.path))
            if key in seen:
                continue
            seen.add(key)
            out.append(lib)
    return out


def _interest(appid: str, ud: dict[str, Any] | None, entry: Any) -> tuple[int, int, str]:
    """目标排序权重: 问题越明确越靠前 (云禁用 > 有 Watching > 有 ac 目录)."""
    score = 0
    if entry is not None:
        if entry.cloud_state == "disabled":
            score += 8
        if entry.watching_files:
            score += 4
        # 日志里有"成功同步记录"= 这游戏的云确实活着, 比"有个 remote 目录但没传过"
        # 有价值得多 (ELDEN RING 这类只有 ChangeNumber 证据的游戏靠它才排得上来).
        if (
            getattr(entry, "synced_change_number", None) is not None
            or getattr(entry, "cloud_roundtrip_ok", False)
        ):
            score += 2
    if ud and ud["has_ac_dir"]:
        score += 2
    if ud and ud["has_remote_dir"]:
        score += 1
    return (-score, 0, appid)


def _entry_label() -> str:
    """记录"本次是怎么被拉起的" —— 只看报告就能判断对方跑的是 exe 还是 .py,
    也顺带证明 exe 入口在**陌生机器**上被真的用过 (此前凭空猜不出来).

    * `exe-onefile`   —— onefile: 依赖被解包到临时目录 `%TEMP%\\_MEIxxxx\\`
    * `exe-onedir`    —— onedir: `_MEIPASS` 指向 exe 同级目录 (非临时)
    * `python-script` —— 直接由解释器运行 .py

    注意 `sys._MEIPASS` 在 onefile / onedir **都会被设置**, 所以不能只看
    `hasattr` —— 必须看解包目录的形态, 否则 onedir 会被误报成 onefile.
    """
    if not getattr(sys, "frozen", False):
        return "python-script"
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        base = Path(str(meipass)).name
        if base.startswith("_MEI") or base.lower() == "temp":
            return "exe-onefile"
    return "exe-onedir"


# entry -> (人读描述, 解释器是否 exe 自带). 报告头据此措辞, 否则会把 exe 内置的
# 解释器写成本机装了 Python —— 朋友端会误以为"我得先去装个 Python".
_ENTRY_DISPLAY: dict[str, tuple[str, bool]] = {
    "exe-onefile": ("单文件 exe（自带解释器, 本机无需安装 Python）", True),
    "exe-onedir": ("exe 目录版（自带解释器, 本机无需安装 Python）", True),
    "python-script": ("Python 脚本（由本机解释器运行）", False),
}


def _entry_display(entry: Any) -> tuple[str, bool]:
    """把 `tool.entry` 翻成 (人读描述, 是否自带解释器).

    未知/缺失的 entry 一律按最保守的"Python 脚本"处理: 宁可少说, 也不要把
    不确定的运行方式写成"自带解释器"去误导对方.
    """
    return _ENTRY_DISPLAY.get(str(entry or ""), _ENTRY_DISPLAY["python-script"])


def collect(args: argparse.Namespace) -> dict[str, Any]:
    prof: dict[str, Any] = {
        "schema": SCHEMA,
        "tool": {
            "name": TOOL,
            "version": VERSION,
            "redacted": False,
            # 记录"本次是怎么被拉起的" —— 只看报告就能判断朋友跑的是 exe 还是 .py,
            # 也顺带证明 exe 入口在**陌生机器**上被真的用过 (此前凭空猜不出来).
            "entry": _entry_label(),
        },
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "os": {
            "platform": sys.platform,
            "system": platform.system(),
            "release": platform.release(),
            "python": platform.python_version(),
            "machine": platform.machine(),
        },
        "errors": [],
        "warnings": [],
        "capabilities": {"steam_found": False},
    }

    # --- 1. Steam 安装 ---
    if args.steam_root:
        installs = [
            discovery.SteamInstall(root=Path(r), source="--steam-root")
            for r in args.steam_root
        ]
        missing = [str(i.root) for i in installs if not i.exists()]
        for m in missing:
            prof["errors"].append(f"--steam-root 指向的路径不存在: {m}")
        installs = [i for i in installs if i.exists()]
        if not installs:
            return prof
    else:
        installs = [i for i in discovery.find_installations() if i.exists()]

    if not installs:
        prof["errors"].append("未发现任何 Steam 安装 (注册表与常见路径均未命中)")
        return prof

    primary = installs[0]
    prof["steam"] = {
        "primary_root": str(primary.root),
        "primary_source": primary.source,
        "installs": [
            {"root": str(i.root), "source": i.source, "is_primary": i is primary}
            for i in installs
        ],
    }

    libraries = _dedupe_libraries(installs)
    manifests = discovery.load_app_manifests(libraries)
    prof["steam"]["libraries"] = [str(lib.path) for lib in libraries]
    prof["steam"]["app_manifest_count"] = len(manifests)
    # 已安装 appid **全量清单** (来自各库盘的 appmanifest_*.acf).
    # 必须单独记录: 逐游戏详查只覆盖 `select_targets` 选中的候选 (需云证据),
    # 而"这台机器装没装某个游戏"是另一个问题. 没有这份清单, 未进入详查的
    # 游戏在报告里**零痕迹**, 会被读成"机器上没装" —— 实测踩过这个坑
    # (朋友机 DS3 374320 确已安装, 但无任何云证据 ⇒ 报告里查不到).
    # appid 是公开商店 ID, 非敏感信息, 脱敏后仍保留.
    prof["steam"]["app_manifest_appids"] = sorted(manifests.keys())

    # --- 2. 账号 ---
    login_users = accounts.load_login_users(primary.root)
    active_id = accounts.get_active_accountid()
    if active_id is None:
        act = accounts.active_account(primary.root)
        active_id = act.account_id if act else None

    userdata_dir = primary.userdata
    all_account_dirs = (
        sorted(d.name for d in userdata_dir.iterdir() if d.is_dir() and d.name.isdigit())
        if userdata_dir.is_dir()
        else []
    )

    if args.account:
        scanned = [a for a in args.account]
    elif args.all_accounts:
        scanned = all_account_dirs
    else:
        scanned = [str(active_id)] if active_id is not None else all_account_dirs[:1]

    # 身份标识 (用于按账号名反查存档目录) 的选择:
    #   * 显式 `--account` 时**必须以它为准** —— 旧实现只改了"扫哪个 userdata
    #     目录", 却仍拿注册表里的当前账号去匹配目录名, 行为自相矛盾: 离线复现/
    #     沙箱测试时身份标识会被钉在真机账号上, 指定账号的存档永远匹配不上 (D5).
    #   * 否则用注册表的当前账号; 再否则用扫到的第一个.
    # "全灌进去会爆炸"的老约束仍成立: 只取显式指定或扫描到的少量账号.
    if args.account:
        ident_source: list[int | None] = [int(a) for a in scanned if a.isdigit()]
    elif active_id is not None:
        ident_source = [active_id]
    else:
        ident_source = [int(a) for a in scanned[:1] if a.isdigit()]

    ident_strings: list[str] = []
    for aid in ident_source:
        if aid is None:
            continue
        ident_strings.extend(discovery.account_ident_strings(int(aid)))
    ident_strings = sorted(set(ident_strings))

    account_forms: dict[str, str] = {}
    for aid in ident_source:
        if aid is None:
            continue
        account_forms[accounts.accountid_to_hex_dir(aid).lower()] = "steamid64-hex"
        account_forms[str(accounts.accountid_to_steamid64(aid))] = "steamid64-decimal"
        account_forms[str(aid)] = "accountid-decimal"

    prof["steam"]["accounts_known"] = [a.to_dict() for a in login_users]
    prof["steam"]["active_accountid"] = active_id
    prof["steam"]["scanned_accountids"] = scanned
    prof["steam"]["account_ident_strings"] = ident_strings
    prof["steam"]["userdata_account_dir_count"] = len(all_account_dirs)

    # --- 3. userdata 扫描 ---
    ud_by_account: dict[str, dict[str, dict[str, Any]]] = {}
    ud_all: dict[str, dict[str, Any]] = {}
    for aid in scanned:
        apps = scan_userdata_apps(userdata_dir, aid)
        ud_by_account[aid] = apps
        for appid, info in apps.items():
            ud_all.setdefault(appid, info)
    prof["steam"]["userdata_apps_by_account"] = {
        aid: sorted(apps) for aid, apps in ud_by_account.items()
    }

    # --- 4. 云日志 ---
    log_paths = [p for p in [primary.logs / "cloud_log.txt", primary.logs / "cloud_log.previous.txt"] if p.is_file()]
    log = cloud_log.parse_cloud_logs(log_paths) if log_paths else cloud_log.CloudLog()

    # 控制台日志: `testappcloudpaths <appid>` 的**执行回执只写这里**.
    # 与 `cloud_log` 的区别很重要 —— 点亮命令不产生任何 cloud_log 行,
    # 只看 cloud_log 就无法区分"用户没执行"和"执行了但被重启清掉".
    console_paths = [
        p
        for p in [primary.logs / "console_log.txt", primary.logs / "console_log.previous.txt"]
        if p.is_file()
    ]
    console_lines: list[str] = []
    for cp in console_paths:
        try:
            console_lines.extend(cp.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError as exc:
            prof["warnings"].append(f"读取 {cp.name} 失败: {exc}")
    console_sessions = sum(1 for ln in console_lines if _CONSOLE_SESSION_MARKER in ln)

    prof["cloud_log"] = {
        "files_read": [str(p) for p in log_paths],
        "entries": {a: e.to_dict() for a, e in sorted(log.entries.items())},
        "disabled_appids": log.disabled_appids,
        "never_synced_appids": log.never_synced_appids,
        "incomplete_sync_appids": log.incomplete_appids,
        "console_files_read": [str(p) for p in console_paths],
        "console_session_count": console_sessions,
    }
    if not log_paths:
        prof["warnings"].append(
            "未找到 cloud_log.txt —— 该文件只在 Steam 运行过之后生成, "
            "缺失时无法获取\"被监视文件清单\"."
        )
    if not console_paths:
        prof["warnings"].append(
            "未找到 console_log.txt —— 该文件只在**打开过 Steam 控制台**之后生成, "
            "缺失时无法核对 `testappcloudpaths` 点亮命令是否执行过."
        )

    # --- 5. localconfig 云设置 ---
    cfg: dict[str, localconfig.AppCloudSetting] = {}
    for aid in scanned:
        p = userdata_dir / aid / "config" / "localconfig.vdf"
        if p.is_file():
            try:
                cfg = localconfig.load_local_config(p)
                prof["cloud_config_source"] = str(p)
                break
            except Exception as exc:  # noqa: BLE001
                prof["warnings"].append(f"localconfig.vdf 解析失败: {exc}")

    # --- 6. Auto-Cloud 声明文件 (辅助证据) ---
    markers: dict[str, list[str]] = {}
    if not args.no_markers:
        for lib in libraries:
            try:
                for path_str in discovery.find_autocloud_markers(lib, max_depth=args.marker_depth):
                    appid = appid_from_path(Path(path_str), manifests)
                    markers.setdefault(appid, []).append(path_str)
            except OSError as exc:
                prof["warnings"].append(f"扫描 {lib.path} 的 autocloud 标记失败: {exc}")
    prof["autocloud_markers"] = {k: sorted(v) for k, v in sorted(markers.items())}

    # --- 7. root 枚举标定 (整机级, 用于交叉验证) ---
    # 注意: calibrate_roots 要的是 (root, relpath, extra_bases) 记录, 不是 cache 对象.
    # root=0 / root=1 只有靠 per-appid 专属基目录 (<SteamRemote> / <GameInstallDir>) 才能命中.
    calib_records: list[tuple[str, str, dict[str, Path]]] = []
    for info in ud_all.values():
        cache = info.get("cache")
        if not cache:
            continue
        extra: dict[str, Path] = {}
        remote_dir = Path(info["path"]) / "remote"
        if remote_dir.is_dir():
            extra["<SteamRemote>"] = remote_dir
        m = manifests.get(info["appid"])
        if m and m.install_dir and m.install_dir.is_dir():
            extra["<GameInstallDir>"] = m.install_dir
        for e in cache.get("entries", []):
            calib_records.append((str(e["root"]), str(e["name"]), extra))
    cal: dict[str, remotecache.RootCalibration] = {}
    if calib_records:
        cal = remotecache.calibrate_roots(calib_records, bases=remotecache.candidate_bases())
        prof["root_calibration"] = {k: v.to_dict() for k, v in sorted(cal.items())}

    # 标定出的**全局基目录**必须回灌给逐游戏探测 —— 否则 root ∈ {2,3,4,9,12,...}
    # 的 remotecache 条目会在 probe_game 里因为拿不到基目录而被静默丢弃.
    root_base_paths = calibrated_root_bases(cal)

    # --- 8. 目标选择 ---
    if args.appid:
        targets = [str(a) for a in args.appid]
    else:
        targets = select_targets(
            manifests, ud_all, markers, log.entries, limit=args.limit
        )

    if args.list:
        prof["targets"] = targets
        return prof

    # --- 9. 逐游戏探测 ---
    games = []
    for appid in targets:
        try:
            games.append(
                probe_game(
                    appid,
                    manifests.get(appid),
                    ud_all.get(appid),
                    log.entries.get(appid),
                    cfg.get(appid),
                    ident_strings,
                    account_forms,
                    root_base_paths=root_base_paths,
                    markers=markers.get(appid, []),
                    console_lines=console_lines if console_paths else None,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 单游戏失败不影响整体
            prof["errors"].append(f"appid {appid} 探测失败: {type(exc).__name__}: {exc}")
    prof["games"] = games

    # --- 9b. 两层云开关诊断 ---
    account_upload_alive = compute_cloud_blockers(games, log.entries)
    machine_uploaders = machine_upload_initiated_appids(log.entries)

    # --- 10. 整机能力判定 ---
    auto_cloud = [g for g in games if "auto-cloud" in g["mechanisms"]]
    api = [g for g in games if "remote-storage-api" in g["mechanisms"]]
    probed_ids = {g["appid"] for g in games}
    not_probed = sorted(a for a in manifests if a not in probed_ids)
    prof["capabilities"] = {
        "steam_found": True,
        "install_count": len(installs),
        "library_count": len(libraries),
        "app_manifest_count": len(manifests),
        "active_account_known": active_id is not None,
        "cloud_log_available": bool(log_paths),
        "console_log_available": bool(console_paths),
        "console_session_count": console_sessions,
        "autocloud_marker_count": len(markers),
        "games_probed": len(games),
        # 覆盖率: 已安装但**未纳入逐游戏详查**的 appid.
        # 它们不是"不存在", 而是"无云证据 / 未命中候选池" ⇒ 明细里没有它们.
        # 显式列出, 杜绝"没看到"被读成"没有".
        "installed_not_probed_appids": not_probed,
        "installed_not_probed_count": len(not_probed),
        "games_auto_cloud": len(auto_cloud),
        "games_remote_api": len(api),
        "games_with_watching_files": len([g for g in games if g["watching_files"]]),
        "games_with_cloud_rules": len([g for g in games if g["cloud_rules"]]),
        "games_with_sync_evidence": len(
            [g for g in games if g["watching_files"] or g["cloud_rules"]]
        ),
        "games_cloud_roundtrip_ok": [g["appid"] for g in games if g["cloud_roundtrip_ok"]],
        "games_queued_not_uploaded": [g["appid"] for g in games if g["queued_not_uploaded"]],
        "games_with_upload_initiated": [
            g["appid"] for g in games if g.get("upload_initiated")
        ],
        "any_app_upload_initiated": bool(machine_uploaders),
        "machine_appids_with_upload": machine_uploaders,
        "steam_cloud_account_upload_alive": account_upload_alive,
        # 点亮命令的执行证据 (来自 console_log, 不是 cloud_log).
        # 用来区分"命令从未执行"与"执行过但被 Steam 重启清掉" —— 两者的下一步动作不同.
        "games_toggle_cmd_executed": [
            g["appid"] for g in games if (g.get("cloud_toggle_console") or {}).get("found")
        ],
        "games_toggle_cmd_missing": [
            g["appid"]
            for g in games
            if g.get("cloud_toggle_console") is not None
            and not g["cloud_toggle_console"].get("found")
        ],
        # 变量**当前值** (来自控制台 `find testappcloudpaths` 的输出): `0` = 未点亮.
        # 有它就能直接回答"现在到底点亮了没有", 不必靠"历史上有没有出现过回执"推断.
        "game_toggle_var_values": {
            g["appid"]: g["cloud_toggle_console"]["value"]
            for g in games
            if (g.get("cloud_toggle_console") or {}).get("value") is not None
        },
        "cloud_layer_order": (
            "账号级(持久) → 游戏级(易失). 先确认账号级已生效 (看是否有任何游戏发起过上传批次), "
            "再点 AutoCloud 游戏级开关; 反之重启 Steam 会清掉游戏级."
        ),
        "games_with_cloud_blocker": {
            g["appid"]: g["cloud_blocker"]["code"]
            for g in games
            if g.get("cloud_blocker")
        },
        "games_with_remote_replica": [
            g["appid"] for g in games if (g["remote_upload"] or {}).get("verified")
        ],
        "games_pending_upload_never_synced": [
            g["appid"]
            for g in games
            if g["remote_upload"] and not g["remote_upload"]["verified"]
        ],
        "games_never_uploaded_remotetime_zero": [
            g["appid"]
            for g in games
            if (g["remote_upload"] or {}).get("max_remotetime") == 0
            and (g["remote_upload"] or {}).get("file_count")
        ],
        "games_cloud_disabled": [g["appid"] for g in games if g["cloud_state"] == "disabled"],
        "games_never_synced_strong": [g["appid"] for g in games if g["never_synced_strong"]],
        "games_save_dir_resolved": len([g for g in games if g["save_dir_resolved"]]),
        "games_with_mod_loader": [
            g["appid"]
            for g in games
            if (g["local_frame"] or {}).get("injection_available")
        ],
    }
    prof["root_calibration_bases"] = (
        {k: str(v) for k, v in remotecache.candidate_bases().items()}
        if calib_records
        else {}
    )
    return prof


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------


def _make_redactions(prof: dict[str, Any]) -> list[tuple[str, str]]:
    """构建 (命中字符串, 替换标记) 表. 长的先替换, 避免前缀吃掉后缀."""
    pairs: list[tuple[str, str]] = []

    home = str(Path(os.path.expanduser("~")))
    for variant in {home, home.replace("\\", "/")}:
        if variant:
            pairs.append((variant, "%USERPROFILE%"))

    steam = prof.get("steam") or {}
    if steam.get("primary_root"):
        pairs.append((str(steam["primary_root"]), "%STEAM_ROOT%"))
    for inst in steam.get("installs", []):
        pairs.append((str(inst["root"]), "%STEAM_ROOT%"))
    for lib in steam.get("libraries", []):
        pairs.append((str(lib), "%STEAM_LIBRARY%"))

    for g in prof.get("games", []):
        if g.get("install_dir"):
            pairs.append((str(g["install_dir"]), f"%GAME_DIR_{g['appid']}%"))

    # 账号标识 -> 稳定短哈希.
    # 关键: **同一个账号的三种写法必须归一到同一个哈希**, 并保留"用了哪种写法"
    # 的信息 —— 因为存档目录命名形态 (SteamID64 十六进制 / 十进制 / accountid)
    # 是每个游戏不同的, 适配器需要知道; 而身份本身不该泄露.
    steam_ids: set[int] = set()
    if steam.get("active_accountid") is not None:
        steam_ids.add(int(steam["active_accountid"]))
    for aid in steam.get("scanned_accountids", []):
        if str(aid).isdigit():
            steam_ids.add(int(aid))
    for acc in steam.get("accounts_known", []):
        v = acc.get("account_id")
        if v:
            steam_ids.add(int(v))

    claimed: set[str] = set()
    for aid in sorted(steam_ids):
        digest = hashlib.sha256(str(aid).encode("utf-8")).hexdigest()[:8]
        forms = {
            accounts.accountid_to_hex_dir(aid): f"<acct-{digest}:sidhex>",
            str(accounts.accountid_to_steamid64(aid)): f"<acct-{digest}:siddec>",
            str(aid): f"<acct-{digest}:accountid>",
            # 账号的 8 位 hex 短形式 (accountid 本身).
            # Steam 会把它编进文件名, 例如手柄配置
            # `preferences_45e-2ff-499602d2.vdf` —— 只替换 16 位整串会漏掉它,
            # 泄漏出来后又能反推回 accountid, 等于脱敏白做.
            f"{aid:08x}": f"<acct-{digest}:accountid-hex>",
            f"{aid:08X}": f"<acct-{digest}:accountid-hex>",
        }
        for tok, rep in sorted(forms.items(), key=lambda kv: -len(kv[0])):
            if len(tok) < 5:
                continue
            pairs.append((tok, rep))
            claimed.add(tok)

    # 未能归属到已知账号的标识 (例如 --all-accounts 下 userdata 里的历史账号)
    orphans: set[str] = set()
    orphans |= {str(x) for x in steam.get("account_ident_strings", [])}
    orphans |= set(steam.get("userdata_apps_by_account", {}) or {})
    for tok in sorted(orphans, key=len, reverse=True):
        if len(tok) < 5 or tok in claimed:
            continue
        digest = hashlib.sha256(tok.encode("utf-8")).hexdigest()[:8]
        pairs.append((tok, f"<acct:{digest}>"))
        claimed.add(tok)
        # sidhex 形态 (0110xxxxxxxxxxxx) 还要连带它的 8 位 accountid-hex 短形式,
        # 否则 `..._499602d2.vdf` 这类文件名会把 accountid 漏出去
        m = re.fullmatch(r"0110([0-9a-fA-F]{12})", tok)
        if m:
            short = m.group(1).lower()
            if short not in claimed:
                pairs.append((short, f"<acct:{digest}:accountid-hex>"))
                claimed.add(short)

    # 去掉空/自反的替换; 长的先替换, 避免前缀吃掉更长的路径.
    #
    # D6 根因之一: 这里原来用 `set(pairs)`, 会打乱**同长度**替换项的先后.
    # 同一物理路径被多个 appid 共用时 (典型: `70` Half-Life 与 `130` Half-Life:
    # Blue Shift 都在 `steamapps\common\Half-Life`), 会生成两条等长的替换项
    # `(同一路径, %GAME_DIR_70%)` / `(同一路径, %GAME_DIR_130%)`, 谁先命中
    # 取决于 set 的迭代顺序 —— 结果不稳定, 且常常是邻居把标签夺走.
    # 改为**保序去重**: 让"先出现的游戏优先"成为确定行为 (逐记录的自优先
    # 在 redact() 里另行保证).
    uniq: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for a, b in pairs:
        if not a or a == b or (a, b) in seen:
            continue
        seen.add((a, b))
        uniq.append((a, b))
    return sorted(uniq, key=lambda kv: -len(kv[0]))


def redact(prof: dict[str, Any]) -> dict[str, Any]:
    """生成可公开共享的脱敏档.

    三类脱敏, 缺一不可:
      * 值里的绝对路径 (str)
      * **字典键**里的账号目录名 (userdata_apps_by_account 的键就是 accountid)
      * **整数型**账号字段 (active_accountid / account_id 是 int, 不是 str)
    """
    pairs = _make_redactions(prof)
    token_map = {a: b for a, b in pairs}
    drop_keys = {"account_name", "persona_name", "steamid64"}

    # D6: 多个 appid 可能共用同一个物理安装目录 (Valve 自家 HL 系最典型),
    # 此时全局替换表只能给这个路径一个标签, 后出现的记录会被"邻居"的标签
    # 覆盖. 修法是**逐记录自优先**: 走每条游戏记录时, 先把它自己的
    # `install_dir -> %GAME_DIR_<自己 appid>%` 放到替换表最前, 命中之后再落到
    # 全局表 (此时路径已被替换, 邻居的同路径项不会再命中).
    own_paths: dict[str, str] = {}
    for g in prof.get("games") or []:
        if not isinstance(g, dict):
            continue
        aid, ip = g.get("appid"), g.get("install_dir")
        if aid is not None and ip:
            own_paths[str(aid)] = str(ip)

    def table_for(appid: Any) -> list[tuple[str, str]]:
        if appid is None:
            return pairs
        own = own_paths.get(str(appid))
        if not own:
            return pairs
        return [(own, f"%GAME_DIR_{appid}%")] + pairs

    def sub_str(s: str, table: list[tuple[str, str]]) -> str:
        for needle, token in table:
            if needle in s:
                s = s.replace(needle, token)
        return s

    def walk(
        node: Any,
        key: str = "",
        table: list[tuple[str, str]] | None = None,
    ) -> Any:
        if table is None:
            table = pairs
        if isinstance(node, dict):
            # 该记录若自带 appid, 优先用属于它自己的目录标签.
            here = table_for(node.get("appid")) if "appid" in node else table
            out: dict[str, Any] = {}
            for k, v in node.items():
                if k in drop_keys:
                    out[k] = None
                    continue
                if k == "timestamp":
                    out[k] = 0
                    continue
                nk = sub_str(k, here) if isinstance(k, str) else k
                out[nk] = walk(v, k, here)
            return out
        if isinstance(node, list):
            return [walk(v, key, table) for v in node]
        if isinstance(node, str):
            return sub_str(node, table)
        if isinstance(node, bool):
            return node
        if isinstance(node, int):
            return token_map.get(str(node), node)
        return node

    red = walk(prof)
    red["tool"] = dict(prof.get("tool", {}))
    red["tool"]["redacted"] = True
    red["redaction"] = {
        "applied": True,
        "rules": [
            "%USERPROFILE% / %STEAM_ROOT% / %STEAM_LIBRARY% / %GAME_DIR_<appid>% 替换绝对路径",
            "账号标识替换为 <acct:sha256前8位> 稳定短哈希",
            "移除 account_name / persona_name / steamid64",
        ],
        "note": "本文件用于公开提交 (邮件 / Issue)。未脱敏完整档在 private/machine_profile.json, 请只留本机。",
    }
    return red


def render_md(prof: dict[str, Any]) -> str:
    L: list[str] = []
    steam = prof.get("steam") or {}
    caps = prof.get("capabilities") or {}
    # 章节号动态计算 —— 警告段是可选的, 写死编号会出现 "1 → 3" 的跳号
    sec = 0

    def head(title: str) -> None:
        nonlocal sec
        sec += 1
        L.append(f"## {sec}. {title}")
        L.append("")

    L.append("# 机器探测摘要 (SteamCloudBridge)")
    L.append("")
    L.append(f"- 生成时间: `{prof.get('generated_at')}`")
    L.append(f"- 探测器: `{prof.get('tool', {}).get('name')} v{prof.get('tool', {}).get('version')}`")
    entry_desc, bundled_python = _entry_display(prof.get("tool", {}).get("entry"))
    py_ver = prof.get("os", {}).get("python")
    py_txt = f"内置 Python `{py_ver}` (exe 自带)" if bundled_python else f"Python `{py_ver}`"
    L.append(
        f"- 系统: `{prof.get('os', {}).get('system')} {prof.get('os', {}).get('release')}` / {py_txt}"
    )
    L.append(f"- 运行方式: {entry_desc}")
    L.append(f"- 数据是否脱敏: **{'是' if prof.get('tool', {}).get('redacted') else '否'}**")
    L.append("")

    head("机器能力判定")
    L.append("| 项 | 值 |")
    L.append("| --- | --- |")
    rows = [
        ("发现 Steam", caps.get("steam_found")),
        ("主安装", f"`{steam.get('primary_root')}`"),
        ("库数量", caps.get("library_count")),
        ("已安装游戏 (manifest)", caps.get("app_manifest_count")),
        ("当前账号已知", caps.get("active_account_known")),
        ("cloud_log 可用", caps.get("cloud_log_available")),
        ("console_log 可用", caps.get("console_log_available")),
        ("账号级云同步(整机)", (
            f"已通 — {len(caps.get('machine_appids_with_upload') or [])} 个游戏发起过上传"
            if caps.get("steam_cloud_account_upload_alive")
            else "未观察到任何游戏上传 (疑似账号级停摆)"
        ) if caps.get("cloud_log_available") else "—"),
        ("Auto-Cloud 声明文件数", caps.get("autocloud_marker_count")),
        ("探测游戏数", caps.get("games_probed")),
        ("已安装但未详查", (
            f"{caps.get('installed_not_probed_count')} 个 (无云证据; "
            "完整清单见 `steam.app_manifest_appids`)"
            if caps.get("installed_not_probed_count")
            else "0 个"
        )),
        ("其中 Auto-Cloud", caps.get("games_auto_cloud")),
        ("其中 API 型", caps.get("games_remote_api")),
        ("有 Watching 记录", caps.get("games_with_watching_files")),
        ("成功定位存档目录", caps.get("games_save_dir_resolved")),
        ("云被禁用", ", ".join(caps.get("games_cloud_disabled", [])) or "—"),
        ("点亮命令已执行 (console_log)", (
            ", ".join(caps.get("games_toggle_cmd_executed", [])) or "—"
        ) if caps.get("console_log_available") else "— (无 console_log)"),
        ("点亮命令未执行 (console_log)", (
            ", ".join(caps.get("games_toggle_cmd_missing", [])) or "—"
        ) if caps.get("console_log_available") else "— (无 console_log)"),
        ("开关当前值 (find testappcloudpaths)", ", ".join(
            f"{a}=「{v}」" for a, v in sorted(caps.get("game_toggle_var_values", {}).items())
        ) or "— (未读到; 在控制台执行 `find testappcloudpaths` 可读出)"),
        ("强证据从未同步", ", ".join(caps.get("games_never_synced_strong", [])) or "—"),
        ("具备 DLL 注入点", ", ".join(caps.get("games_with_mod_loader", [])) or "—"),
    ]
    for k, v in rows:
        L.append(f"| {k} | {v if v is not None else '—'} |")
    L.append("")
    L.append(
        "> 注意: 上表只统计**已逐游戏详查**的游戏。**已安装但没有云证据**的游戏"
        "不会出现在下文明细里 —— 它们不是\"没装\"。本机已安装 appid 的完整清单"
        "见 `machine_profile.share.json` 的 `steam.app_manifest_appids`；"
        "若需强制详查某个 appid, 用 `--appid <id>` (或 `RUN-PROBE-TARGET.bat`)。"
    )
    L.append("")

    if prof.get("warnings"):
        head("警告")
        for w in prof["warnings"]:
            L.append(f"- {w}")
        L.append("")

    head("逐游戏探测结果")
    games = prof.get("games", [])
    if not games:
        L.append("_未选中任何目标游戏。_")
        L.append("")
    else:
        L.append("| appid | 游戏 | 机制 | 云态 | 被监视文件 | 存档目录 | 开关需求 |")
        L.append("| --- | --- | --- | --- | --- | --- | --- |")
        for g in games:
            name = (g.get("name") or "").replace("|", "\\|") or "—"
            mech = g.get("mechanism", "unknown")
            state = g.get("cloud_state", "unknown")
            wf = len(g.get("watching_files") or [])
            save = g.get("save_dir_resolved") or "— 未定位"
            toggle = "需要" if (g.get("cloud_toggle") or {}).get("needed") else "—"
            L.append(
                f"| `{g['appid']}` | {name} | {mech} | {state} | {wf} | `{save}` | {toggle} |"
            )
        L.append("")

        L.append("### 详情")
        L.append("")
        for g in games:
            L.append(f"**{g.get('name') or '?'}** (`{g['appid']}`)")
            L.append("")
            v = g.get("verdict") or {}
            L.append(f"- 可借道: **{'是' if v.get('bridgeable') else '否'}**")
            if v.get("reasons"):
                for r in v["reasons"]:
                    L.append(f"  - {r}")
            cn = g.get("change_numbers") or {}
            _ru = g.get("remote_upload") or {}
            _extra = ""
            if _ru:
                _extra = (
                    f" / 云端变更号={_ru.get('change_number')}"
                    f" / 云端有副本={'是' if _ru.get('verified') else '否'}"
                )
            L.append(
                f"- 同步进度: local={cn.get('local')} → global={cn.get('global')}"
                f" / 曾推进过={g.get('ever_advanced')}{_extra}"
            )
            if g.get("cloud_toggle", {}).get("command"):
                L.append(f"- 点云命令: `{g['cloud_toggle']['command']}` (复位: `{g['cloud_toggle']['reset_command']}`)")
            _tack = g.get("cloud_toggle_console")
            if _tack:
                if _tack.get("found"):
                    L.append(
                        "- 点亮命令执行回执 (`console_log`): **有**"
                        + (f" — `{_tack.get('last_line')}`" if _tack.get("last_line") else "")
                    )
                else:
                    L.append(
                        "- 点亮命令执行回执 (`console_log`): **无** — "
                        "该命令从未在本机执行过 (执行后**不要重启 Steam**, 直接进游戏一次)"
                    )
                # 变量当前值: 比"历史上出现过回执"更硬 —— 开关易失, 值会被重启 /
                # 复位命令改回去, 而旧回执永远留在日志里. 值 = 本会话当前生效的 appid.
                _tv = _tack.get("value")
                if _tv is not None:
                    L.append(
                        f"- 开关当前值 (控制台变量 `testappcloudpaths`): `{_tv}` ⇒ "
                        + (
                            "**已点亮 (本会话生效)**"
                            if _tack.get("value_matches_appid")
                            else f"**未点亮** (值不等于 `{g['appid']}`)"
                        )
                    )
                elif g.get("cloud_state") == "disabled":
                    L.append(
                        "- 开关当前值: 未知 —— 让操作者在控制台执行 `find testappcloudpaths` "
                        f"即可读出 (显示 `{g['appid']}` = 已点亮; 显示 `0` = 未点亮). "
                        "点亮命令成功时**不打印**任何提示语, 只回显一行变量赋值, "
                        "所以「看着没反应」不等于没生效 —— 用上面那条命令读值才是准的."
                    )
            _blk = g.get("cloud_blocker")
            if _blk:
                _layer = "账号级(持久)" if _blk["layer"] == "account" else "游戏级(易失)"
                L.append(f"- **当前阻塞 [{_layer}] `{_blk['code']}`**: {_blk['detail']}")
                if _blk.get("fix"):
                    L.append(f"  - 解法: `{_blk['fix']}`")
            if g.get("watching_files"):
                L.append("- 被监视文件 (Steam 实际同步):")
                for f in g["watching_files"][:10]:
                    L.append(f"  - `{f}`")
            if g.get("save_dir_source"):
                L.append(f"- 存档定位来源: `{g['save_dir_source']}`")
            if g.get("save_dir_rejected"):
                L.append(f"- 存档目录已拒绝: {g['save_dir_rejected']}")
            if g.get("autocloud_marker_dirs"):
                L.append(
                    "- Auto-Cloud 声明文件所在目录: "
                    + ", ".join("`%s`" % d for d in g["autocloud_marker_dirs"][:5])
                )
            snap = g.get("save_snapshot")
            if snap and snap.get("exists"):
                L.append(
                    f"- 存档目录内容: {snap.get('file_count')} 个文件 / "
                    f"{snap.get('bytes')} 字节; 扩展名分布 `{snap.get('extensions')}`"
                )
            if g.get("watched_file_state"):
                L.append("- 被监视文件现状:")
                for w in g["watched_file_state"]:
                    state = "存在" if w["exists"] else "缺失"
                    L.append(f"  - `{w['name']}`: {state} ({w['size']} 字节)")
            frame = g.get("local_frame") or {}
            if frame.get("loader_dirs"):
                L.append(f"- DLL 加载目录 (注入点): {', '.join('`%s`' % d for d in frame['loader_dirs'])}")
            if frame.get("config_files"):
                L.append(f"- 加载器配置: {', '.join('`%s`' % d for d in frame['config_files'])}")
            if frame.get("mod_dirs"):
                L.append(f"- 普通 mod 目录 (非注入点): {', '.join('`%s`' % d for d in frame['mod_dirs'])}")
            if frame.get("launchers"):
                L.append(f"- 启动器: {', '.join('`%s`' % d for d in frame['launchers'])}")
            if g.get("ac_root_map"):
                L.append("- Auto-Cloud 根语义:")
                for m in g["ac_root_map"]:
                    L.append(
                        f"  - `{m['ac_dir']}` -> `{m['base'] or '无法解析 (需游戏安装目录)'}`"
                    )
            L.append("")

    head("把数据交回项目")
    L.append(
        "把 **`machine_profile_bundle.zip`** (或 `machine_profile.share.json`) 发回给开发者即可 —— "
        "它已移除账号 ID 与用户名。"
    )
    L.append("")
    L.append(
        "⚠ **不要发 `private/` 目录。** 里面的 `machine_profile.json` 是**未脱敏完整档**, "
        "含你的 Steam 账号 ID、本机用户名与全部绝对路径 —— **只留本机自用**。"
        "本机若有多个 Steam 账号, 它还会带上别人的账号 ID。"
    )
    L.append("")
    L.append("> 只发 `machine_profile_bundle.zip` 一个文件就够; "
             "它和 `machine_profile.share.json` 内容一样 (多一份人读摘要)。")
    L.append("")
    L.append("下面这条**只有拿到源码的开发者需要执行, 你所在的发行包里没有 `tools/` 目录, 不用跑**:")
    L.append("")
    L.append("```bash")
    L.append("python tools/gen_adapter.py --profile machine_profile.share.json --out-dir adapters")
    L.append("```")
    L.append("")

    if prof.get("errors"):
        head("错误")
        for e in prof["errors"]:
            L.append(f"- {e}")
        L.append("")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# 可选回传
# ---------------------------------------------------------------------------


def upload(url: str, payload: Path, token: str | None = None, timeout: int = 20) -> str:
    """把脱敏档 POST 到用户**自己指定**的收集端 (默认不启用)."""
    import urllib.error
    import urllib.request

    headers = {"Content-Type": "application/json", "User-Agent": f"{TOOL}/{VERSION}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=payload.read_bytes(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 用户显式指定
            return f"{resp.status} {resp.reason}"
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code} {exc.reason}"
    except Exception as exc:  # noqa: BLE001
        return f"失败: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="machine_probe",
        description="扫描本机 Steam 云存全貌与目标游戏存档落点 (纯只读, 零依赖).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--appid", action="append", default=[], help="指定 appid, 可重复")
    p.add_argument(
        "--version",
        action="version",
        version=f"{TOOL} {VERSION}",
        help="打印版本号后退出 (报障时请附上)",
    )
    p.add_argument("--account", action="append", default=[], help="指定 accountid, 可重复")
    p.add_argument("--all-accounts", action="store_true", help="扫描 userdata 下全部账号")
    p.add_argument("--steam-root", action="append", default=[], help="手动指定 Steam 根目录")
    p.add_argument("--limit", type=int, default=40, help="自动模式下最多探测多少个游戏 (0=不限)")
    p.add_argument("--marker-depth", type=int, default=4, help="autocloud 声明文件搜索深度")
    p.add_argument("--no-markers", action="store_true", help="跳过 autocloud 声明文件扫描")
    p.add_argument("--out-dir", default=str(_default_out_dir()), help="输出目录")
    p.add_argument("--zip", action="store_true", help="额外打包成 machine_profile_bundle.zip")
    p.add_argument("--upload", default="", help="把脱敏档 POST 到你自己的收集端 URL")
    p.add_argument("--token", default="", help="--upload 的 Bearer token")
    p.add_argument("--list", action="store_true", help="只列出可探测的 appid, 不逐游戏详查")
    p.add_argument("--quiet", action="store_true", help="少打印")
    return p


def main(argv: list[str] | None = None) -> int:
    _harden_console()  # 必须在任何 print 之前: 控制台编码不能让进程崩掉
    args = _build_parser().parse_args(argv)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def say(msg: str) -> None:
        if not args.quiet:
            print(msg, flush=True)

    say(f"[{TOOL} v{VERSION}] 只读探测中, 不会写入任何 Steam / 游戏目录 ...")
    prof = collect(args)

    if args.list:
        targets = prof.get("targets", [])
        say(f"可探测 appid ({len(targets)} 个):")
        for a in targets:
            say(f"  {a}")
        return 0

    if not (prof.get("capabilities") or {}).get("steam_found"):
        errs = prof.get("errors") or ["未发现 Steam"]
        for e in errs:
            say(f"错误: {e}")
        return 2

    # 目录分层: **根目录只放可以外发的东西**, 未脱敏完整档收进 private/.
    # 起因: 朋友"把 reports 文件夹整个压缩发回来"是最自然的动作, 而旧布局把
    # 完整档 (含真实账号 ID + 全部绝对路径) 与脱敏档并排放, 谁都会一起发出来.
    share_path = out_dir / "machine_profile.share.json"
    md_path = out_dir / "machine_profile.md"
    list_path = out_dir / "machine_profile.appids.txt"
    private_dir = out_dir / "private"
    private_dir.mkdir(parents=True, exist_ok=True)
    full_path = private_dir / "machine_profile.json"

    full_path.write_text(json.dumps(prof, ensure_ascii=False, indent=2), encoding="utf-8")

    red = redact(prof)
    share_path.write_text(json.dumps(red, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_md(red), encoding="utf-8")

    all_appids = sorted(
        {*(prof.get("steam", {}).get("userdata_apps_by_account", {}) or {}).keys()}
        | set(prof.get("autocloud_markers", {}))
        | set(prof.get("cloud_log", {}).get("entries", {}))
    )
    if prof.get("steam", {}).get("userdata_apps_by_account"):
        flat: set[str] = set()
        for apps in prof["steam"]["userdata_apps_by_account"].values():
            flat |= set(apps)
        all_appids = sorted(flat)
    list_path.write_text("\n".join(all_appids) + ("\n" if all_appids else ""), encoding="utf-8")

    caps = prof["capabilities"]
    say("")
    say(f"  Steam 安装   : {caps['install_count']} 处 / 库 {caps['library_count']} 个")
    say(f"  已装游戏     : {caps['app_manifest_count']}")
    say(f"  当前账号已知 : {caps['active_account_known']}")
    say(f"  探测目标     : {caps['games_probed']} 个 (Auto-Cloud {caps['games_auto_cloud']} / API {caps['games_remote_api']})")
    say(f"  定位到存档   : {caps['games_save_dir_resolved']} 个")
    for g in prof.get("games", []):
        save = g.get("save_dir_resolved") or "未定位"
        flag = "OK " if g.get("verdict", {}).get("bridgeable") else "-- "
        say(f"    {flag} {g['appid']:>8}  {(g.get('name') or '')[:34]:<36} {save}")
    say("")
    say(f"  完整档 (本机留存): {full_path}")
    say(f"  脱敏档 (可公开)  : {share_path}")
    say(f"  摘要             : {md_path}")

    if args.zip:
        bundle = out_dir / "machine_profile_bundle.zip"
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(share_path, "machine_profile.share.json")
            zf.write(md_path, "machine_profile.md")
        say(f"  回传包           : {bundle}")

    # 把"发哪个文件"写在目录里最显眼的位置. 不带这一份, 用户最自然的动作就是
    # 把整个 reports 文件夹压回来 —— 于是未脱敏完整档跟着走.
    send_name = "machine_profile_bundle.zip" if args.zip else "machine_profile.share.json"
    (out_dir / "00-发送说明.txt").write_text(
        "\n".join([
            "要发回去的只有一个文件:",
            "",
            f"    {send_name}",
            "",
            "它已脱敏 —— 账号 ID 换成了短哈希, 绝对路径换成了 %USERPROFILE% / %GAME_DIR_<appid>% 之类的占位符。",
            "",
            "不要发 private 目录。",
            "里面的 machine_profile.json 是未脱敏完整档, 含你的 Steam 账号 ID、",
            "本机用户名与全部绝对路径; 如果这台机器上有多个 Steam 账号, 还会带上别人的",
            "账号 ID。它只留本机自用 (自己排查问题时可以看)。",
            "",
            "也可以只发 machine_profile.share.json —— 内容与上面的回传包一样。",
            "",
        ]),
        encoding="utf-8",
    )

    if args.upload:
        target = out_dir / "machine_profile_bundle.zip" if args.zip else share_path
        result = upload(args.upload, target, args.token or None)
        say(f"  回传结果         : {result}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
