# SteamCloudBridge

复用 Steam **现成的云存档通道**作为跨机器存档传输层, 并提供只读的云存全库扫描器.

> ⚠️ **非官方工具**. 与 Valve、FromSoftware 及任何游戏发行商无关.
> 使用前请自行备份存档. 部分游戏（尤其带多人/在线反作弊的）可能因存档异常或被服务器分析而**导致封号**, 风险自负.

---

## 这是什么

- **一个只读扫描器**: 枚举本机所有 Steam 云存, 告诉你哪个游戏用什么机制、哪些文件真的在被同步、云开关是否正常.
- **一个可移植的通用内核**: Steam 安装发现、账号换算、`cloud_log` 解析、`remotecache` root 标定 —— 全部**运行时探测**, 换机器不改代码、不加配置.
- **一个传输器**（魂三 `374320`）: 复用 Steam **现成的** Auto-Cloud 通道 —— 在启动游戏前把存档写进 Steam 已经在监视的路径, 让 Steam 自己上传; 另一台机同账号启动时自动下发.
- **一个文件夹传输层**（魂二 `335300`, v1.2.0）: 魂二**没有** Steam 云（见「已知的坑」#13）⇒ 改成把存档导出成一个带清单的包, 放进你自己的网盘同步目录 / U 盘, 换台机器再导入回来.

## 这**不是**什么

- 不是 Steamworks SDK 集成: DS3 这类游戏属 Auto-Cloud, **与 `ISteamRemoteStorage` 互斥**,
  往 `remote\` 写文件游戏根本不读.
- 不是存档修改器: 不解析、不修改存档内容, 只做同账号自己存档的字节级搬运.
- 不是开箱即用: 见下方「已知的坑」.

---

## 手敲命令（v1.0 的核心用法）

**从 v1.0 起，「往 Steam 控制台发命令」不再由程序自动完成 —— 由你来敲。**

原因很直接：自动发键靠"点输入行"的坐标配方，跟窗口尺寸 / 系统缩放绑得很死，
在别人的机器上实测 **4 次重试全部打空**。人就在键盘前时，敲一行花 10 秒，
既稳得多，也**绝不会把命令误发到别的应用**。

程序负责**把该敲的那一行打给你看**，并自己盯日志等结果；你敲完**不用重跑脚本**。

### 就两行

```
testappcloudpaths 374320      <- 第 1 行: 打开这个游戏的云通道 (别跳过)
cloud_sync_down 374320        <- 第 2 行: 让 Steam 从云端下载
```

**开控制台**：`Win+R` -> 输入 `steam://open/console` -> 回车。
（也可以点 Steam 左上角 `Steam` 菜单 -> `控制台` / Console。）

- ⚠ 控制台**打开了 != 输入行有焦点** —— 先用鼠标点一下窗口**最底下那一行输入框**再敲。
- ⚠ 敲对了**没有任何成功提示**，这是正常的（有输出反而说明打错了）。
- ⚠ **顺序不能反**：通道没开时 Steam 会直接拒绝下载，日志里写 `AutoCloud is disabled`。
- 自证第 1 行生效：在控制台敲 `find testappcloudpaths` —— 看到 `"374320"` = 生效；`"0"` = 没生效。

> 发行包里**唯一一份**面客说明：[`packaging/README.md`](packaging/README.md)
> —— 抄写卡、常见疑问表、封号问题的正面回答、"为什么不能装完一直生效"全在里面。

---

## 日常同步（`RUN-SYNC.bat`）—— 每次要玩之前双击一次

双击 = `daily --appid 374320 --manual --manual-wait 240 --launch`，它按顺序做四件事：

```
1) 检查游戏没在运行
2) 检查 Steam 在运行  (--start-steam 可以让它自己拉起)
3) 把第 1 行打给你看 -> 你敲 -> 它盯 console_log 等回执
4) 让 Steam 启动《黑暗之魂3》
```

然后正常玩；**退出游戏时 Steam 自己把存档同步上去**（这就是上传那半环）。

这就是"装完一直生效"的**等价做法**：第 2 层开关（游戏级）**没有本地持久化落点**
（`appmanifest_374320.acf` 里一个 cloud 字段都没有），它是 Steam 客户端的**会话级**
覆盖，重启 Steam 即失效。所以只能"每次开游戏前点亮一次"，本命令把这一步压成一次双击。

⚠ **退出游戏前不要重启 Steam**（会丢掉本次点亮）。真发生了就再双击一次 —— 点亮是幂等的。

```bash
# 只点亮、不启动游戏
python tools/bridge.py daily --appid 374320 --manual

# 自动发键优先, 失败自动转人工
python tools/bridge.py daily --appid 374320
```

---

## 快速开始

#### v0.1.1 修掉的 4 个缺陷（都由朋友机回传的真实报告暴露）

| | 缺陷 | 症状（实测样本） | 修法 |
|---|---|---|---|
| D1 | `root_base_paths` 从未传进 `probe_game`（形参在、调用点漏传） | `692850` 的 `cache_roots=["3"]`，而 root 3 已标定为 `%LOCALAPPDATA%` 79/79 命中，却仍判"未能定位存档目录" | 新增 `calibrated_root_bases()`，把**全局且非低置信**的标定回灌；单测锁 `per-appid`/low 两类必须被排除 |
| D2 | `steam_autocloud.vdf` 只用于选目标，不参与机制判定与存档定位 | `692850` 明明有 `<GameDir>\BloodstainedRotN\Saved\SaveGames\steam_autocloud.vdf`，仍判"机制未知 + 未定位" | marker 所在目录作为 `autocloud-marker` 候选并补位判 `auto-cloud`；**不夺权**（已判 API 型的不改判），且必须落在自己的安装目录内 |
| D3 | 存档目录被解析成**游戏安装目录本身** | `113200` / `4659620` 解析结果 = 安装根；`130` 解析成 `...\bshift`（3430 个 .wav/.mdl/.dll），而非 `...\bshift\SAVE` | ① 硬规则：候选等于安装根一律拒绝（宁可报未定位）；② 同级别候选里优先"目录名像存档目录"的 |
| D4 | 候选池不认"云端变更号已推进"，且 `∩ manifests` 剔除已卸载游戏 | `1245620` 艾尔登法环 `synced_change_number=23` 却没被探测 | 候选池补入 `synced_change_number > 0`；允许已卸载游戏（与解析器注释一致）；`ChangeNumber=0` 不算证据 |

附带修掉一个**归属误判**：`appid_from_path` 原先只做字符串前缀比较，于是
`common/Half-Life 2/hl2/save/...` 被算到 install-dir 为 `Half-Life` 的那个 appid 头上
（本机实测被算到 `10` Counter-Strike）。现改为路径分隔符边界匹配。

**本机前后对比（真实数据，零回归）**：`413150` Stardew Valley / `1126290` Lost 从"未定位"变为
正确定位（D1）；`4659620` 从"安装目录"变为 `...\th06nc\replay`，`113200` 由"安装目录"改为
拒绝（D3）；`2358720` Black Myth: Wukong 新增进探测，解析路径与朋友机同形（D4）。
解析成功数 14/17 → 17/18。

#### v0.1.2 修掉的缺陷（沙箱端到端测试暴露）

| | 缺陷 | 症状 | 修法 |
|---|---|---|---|
| D5 | `--account` 只决定"扫哪个 userdata 目录"，**身份标识仍取自注册表** | 手动指定账号时，报告里混进真机 accountid（`userdata_apps_by_account={"<真机 id>": []}`），被指定账号的游戏直接失联 | 身份标识与扫描目标统一以 `--account` 为准 |

D5 只在显式传 `--account` 时触发（两个一键 bat 都不传），对朋友测试无直接影响；
但它是**隔离漏洞** —— 用假环境跑端到端时把真机账号泄漏进了报告，必须堵。
发现方式见 `docs/sandbox-testing.md`：单测抓不到，只有端到端场景才会暴露。

#### v0.2.0 · 把搬运器做成朋友向发行包

v0.1.x 的包里只有**只读**探测器。v0.2.0 把 M2/M3 的**搬运器**也放了进去，
于是朋友那一端能自己完成"把一份 `.sl2` 放回正确位置（并推上云）"。

| 变化 | 说明 |
|---|---|
| 新增 `SteamCloudBridgeBridge.py` / `.exe` | 搬运器单文件版（328 KB / 8.75 MB），行为与 `tools/bridge.py` 逐字一致 |
| 新增 `RUN-BRIDGE.bat` | 双击即走交互向导：诊断 → 计划 → 输入 `YES` → 搬运 →（可选）上云 |
| 新增 `05-搬运器使用说明.md` | 含"后悔药"（备份 + 快照 + `rollback`）与退出码表 |
| 新增 `bridge.py wizard` | 交互流程本体在 `src/transport/wizard.py`，靠注入 `prompt/echo/invoke` 做到可单测 |
| **版本号收成单一来源** | `src/version.py`。此前探测器 `--version`／发行包名／搬运器各写一份，已经对不上过一次 |
| 单文件构建器支持多目标 | `--target probe\|bridge`；内嵌顺序改由 **AST 扫依赖 + 拓扑排序**决定，不再手工维护 |
| `_harden_console` 抽到 `src/console.py` | 搬运器的输出里也有 `DARK SOULS™ III`，同样会被 GBK 控制台干掉 |

**为什么向导写在 Python 里而不是 `.bat` 里**：一键脚本做不了"提问 → 判断 → 中止"，
`cmd` 的 `call`/`goto` 与编码都很容易踩坑；把流程写成纯函数后，整条交互路径能在单测里
跑完（19 项），而且向导走的每一步都是一次真实 CLI 调用 ⇒ **向导里能过的手打命令也一定能过**。

**顺带堵住一个文档错误**：`docs/m3-cloud-channel.md` 的退出码表把 `bridge.py` 与
`tools/steam_console_send.py` 两套码混着写，声称 `bridge` 会返回 `5`（"没抢到前台"），
而 `bridge` 从来不用 5（它把这类失败归成 `1` 并写明原因）。已更正。

