#!/usr/bin/env python3
"""把 `machine_profile.json` 变成**这台机器可直接用**的适配器文件.

这是"自动适配机器"的最后一环: 探测器负责"看清这台机器", 本工具负责把看清的
结果编译成 `adapters/steam_<appid>.adapter.json` (+ 同名 `.yaml` 供人读/手改).

适配器里放的是**落地要用的一切**:
  * 存档目录 + 定位来源 + 置信度 + 账号目录命名形态
  * Steam 云开关状态与点亮命令
  * 被监视文件清单 (Steam 到底同步哪些文件)
  * 本机注入点 (mod 加载目录 / 启动器)
  * 由此推出的**操作步骤序列**

支持两种输入:
  * `private/machine_profile.json` (完整档) -> 绝对路径, 只在本机可用
  * `machine_profile.share.json` (脱敏档) -> `%APPDATA%` 这类模板路径,
    可提交回项目, 由维护者据此为同型号环境补适配器

用法:
    python tools/gen_adapter.py --profile reports/private/machine_profile.json
    python tools/gen_adapter.py --profile reports/machine_profile.share.json --out-dir adapters
    python tools/gen_adapter.py --profile <p> --appid 374320
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ADAPTER_SCHEMA = "steamcloudbridge.adapter/1"


# ---------------------------------------------------------------------------
# 极简 YAML 输出 (无第三方依赖)
# ---------------------------------------------------------------------------


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value).replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\n", "\\n")
    return f'"{s}"'


def to_yaml(node: Any, indent: int = 0) -> list[str]:
    """把 dict/list/scalar 转成 YAML 行 (只覆盖本项目用到的子集, 全部标量加引号)."""
    pad = "  " * indent
    if isinstance(node, dict):
        if not node:
            return [pad + "{}"]
        out: list[str] = []
        for k, v in node.items():
            if isinstance(v, (dict, list)) and v:
                out.append(f"{pad}{k}:")
                out += to_yaml(v, indent + 1)
            elif isinstance(v, (dict, list)):
                out.append(f"{pad}{k}: {'{}' if isinstance(v, dict) else '[]'}")
            else:
                out.append(f"{pad}{k}: {_yaml_scalar(v)}")
        return out
    if isinstance(node, list):
        if not node:
            return [pad + "[]"]
        out = []
        for item in node:
            if isinstance(item, (dict, list)) and item:
                sub = to_yaml(item, indent + 1)
                out.append(f"{pad}- {sub[0].strip()}")
                out += sub[1:]
            else:
                out.append(f"{pad}- {_yaml_scalar(item)}")
        return out
    return [pad + _yaml_scalar(node)]


# ---------------------------------------------------------------------------
# 适配器构建
# ---------------------------------------------------------------------------


def _confidence(game: dict[str, Any]) -> str:
    if not game.get("save_dir_resolved"):
        return "unresolved"
    if not game.get("save_dir_confident"):
        return "guess"
    src = game.get("save_dir_source")
    if src == "cloud_log":
        return "confirmed-by-cloud-log"
    if src == "remotecache":
        return "confirmed-by-remotecache"
    if src == "remote-dir":
        return "confirmed-remote-dir"
    return "confirmed-by-name-match"


def _steps(game: dict[str, Any], redacted: bool) -> list[str]:
    """由观测状态推出操作步骤 (这是用户实际要照着做的事)."""
    steps: list[str] = []
    appid = game["appid"]
    save_dir = game.get("save_dir_resolved")
    source = game.get("save_dir_source")

    # --- 两层云开关: 这是本项目最容易踩的坑, 必须优先说明执行顺序 ---
    blocker = game.get("cloud_blocker") or {}
    bcode = blocker.get("code")
    # 兼容旧档: 若 profile 由未输出 cloud_blocker 的探测器生成, 由 cloud_state 兜底推断
    if not bcode and game.get("cloud_state") == "disabled":
        bcode = "game-toggle-off"
    is_auto_cloud = "auto-cloud" in (game.get("mechanisms") or []) or game.get("mechanism") == "auto-cloud"
    if is_auto_cloud:
        steps.append(
            "**执行顺序 (先读这条)**: Steam 的游戏云上传受**两层开关**控制 —— "
            "第 1 层「账号级, 持久」: 设置 → 云服务 → 为此账号启用 Steam 云同步; "
            "第 2 层「游戏级, 易失」: AutoCloud 隐藏命令 `testappcloudpaths <appid>`, "
            "**每次重启 Steam 都会失效**. 正确顺序是**先确认第 1 层已生效, 再点第 2 层** —— "
            "反过来先点第 2 层、再为改第 1 层重启 Steam, 第 2 层就白点了. "
            "第 1 层是否生效可用「本机是否有任何游戏发起过 `Upload batch initiated`」反向确认."
        )
    if bcode == "game-toggle-off":
        steps.append(
            f"当前阻塞在第 2 层 (游戏级): 在 Steam 控制台执行 "
            f"`{(game.get('cloud_toggle') or {}).get('command')}` 点亮; 复位用 "
            f"`{(game.get('cloud_toggle') or {}).get('reset_command')}`."
        )
    elif bcode == "account-cloud-off":
        steps.append(
            "当前阻塞在第 1 层 (账号级): 整机没有任何游戏发起过上传批次 ⇒ 账号级云同步未启用. "
            "先在「设置 → 云服务」开启并重启 Steam, **再**回来点第 2 层开关."
        )
    elif (game.get("cloud_toggle") or {}).get("volatile"):
        steps.append(
            "云通道已被开发者测试命令点亮 (**当前会话生效**); 但它**不跨 Steam 重启** —— "
            "每次启动 Steam 后都要重查 cloud_log, 一旦又出现 `AutoCloud is disabled` 就需重新点亮."
        )
    elif game.get("cloud_state") == "state-field-only":
        steps.append("localconfig 里有云状态字段但日志未记录开关动作, 建议启动一次游戏后用 cloud_log 复核.")

    if not save_dir:
        steps.append(
            "存档目录未定位: 先跑 `machine_probe.py --all-accounts`, "
            "或在适配器里手工填 `save.dir`."
        )
        return steps

    steps.append(f"存档目录: `{save_dir}` (来源: {source})")
    if not game.get("save_dir_confident"):
        steps.append("该存档目录为低置信推断, 使用前请人工确认内容是否为目标游戏的存档.")

    if source == "remote-dir":
        steps.append("该游戏走 ISteamRemoteStorage, 文件实体已在 Steam 云目录, 通常无需借道.")

    watching = game.get("watching_files") or game.get("persisted_files") or []
    patterns = game.get("rule_patterns") or []
    if watching:
        steps.append(
            f"Steam 实际只同步 {len(watching)} 个文件"
            f" (主档: {', '.join(game.get('watched_file_names') or []) or '未知'}); "
            "**跨机器时只有这些文件会被搬运**, 伴生文件 (.co2/.ds3os/.bak 等) 不会."
        )
        missing = [w["name"] for w in (game.get("watched_file_state") or []) if not w.get("exists")]
        if missing:
            steps.append(f"注意: 被监视文件中 {', '.join(missing)} 当前本机不存在, 上传会变成删除语义.")
    elif patterns:
        steps.append(
            f"云同步规则为 pattern={', '.join(patterns)} "
            f"(规则文件集: {', '.join(game.get('rule_file_names') or []) or '未知'}); "
            "**跨机器时只有规则命中的文件会被搬运**, 伴生文件不会."
        )
    else:
        steps.append(
            "尚无 `Watching file` 记录 (云未点亮或从未同步), 无法确证同步文件集; "
            "建议点亮云并进出游戏一次后重新探测."
        )

    # 上传往返: 判据是 remotecache 的 remotetime, **不是** cloud_log 的
    # `Successfully synced` (那是通用同步语句, 上传路径里它出现在
    # `Upload batch initiated` 之前; 见 docs/how-steam-cloud-works.md).
    up = game.get("remote_upload") or {}
    if up.get("verified"):
        steps.append(
            f"云端已存在副本 (`remotecache` 的 `remotetime`={up.get('max_remotetime')}); "
            "该云通道**实测可用**, 可直接作为跨机器传输层."
        )
    elif up.get("file_count"):
        pending = ", ".join(up.get("pending_files") or []) or "未知"
        steps.append(
            f"⚠️ 云端**尚无副本**: `remotecache.vdf` 里 {pending} 的 `remotetime` 仍为 0, "
            "说明文件只登记在本地、从未落到 Valve 服务器. 判据只看 `remotecache` 的 `remotetime` "
            "与 `Upload batch initiated` 是否出现 (单看 cloud_log 的某行不足以断定)."
        )
        if bcode == "game-toggle-off":
            steps.append(
                "本游戏的上传被**游戏级开关**挡住, 先按上面的 `testappcloudpaths` 点亮, "
                "再进游戏存一次档并正常退出."
            )
        elif bcode == "account-cloud-off":
            steps.append(
                "本游戏的上传被**账号级开关**挡住: 日志里从未出现 `Upload batch initiated` ⇒ "
                "Steam 从未向服务器提交过上传批次. 开启账号级云同步并重启 Steam 后重扫即可解除."
            )
        else:
            steps.append(
                "促成首次上传的关键: Steam 会 `Skipping un-modified file` 跳过 mtime 未变的文件, "
                "且 `(full)` 全量同步不在退出时机执行 ⇒ 只有让目标文件的 mtime 真正更新才会被判为"
                "\"已修改\"并上传. 写入存档后必须更新 mtime, **严禁**用 copy2/copystat 保留源时间戳."
            )
    elif game.get("queued_not_uploaded"):
        steps.append(
            "注意: 观察到 `Persisting file ... to the cloud` 但本机无 remotecache 证据, "
            "无法确认是否真正上云; 建议进出游戏一次后重新探测."
        )

    frame = game.get("local_frame") or {}
    if frame.get("loader_dirs"):
        steps.append(
            f"检测到 DLL 自动加载目录: {', '.join(frame['loader_dirs'])}; "
            "DLL 形态可直接 drop-in 到该目录, 由现有加载器负责注入."
        )
    elif frame.get("config_files"):
        steps.append(
            f"检测到 mod 加载器配置 {', '.join(frame['config_files'])}, 但未找到 dllMods 目录; "
            "先确认该加载器的 DLL 投放目录再决定形态."
        )
    else:
        if frame.get("mod_dirs"):
            steps.append(
                f"仅有普通 mod 资源目录 {', '.join(frame['mod_dirs'])}, "
                "**不是** DLL 注入点."
            )
        steps.append("未检测到 DLL 加载目录: 若走 DLL 形态需自建代理加载器 (如 dinput8.dll).")

    if frame.get("launchers"):
        steps.append(f"可用启动器: {', '.join(frame['launchers'])} (可用它替代 steam://rungameid/{appid}).")

    if game.get("mechanism") == "auto-cloud":
        steps.append(
            "红线: 只做**同账号自己存档的字节级搬运**, 不解析/不改内容, 禁止跨账号导入; "
            "Steam 云是**传输层, 必须保持开启**——工具只在游戏启动前把目标文件就位, "
            "由 Steam 在 AutoCloud 时机自行同步; 严禁在 Steam 运行中改写被监视文件 (会与云仲裁抢文件)."
        )
    else:
        steps.append(
            "红线: 只做**同账号自己存档的字节级搬运**, 不解析/不改内容, 禁止跨账号导入."
        )
    if redacted:
        steps.append("注意: 本适配器来自脱敏档, 路径为模板 (%APPDATA% 等), 需在本机展开后使用.")
    return steps


def build_adapter(prof: dict[str, Any], game: dict[str, Any]) -> dict[str, Any]:
    redacted = bool((prof.get("tool") or {}).get("redacted"))
    snap = game.get("save_snapshot") or {}
    frame = game.get("local_frame") or {}

    return {
        "schema": ADAPTER_SCHEMA,
        "appid": game["appid"],
        "name": game.get("name") or "",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "provenance": {
            "probe_version": (prof.get("tool") or {}).get("version"),
            "profile_redacted": redacted,
            "profile_os": (prof.get("os") or {}).get("system"),
            "steam_primary_root": (prof.get("steam") or {}).get("primary_root"),
        },
        "mechanism": game.get("mechanism"),
        "mechanisms": game.get("mechanisms"),
        "cloud": {
            "state": game.get("cloud_state"),
            "last_sync_state": game.get("cloud_last_sync_state"),
            "toggle_required": bool((game.get("cloud_toggle") or {}).get("needed")),
            "toggle_command": (game.get("cloud_toggle") or {}).get("command"),
            "reset_command": (game.get("cloud_toggle") or {}).get("reset_command"),
            "sync_progress": game.get("change_numbers"),
            "ever_advanced": game.get("ever_advanced"),
            "never_synced_strong": game.get("never_synced_strong"),
            "incomplete_sync_count": game.get("incomplete_sync_count"),
            # 两层开关诊断: layer=account(持久) / game(易失), code 见 README
            "blocker": game.get("cloud_blocker"),
        },
        "watch": {
            "files": game.get("watching_files") or game.get("persisted_files") or [],
            "file_names": game.get("watched_file_names") or [],
            "file_state": game.get("watched_file_state"),
            "save_dirs_seen": game.get("watched_save_dirs") or [],
            # 文件集的证据等级: watching-file > persisted > rule > 无
            "evidence": (
                "watching-file"
                if game.get("watching_files")
                else (
                    "persisted"
                    if game.get("persisted_files")
                    else ("rule" if game.get("cloud_rules") else None)
                )
            ),
            "rules": game.get("cloud_rules") or [],
            "patterns": game.get("rule_patterns") or [],
            "roundtrip_ok": game.get("cloud_roundtrip_ok"),
            # 上传往返的权威判据: remotecache 的 remotetime (见 src/steam/remotecache.py)
            "remote_replica": bool((game.get("remote_upload") or {}).get("verified")),
            "remote_upload": game.get("remote_upload"),
            "queued_not_uploaded": bool(game.get("queued_not_uploaded")),
        },
        "save": {
            "dir": game.get("save_dir_resolved"),
            "path_style": "templated" if redacted else "absolute",
            "source": game.get("save_dir_source"),
            "confidence": _confidence(game),
            "account_dir_form": game.get("account_dir_form"),
            "candidates": [
                {
                    "path": c.get("path"),
                    "source": c.get("source"),
                    "match": c.get("match_level"),
                }
                for c in (game.get("save_candidates") or [])[:8]
            ],
            "observed": {
                "file_count": snap.get("file_count"),
                "bytes": snap.get("bytes"),
                "extensions": snap.get("extensions"),
                "files": (snap.get("files") or [])[:20],
            },
        },
        "injection": {
            "available": bool(frame.get("injection_available")),
            "kind": (
                "dll-loader-dir"
                if frame.get("loader_dirs")
                else ("loader-config-only" if frame.get("config_files") else "none-detected")
            ),
            "loader_dirs": frame.get("loader_dirs") or [],
            "config_files": frame.get("config_files") or [],
            "mod_dirs": frame.get("mod_dirs") or [],
            "launchers": frame.get("launchers") or [],
            "steam_appid_txt": frame.get("steam_appid_txt"),
        },
        "bridge": {
            "supported": bool((game.get("verdict") or {}).get("bridgeable")),
            "reasons": (game.get("verdict") or {}).get("reasons") or [],
            "steps": _steps(game, redacted),
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="gen_adapter",
        description="由机器探测档生成适配器文件 (JSON + YAML).",
    )
    p.add_argument("--profile", required=True, help="machine_profile.json 或其脱敏档")
    p.add_argument("--out-dir", default="adapters", help="适配器输出目录")
    p.add_argument("--appid", action="append", default=[], help="只生成指定 appid")
    p.add_argument("--supported-only", action="store_true", help="只生成可借道的游戏")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    prof_path = Path(args.profile)
    if not prof_path.is_file():
        print(f"错误: 找不到探测档 {prof_path}", file=sys.stderr)
        return 2
    prof = json.loads(prof_path.read_text(encoding="utf-8-sig"))

    games = prof.get("games") or []
    if not games:
        print("错误: 探测档里没有 games 段 (先用 machine_probe.py 跑一轮)", file=sys.stderr)
        return 2

    if args.appid:
        wanted = {str(a) for a in args.appid}
        games = [g for g in games if str(g["appid"]) in wanted]
    if args.supported_only:
        games = [g for g in games if (g.get("verdict") or {}).get("bridgeable")]

    if not games:
        print("没有匹配的游戏。")
        return 0

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for game in games:
        adapter = build_adapter(prof, game)
        stem = out_dir / f"steam_{game['appid']}.adapter"
        js = stem.with_suffix(".adapter.json")
        yml = stem.with_suffix(".adapter.yaml")
        js.write_text(json.dumps(adapter, ensure_ascii=False, indent=2), encoding="utf-8")
        yml.write_text("\n".join(to_yaml(adapter)) + "\n", encoding="utf-8")
        written += [js, yml]
        if not args.quiet:
            conf = adapter["save"]["confidence"]
            sup = "支持" if adapter["bridge"]["supported"] else "不支持"
            print(f"  {game['appid']:>8}  {(game.get('name') or '')[:34]:<36} {sup:<4} 存档={conf}")

    if not args.quiet:
        print("")
        print(f"  共写出 {len(written)} 个文件到 {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
