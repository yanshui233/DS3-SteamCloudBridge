# SteamCloudBridge v1.3.0 · User guide

**What this is**: a save-file mover for *Dark Souls III* and *Dark Souls II*. Four things it does:
① get DS3 saves onto / down from Steam Cloud properly; ② put a DS3 save from another machine into the right place on this one;
③ read-only view of this machine's cloud-save state; ④ DS2 **has no Steam Cloud at all**, so it exports saves into a plain folder you carry with your own cloud drive or a USB stick.

The whole package **works straight after unzipping**: no Python install, no Steam settings to change, nothing uploaded anywhere.

> **DS2 and DS3 are not the same case.** DS3 supports Steam Cloud (hence the "turn the cloud channel on" step); DS2 **does not support cloud saves at all** — it is not a setting you forgot to enable, the game simply has no such feature, there is no switch to flip, **and nothing can change that** (cloud saves are up to the game's developer; a third party cannot add them). So DS2 goes through "export to a folder → drop it in a cloud-sync folder / USB stick → import on the other machine". See **section 6**.

> **Language**: the wizard picks its language from your system on first launch (Chinese systems get Chinese, everything else gets English). The **中文 / English** switch in the top-right corner changes it any time, and your choice is remembered (`%APPDATA%\SteamCloudBridge\settings.json`). To force it: `SteamCloudBridgeGUI.exe --lang en`. The command-line version stays Chinese; this setting does not affect it.

---

## What's in the package

After unzipping, the **top level contains only two files** — these are the only ones you touch day to day:

| Top-level file | What it does | Writes to disk? |
|---|---|---|
| `RUN-GUI.bat` | **Double-click this one.** Opens the graphical wizard and walks you through | Only when you click "Back up / Import" |
| `SteamCloudBridgeGUI.exe` | The wizard itself. The launcher finds it for you | ❌ never touches saves |
| `support\` | **Everything else lives in here** — you can ignore it | —— |

Inside `support\` (command line / troubleshooting only):

| File | What it does | Writes to disk? |
|---|---|---|
| `RUN-SYNC.bat` | Command-line daily entry (no window; you type commands) | ❌ no |
| `RUN-BRIDGE.bat` | Moves a `.sl2` save into the right place (optionally pushes it to the cloud) | ✅ yes (needs `YES`) |
| `RUN-PROBE.bat` | Read-only checkup: shows this machine's cloud-save state; the report lands on **your own computer** | ❌ no |
| `RUN-PROBE-TARGET.bat` | Narrow scan: DS3 `374320` / Elden Ring `1245620` only | ❌ no |
| `RUN-CROSS-VERIFY.bat` | Checks whether two machines hold **byte-identical** saves (never launches the game) | ✅ yes (pulls one file down) |
| `RUN-CLOUD-BAIT.bat` | Cloud-acceptance test (bait method; needs Python 3 installed) | ✅ yes (adds then removes one bait file) |
| `*.exe` / `*.py` | The actual tools those scripts call. `.exe` needs no Python; `.py` is the readable source with identical behaviour (needs Python; the GUI variant also needs tkinter) | —— |
| `README.md` / `README.en.md` | All documentation, in Chinese and English | —— |

> **You only need to remember `RUN-GUI.bat`.** Everything else is for the command line or troubleshooting.
> Every tool here **moves bytes and nothing else** — it does not edit save contents, inject, or modify game files.

> Why everything moved into `support\`: a dozen files flat in one folder meant the first question was always "which one do I double-click?". Now the top level holds only the **launcher + the wizard**. The command-line scripts still work — each sits next to the `.exe` it calls, so paths are none of your business.

---

## 1. The wizard: double-click `RUN-GUI.bat`

Five steps down the left side; just follow them. No commands to memorise.

| Step | What it does |
|---|---|
| **1 Check setup** | Works out: where Steam is, whether the game is installed, where the saves are, whether the cloud holds a copy, whether the channel is on. **Read-only.** |
| **2 Back up saves** | One click copies the whole save folder next to the program and verifies every file afterwards. Restorable if things go wrong. |
| **3 Before each session** | Three moves: ① click the button that opens Steam's console → ② **copy-paste** the line shown in the window → ③ the window confirms by itself; then click "Start the game". |
| **4 Troubleshooting** | Common failures, plus shortcut buttons for "open the save folder / open the backup folder". |
| **5 DS2 saves** | DS2 has no cloud, so this page **lights nothing up**: export the saves to a folder, then import from that folder on the other machine. Details in section 6. |

**The window waits for you**: it shows "● not started" until you type the line, then flips to "✓ on" within seconds.

> The UI says "no success message is normal" — that is literally true. Steam's console prints **nothing** for this command, only the echoed assignment. So the wizard judges whether you typed it by **reading Steam's own log**, not by looking for words on screen.

**Wizard won't open?** Use the command-line version, `RUN-SYNC.bat` (section 2) — same features.

---

## 2. Command line: double-click `RUN-SYNC.bat` before each session

> Everything in this section lives in the **`support\`** folder. From here on it is "advanced / troubleshooting" territory; you do not need it day to day.

1. **Double-click `support\RUN-SYNC.bat`** (it first checks that the game is not running and Steam is).
2. **Type one line as prompted** (how, see section 3):
   ```
   testappcloudpaths 374320
   ```
3. Once you see `[OK] the cloud channel is ON ...` → **the game starts by itself** → play normally → **Steam uploads your save when you quit**.

To light the channel up without launching the game:
```bat
RUN-SYNC.bat daily --appid 374320 --manual
```

**Why every single time?**
Steam's cloud switch has **two layers**: ① account level (written once, remembered); ② **game level** — for DS3 this level only lives for the current Steam session, **it dies when Steam restarts**, and there is no local config file to write it to. So "set it once, works forever" is not technically possible. The cost is ten seconds per session, and lighting it up is **idempotent** (repeating it is harmless).

⚠ **Do not restart Steam after lighting it up** — that voids it; just double-click once more.

---

## 3. The line you type

**Open Steam's console first** (Steam must be running):
press **`Win + R`** → paste `steam://open/console` → Enter. Steam grows a **Console** tab.
(If nothing happens: paste the same string into a browser address bar; or add ` -console` to the end of Steam's shortcut target and restart Steam.)

> ⚠ The console **being open ≠ the input row having focus**. Before typing, **click once on the bottom input row**.

**Line one** — open the cloud channel for this game:
```
testappcloudpaths 374320
```

**Line two** — make Steam pull from the cloud once:
```
cloud_sync_down 374320
```

Key points:
- **Order matters**: `testappcloudpaths` (open) must come before `cloud_sync_down` (download). Reversed, Steam refuses and the log says `AutoCloud is disabled`.
- **No "success" message is normal** — success is just an echoed assignment.
- To self-check: type `find testappcloudpaths`; a value of `"374320"` means it is on, `"0"` means it did not take — type line one again.
- **Typing it wrong breaks nothing.** These two lines are **Steam's own built-in cloud-save debug commands** (from Valve's official Steamworks docs). They are not a cheat, not a crack, and they do not touch any game file.

---

## 4. Move a save onto this machine

**When**: you have a DS3 save on **another machine** and Steam Cloud did not bring it over; or you backed up `DS30000.sl2` by hand and want it back.
**When not to**: editing save contents (this is not a save editor, it moves whole files); moving **someone else's** save (it will be refused outright).

**How**: fully quit Steam → double-click `RUN-BRIDGE.bat` → answer five questions.
- At `2)` you can **drag the file into the window** instead of typing the path.
- At `4)` **only `YES` writes to disk**; anything else cancels.
- At `5)` Enter means "then push it to the cloud too" (Steam gets started for you).
- Everything before step 4 is **read-only**.

**Three hard rules** (enforced by the tool, not bypassable):
1. **Your own account's saves only** — it compares the `accountid` recorded in the save folder against the account Steam is logged into, and refuses on mismatch.
2. **Steam must be fully closed** — if Steam is running it refuses.
3. **No one-way overwrites** — it force-backs-up and snapshots before writing, so a bad write can be rolled back.

**Undo** (prints what it would do unless you add `--yes`):
```bat
RUN-BRIDGE.bat rollback --snapshot "backup\DS30000.sl2.snapshot.json"
RUN-BRIDGE.bat rollback --snapshot "backup\DS30000.sl2.snapshot.json" --yes
```
You can also skip the tool entirely: copy `backup\DS30000.sl2` back into the save folder by hand (the checkup step prints that folder for you).

---

## 5. See what this machine's cloud saves look like

Double-click **`RUN-PROBE.bat`** → a few seconds later the `reports` folder opens; everything stays **on your own machine**:

| File | What it is |
|---|---|
| `machine_profile.md` | Human-readable summary |
| `machine_profile.share.json` | Redacted structured data |
| `machine_profile.appids.txt` | Games that show cloud-save traces |
| `private\machine_profile.json` | Full profile, **contains account IDs and absolute paths** — do not send it anywhere |

It is **read-only throughout**: it reads file names, sizes and timestamps, never save contents, writes nothing, and does not go online.

(The wizard's step 1 does the same thing, just more focused.)

---

## 6. Dark Souls II: no cloud, so move a folder

### Why DS2 is different

**Dark Souls II does not support Steam Cloud saves.** It is not "your settings are off" — the game **itself** lacks the feature, so there is no `steam_autocloud.vdf`, no cloud copy, and **no cloud channel to "light up"** (the two commands in section 3 mean nothing for DS2).

This is not a guess; four local facts line up: the account config `localconfig.vdf` has no cloud record for this game; the `userdata\<account>\335300\` folder **does not exist** (never synced); the save folder has no `steam_autocloud.vdf`; and the cloud log has exactly one line for it, `Not running AutoCloud. No config data found`. Its Steam store page does not list cloud support either.

**Could we add it?** No. Cloud saves are a feature the game's developer wires into Steam; a third party cannot add them, and this tool does not modify games or inject anything. **So "DS2 via Steam Cloud" is a dead end — stop poking at the "light up the cloud channel" page, it will never respond for DS2.**

What we can do is use a **transport channel you control**: pack the saves into a folder and make that folder reachable from both machines. This is the tool's **final answer** for DS2.

### Two channels, pick either

| Channel | How |
|---|---|
| **Cloud-sync folder** | Point the export at a folder that syncs by itself — OneDrive / Dropbox / Syncthing / anything similar. On the other machine, open the same folder and import. |
| **USB stick / external disk** | Export straight onto the stick. Plug it into the other machine and "import from folder". |

To the tool they are identical: the export destination and the import source are just folders you pick on the spot.

### How to use it (wizard step 5)

1. Open the wizard → click **"5 DS2 saves"** on the left. The page shows where DS2 saves are, which account they belong to, how many files, and how big.
2. Click **"Export to folder…"** → pick a folder (cloud-sync folder or USB stick) → wait for the checksums.
   - The export folder gets `savepack.json` (manifest with a sha256 per file), `files\` (the saves), and a `READ-ME-FIRST.txt`.
3. On the **other machine**, open step 5 again → click **"Import from folder…"** → pick the same folder → confirm.
   - Before importing it **backs up whatever saves already exist on that machine**, then writes byte by byte and re-verifies.
   - It is fine if that machine **has never played DS2**: the tool derives the target folder from your Steam account and creates it.

### Command line (the same two jobs)

```bat
RUN-BRIDGE.bat save-status                       :: where are the saves (read-only)
RUN-BRIDGE.bat save-out --to "D:\OneDrive\ds2saves"          :: export into a pack
RUN-BRIDGE.bat save-in  --from "D:\OneDrive\ds2saves" --yes  :: import it back (writes)
```

Swap `--yes` for `--dry-run` to see what it would do without writing.

### The same three hard rules

1. **Same Steam account only** — the manifest records the exporting `accountid`; a mismatch with the target machine is **refused outright** (no "continue anyway").
2. **The game must be fully closed when importing** — if DS2 is running (`DarkSoulsII.exe`) it will not write.
3. **Forced backup before writing** — if the backup fails it aborts without writing a byte; writes go through a temp file plus atomic replace, so you never end up with a half-written save.

### Two commonly confused points

- **What is `.co2`?** If you use the Seamless Co-op mod, its saves are `.co2`. Both are exported **by default**, because for many people the newest progress is actually in the `.co2`, not the `.sl2`. Carrying both is the safe choice.
- **This is not "the cloud"**: the tool uploads nothing. The bytes only appear in the folder you picked; whether that folder syncs is up to your cloud client, not this tool.

---

## 7. Will I get banned? — a straight answer

- **Steam / VAC: not applicable.** *Dark Souls III* **does not use VAC**. Those two lines are debug commands from Valve's own documentation; Steam bans are about account theft, fraud, market manipulation and refund abuse — nothing to do with typing a client's built-in command.
- **DS3 softban: does not apply.** It triggers when **FromSoftware's weekly scan finds illegal data in the save you uploaded** (impossible items/stats, skipped progress). This tool **moves bytes only, never edits contents, refuses cross-account, never injects, never modifies game files**.
- **DS2 likewise**: this path bypasses the game servers entirely; the tool copies bytes between two of your own folders and the game never knows.
- **The one red line**: do not use it to move **someone else's** save — that is "using another person's save data", and the risk comes from the save contents, not from clouds.

**Tool boundaries**: `RUN-PROBE.bat` and wizard step 1 are purely read-only; disk writes happen only after you explicitly click "Back up / Import" or type `YES` / add `--yes` to `RUN-BRIDGE.bat`, and always after a backup, always rollback-able.

---

## 8. When something goes wrong

| Symptom | What to do |
|---|---|
| Window flashes and vanishes | Something errored. Type `cmd` in the folder's address bar and press Enter, then run the script by name and read the error |
| Blue "Windows protected your PC" | Click "More info" → "Run anyway". It is because we have no code-signing certificate, not a virus |
| Antivirus complains | The exe reads and writes Steam folders, which can trip heuristics. **If you would rather not, use the `.py` version next to it** — readable source, identical output |
| Wizard window will not open | Use the command line, `support\RUN-SYNC.bat` — same features. Or check that `RUN-GUI.bat` and `SteamCloudBridgeGUI.exe` are **on the same level** (do not drag the launcher out on its own) |
| "`.exe` not found" | Do not split the package up: `RUN-GUI.bat` must sit beside `SteamCloudBridgeGUI.exe`, and the scripts in `support\` must stay with the `.exe` files in `support\` |
| `Steam not found` | If Steam is installed on a non-default drive, check that its install folder is readable |
| Wizard says "still not on" | Lighting up only lasts **for the current Steam session**. If you restarted Steam, type it again; if it still will not change, click the console's bottom input row with the mouse before typing |
| You restarted Steam halfway | This session's light-up is void — double-click once more |
| Move plan was blocked | Follow the reason given: cross-account → use your own account's save; Steam running → quit it fully; landing spot unknown → let the game sync once first |
| Want to compare saves on two machines | Double-click `RUN-CROSS-VERIFY.bat`, type the two lines from section 3 as prompted, and **do not launch the game at any point** |
| Step 5 says "DS2 save folder not found" | This machine has never played DS2 — that is normal. You **can still "import from folder"**; the tool creates the folder from your account |
| Import says "pack incomplete or corrupt" | The bytes in `savepack.json` and `files\` disagree (sync not finished, cloud client rewrote something, or a partial copy). Let the sync finish / copy again, then import |
| Import refused as "cross-account" | That pack came from **a different Steam account**. Use a pack exported from your own account (the tool will not let you cross that line) |
| Imported, but the game shows no progress | First confirm **the game was closed** during the import; DS2 reads `DS2SOFS0000.sl2`, and `save-in` prints exactly which files it wrote — if that does not add up, keep that `READ-ME-FIRST.txt` and the on-screen output |
| Want the pre-import save back | The import prints the backup folder (by default `save-backup\<timestamp>\` next to the save folder); copy it back wholesale |

---

## 9. Disclaimer

Unofficial tool, not affiliated with Valve, FromSoftware or any game publisher. Use at your own risk (see `LICENSE`).