单测 259 → **308**（其中 30 项是新增的**发行包不变量**：`.bat` 纯 ASCII / CRLF / `goto` 目标
齐全 / 只用相对路径、单文件清单必须覆盖全部 `src/*`、`win_dpi` 必须排在 `steam_console_send`
之前、各构建器的文件名与版本号必须互相对齐）。

#### v0.1.3 修掉的缺陷 + 新增验证器

| | 缺陷 | 症状（朋友机第二轮实测） | 修法 |
|---|---|---|---|
| D6 | 多个 appid 共用同一物理安装目录时，`%GAME_DIR_<appid>%` 标签归属错 | `70` Half-Life 的 `install_dir` 显示成 `%GAME_DIR_130%`（HL 与 Blue Shift 都在 `steamapps\common\Half-Life`） | **两层修**：① 脱敏表原来用 `set()` 去重，打乱了**同长度**替换项的先后（结果不确定），改为**保序去重**；② 全局替换表无法区分"这条 `install_dir` 属于谁"，改为**逐记录自优先** —— 走每条游戏记录时先套自己的 appid 标签 |

D6 只影响报告可读性（物理路径一直是对的），但根因是**脱敏结果不确定**，属于必须堵的一类。
两条回归单测：`test_shared_install_dir_keeps_own_label` / `test_redaction_is_deterministic`。

**新增：云接受性验证器（诱饵法）** —— `cloud_bait_check.py` + `RUN-CLOUD-BAIT.bat` + `04-云接受性验证.md`。

搬运器的全部前提是「**把文件放进存档目录后，Steam 云会把它当真实进度留下，而不是用云端旧版覆盖它**」。
这句话此前**没有任何机器验证过**。验证器往存档目录放一个**无害诱饵**（同目录同后缀、非存档本体、
游戏读不懂），启动一次 Steam，再看 `remotecache.vdf` / `cloud_log.txt` 是否登记了它 ——
据此判定 **ACCEPTED / LIKELY_ACCEPTED / PARTIAL / NOT_SEEN**。四步幂等，`plan` 全只读，
**不读不改真实存档**，跑完即清理。

单测 96 → **106**。

### 用法 0 · 打包交付给别人用 (v1.0.0 发行包)

不用装仓库、不用装依赖、**对方也不用装 Python** —— 这是给**非技术使用者**准备的形态:
拿到解压就能用, 不需要回传任何东西 (报告留在使用者自己机器上)。

```bash
python tools/build_single_file.py              # 1. 产出 dist/SteamCloudBridge{Probe,Bridge}.py
python tools/build_exe.py --python <py.exe>    # 2. 产出 build/pyi-dist/*.exe (需 PyInstaller)
python tools/build_release.py                  # 3. 产出 release/SteamCloudBridge-v1.0.0.zip
```

把 `release/SteamCloudBridge-v1.0.0.zip` 直接发出去。解压后目录长这样:

```
SteamCloudBridge-v1.0.0/
├─ RUN-PROBE.bat               ← 只读探测 (优先用 exe, 无需任何安装)
├─ RUN-PROBE-TARGET.bat        ← 4b 专项: 只扫 DS3(374320) + 艾尔登法环(1245620)
├─ RUN-CLOUD-BAIT.bat          ← 云接受性验证 (诱饵法, 这一步需要 Python 3)
├─ RUN-BRIDGE.bat              ← 搬运向导 (唯一会写盘的入口; 需输入 YES)
├─ RUN-SYNC.bat                ← v1.0.0 日常同步: 点亮本会话通道 + 帮你启动游戏
├─ RUN-CROSS-VERIFY.bat        ← v0.2.7 跨机往返 阶段2: 触发下行 + 比对字节 (不启动游戏)
│                                默认 --manual: 把要敲的两行打给你, 不碰 GUI
├─ SteamCloudBridgeProbe.exe   免装 Python 的只读探测器 (~9.3 MB)
├─ SteamCloudBridgeBridge.exe  免装 Python 的搬运器   (~8.8 MB)
├─ SteamCloudBridgeProbe.py    探测器源码版 (可读、可自行审计)
├─ SteamCloudBridgeBridge.py   搬运器源码版
├─ cloud_bait_check.py         诱饵验证器本体 (纯标准库)
├─ README.md                   ← 包里唯一一份说明: 怎么操作、手敲哪两行、出问题怎么办
├─ LICENSE
└─ VERSION.txt
```

**两个工具互相独立**：探测器纯只读；搬运器会写盘，但必须输入 `YES`、写前强制备份 + 拍
快照、Steam 运行中直接拒绝、跨账号直接拒绝。发出去之前可以先只发探测器那一半。

设计要点 (每条都是实测踩出来的, 有回归测试盯着):

- **exe 是首选路径** —— 朋友电脑通常没有 Python。`.bat` 的降级链是
  `SteamCloudBridgeProbe.exe` → `runtime\python.exe` (若打包了便携运行时)
  → 系统 `py -3` / `python` / `python3`。
- **onefile 下 `__file__` 指向 `%TEMP%\_MEIxxxx\`** —— 报告默认目录必须优先认
  `sys.executable`, 否则报告写进临时目录、进程一退出就被删, 用户看到的是
  "跑完了但什么都没生成"。
- **控制台编码必须容错** —— 中文 Windows 控制台是 GBK, 游戏名里的 `™`(U+2122)
  会让 `print` 抛 `UnicodeEncodeError` 直接把进程干掉、报告只写一半。修法是只把
  `errors` 设成 `replace`, **不动 `encoding`**(否则走 bat 时中文会乱码)。
- **`.bat` 内容是纯 ASCII + CRLF** —— 批处理解析器与非 ASCII 输出混用不可靠,
  中文说明一律放 `.md`。`goto` 目标齐全、只用 `%~dp0` 相对路径。
- **只发 `machine_profile_bundle.zip`** —— 它只含脱敏的两份文件。

### 用法 A · 单文件 (推荐给普通用户)

不想 clone 仓库就用这个。`dist/SteamCloudBridgeProbe.py` 是**单文件、纯标准库**:

```bash
python SteamCloudBridgeProbe.py                  # 自动挑有云存痕迹的游戏
python SteamCloudBridgeProbe.py --appid 374320   # 只查指定游戏
python SteamCloudBridgeProbe.py --list           # 只列可探测 appid
```

Windows 上也可以直接双击 `scripts/probe.bat`。

产物 (在 `reports/`):

- `machine_profile.json` —— **完整档, 只留本机** (含账号 ID 与绝对路径)
- `machine_profile.share.json` —— **脱敏档, 可直接公开**
- `machine_profile.md` —— 人读摘要

然后把它编译成这台机器可用的适配器:

```bash
python tools/gen_adapter.py --profile reports/machine_profile.json --out-dir adapters
```

详见 [`docs/portable-probe.md`](docs/portable-probe.md)。

### 用法 B · 全库扫描 (仓库版)

```bash
python tools/cloud_scan.py                  # 主安装 + 当前账号
python tools/cloud_scan.py --all-accounts   # userdata 下全部账号
python tools/cloud_scan.py --no-markers     # 更快 (跳过 steam_autocloud.vdf 扫描)
python tools/cloud_scan.py --stdout         # 同时打印 Markdown 到终端
```

输出到 `reports/`:

- `steam_cloud_scan_<时间戳>.md` —— 人读报告
- `steam_cloud_scan_<时间戳>.json` —— 结构化全量数据

**报告含账号 ID 与本机绝对路径, 已在 `.gitignore` 中排除, 请勿直接公开。**

### 扫描器会告诉你什么

| 章节 | 内容 |
|---|---|
| 1. 环境 | Steam 安装、游戏库、账号与 SteamID 三种写法换算 |
| 2. root 枚举标定 | 用真实文件存在性反推 `remotecache.vdf` 的 `root` 语义（含置信度） |
| 3. 概览统计 | 机制分布、云开关异常、同步失败统计 |
| 4. 全库清单 | 每个 appid 的机制 / 云文件数 / 云开关 / 同步进度 |
| 5. 实测文件清单 | `cloud_log` 的 Auto-Cloud **规则原文** + `Watching file` / `Persisting file` 落盘证据 |
| 6. DS3 专项 | 隐藏云开关、被监视文件集、本机 mod 加载点探测 |
| 7. 发现与风险 | 分级告警（强证据 / 弱提示） |

### 用法 C · 搬运器 (M2) + 云通道 / 同步 / 回滚 (M3)

```bash
# ⓪ [v0.2.0] 交互向导: 诊断 -> 计划 -> 输入 YES -> 搬运 -> (可选) 上云
#    朋友向发行包里的 RUN-BRIDGE.bat 双击后走的就是它
python tools/bridge.py wizard

# ① 只读诊断: 这台机器上该游戏的存档落点 / 云通道状态 (不改任何文件)
python tools/bridge.py status --appid 374320

# ② 搬运计划 (默认 dry-run, 只打印将发生什么)
python tools/bridge.py plan --appid 374320 --source D:\backup\DS30000.sl2

# ③ 真跑 (必须显式 --yes; 覆盖前自动备份, 并在写盘前拍快照)
python tools/bridge.py apply --appid 374320 --source D:\backup\DS30000.sl2 \
    --yes --backup-dir D:\scb-backup --journal D:\scb-backup\journal.jsonl

# ④ [M3] 本会话云通道点亮了吗 (每次重启 Steam 都会失效)
python tools/bridge.py channel --appid 374320
python tools/bridge.py channel --appid 374320 --enable

# ⑤ [M3] 触发上行同步 + 校验 + 失败重试 (不必启动游戏)
python tools/bridge.py sync --appid 374320

