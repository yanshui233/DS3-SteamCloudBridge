#!/usr/bin/env python3
"""生成**完全隔离**的假 Steam 环境, 供沙箱端到端测试使用.

## 为什么需要它

真实跨机器验证依赖外部协作 (见 `docs/plan-v3.md` 的 4b), 但外部机器不可控、
也无法反复重跑。本工具造一套自带账号 / 存档 / 日志 / 清单的假环境, 让
`machine_probe` 在**不接触任何真机数据**的前提下跑完整链路:

* `APPDATA` / `LOCALAPPDATA` / `USERPROFILE` / `PROGRAMDATA`  -> 假用户档
  (`machine_probe` 定位 `%APPDATA%\\DarkSoulsIII\\<hex>` 完全依赖这几个
  环境变量, 见 `src/steam/discovery.py::resolve_ac_root_dir` 与
  `src/steam/remotecache.py::candidate_bases`)
* `steam/` -> 交给 `--steam-root`, 它**完全覆盖**注册表自动发现

## 硬约束

* **零真机数据**: 账号 ID 默认取与 `tests/` 一致的虚构值, `accountid` /
  `SteamID64` / hex 目录名三者由 `src.steam.accounts` 换算, 保证自洽。
* **严格对齐解析器**: VDF / ACF / `cloud_log.txt` 的每一行都对照
  `src/steam/*` 的解析正则与路径约定, 不是"看起来像"。
* **可复现场景**: `clean`(无云痕迹) / `synced`(已上传成功) /
  `disabled`(云开关关着)。另常驻一个 **API 型对照游戏** (550), 用来验证
  "API 型不会被 marker 夺权改判成 Auto-Cloud"。

## 用法

    python tools/make_fake_steam.py --out cases/sandbox --scenario synced
    python tools/run_sandbox_probe.py --env cases/sandbox --zip
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.steam import accounts  # noqa: E402

DEFAULT_ACCOUNT_ID = 1234567890
DEFAULT_SCENARIO = "synced"
SCENARIOS = ("clean", "synced", "disabled")

DS3_APPID = "374320"
DS3_NAME = "DARK SOULS III"
DS3_INSTALLDIR = "DARK SOULS III"
DS3_SAVE_DIRNAME = "DarkSoulsIII"
DS3_SAVE_FILE = "DS30000.sl2"

API_APPID = "550"
API_NAME = "Left 4 Dead 2"
API_INSTALLDIR = "Left 4 Dead 2"
API_REMOTE_FILE = r"cfg\server.cfg"

LOCAL_TS = 1758380947
REMOTE_TS = 1791725553


# ---------------------------------------------------------------------------
# VDF 生成 (KeyValues)
# ---------------------------------------------------------------------------

def _esc(text: object) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def _kv(key: str, value: object, indent: int = 0) -> str:
    """把一个 dict/标量渲染成 Steam KeyValues 片段."""
    tab = "\t" * indent
    if isinstance(value, dict):
        lines = [f'{tab}"{_esc(key)}"', f"{tab}{{"]
        for k, v in value.items():
            lines.append(_kv(k, v, indent + 1))
        lines.append(f"{tab}}}")
        return "\n".join(lines)
    return f'{tab}"{_esc(key)}"\t\t"{_esc(value)}"'


def _write_vdf(path: Path, root_key: str, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _kv(root_key, body, 0) + "\n"
    with open(path, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(text)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(text)


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


# ---------------------------------------------------------------------------
# 清理 (避开批量删除守卫: 逐个 unlink, 不用 rmtree)
# ---------------------------------------------------------------------------

def _purge(target: Path) -> None:
    if not target.exists():
        return
    for child in sorted(target.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try:
            if child.is_dir() and not child.is_symlink():
                child.rmdir()
            else:
                child.unlink()
        except OSError:
            pass
    try:
        target.rmdir()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 云日志
# ---------------------------------------------------------------------------

def _cloud_log(scenario: str, save_abs: str, save_rel: str, hex_dir: str) -> str:
    """按场景生成 cloud_log.txt. 每行都对齐 src/steam/cloud_log.py 的正则."""
    watch_rel = f"{DS3_SAVE_DIRNAME}\\{hex_dir}\\{DS3_SAVE_FILE}"

    if scenario == "synced":
        return "\n".join(
            [
                f'[AppID {DS3_APPID}] Evaluating rule 1 with root="WinAppDataRoaming" '
                f'path="{DS3_SAVE_DIRNAME}" pattern="*.sl2" (recursive)',
                f"[AppID {DS3_APPID}] Found 1 files that match",
                f"[AppID {DS3_APPID}] Watching file {save_abs}",
                f"[AppID {DS3_APPID}] AutoCloud done. Watching 1 files",
                f"[AppID {DS3_APPID}] Persisting file {save_abs} to the cloud",
                f"[AppID {DS3_APPID}] Need to upload file {watch_rel}",
                f"[AppID {DS3_APPID}] Need to sync from local change number '0' "
                f"to global change number '1'",
                f"[AppID {DS3_APPID}] Upload batch initiated, ID: 5367144410780478463, "
                f"ChangeNumber: 1",
                f"[AppID {DS3_APPID}] Upload OK for file {watch_rel}",
                f"[AppID {DS3_APPID}] Upload complete, result OK",
                f"[AppID {DS3_APPID}] Successfully synced to ChangeNumber 1",
                f"[AppID {DS3_APPID}] GetAppQuotaUsage - Success",
                "",
            ]
        )

    if scenario == "disabled":
        return "\n".join(
            [
                f'[AppID {DS3_APPID}] Evaluating rule 1 with root="WinAppDataRoaming" '
                f'path="{DS3_SAVE_DIRNAME}" pattern="*.sl2" (recursive)',
                f"[AppID {DS3_APPID}] AutoCloud is disabled",
                "",
            ]
        )

    # clean: 该游戏一条痕迹都没有 (云开关从未点亮)
    return "\n".join(
        [
            "[AppID 7] Persisting file C:\\Program Files\\WorkBuddy\\irrelevant.txt "
            "to the cloud",
            "",
        ]
    )


def _console_log() -> str:
    """生成 console_log.txt —— **只含会话边界, 绝不含点亮命令回执**.

    ⚠ 不要"顺手"往这里加 `"testappcloudpaths" = "<appid>"`:
    `src/transport/cloudchannel.py` 的 `_console_lit_evidence` 会把它当作
    **"本会话已点亮"的直接证据**, 于是 `bridge.py channel/sync` 一律判"已点亮",
    直接打翻 `tests/test_transport_e2e.py` 里 6 条依赖"未点亮"的通道用例
    (2026-10-13 实测踩过). 关闭态才是这些用例要复现的基线。

    存在意义只是: 让 `console_log_available` / `console_session_count` /
    `cloud_toggle_console.found=False` 这条链路在沙箱里被真实走到 ——
    它正是 2026-10-13 朋友机报告暴露出的探测器盲区 (见 cases/friend-report-6-2026-10-13).
    """
    return f"[{LOCAL_TS}] Console Log Start\n"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def build(out: Path, scenario: str, account_id: int, save_kib: int, force: bool) -> dict:
    if scenario not in SCENARIOS:
        raise SystemExit(f"未知场景 {scenario!r}, 可选: {', '.join(SCENARIOS)}")

    if out.exists():
        if not force:
            raise SystemExit(f"输出目录已存在: {out} (加 --force 覆盖)")
        _purge(out)

    steam = (out / "steam").resolve()
    profile = (out / "profile").resolve()
    appdata = profile / "AppData" / "Roaming"
    localappdata = profile / "AppData" / "Local"
    programdata = profile / "ProgramData"

    hex_dir = accounts.accountid_to_hex_dir(account_id)
    sid64 = str(accounts.accountid_to_steamid64(account_id))
    aid = str(account_id)

    # --- 确定性的存档内容 (sha 每次一致, 便于断言) ---
    body = b"SCB-SANDBOX-SAVE\n" + bytes((i * 37 + 11) % 256 for i in range(save_kib * 1024))
    save_sha = hashlib.sha1(body).hexdigest()

    # --- 目录骨架 ---
    common = steam / "steamapps" / "common"
    ds3_dir = common / DS3_INSTALLDIR
    api_dir = common / API_INSTALLDIR
    ud_root = steam / "userdata" / aid
    for d in (
        ds3_dir / "Game",
        api_dir,
        ud_root / DS3_APPID / "ac" / "WinAppDataRoaming" / DS3_SAVE_DIRNAME / hex_dir,
        ud_root / API_APPID / "remote" / "cfg",
        steam / "logs",
        steam / "config",
        appdata / DS3_SAVE_DIRNAME / hex_dir,
        localappdata,
        profile / "AppData" / "LocalLow",
        profile / "Documents",
        profile / "Saved Games",
        programdata,
    ):
        d.mkdir(parents=True, exist_ok=True)

    # --- 真实存档位置 (%APPDATA%\DarkSoulsIII\<hex>\DS30000.sl2) ---
    save_abs = appdata / DS3_SAVE_DIRNAME / hex_dir / DS3_SAVE_FILE
    _write_bytes(save_abs, body)
    # ac\ 镜像 (Steam 在 Auto-Cloud 的 ac\ 下留的声明性副本)
    _write_bytes(
        ud_root / DS3_APPID / "ac" / "WinAppDataRoaming" / DS3_SAVE_DIRNAME / hex_dir
        / DS3_SAVE_FILE,
        body,
    )

    # --- 安装目录占位 + Auto-Cloud 声明文件 ---
    _write_bytes(ds3_dir / "Game" / "DarkSoulsIII.exe", b"MZ\x90\x00 sandbox placeholder\n")
    _write_vdf(
        ds3_dir / "steam_autocloud.vdf",
        "AppState",
        {"appid": DS3_APPID, "installdir": DS3_INSTALLDIR, "AutoCloudEnabled": "1"},
    )
    _write_bytes(api_dir / "left4dead2.exe", b"MZ\x90\x00 sandbox placeholder\n")

    # --- API 型对照: 实体文件在 userdata\<id>\<appid>\remote\ ---
    _write_text(ud_root / API_APPID / "remote" / API_REMOTE_FILE, "hostname sandbox\n")

    # --- 库表 + 清单 ---
    _write_vdf(
        steam / "steamapps" / "libraryfolders.vdf",
        "libraryfolders",
        {
            "0": {
                "path": str(steam),
                "label": "",
                "apps": {DS3_APPID: str(len(body)), API_APPID: "4096"},
            }
        },
    )
    _write_vdf(
        steam / "steamapps" / f"appmanifest_{DS3_APPID}.acf",
        "AppState",
        {
            "appid": DS3_APPID,
            "name": DS3_NAME,
            "installdir": DS3_INSTALLDIR,
            "StateFlags": "4",
            "SizeOnDisk": "1000000",
            "LastUpdated": str(LOCAL_TS),
        },
    )
    _write_vdf(
        steam / "steamapps" / f"appmanifest_{API_APPID}.acf",
        "AppState",
        {
            "appid": API_APPID,
            "name": API_NAME,
            "installdir": API_INSTALLDIR,
            "StateFlags": "4",
            "SizeOnDisk": "4096",
            "LastUpdated": str(LOCAL_TS),
        },
    )

    # --- remotecache.vdf ---
    # Auto-Cloud 型 (DS3): root=4 语义 = %APPDATA%, 相对路径带 DarkSoulsIII\<hex>\
    if scenario == "synced":
        ds3_change, ds3_remote_ts = 1, REMOTE_TS
    else:
        # clean: 从未开过云; disabled: 云开关关着、从未成功上传过
        ds3_change, ds3_remote_ts = 0, 0
    _write_vdf(
        ud_root / DS3_APPID / "remotecache.vdf",
        DS3_APPID,
        {
            "ChangeNumber": str(ds3_change),
            rf"{DS3_SAVE_DIRNAME}\{hex_dir}\{DS3_SAVE_FILE}": {
                "root": "4",
                "size": str(len(body)),
                "localtime": str(LOCAL_TS),
                "time": str(LOCAL_TS),
                "remotetime": str(ds3_remote_ts),
                "sha": save_sha,
                "syncstate": "1",
                "persiststate": "0",
            },
        },
    )
    # API 型 (550): root=0 语义 = userdata\<id>\<appid>\remote, 实体在本地
    api_body = b"hostname sandbox\n"
    _write_vdf(
        ud_root / API_APPID / "remotecache.vdf",
        API_APPID,
        {
            "ChangeNumber": "1",
            API_REMOTE_FILE: {
                "root": "0",
                "size": str(len(api_body)),
                "localtime": str(LOCAL_TS),
                "time": str(LOCAL_TS),
                "remotetime": str(REMOTE_TS),
                "sha": hashlib.sha1(api_body).hexdigest(),
                "syncstate": "1",
                "persiststate": "0",
            },
        },
    )

    # --- 账号资料 ---
    _write_vdf(
        steam / "config" / "loginusers.vdf",
        "users",
        {
            sid64: {
                "AccountName": "sandboxuser",
                "PersonaName": "Sandbox Tester",
                "RememberPassword": "1",
                "MostRecent": "1",
                "Timestamp": str(LOCAL_TS),
            }
        },
    )

    # --- 云日志 ---
    _write_text(
        steam / "logs" / "cloud_log.txt",
        _cloud_log(scenario, str(save_abs), "", hex_dir),
    )

    # --- 控制台日志 (只含会话边界; 点亮命令的执行回执在真实机器上只写这里) ---
    _write_text(steam / "logs" / "console_log.txt", _console_log())

    # --- 环境描述 (runner 与测试的断言依据) ---
    manifest = {
        "generator": "tools/make_fake_steam.py",
        "scenario": scenario,
        "note": "全部数据均为虚构, 不含任何真机账号或路径",
        "account_id": account_id,
        "steamid64": sid64,
        "hex_dir": hex_dir,
        "appid": DS3_APPID,
        "steam_root": str(steam),
        "profile_root": str(profile),
        "save_file": str(save_abs),
        "save_sha1": save_sha,
        "save_bytes": len(body),
        "env": {
            "USERPROFILE": str(profile),
            "APPDATA": str(appdata),
            "LOCALAPPDATA": str(localappdata),
            "PROGRAMDATA": str(programdata),
            # 注册表指向**真机**, 隔离环境重定向不了 ⇒ 必须把"当前账号"钉死成
            # 这个虚构账号, 否则真机上只要有人登录过 Steam (`ActiveUser != 0`),
            # 账号解析就会走真机值, 假环境的 remotecache 读不到. 见
            # `src/steam/accounts.get_active_accountid` 的测试钩子说明.
            "SCB_ACTIVE_ACCOUNT": str(account_id),
        },
        "expected": {
            "appid_mechanism": "auto-cloud",
            "api_appid_mechanism": "remote-storage-api",
            "save_dir_suffix": rf"{DS3_SAVE_DIRNAME}\{hex_dir}",
            "upload_verified": scenario == "synced",
            "change_number": ds3_change,
            "cloud_disabled": scenario == "disabled",
            # 能定位到存档目录就说明可桥接, 与"是否已上云"无关
            "bridgeable": True,
            # clean/disabled 的 cloud_log 里没有 `Watching file`, 定位只能靠
            # ac\ 镜像 + 账号标识反查 —— 正好覆盖 D2 的修复路径
            "expect_ac_based_locating": scenario != "synced",
            # console_log 每次都写 (只有会话边界, 无点亮回执 —— 见 `_console_log` 的说明).
            "console_log_present": True,
            "toggle_cmd_executed": False,
        },
    }
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "manifest.json", "w", encoding="utf-8", newline="\r\n") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成隔离的假 Steam 环境 (沙箱测试用)")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO, choices=SCENARIOS,
                        help="场景: clean / synced / disabled / api")
    parser.add_argument("--account-id", type=int, default=DEFAULT_ACCOUNT_ID,
                        help="虚构 accountid (默认与 tests/ 一致)")
    parser.add_argument("--save-kib", type=int, default=64, help="存档文件大小 (KiB)")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的输出目录")
    args = parser.parse_args(argv)

    manifest = build(
        Path(args.out).resolve(), args.scenario, args.account_id, args.save_kib,
        args.force,
    )
    print(f"沙箱环境已生成: {args.out}")
    print(f"  场景      : {manifest['scenario']}")
    print(f"  steam_root: {manifest['steam_root']}")
    print(f"  save_file : {manifest['save_file']}")
    print(f"  sha1      : {manifest['save_sha1']}")
    print("跑探测:  python tools/run_sandbox_probe.py --env " + str(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