# ⑥ [M3] 出事退回去 (默认 dry-run)
python tools/bridge.py rollback --snapshot D:\scb-backup\374320__DS30000.sl2__123.snapshot.json --yes
```

**默认不改动任何文件**；`apply` 不带 `--yes` 会直接退出。

落点**只从机器自身证据反推**（`cloud_log` 的 `Watching file` > `remotecache` 条目 + root 展开 >
拒绝），因为账号目录的写法**因游戏而异**（DS3 用 SteamID64 十六进制，法环用十进制）。
不确定就拒绝，绝不自己拼目录名。写入前有 12 项守卫（Steam 是否在跑 / 跨账号 /
落点是否被解析到安装目录 / **本会话云通道是否点亮**…），任一 `block` 命中即拒绝。

**M3 的三件硬事实**（都有实测与回归测试）：

* **云通道是会话级状态** —— `testappcloudpaths` 重启即失效。旧的全文件统计会被上一会话的
  `Watching file` 行骗过（实测 `cloud_state=enabled` 而本会话其实没点亮），必须按
  `Client version:` 划出的会话边界判。
* **回滚不能靠删文件** —— 删本地会被云端灌回来；只有"快照时目标不存在"才允许删，
  且必须紧接着 `cloud_sync_up`、中间不能夹游戏启动。
* **上传会静默失败** —— `Upload Timeout` 且没有 `HTTP upload ... beginning`，重试常即成功，
  所以 `sync` 是"触发 → 校验 → 重试"。

详见 [`docs/m2-bridge.md`](docs/m2-bridge.md) 与 [`docs/m3-cloud-channel.md`](docs/m3-cloud-channel.md)。

### 用法 D · 魂二 (v1.2.0) —— **没有云**, 用文件夹搬

魂二 (`335300`) **不支持 Steam 云存档**: 没有 `steam_autocloud.vdf`、没有云端副本、
`cloud_log` 里只有一行 `Not running AutoCloud. No config data found`, 因此**没有云通道可点亮**
(第三节那两行对它无意义). 四条本机证据 + 商店页一致, 详见「已知的坑」#13.

**这条路不通, 也不打算绕** —— 云存档由游戏方决定, 第三方加不上, 本工具不改游戏、不注入.
所以对魂二换一条**用户可控的传输通道**(网盘同步目录 / U 盘), 这也是它的**最终方案**:

```bash
# ① 看存档在哪 (只读). 存档在 %APPDATA%\DarkSoulsII\<SteamID64 十六进制>\
python tools/bridge.py save-status

# ② 导出成带清单的包 -> 放进同步盘 / U 盘
python tools/bridge.py save-out --to "D:\OneDrive\魂二存档" --label pc-home

# ③ 在另一台机器上导回来 (默认 dry-run; --yes 才真写, 写前强制备份)
python tools/bridge.py save-in --from "D:\OneDrive\魂二存档" --dry-run
python tools/bridge.py save-in --from "D:\OneDrive\魂二存档" --yes
```

包格式 `scb-savepack/1`: `savepack.json`(逐文件 sha256) + `files/` + `先读我.txt`。
内核在 [`src/transport/ds2.py`](src/transport/ds2.py), 判据与魂三**同一套纪律**:

* **只用磁盘证据定位** —— 先在 `<APPDATA>\DarkSoulsII\` 下找能反解成 accountid 的目录;
  唯一一个就用它, 多个且无 `--account` 就**拒绝猜**. 都没有时**只有** `create=True`
  才按已验证规则(`accountid_to_hex_dir`)推导目录名 —— 推导**不落盘**, `mkdir`
  留给导入写入那一步 (所以 `locate()` 永远是只读的).
* **导入前五道闸** —— 包 sha256 逐文件复核 -> 账号核对(不一致直接拒) -> 游戏进程检查
  (`DarkSoulsII.exe`) -> **强制备份**目标目录 -> 原子写入 + 写后复校.
* **默认只报告不写盘** —— `save-in` 不加 `--yes` 就是 dry-run.

⚠ 两个真实世界的坑: `.co2`(Seamless Coop mod 存档) 默认**一起导出** —— 很多人的最新进度
其实在 `.co2` 里; 以及`.co2` **不进任何云**, 别指望它自己回来.

---

## 为什么它能在**别人的电脑**上跑

盘符、账号 ID、安装目录、mod 框架 —— 这些每台机器都不一样, 写死任何一项工具就只对
一台机器有效。所以分三层处理:

| 层 | 内容 | 可移植 |
|---|---|---|
| 机制 | Auto-Cloud 搬运对任何 Steam Auto-Cloud 游戏成立 | ✅ |
| 内核 | Steam 发现 / 账号换算 / 日志解析 / root 标定 | ✅ 全运行时探测 |
| 机器事实 | 盘符、账号、安装目录、注入点 | ❌ **现扫现得, 不写进代码** |

存档目录定位按可信度排序取证据: `cloud_log`（`Watching file` / `Persisting file` /
`Evaluating rule` 三级）> 该游戏自己的 `remotecache` 落点 > `remote\` 目录 >
`ac\` 语义 + 目录名精确匹配。
账号名只当**候选生成器**, 不当置信来源 —— 因为 `%APPDATA%` 下一个账号名会对应
十几个游戏目录, 拿它当置信度会把 DS3 的目录错认成 Stardew 的存档目录。

---

## 机制速览

Steam 并存两套**互斥**的云存实现:

```
A 型 · ISteamRemoteStorage   游戏调 API, 文件在 userdata\<id>\<appid>\remote\
B 型 · Auto-Cloud (UFS)      Steam 按声明同步游戏自己的目录, userdata 下只有 ac\ 骨架
```

判定「哪些文件真的在被同步」最可靠的不是 `Watching file`, 而是 Auto-Cloud 的**规则原文**:

```
[AppID 374320]     Evaluating rule 0 with root="WinAppDataRoaming" path="DarkSoulsIII" pattern="*.sl2" (recursive)
```

> `Watching file` 只在本机已有云端缓存时才打印。DS3 这类云从未通过的游戏启动阶段只报
> `Watching 0 files`, 真相要到退出阶段的规则评估才有。工具做三级降级:
> `Watching file` → `Persisting file` → `Evaluating rule`。

完整实测记录见 [`docs/how-steam-cloud-works.md`](docs/how-steam-cloud-works.md).

---

## 与社区方案的关系

这个领域已有很多成熟作品, 本项目只瞄准其中一格。完整调研见 [`docs/community-prior-art.md`](docs/community-prior-art.md)。

| 已有方案 | 传输层 / 机制 | 与本项目的关系 |
|---|---|---|
| [TanukiSharp/DarkSoulsCloudSave](https://github.com/TanukiSharp/DarkSoulsCloudSave) | Dropbox / Google Drive | 同为 DS3 专用, 但需额外账号授权; 本项目复用 **Steam 现成云** |
| [Fldicoahkiin/SteamCloudFileManager](https://github.com/Fldicoahkiin/SteamCloudFileManager) | 修改 `appinfo.vdf` 的 `ufs` 节 / Steamworks SDK | 最接近的对手: 可给任意游戏注入云配置, 但**未做 DS3 适配**, 且需 Steam 调试模式 (GPL-3.0) |
| [Ludusavi](https://github.com/mtkennerly/ludusavi) | Rclone / WebDAV / 各网盘 | 通用存档备份的事实标准; 若只需"跨机同步", 它 + WebDAV 是零开发量方案 |
| [datbird/blockslot](https://github.com/datbird/blockslot) | 自托管 Docker + S3 | 架构最像 (用 Steam launch option 拦截), 完成度更高 |

**本项目唯一站得住的交集**: DARK SOULS III × 复用 Steam 现成云 × drop-in 到已有的 `dllMods\`。

---

## 已知的坑

| # | 坑 | 说明 |
|---|---|---|
| 1 | 部分游戏的云开关**默认隐藏** | 例如 DARK SOULS III 需要在 Steam 控制台执行 `testappcloudpaths 374320` 才会出现云开关。该命令出自 **Valve 官方 Steamworks 文档**（云存"预发行测试"节，配套 `set_spew_level 4 4`），玩家社区将其复用于零售游戏 |
| 2 | 该开关**不持久** | 每次重开 Steam 都要重做 |
| 3 | Auto-Cloud 只同步**主档** | ✅ **已实测确证**: DS3 的规则是 `path="DarkSoulsIII" pattern="*.sl2" (recursive)` —— 伴生文件（`.co2` / `.ds3os`）确定不走云。目标机器上的陈旧伴生文件是否引发存档异常仍 `[未验证]` |
| 4 | Auto-Cloud 会**静默失败** | 日志里的 `detected incomplete sync after previous run`; 必须自带 sha 校验 |
| 5 | **入队 ≠ 上云，且别用日志判断** | `Persisting file ... to the cloud` 只表示本地登记进云缓存。判据是 `remotecache.vdf` 的 **`remotetime`**：`>0` 才真的上过云。**不要**用 cloud_log 的 `Successfully synced` —— 它是通用同步语句，上传路径里反而出现在 `Upload batch initiated` **之前**，Auto-Cloud 型（DS3）则从不打印它 |
| 6 | `Sync Disabled` **表示本次同步不带网络传输** | 会真的传的会话括号里带 `up`（实测另一账号的 730：`Starting sync (up,AC Exit,)`）；写成 `Sync Disabled` 的这次不会传。它是**每次同步的即时状态**，不等于「云功能被关闭」——后者只有 `AutoCloud is disabled` |
| 7 | **写入后必须刷新 mtime** | `Skipping un-modified file` 会跳过 mtime/size 未变的文件，`(full)` 全量同步也不在退出时机执行。⇒ 写入存档后**必须**更新 mtime，别用 `copy2`/`copystat` 保留源时间戳。（2026-10-05 曾把这条当作 DS3 未上云的**主因**，10-11 实测证明主因是账号级开关；本条降级为**次要约束**保留） |
| 8 | **上传有两层开关，且顺序不能反** | 第 1 层「账号级、持久」：设置 → 云服务 → 为此账号启用 Steam 云同步；第 2 层「游戏级、易失」：`testappcloudpaths <appid>`，**每次重启 Steam 都失效**。⚠️ 修第 1 层常要重启 Steam，而重启会清掉第 2 层 —— **先确认第 1 层已生效，再点第 2 层**。判定第 1 层是否生效：看有没有**任何**游戏发起过 `Upload batch initiated`（无需开游戏） |
| 9 | **开关点亮后不一定打印规则行** | 别只用「日志里还有没有 `AutoCloud is disabled`」或「有没有 `Evaluating rule`」判开关。实测 `testappcloudpaths` 生效后走 `AC Launch` 路径时**只打印** `Running AutoCloud on launch` + `Watching file` + `AutoCloud done. Watching N files`，**一条规则行都没有**。反过来，禁用期 Steam 根本不会进入文件枚举，所以**不会**有 Watching 行 —— 因此「最后一条禁用行之后出现 Watching / Running AutoCloud」就是开关已生效的可靠证据。（2026-10-11 修正：旧判据漏掉此类，曾把已点亮的 DS3 误报成 `game-toggle-off`） |
| 10 | **账号级判据是整机级的** | 「账号是否已启用云同步」只与**整机有没有游戏传上去过**有关，与本次探测了哪几个游戏无关。用 `--appid <单个游戏>` 探测时，若拿探测子集去算，会把健康账号误报成「账号级停摆」。另需排除 **AppID 7（Steam 自身）** —— 它传的是控制器配置/截图，与游戏云同步不是同一条链路（2026-10-05 实测停摆期间它仍在上传） |
| 11 | **`Upload OK` / `Delete OK` / `result OK` 三者语义不同** | `Upload OK for file <p>` = 该文件**传上去了**（唯一的上传成功证据）；`Delete OK for file <p>` = 云端那份**被删掉了**（相反语义）；`Upload complete, result OK` = 整批结束，**删除批也会打印**。实测 PEAK（3527290）：`Need to delete file` → `Delete OK for file` → `Upload complete, result OK` → `empty vector, deleting remotecache.vdf` —— 它**没有**云端副本却带着 `result OK`，所以判据必须落在 `Upload OK for file` 上。另外 `Upload complete in build list` 是 build-list 语句，**下载路径也会打印**，同样不能当上传成功 |
| 12 | 封号风险 | 部分游戏的存档会被服务器在线分析 |
| 13 | **别把"魂三有云"推广成"魂系都有云"** | **魂二（`335300`）不支持 Steam 云存档** —— 不是配置没开、也不是开关被藏起来，是游戏**本身**没有该功能：`localconfig.vdf` 里没有 `cloud/last_sync_state` 与 `autocloud/*`、`userdata/<acct>/335300/` 不存在、存档目录没有 `steam_autocloud.vdf`、`cloud_log` 只有一行 `Not running AutoCloud. No config data found`（PCGamingWiki 的 Steam Cloud 列也为空）。⇒ `testappcloudpaths 335300` 对它**没有任何意义**，别照着魂三的步骤让人白找开关。⚠ **且这一点改不了** —— 云存档是游戏方接进 Steam 的功能，第三方加不上，本工具也不改游戏/不注入 ⇒ 别再指望把魂二并进云通道那套。它走独立的"文件夹传输层"（**最终方案**）：见「用法 D」 |

---

## 项目状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| M0 | 侦察: 机制、链路、日志数据源 | ✅ 完成 |
| M1 | 通用内核 + 只读全库扫描器 | ✅ 完成（21 项单测） |
| **M1.5** | **便携机器探测 + 自动适配器生成 + 单文件发行版** | ✅ **已完成**（**96 项**单测、含**沙箱端到端**、单文件隔离自检通过） |
| **M2** | **Auto-Cloud 搬运器: 落点解析 + 前置守卫 + 计划 + 执行** | ✅ **已完成**（`src/transport/` + `tools/bridge.py`；单测 **49 项** + CLI 端到端 **12 项**）|
| | └ 落点解析 (`target.py`) | 三级降级：`cloud_log Watching file` > `remotecache` 条目 + root 展开（磁盘可证）> **拒绝**。**绝不自拼目录名**（账号目录形态因游戏而异）|
| | └ 前置守卫 (`guard.py`) | 11 项：`STEAM_RUNNING` / `SAME_FILE` / `CROSS_ACCOUNT`（读 `steam_autocloud.vdf` 归属账号）/ `TARGET_IS_INSTALL_DIR`（**D3 的形态**）/ `SOURCE_EMPTY` … 命中 `block` 即拒绝执行 |
| | └ 写入安全 (`autocloud.py`) | 备份 → 临时文件 + 原子替换 → **显式 `os.utime`** → 回读 sha1 校验。保留源文件旧 mtime 会让 Steam 判 `Skipping un-modified file`（改动永不上云）；**备份失败即中止**，不做没有退路的覆盖 |
| | └ 伴生文件报告 | 显式列出 `.co2` / `.ds3os` 与源档的新旧关系（4b 的真风险点）；M2 只报告不动手（清理属 M3）|
| | └ 默认 dry-run | `plan` 跑完存档**一字节没变**（有回归测试锁）；`apply` 必须 `--yes` |
| **M3** | **云通道守卫 + 快照/回滚 + 上传校验 + trigger-sync** | ✅ **已完成**（`src/transport/cloudchannel.py` / `snapshot.py` / `verify.py` / `trigger.py`；单测 **39 项** + trigger **25 项** + CLI 端到端新增 **18 项**）|
| | └ 云通道守卫 (`cloudchannel.py`) | **按 Steam 会话判**（边界 = `Client version:` 行）。旧的全文件统计会被上一会话的 `Watching file` 行骗过 —— 实测 `cloud_state=enabled` 而本会话其实未点亮。**未点亮时 Steam 连 `AutoCloud is disabled` 都不打**，故"本会话零活动"也是有效信号 |
| | └ 快照 / 回滚 (`snapshot.py`) | 写盘前自动拍快照（含 sha1/size/mtime + 云端登记 + 副本）。**回滚不靠删文件**：快照时目标存在就写回内容，不存在才允许删（并强制提示随后 `cloud_sync_up`、中间不能夹游戏启动）；无副本时**拒绝执行**而不是退化成删除 |
| | └ 上传校验 + 重试 (`verify.py`) | `Upload OK` / `Upload Timeout`（且无 `HTTP upload ... beginning` 即静默失败）/ `Skipping un-modified` + 云端有副本 = 已同步。**会话作用域**：只认最后一次 `Client version:` 之后的记录。最终权威仍是 `remotecache` 的 `remotetime > 0` |
| | └ 发键安全修复 | `SetForegroundWindow` 返回 1 **不代表**窗口真的到前台（实测踩过：前台是浏览器，盲发按键会打进别的应用）→ 改为 `focus_verified()` 回读确认，失败即**拒绝发送**（rc=5）|
| | └ M3 真机闭环 | ✅ **已闭环 (2026-10-12)**：`channel` 报"已点亮"、`sync` 一次成功判定 **`in-sync`**（本地==云端，幂等），`cloud_log` 出现完整 Auto-Cloud 上行流程；**存档 sha1/mtime 零变化**。证据与三个根因见 `cases/live-m3-2026-10-12/ANALYSIS.md` |
| | └ M3 **真上传** | ✅ **`Upload OK` 已验 (2026-10-12)**：用**不同内容**的 `.sl2` 走同一条链路，拿到 `HTTP upload ... beginning` + `Upload OK for file`（CN 4→5），随后 `Forget OK`（CN 5→6）清理。顺带查明 **Steam 去重按内容 sha，改 mtime 只会重新 persist**。证据见 `cases/live-m3b-realupload-2026-10-12/ANALYSIS.md` |
| | └ M3 **rollback 真机** | ✅ **已验 (2026-10-12)**：Steam 关闭下 `apply` 不同内容 → `rollback` 写回，`校验通过: sha1=<原值>`；全程 Steam 关闭 ⇒ 云端零风险 |
| | └ M3 判据修复 | ✅ `accountid` 探测失效（注册表 `ActiveUser` 常为 0）→ 云端副本假告警 + 跨账号红线**静默失效**；`Client version:` 懒惰 → 重启后**假报已点亮**。均已修 + 回归 |
| | └ 真档同路径替换 | ✅ **(2026-10-12 晚)**：用 `.co2`（**SeamlessCoop 存档**，非“原版伴生档”）做**同路径内容替换**，拿到真 `Upload OK`（CN 6→7），随后回滚 + 复位同步（CN 7→8），本地与云端都回到原档。见 `cases/live-m3d-real-save-2026-10-12/ANALYSIS.md` |
| | └ **[M3] trigger-sync**（写完即上云） | ✅ **已实现 (2026-10-12)**：`src/transport/trigger.py` 编排 `启动 Steam → 校验文件未被云端覆盖 → 点亮本会话通道 → 触发+校验+重试`；`src/steam/launch.py` 用 `DETACHED_PROCESS` 分离拉起（**不附加任何开关** —— `-offline`/`-offlinemode` 实测都被忽略）。CLI: `apply --then-sync` / `sync --start-steam --enable`。**为什么必须跨一道 Steam 启停**：`apply` 写盘要求 Steam **关闭**（防与云端仲裁抢文件），而上传要求 Steam **运行**，且**游戏级开关是会话级易失的** —— 新会话通道未点亮，直接发 `cloud_sync_up` 不生效。**新的安全检查**：起来后先比 sha1，被云端覆盖即中止（`rc=6`）而非误报成功。⏳ 真机端到端未执行（会覆盖真实存档 + 改云端 CN），留待真实迁移时跑 |
| | └ 4b-3 已闭环 | **✅ 本机等价复现通过 (2026-10-12 17:03)**：DS3 **原版启动**（`DarkSoulsIII.exe` 不导入 `dinput7.dll` ⇒ **不加载任何 mod**，`ds3sc_launcher.exe` 读的是 `.co2` 验不到 `.sl2`）→ **Steam UI 菜单切离线**（其余 4 种手段全无效）→ 主菜单「继续游戏」+「加载游戏」列出 **5 个角色**（等级 210/142/142/341/66）⇒ 游戏侧完整读档。全程零传输、云端零改动。证据 `cases/live-4b3-2026-10-12/ANALYSIS.md` |
| M4 | 实测验证 | 🔶 **4a / 4a' / 4a'' 全部闭环** |
| | 4a. DS3 的被同步文件集 | ✅ 规则实测为 `path="DarkSoulsIII" pattern="*.sl2" (recursive)` |
| | 4a'. 文件是否真的落到 Valve 服务器 | ✅ **账号级根因已解除**（2026-10-11） |
| | 4a''. DS3 端到端上传 | ✅ **已闭环**（2026-10-11 21:33）：`testappcloudpaths 374320` 点亮 → 存档（mtime 21:32:34）→ 退出 → `Starting sync (up,AC Exit,)` → `Persisting file ...DS30000.sl2 to the cloud` → `Upload batch initiated`（CN 0→1）→ `remotecache.remotetime = 1791725553`、sha1 与文件一致 |
| | 4a 复现. 被同步文件集（本机实测） | ✅ **本机复核确认只有 `DS30000.sl2`**（2026-10-12）：`AutoCloud done. Watching 1 files`，`.co2` / `.ds3os` 均不在列 |
| | 4b-1. 云认不认"外部放进来的文件" | ✅ **本机实测通过**（2026-10-12）：无害诱饵 → `Persisting file ... to the cloud` → `Upload OK for file`（CN 2→3）。`cloud_bait_check --mode inspect` 判定 **`ACCEPTED`**。**M2 的核心前提成立** |
| | 4b-2. 下行（云→本地）是否字节精确 | ✅ **本机实测通过**：删本地诱饵 → 启动期被云端恢复 → sha256 与上传前**逐字节一致** |
| | 4b-3. 跨机器只搬 `.sl2` 后 DS3 能否加载 | ✅ **本机等价复现通过**（2026-10-12 17:03）：**游戏侧**完整读档（5 个角色可见）；传输层由 Steam 负责，故本机复现已排除"游戏侧不认这份档"这一失败模式。见 `cases/live-4b3-2026-10-12/ANALYSIS.md` |
| | 4b 副产物 · 云控制台命令表 | ⭐ `cloud_sync_up` / `cloud_sync_down` / `cloud_sync_app <appid>` —— **不启动游戏也能驱动云同步**；`bCloudAcceptUnexpectedAutoCloudFileChanges=1` 就是"接受外部文件"的开关 |
| | 4b 副产物 · 回滚语义 | ⚠️ **删除不会自动同步到云端** —— 启动期的下行会把删掉的文件灌回来。正确做法：删本地 + `cloud_sync_up`，中间**不能夹一次游戏启动**。另实测到一次 `Upload Timeout` 静默失败，重试即成功 ⇒ M3 必须做上传后校验 |
| **v0.1.0** | **对外测试发行包**（`release/SteamCloudBridge-v0.1.0.zip`） | ✅ 已产出（8 文件 / **9.4 MB**，含**免装 Python 的 exe** + 一键 bat + 3 篇朋友向文档） |
| **v0.1.1** | **第二轮发行包**（`release/SteamCloudBridge-v0.1.1.zip`） | ✅ 已产出 —— 修掉朋友机回传报告暴露的 **4 个探测器缺陷**（D1–D4，见下），单测 80 → **90**；新增 `RUN-PROBE-TARGET.bat` 专项扫 DS3/艾尔登法环 |
| **v0.1.2** | **第三轮发行包**（`release/SteamCloudBridge-v0.1.2.zip`） | ✅ 已产出并被**朋友机实测验证**（2026-10-12）：`D1–D4 全部生效`，**28 探到 / 28 定位 / 0 拒绝**，定位来源全为实锤（remotecache 25 + cloud_log 3，猜测 0）|
| | └ D6（已修，v0.1.3） | 多 appid 共用安装目录时 `%GAME_DIR_<appid>%` 标签归属错（`70` HL 显示成 `%GAME_DIR_130%`）。根因比最初记录更深：脱敏表用 `set()` 打乱**同长度**替换项，且全局替换表无法区分归属 | ✅ 已修（保序去重 + 逐记录自优先）|
| | └ 新约束（M2 用） | **账号目录形态因游戏而异**（DS3 hex / ER·Sekiro dec）→ 搬运器必须以目标机 `remotecache.vdf` 实际落点为准，不许自拼 |
| **v0.1.3** | **第四轮发行包**（`release/SteamCloudBridge-v0.1.3.zip`） | ✅ 已产出（12 文件 / **9.15 MB**）：D6 修复 + **新增云接受性验证器**，单测 96 → **106** |
| | └ 云接受性验证器 | `cloud_bait_check.py` / `RUN-CLOUD-BAIT.bat` / `04-云接受性验证.md`。诱饵法回答搬运器唯一的赌注假设「**外部放进存档目录的文件，云认不认**」。四步幂等、`plan` 全只读、不读不改真实存档。判定 `ACCEPTED` / `LIKELY_ACCEPTED` / `PARTIAL` / `NOT_SEEN` |
| **v0.2.0** | **第五轮发行包：搬运器进包**（`release/SteamCloudBridge-v0.2.0.zip`） | ✅ 已产出（**16 文件 / 17.85 MB**）：新增 `SteamCloudBridgeBridge.py/.exe` + `RUN-BRIDGE.bat` + `05-搬运器使用说明.md`；单测 259 → **308** |
| | └ 交互向导 `bridge.py wizard` | 流程在 `src/transport/wizard.py`，纯函数 + 注入 `prompt/echo/invoke` ⇒ 19 项单测覆盖全部分支（没源文件 / 源文件不存在 / 计划被拦 / 未输入 YES / 上云开关 / apply 失败）。**每一步都是一次真实 CLI 调用**，向导里能过的手打命令也一定能过 |
| | └ 单文件构建器多目标化 | `--target probe\|bridge`；内嵌顺序由 **AST 扫真实 import + 拓扑排序**决定（循环依赖构建期报错），不再手工维护 —— 之前 `src.transport.*` 之间的 `from .guard import ...` 会在"按名字排序"下直接崩 |
| | └ 版本号唯一来源 | 新增 `src/version.py`；探测器 `--version` / 发行包名 / `VERSION.txt` / 搬运器 `--version` 全部读它（此前各写一份，已对不上过一次） |
| | └ 修正的一处文档错误 | `docs/m3-cloud-channel.md` 的退出码表把 `bridge.py` 与 `tools/steam_console_send.py` 两套码混写，声称 `bridge` 会用 `5`（"没抢到前台"）—— 实际 `bridge` 从不用 5，它把这类失败归成 `1` 并写明原因 |
| | └ 未执行的一步 | `apply --then-sync` 的**真机端到端**没跑（它会覆盖本机真实存档并改动云端 `ChangeNumber`）。复现命令见 `docs/m3-cloud-channel.md` §5 |
| **v0.2.1** | **第六轮发行包：朋友机回传修复**（`release/SteamCloudBridge-v0.2.1.zip`） | ✅ 已产出（16 文件 / **17.02 MB**）：修掉朋友机 v0.2.0 报告暴露的 **3 个面客缺陷**，单测 308 → **316** |
| | └ 缺陷 A：§3 让朋友跑他跑不了的命令 | 报告 §3 照抄了开发者侧 `python tools/gen_adapter.py ...`，但**发行包里没有 `tools/`**、朋友也没 Python。已改为明确标注"只有拿到源码的开发者需要执行，你不用跑" |
| | └ 缺陷 B：未脱敏完整档躺在报告根目录 | `machine_profile.json`（含账号 ID + 全部绝对路径）与可外发的 zip 同层并排 ⇒ 用户"压缩整个 reports"就会外泄。已改**目录分层**：根目录只放可外发的，完整档收进 `reports/private/`；根目录加 `00-发送说明.txt`；入口 `.bat` 的 `Do NOT send:` 同步 |
| | └ 缺陷 C：报告说不清跑的是 exe 还是 .py | `tool` 块新增 `entry` = `exe-onefile` / `exe-onedir` / `python-script`。⚠ `sys._MEIPASS` 在 onefile 和 onedir **都会设置**，只看 `hasattr` 会把 onedir 误报成 onefile，必须看解包目录形态 |
| | └ 同机回归对照 | v0.1.2（00:23）→ v0.2.0（20:33）同机同账号：376 行里**只 12 行变化且全部该变**（`70` HL 标签 `%GAME_DIR_130%`→`%GAME_DIR_70%` 确认已修；另 3 处是朋友真玩过游戏 ⇒ 反向证明探测链路是活的）。见 `cases/friend-report-2026-10-12/ANALYSIS.md` |
| | └ 跨机链路验证 | 用朋友回传的**脱敏档**在开发者侧跑 `gen_adapter.py` ⇒ **56 个适配器**全部生成。首次用真正的第三方脱敏档验证"脱敏后信息保真度足够" |
| **v0.2.2** | **第七轮发行包：exe 首验 + 报告头自洽**（`release/SteamCloudBridge-v0.2.2.zip`） | ✅ 已产出（16 文件 / **17.03 MB**）：单测 316 → **319** |
| **v0.2.3** | **第八轮发行包："报告里没有" ≠ "机器上没有"**（`release/SteamCloudBridge-v0.2.3.zip`） | ✅ 已产出（16 文件 / **17.03 MB**）：新增 `steam.app_manifest_appids` 已安装全量清单 + `installed_not_probed_*` 覆盖率字段 + 报告防误读提示；单测 319 → **324** |
| **v1.2.0** | **魂二（`335300`）：它没有云, 改用"文件夹传输层"**（`release/SteamCloudBridge-v1.2.0.zip`） | ✅ 已产出：① 用 **4 条独立本机证据 + 外部核对**确认魂二**不支持 Steam 云**（属性而非配置 ⇒ 写进 `GameProfile.steam_cloud=False`）；② 新增 `src/transport/ds2.py` + CLI `save-status` / `save-out` / `save-in` + 图形向导**第 5 页**；③ 包格式 `scb-savepack/1`（`savepack.json` 逐文件 sha256 + `files/` + `先读我.txt`）；④ 导入**五道闸**：包校验 → 账号核对 → 游戏进程检查 → **强制备份**（失败即中止）→ 临时文件 + `os.replace` + 写后复校；⑤ `locate()` **永远只读**（`create=True` 也只**推导目录名不落盘**, `mkdir` 留给导入那一步）；⑥ 端到端 **8 步**全绿（含跨账号 `rc=2` / 改包 `rc=2` / 游戏在跑 `rc=3`），证据 `cases/ds2-folder-transport-2026-10-14/`；单测 **472** |
| | └ 顺带修掉的一处"看着对其实不对" | `locate()` 原本在"磁盘上恰好一个账号目录"时**仍去读注册表**取本机账号 —— 白读一次，还在证据里留一行"取本机当前账号"，让人误判成**磁盘证据不足**。已改为只在候选 ≠ 1 时才回退（回归锁：该分支下 `get_active_account_id` 被调用即失败）；另新增一条提示：**调用方显式 `--account` 与磁盘唯一候选冲突时按磁盘走、但必须说出来**，否则用户以为是自己指定的账号生效了 |
| | └ 两个真实世界的坑 | ① `.co2`（Seamless Coop mod 存档）**默认一起导出** —— 实测本机 `.co2` 比 `.sl2` 新，最新进度常在它里面；② `.co2` **不进任何云**，别指望它自己回来 |
| **v1.1.0 – v1.1.2** | **图形向导 + 交付面收口**（`release/SteamCloudBridge-v1.1.2.zip`） | ✅ 已产出：新增 windowed 图形向导（`src/gui/` + `RUN-GUI.bat`），界面层**只渲染**、有副作用的动作全在 `src/gui/service.py`（该层被测试锁死**不许 import tkinter**）；打包布局把杂项收进 `support\`；面客文档由 8 篇编号文档合并为**单一** `packaging/README.md`；`build_release` 只收 `DOC_FILES` / `BAT_FILES` **清单内**的文件（清单外**只警告不打包**）—— 踩过"往展开目录里解压了上一版 zip，旧 `rglob('*')` 把整份副本打进新包，只有体积能看出来（17.9 MB → 35.9 MB）" |
| **v1.0.0** | **首个正式版：手敲优先 + 日常同步入口**（`release/SteamCloudBridge-v1.0.0.zip`）| ✅ 已产出：① 跨机验证的**3 个调用点全部**改为 `--manual --manual-wait 240` —— **默认不再碰 GUI**；② 人工接管提示**固定给两行**（`testappcloudpaths` 先、`cloud_sync_down` 后）—— 修掉一个会让对方**白跑一整轮**的洞（P5 点亮闸门在「已点亮」判假阳性时会被整段跳过，而旧提示只给下行 ⇒ 被 `AutoCloud is disabled` 拒掉；这正是此前两轮真机的真实死因）；③ 新增 **`daily` 子命令**与 **`RUN-SYNC.bat`**：双击一次 = 点亮本会话通道 + 让 Steam 启动游戏，即「装完一直生效」的**等价做法**（第 2 层开关**无本地持久化落点**，已查证）；④ 新增 `07-手动敲命令-操作卡.md`（抄写卡 + 封号正面回答）与 `08-日常同步-使用说明.md`；⑤ 版本号 0.2.9 → **1.0.0**（`src/version.py` 单一来源，`src/__init__.py` 一并归位）；`06-*` 口径翻转为「手敲优先」| 
| **v0.2.9 · 附** | **本机真机验证：`cloud_sync_down` 真的会下载**（测试 A，2026-10-13 20:57） | ✅ 已实测（证据 `cases/live-sync-down-2026-10-13/`）：全仓此前 **0 条**由 `cloud_sync_down` 产生的 `Download OK for file`，README 里那句"等价于游戏启动时的 on-launch sync"只来自控制台自带说明，属**推断**。本轮把本地档做成"不存在"（改名 `.scbhold`，**后缀不匹配云规则 `*.sl2`**）后**只发一条 `cloud_sync_down 374320`、全程不启动游戏**，Steam 如实走完 `Starting sync (AC Launch,down,)` → `(ValidateCache) … missing from disk` → `Need to download file` → `HTTP download … - Success.` → `Download OK for file` → `Successfully synced to ChangeNumber 11`；落盘 sha1 `06198b2b…` 与契约**逐字节一致**，`xfer-verify` **一次触发**判定 `pass`（`down.mode=auto`）。⇒ 跨机测试的**唯一驱动动作**已证明可用。关键设计：起点必须是"本地不存在"——因为 `cloud_sync_down` 就是 on-launch sync（**双向评估**），而当时本地档比云端**新**，直接跑有反向上传覆盖云端的风险。附两点新发现（**均未改代码**）：① `cloud_log.txt` 会在 ~1 MiB 处**自动轮转**成 `cloud_log.previous.txt`（本次真机撞上，`steam.exe` 未重启），新档里没有 `Client version:` 会话边界行 ⇒ `detect_channel()` 退回全文件判定；② 同类判据缺口：`testappcloudpaths 0` 关掉通道后 `detect_channel()` 仍因"更早的活动"报"已点亮"（影响面仅限手动关通道之后，朋友机正常流程不产生 `= "0"` 回执，故不影响他跑）。真实存档测试前后已完整复原（双份备份 + 逐文件 sha256 + `os.replace` 回位），云端 `ChangeNumber` 全程不变 |
| **v0.2.9** | **第十四轮发行包：修掉"云通道已点亮"的假阳性**（`release/SteamCloudBridge-v0.2.9.zip`） | ✅ 已产出：朋友机回传的报告显示 `verdict=timeout`，日志里同时出现 `Starting sync (AC Launch,Sync Disabled,)` 与 `AutoCloud is disabled` —— 而报告却写着**本会话通道: 已点亮**。复现后确认是**判据缺陷**：`ACTIVE_MARKERS` 收了 `Starting sync` / `Running AutoCloud` / `Watching file` / `AutoCloud complete`，注释还写着"禁用期不会出现任何一个" —— **那句是错的**，拿本仓基线日志实测，一个**禁用态** burst 会把这些**一条不少地照打**。`xfer-verify` 的 P5 因此 `lit is True` ⇒ **跳过点亮** ⇒ `cloud_sync_down` 被 Steam 拒掉（`AutoCloud is disabled`）⇒ 整轮白跑。修法：①只有**会传输**的同步才算活动证据（`Starting sync` 的标记位含 `down`/`up`，或 `Attempting to sync` / `Successfully synced to ChangeNumber` / `Upload·Download OK for file` 等真传输行）；②`Running AutoCloud` / `Watching file` / `AutoCloud done` / `Persisting file` / 中性的 `Starting sync (init,eval,)` 一律**不作证据**；③禁用证据（`AutoCloud is disabled` / `Sync Disabled`）与点亮的控制台回执**按时间戳比谁更晚**，同秒或无法比较时按**未点亮**（代价不对称：多点亮一次是幂等的，少点亮一次整轮白跑）。附带：点亮的"自动键入失败"也**转人工接管**（打印 `testappcloudpaths <appid>` 并等回执行，**不必重跑**，与下行那步同一套约定）；回传包新增 `cloud_log_session_appid.txt`（带**本会话该 appid 的全部行**，否则下一轮还是只能靠复现猜）；`tool.entry` 不再硬编码占位串。单测 367 → **377** |
| **v0.2.8** | **第十三轮发行包：自动发键失败 ≠ 任务失败（转人工接管）**（`release/SteamCloudBridge-v0.2.8.zip`） | ✅ 已产出：真机上 `xfer-verify` 卡在最后一步 —— 账号核对 / 游戏未运行 / 干净起点 / 本会话通道已点亮**全绿**，但"点控制台输入行 → 键入"这套坐标配方 **4 次重试全打空**（对方窗口尺寸/DPI 与本机不同），工具按设计**拒绝回车**（`送达: 否`），整轮白跑。两条修法：① 落点改为**竖直阶梯** `_Y_RATIOS`（4 档比例，覆盖不同窗口高度）；② 自动失败 ⇒ **转人工**：打印要敲的那一行 + 加长等待窗口，工具**继续自己看日志**出报告（判定与报告 schema 一个字不改，所以旧 `expected.json` 照用）；新增 `--manual`（完全不碰 GUI）与 `--manual-wait`；失败时打印**窗口尺寸 / DPI / 输入行墨迹量**诊断。单测 364 → **367** |
| **v0.2.7** | **第十二轮发行包：跨机往返（"云端 → 另一台机器"这段）可全自动验证**（`release/SteamCloudBridge-v0.2.7.zip`） | ✅ 已产出：新增两个子命令 —— `xfer-push`（发起侧：真写档 + 推上云 + 产出 `expected.json`）与 `xfer-verify`（对方机器：核对账号 → 点亮通道 → 发 `cloud_sync_down <appid>` → **逐字节**比对 → 出报告）。**全程不启动游戏**（依据：控制台 `cloud_sync_down` 等价于游戏启动时的 on-launch sync，证据见 `cases/local-4b-2026-10-12/ANALYSIS.md` §四），所以不会碰到存档内容。判据两条同时成立才算通过：本地 sha1 == `expected.json` 的 sha1，且 `cloud_log.txt` 本会话新增行出现 `Download OK for file <rel>`；`Currently already synced ... nothing to download` **单独报出来且不算通过**。`expected.json` 是外部输入 ⇒ 做了路径穿越防护 + schema/字段校验（归一化**之前**先判绝对路径）。**游戏在跑时硬拒绝**（`rc=3`，不靠说明书提醒：游戏自己也在跑 Auto-Cloud，会抢同一个文件）。新增 `RUN-CROSS-VERIFY.bat` + `06-跨机往返-朋友操作说明.md`。另加**零写入模式** `xfer-push --from-cloud`：不写档、不上传、不碰 Steam，只读 `remotecache.vdf` 里那一行登记，直接声明**云端已有的副本** ⇒ 只想验下行时**不必**把云端换成测试档、**不必**复位（`remotetime<=0` 时 `rc=1` 拒绝：声明不了不存在的东西）。单测 331 → **364** |
| **v0.2.6** | **第十一轮发行包：让测试者能自己看见开关状态**（`release/SteamCloudBridge-v0.2.6.zip`） | ✅ 已产出：新增读取控制台变量 `testappcloudpaths` 的**当前值**（即 `find testappcloudpaths` 的输出；`0` = 未点亮）—— 点亮命令成功时**不打印**任何提示语、只回显一行变量赋值，所以"看着没反应"完全可能已生效；`disabled` 判定拆成**三路**（值 = 本 appid ⇒ 开关已开、**别重启**直接进游戏 / 值 ≠ 本 appid ⇒ 未点亮、给命令 / 值读不到 ⇒ 给出自证命令 `find testappcloudpaths`）；复用既有原语 `cloudchannel.console_var_value`（此前定义但从未被探测器使用）；单测 329 → **331** |
| **v0.2.5** | **第十轮发行包：点亮命令"执行过没有"可判定**（`release/SteamCloudBridge-v0.2.5.zip`） | ✅ 已产出：探测器新增读取 `console_log.txt`，报告给出 `cloud_toggle_console`（`found`/`count`/`last_line`）+ `capabilities.games_toggle_cmd_executed|missing`；`disabled` 判定拆成"命令从未执行过"与"执行过但之后仍无活动（多为执行后又重启了 Steam）"两种可操作文案；单测 326 → **329** |
| | └ 为什么要改 | 第十轮朋友机报告（00:12）里 `disabled_count` 由 2 **升到 4**（Steam 又评估了两次），但报告只有 `cloud_log` ⇒ **"朋友到底敲没敲 `testappcloudpaths 374320`"无从回答**，整轮分析只能靠猜。根因：该命令是**控制台变量赋值，回执只写 `console_log.txt`**，探测器此前完全不读它 |
| | └ 附带清掉一处历史陷阱 | 项目文档（`docs/how-steam-cloud-works.md` §两层开关）早已写明正确顺序是**先账号级、后游戏级，两步做完再进游戏；中途重启 Steam 会把游戏级清掉** —— 而上一轮给朋友的口头步骤恰好是"先敲命令、再重启 Steam"，正好踩中该陷阱。v0.2.5 把正确顺序直接写进判定文案 |
| **v0.2.4** | **第九轮发行包：别把人指错路**（`release/SteamCloudBridge-v0.2.4.zip`） | ✅ 已产出：`cloud_state == "disabled"` 时，`未能定位存档目录` 不再提示 `--all-accounts`（无效指路），改为指向根因 `testappcloudpaths <appid>` + 启动一次游戏；两条原因**顺序调换**（根因在前）；单测 324 → **326** |
| | └ 为什么要改 | 第九轮朋友机报告里，DS3 首次出现 `AutoCloud is disabled`（`disabled_count=2`）⇒ `cloud_state` 由 `unknown` 变 **`disabled`**、`never_synced` 由弱提示（`no_change_advance`）升级为**强证据**。此时本机**连 `userdata\<acct>\374320` 目录都不存在**，`--all-accounts` 也扫不出来 ⇒ 原文案把用户往死路上引 |
| | └ `entry` 拿到第一个实机证据 | 朋友第三轮回传（v0.2.1，21:38）标着 **`entry=exe-onefile`** ⇒ 首次证明**发行版 exe 在陌生机器上真被跑起来**（此前只证明"本机干净目录能跑"，无法证明朋友会用它、`.bat` 降级链会被走到）。⚠ 仍不能据此断定该机没装 Python |
| | └ 修正：报告头与 `entry` 自相矛盾 | v0.2.1 的报告头对 exe 运行仍写 `系统: Windows 11 / Python 3.13.14`，会被读成"这台机器装了 Python"——**而 exe 存在的意义正是不用装 Python**。已改为 exe 场合写 `内置 Python x.y.z (exe 自带)`，并新增「运行方式」行让人读报告也能看到 `entry`。未知/缺失的 `entry` 一律按最保守的"Python 脚本"处理 |
| | └ 脱敏审计首次做全 | 本轮包内同时含同一轮的完整档与脱敏档 ⇒ 可做真实泄漏审计：完整档 454 条绝对路径，**0 条**泄漏进脱敏档；包内 `share.json`/`md` 与目录内容一致。唯一残留的 `C:\ProgramData` 是固定系统根、不含用户信息，判定非泄漏。见 `cases/friend-report-2026-10-12b/ANALYSIS.md` |
| | └ 三轮同机回归 | v0.2.0 → v0.2.1：**21 行差异全部可解释**（v0.2.1 的 §3 受众修正 + Sekiro 措辞修正实测生效 + 悟空存档自然增长）；appids 清单逐字节一致 |
| | └ ⚠ **已修正的错误结论** | 初版这里写"朋友机**没有 DS3**（374320 零命中）"，**是错的**。真实情况：DS3 装了但**在该机没有任何云证据**（手柄配置 `Steam Controller Configs/493102335/config/374320/controller_ps4.vdf` 证明它被运行过）。错因是档案当时**不记已安装 appid 清单**，所以"报告里没有"被我读成"机器上没有" ⇒ v0.2.3 修掉 |
| M5 | 单文件 exe 打包（免装 Python） | ✅ **已完成** —— `tools/build_exe.py`（**v0.2.0 起支持双目标**），PyInstaller 6.21.0 / onefile / `--noupx`（降误报）；已在干净目录实测跑通 |

**已实测可用**: 只读扫描与机器探测（M1 / M1.5）—— 换机器无需改代码, 产出脱敏档可回传。
**已实测可用（2026-10-11）**: DS3 的云上传链路端到端跑通（4a''）—— 存档真的落到了 Valve 服务器
（`remotetime` 0 → 1791725553、`ChangeNumber` 0 → 1、sha1 与本地文件一致）。
**已实测可用（2026-10-12）**: 云**传输层**两个方向都跑通 —— 外部放进存档目录的文件会被云端接受并上传
（4b-1），云端也能把文件原样灌回本地（4b-2）；且拿到了手动驱动云同步的控制台命令
（`cloud_sync_up/down`，**不必启动游戏**）。见 `cases/local-4b-2026-10-12/ANALYSIS.md`。
**已可运行（2026-10-12）**: **M2 搬运器**（`tools/bridge.py`）—— 落点解析 / 前置守卫 /
dry-run 计划 / 带备份的原子写入。默认不动任何文件；写入前 11 项守卫，跨账号与
"把安装目录当存档目录"这两类红线上有专门拦截。见 [`docs/m2-bridge.md`](docs/m2-bridge.md)。
**已实测可用（2026-10-12 · 4b-3 闭环）**: **4b-3 通过** —— 把 `.sl2` 放到存档路径后，
**DS3 游戏侧能完整读取**：原版启动 → 主菜单「继续游戏」→「加载游戏」列出 5 个角色
（等级 210/142/142/341/66 + 时长 + 所在地）。全程零传输、云端零改动（`ChangeNumber` 保持 8）。
⇒ 四个里程碑（M1/M1.5/M2/M3/M5）之外，**4b-3 这一未知项也已关闭**。
见 `cases/live-4b3-2026-10-12/ANALYSIS.md`。
**4b-3 的范围（原判定已收窄）**（2026-10-12 静态只读证据，见 `cases/ds3-save-structure-2026-10-12/ANALYSIS.md`）：
三件套同为 `BND4` 容器 + 全高熵加密数据区；`co2`/`ds3os`（**实为 SeamlessCoop 族的 mod 存档**，非原版伴生档）的 mtime 停在 **6 月 / 4 月**
（DS3 半年不写它们）⇒ **本机本身就是"伴生文件远旧于主档"的活样本且游戏正常**，
原先认定的"伴生文件 = 最大风险点"**降级**；三件套内**无明文 SteamID64/accountid**
（账号信息在加密区）⇒ 跨账号搬运在游戏侧几乎必然失败，`CROSS_ACCOUNT` 红线有**机制性依据**。
**仍未验项**：真·第二台物理机器（朋友机）上的完整往返 —— 但**下行半环的机制已于 2026-10-13 本机实测确证**
（只发一条 `cloud_sync_down 374320`、**不启动游戏**即真实下载，落盘 sha1 与契约逐字节一致；
见 `cases/live-sync-down-2026-10-13/ANALYSIS.md`），剩下的只是**环境**验证。
**已补全**：`[M3] trigger-sync`（`apply --then-sync` / `sync --start-steam --enable`）——
写完存档自动拉起 Steam、点亮本会话通道、触发并校验上传；**32 项新增单测**。
**已补全**：v0.2.0 把搬运器做成朋友向发行包（`RUN-BRIDGE.bat` + 交互向导 + `05-*`），
v0.2.x 线**新增 115 项单测**（向导 19 + 发行包不变量 34 + 报告布局/受众 8 + 已安装清单 4 + 点亮回执 3 + 开关值自证 2 + 跨机往返 37 + 通道判据 8）；v1.0.0 再**新增 8 项**（日常同步沙箱 3 + 日常入口 `.bat` 不变量 5）；**全套件总计 384**。
真机端到端那一步未执行（会覆盖真实存档并改动云端 `ChangeNumber`），复现命令见
[`docs/m3-cloud-channel.md`](docs/m3-cloud-channel.md) §5。
**关于云上传的诚实说明**: 2026-10-05 本机上传整体停摆（账号级），2026-10-11 开启账号级云同步后
已恢复（11 个游戏 appid 发起上传批次）；DS3 补点游戏级开关（`testappcloudpaths 374320`）
后于 21:33 首次上传成功。**注意游戏级开关每次重启 Steam 都会失效**，换会话要重新点亮。
工具不假装可用：每个游戏的 `cloud_blocker` 会明确指出当前被**哪一层**挡住，
整机层有 `steam_cloud_account_upload_alive`。

---

## 仓库结构

```
SteamCloudBridge/
├─ src/
│  ├─ vdf.py                   KeyValues/VDF 只读解析器 (含 Windows 路径转义处理)
│  ├─ console.py               控制台编码加固 (只改 errors 不改 encoding; 探测器/搬运器共用)
│  ├─ version.py               **版本号唯一来源** (--version / 发行包名 / VERSION.txt 都读它)
│  ├─ steam/
│  │  ├─ discovery.py          注册表 + libraryfolders + appmanifest + ac 根语义
│  │  ├─ accounts.py           账号发现 + SteamID/accountid/hex 换算
│  │  ├─ cloud_log.py          cloud_log.txt 解析 (云文件集真相来源)
│  │  ├─ remotecache.py        remotecache.vdf 解析 + root 枚举实机标定
│  │  ├─ localconfig.py        localconfig.vdf 云设置
│  │  ├─ processes.py          steam.exe 进程事实 (启动时间 = 会话边界兜底)
│  │  └─ launch.py             分离启动 Steam (trigger-sync 用)
│  └─ transport/               **搬运层 (M2 + M3)**
│     ├─ target.py             落点解析 (cloud_log > remotecache > 拒绝)
│     ├─ guard.py              前置守卫 (Steam 在跑 / 跨账号 / D3 形态 / 云通道 …)
│     ├─ plan.py               搬运计划 (纯计算, 默认不落盘)
│     ├─ autocloud.py          执行器 (备份 → 原子写 → 刷新 mtime → 校验)
│     ├─ cloudchannel.py       [M3] **会话级**云通道判定 + 点亮/同步命令
│     ├─ snapshot.py           [M3] 快照与回滚 (回滚**不靠删文件**)
│     ├─ verify.py             [M3] 上传结果判定 + 触发重试
│     ├─ trigger.py            [M3] **trigger-sync 编排** (启动 Steam → 防覆盖校验 → 点亮 → 上传)
│     ├─ crossmachine.py       [M6] 跨机往返 (xfer-push / xfer-verify 的判定内核)
│     ├─ ds2.py                [v1.2.0] **魂二文件夹传输层** (魂二无云 ⇒ 导出/导入带清单的包)
│     └─ wizard.py             [v0.2.0] **交互向导流程** (纯函数 + 注入 prompt/echo/invoke, 可单测)
│  └─ gui/                     **图形向导层 (v1.1.0)**
│     ├─ service.py            **后端层**: 体检 / 备份 / 点亮 / 启动 / 魂二 全靠它 (被锁死**不许 import tkinter**)
│     ├─ app.py                窗口与页面 (界面线程**只渲染**, 有副作用的动作一律委托 service)
│     ├─ widgets.py            卡片 / 提示条 / 状态行 / 命令框 / 步骤条
│     └─ theme.py              字体探测 + DPI 缩放
├─ tools/
│  ├─ bridge.py                **CLI: status / plan / apply / channel / sync / rollback / daily / save-status / save-out / save-in / xfer-push / xfer-verify / wizard**
│  ├─ bridge_gui.py            GUI 入口 (windowed; `--smoke` 只建界面就退)
│  ├─ cloud_scan.py            CLI: 只读扫描全库云存 → Markdown / JSON
│  ├─ machine_probe.py         CLI: 便携机器探测 → 完整档 + 脱敏档 + 摘要
│  ├─ gen_adapter.py           CLI: 探测档 → 每机器/每游戏适配器 (JSON + YAML)
│  ├─ cloud_bait_check.py      诱饵验证器: 云认不认"外部放进来的文件"
│  ├─ win_dpi.py               **进程 DPI 感知 (per-monitor v2)** —— 必须在任何 GDI 调用前 import
│  ├─ steam_console_send.py    驱动 Steam 控制台 (ctypes 直调 user32; 验前台 + 点输入行 + **墨迹校验后才回车**)
│  ├─ steam_console_click.py   独立的"点控制台 + 键入"小工具 (脚手架, 已被 send_command 覆盖)
│  ├─ steam_game_quit.py       给游戏进程发退出键 (Alt+F4)
│  ├─ win_screenshot.py        纯 ctypes 窗口截图 (沙箱里 Add-Type 被拦时的替代)
│  ├─ make_fake_steam.py       造隔离假 Steam 环境 (沙箱测试用)
│  ├─ run_sandbox_probe.py     假环境端到端跑探测器并断言
│  ├─ build_single_file.py     打包单文件发行版 (**多目标**; 内嵌顺序由 AST 拓扑排序决定)
│  ├─ build_exe.py             打包**免装 Python** 的单文件 exe (PyInstaller onefile, 双目标)
│  └─ build_release.py         组装对外发行包 (packaging/ + 单文件 + exe → release/*.zip)
├─ dist/
│  ├─ SteamCloudBridgeProbe.py 单文件只读探测器 (自动生成, 勿手改)
│  ├─ SteamCloudBridgeBridge.py 单文件搬运器 (自动生成, 勿手改)
│  └─ SteamCloudBridgeGUI.py   单文件图形向导 (自动生成, 勿手改)
├─ build/                      exe 构建中间产物 (gitignore)
│  └─ pyi-dist/SteamCloudBridge{Probe,Bridge,GUI}.exe   免装 Python 的发行产物
├─ packaging/                  **朋友向**发行包素材 (源码, 随版本走)
│  ├─ RUN-GUI.bat              [v1.1.0] **日常入口** —— 双击打开图形向导
│  ├─ RUN-PROBE.bat            一键探测 (纯 ASCII + CRLF, 只用 %~dp0 相对路径)
│  ├─ RUN-PROBE-TARGET.bat     专项探测 DS3 / 艾尔登法环
│  ├─ RUN-CLOUD-BAIT.bat       云接受性验证 (诱饵法)
│  ├─ RUN-BRIDGE.bat           [v0.2.0] 搬运向导 (唯一会写盘; 需输入 YES)
│  ├─ RUN-CROSS-VERIFY.bat     [v0.2.7] 跨机往返 阶段2 (在对方机器跑; 手敲优先)
│  ├─ RUN-SYNC.bat             [v1.0.0] 日常同步 (点亮本会话通道 + 启动游戏)
│  └─ README.md                **唯一一份面客说明** (原先 01~08 八篇合并于此)
├─ scripts/
│  ├─ probe.bat                Windows 一键探测 (自动找 Python)
│  └─ probe.sh                 *nix 一键探测
├─ tests/
│  ├─ test_parsers.py          内核单测 (VDF / 日志 / root 标定 / 目录名匹配)
│  ├─ test_probe.py            探测与脱敏单测 (含错误归属的回归)
│  ├─ test_bait.py             诱饵验证器单测
│  ├─ test_sandbox_e2e.py      假环境端到端 (探测器)
│  ├─ test_transport.py        **搬运层单测 (落点 / 守卫 / 计划 / 执行)**
│  ├─ test_transport_m3.py     **[M3] 云通道 / 快照回滚 / 上传校验单测**
│  ├─ test_trigger.py          **[M3] trigger-sync 编排单测 (全部外部动作注入假件)**
│  ├─ test_wizard.py           **[v0.2.0] 交互向导分支单测 (中止条件 / 确认 / 上云开关)**
│  ├─ test_packaging.py        **[v0.2.0] 发行包不变量 (.bat 约束 / 内嵌覆盖 / 版本一致)**
│  ├─ test_crossmachine.py     **[M6] 跨机往返判定 (pass / byte-mismatch / nothing-to-do / pending)**
│  ├─ test_gui_service.py      **[v1.1.0] 图形向导**后端层**单测 (含"不许 import tkinter" + 魂二包装层)**
│  ├─ test_ds2.py              **[v1.2.0] 魂二文件夹传输层单测 (落点 / 导出 / 校验 / 导入五道闸)**
│  ├─ test_redaction.py        **脱敏回归锁** (真标识符不许出现在会随包发出的地方)
│  └─ test_transport_e2e.py    **搬运器 CLI 端到端 (参数 → 落盘整条链路)**
├─ docs/
│  ├─ how-steam-cloud-works.md 机制实测记录 (root 枚举 / 日志字段 / 三套账号写法)
│  ├─ portable-probe.md        便携探测与自动适配的完整说明
│  ├─ m2-bridge.md             **搬运器 (M2) 设计与红线**
│  ├─ m3-cloud-channel.md      **云通道 (M3) / 快照回滚 / 上传校验 设计与实测**
│  ├─ community-prior-art.md   社区相关工作调研 (16 条)
│  ├─ sandbox-testing.md       沙箱端到端测试 (不依赖第二台机器)
│  ├─ live-checklist.md        **真机闭环任务单** (① M3 通道链 / ② 4b-3 本机档)
│  └─ plan-v3.md               实施计划 v3
├─ reports/                    扫描/探测输出 (gitignore)
└─ release/                    发行包产物 (gitignore, 对外走 GitHub Release)
```

## 测试

```bash
python -m unittest discover -s tests -v      # 472 项, 无需依赖
python tools/build_single_file.py --check    # 两个单文件分别隔离自检 (+ 真实探测自检)
python tools/build_single_file.py --target bridge   # 只重建搬运器单文件
python tools/build_exe.py --python <py.exe>  # 免装 Python 的 exe x2 (需 PyInstaller)
python tools/build_exe.py --check            # 校验 exe: 新鲜度 / PE 头 / 实跑 --version
python tools/build_release.py                # 组装发行包 (含逐文件 sha256)

# 沙箱端到端 —— 造假 Steam 环境, 全程不碰真机数据 (见 docs/sandbox-testing.md)
python tools/make_fake_steam.py --out cases/sandbox --scenario synced
python tools/run_sandbox_probe.py --env cases/sandbox --zip --verify
```

### 沙箱测试

不需要第二台机器也能跑完整链路: `make_fake_steam.py` 造一套含账号 / 存档 /
`remotecache.vdf` / `cloud_log.txt` 的**假 Steam 环境**, `run_sandbox_probe.py`
把子进程的 `USERPROFILE` / `APPDATA` / `LOCALAPPDATA` 重定向到假用户档、并用
`--steam-root` 钉死 Steam 根, 于是探测器既读不到真机 Steam, 也不会去真
`%APPDATA%` 下找存档。

场景 `clean` / `synced` / `disabled` 分别对应"从未开云 / 已上传成功 / 游戏级开关
关着", 三个都带断言, 已进 `tests/test_sandbox_e2e.py` 随单测一起跑。

**它测不了什么**: 沙箱没有游戏本体, 所以**回答不了 4b 的"`.sl2` 搬过去游戏能不能加载"**。
它把"搬运物流层"验证干净, 游戏是否接受仍要真机 —— 详见 `docs/sandbox-testing.md` 第五节。

## 开源合规

- 仓库**不含任何 Valve 文件**（`steam_api64.dll`、SDK 头文件、`steam_autocloud.vdf` 样本一律不入库）
- 不内置任何存档修改逻辑
- 扫描报告默认只存本地; 存档快照若要进版本控制**必须私有仓库**（含 SteamID 与全部进度）
- **源码、文档与发行包里不出现任何真实 accountid / SteamID64 / 用户名** —— 实测记录中的
  真实值统一替换为一组**自洽的虚构值**（accountid `1234567890` ↔ SteamID64 `76561199194833618`
  ↔ 目录名 `01100001499602d2`，三者满足 `SteamID64 = 76561197960265728 + accountid`），
  这样回归测试里的换算断言依然成立
- 平台: **Windows only**（Linux/macOS 下 DS3 走 Proton, 路径与枚举均不同）

## License

MIT
