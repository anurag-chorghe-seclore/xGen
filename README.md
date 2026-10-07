<div align="center">

# ⚡ xGen — XPath Inspector & Locator Studio
**A resilient selector engine for Appium's Windows Driver and Mac2 Driver**

[![Python 3.10+](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011%20%7C%20Server%20%7C%20macOS-0078D6.svg)](#️-platform-support)
[![UI: PyQt6](https://img.shields.io/badge/GUI-PyQt6-green.svg)](https://www.riverbankcomputing.com/software/pyqt/)
[![Tests](https://img.shields.io/badge/tests-202-brightgreen.svg)](#-running-the-tests)
[![License: MIT](https://img.shields.io/badge/license-MIT-purple.svg)](LICENSE)

</div>

---

## 📖 Overview

**xGen** is a standalone desktop application for eliminating brittle selectors in desktop test automation. It walks the live accessibility tree, scores every candidate for volatility, verifies each one against your running Appium session, and hands you ranked, resilient XPath expressions in milliseconds.

It targets **Appium Windows Driver / WinAppDriver** on Windows and **Appium Mac2 Driver (XCUITest)** on macOS.

---

## 🖥️ Platform support

| | Windows | macOS |
|---|---|---|
| **OS** | 10, 11, Server | Sequoia, Tahoe |
| **Driver** | Appium Windows Driver, WinAppDriver | Appium Mac2 Driver (XCUITest) |
| **Session scope** | Desktop Root (whole desktop) or one window | One application |
| **Native layer** | UI Automation, Win32 | Accessibility (AX), Quartz, AppKit |
| **Packaging** | `xGen.exe` (folder distribution) | `xGen.app` bundle |

These are genuinely different targets, not a port with a compatibility shim — the differences that matter to you are called out throughout this README.

### How the platform split works

Every OS-specific call lives behind a `PlatformBackend` in `xgen/platform/`. **`xgen/platform/factory.py` is the only place in the codebase that checks `sys.platform`** — nothing else imports `uiautomation`, `pywin32`, `ctypes.windll`, `objc`, `Quartz` or `AppKit` directly.

Separately, the *selector vocabulary* comes from a **driver dialect** (`xgen/core/driver_dialect.py`), chosen from the **session's capabilities** rather than from the host OS. Windows speaks `Button` / `@Name` / `@AutomationId`; Mac2 speaks `XCUIElementTypeButton` / `@label` / `@identifier`. Keeping those two concerns apart is what lets xGen running on Windows drive a remote Mac Appium server and still generate correct Mac selectors.

---

## 🌟 Features

### 🎯 Continuous Inspect Mode (`F3`)
Hover any element for a live bounding box; click to lock it in, with the native click suppressed so you don't fire buttons by accident while inspecting.

**Scoping:** a Mac2 session attaches to exactly one application, so on macOS inspect mode resolves **only** elements inside the attached app. Hovering anything else highlights nothing and the status bar says why — elements outside the session can never appear in the fetched tree, and no selector from that session could address them. A Windows Desktop Root session keeps inspecting the whole desktop, which is its entire purpose.

The Windows Taskbar and System Tray, the macOS menu bar and Dock, and xGen's own windows are always excluded.

### 🧠 Multi-tier XPath generation (T1–T8)
Ranked candidates, restricted to the subset each driver's engine actually supports:

- **Tier 1 — Semantic name / ID**: `//Button[@Name='Save']`, or on macOS `//XCUIElementTypeButton[@label='Save']`
- **Tier 2 — Starts-with / contains**: `//Window[starts-with(@Name, 'Untitled - Notepad')]` for titles carrying document names or build numbers
- **Tier 3 — Type + substring**: `//Edit[contains(@Name, 'Search')]`
- **Tier 4 — Ancestor climbing**: finds the nearest unique container, e.g. `//Group[@Name='Toolbar']//Button[@Name='Save']`
- **Tier 5/6 — Positional fallbacks**: indexed variants like `(//ListItem)[3]`, with tree-ordered match inspection

The **`//Window` prefix** toggle appears only for drivers it means something for. A Mac2 session is already scoped to one application and its root is `XCUIElementTypeApplication`, not a window, so the prefix adds length without adding uniqueness — the control hides itself there.

### 📊 Stability scoring & volatility classification
- **Volatility heuristics** — Shannon entropy and regex classifiers flag temporary GUIDs, machine hashes and dynamic counters (`btn_482910_temp`) and downgrade their score.
- **Localization risk (`🌐 Loc Risk`)** — warns when a selector depends on a localized UI string, which will break across language packs (German *Speichern*, French *Enregistrer*).
- **Hard ceilings** — purely positional or indexed selectors cap at `45` (🔴 Fragile), to keep them out of CI.

### ▶ Live driver verification
- **`▶ Test`** — sends `POST /session/{id}/element` to your live session and reports real latency (`⚡ Found 1 match (28ms)`).
- **👆 Click**, **🎯 Hover**, **⌨️ SendKeys** — dispatch real W3C actions to confirm focus and keystroke behaviour without writing a test first.

### ⚡ Transient UI capture (`F4`)
Inspect context menus, dropdowns and flyouts that vanish the moment focus moves:
- **`F4`** freezes whatever is under the cursor without shifting focus.
- **Timed capture** — set a 3s/5s countdown, navigate into a nested menu, and xGen snapshots the tree when it fires.
- **🔒 Frozen mode** stops background polling from clearing captured popup nodes.

### 🛠️ Capability builder
The session dialog takes a JSON block of extra Appium capabilities, merged last so it can override anything xGen sets. Available on both platforms.

### 📐 High-DPI & multi-monitor
Per-Monitor V2 DPI awareness on Windows, with physical→logical translation for fractional scaling (125/150/175/200%) and negative monitor coordinates.

macOS works differently, deliberately: the accessibility API *and* Mac2 Driver both report **points**, which is already Qt's coordinate space, so no conversion happens at all. Scaling those values would put the three coordinate sources out of step by exactly the display's scale factor — which is what previously made every highlight land in the wrong place on Retina.

---

## ⌨️ Keyboard shortcuts

| Shortcut | Action |
|:---:|---|
| **`F3`** | Toggle Inspect Mode |
| **`F4`** | Transient snapshot of the element under the cursor |
| **`Ctrl + R`** / **`Cmd + R`** | Refresh the UI tree |
| **`Esc`** | Exit Inspect Mode |

---

## 🚀 Getting started

### Prerequisites

**Both platforms**
- Python 3.10+
- [Appium 2.x](https://appium.io/)

**Windows**
- Windows 10, 11, or Server
- [`appium-windows-driver`](https://github.com/appium/appium-windows-driver), *or* standalone [WinAppDriver](https://github.com/microsoft/WinAppDriver)

**macOS**
- macOS Sequoia or Tahoe
- [`appium-mac2-driver`](https://github.com/appium/appium-mac2-driver) — needs Xcode installed, since it builds and runs WebDriverAgentMac
- The three permission grants below

### Install

```bash
git clone https://github.com/anurag-chorghe-seclore/xGen.git
cd xGen
```

```bash
# Windows
python -m venv .venv
.venv\Scripts\activate
```

```bash
# macOS
python3 -m venv .venv
source .venv/bin/activate
```

```bash
pip install -e ".[dev]"
```

### Run

```bash
python -m xgen.main
```

`pip install -e .` also puts an `xgen` command on your PATH.

---

## 🔐 macOS permissions

macOS grants these **per application binary**, which has a consequence worth knowing up front: permissions granted to your Python interpreter do **not** carry over to a packaged `xGen.app`. It is a different binary and starts with none of them.

Open **System Settings → Privacy & Security** and enable:

| Permission | Needed for | Without it |
|---|---|---|
| **Accessibility** | every `AXUIElement` call | no element resolution at all |
| **Input Monitoring** | pynput's global mouse/keyboard hooks | `F3`/`F4` and click capture silently do nothing |
| **Screen Recording** | per-window titles from `CGWindowListCopyWindowInfo` | apps still list, but window titles come back empty |

There is no programmatic elevation on macOS — no equivalent of Windows' UAC relaunch. These have to be toggled by hand.

> **Unsigned builds:** the `.app` is neither code-signed nor notarized, and macOS keys permission grants to a binary's signature. Each rebuild therefore looks like a *new* app. If inspect mode stops working after a rebuild, remove the stale xGen entry from each list and re-add it — a toggle that looks ON can be pointing at the previous build.

---

## 🔌 Connecting to an application

Click **Connect Session** on the toolbar. What you can target depends on the driver.

### Windows
1. **Desktop Root** *(recommended)* — `app: "Root"`, inspects every application; switch freely between top-level windows.
2. **Launch an executable** — e.g. `C:\Program Files\Notepad++\notepad++.exe`.
3. **Attach to a window handle** — `0x001A0B2C`, or decimal `1706796`.

### macOS
A Mac2 session attaches to **one application**, and macOS has no desktop-wide equivalent of Root — so xGen does not offer a Desktop Root entry there. It would promise something no Mac session can deliver, and picking it would silently land you on Finder, which is Mac2 Driver's own default.

1. **Pick a running application** — the target list shows applications, frontmost first, each with its bundle identifier.
2. **Launch or attach by path / bundle ID** — `/Applications/TextEdit.app`, or `com.apple.TextEdit`.
3. **Attach without relaunching** — a checkbox that sets `noReset`, for when the app is already in the state you want.

xGen always sends `appium:skipAppKill` on macOS. Mac2 Driver otherwise terminates the application it was attached to when the session is deleted, so disconnecting xGen would quit the app out from under you. An inspector should not destroy what it is inspecting. Override it through the capability builder if you ever want the opposite.

---

## 📦 Building a standalone binary

```bash
python build_exe.py
```

PyInstaller is **not** a cross-compiler: build the Windows executable on Windows and the macOS bundle on macOS. The shared [`xgen.spec`](xgen.spec) branches on `sys.platform` to produce the right one.

| | Output | Notes |
|---|---|---|
| **Windows** | `dist/xGen/xGen.exe` | Ships as a folder — `xGen.exe` plus its Qt runtime. Distribute the zip; users run `xGen.exe`. |
| **macOS** | `dist/xGen.app` | Self-contained bundle. The `dist/xGen/` folder beside it is PyInstaller's intermediate output and is not shipped. |

The macOS bundle sets `NSHighResolutionCapable`, which is what makes macOS report true Retina point coordinates instead of silently downscaling the whole UI. CI fails the build if that key is missing, because nothing at runtime can recover from it.

### First launch on macOS

The bundle is unsigned, so Gatekeeper blocks it:

```bash
xattr -dr com.apple.quarantine /Applications/xGen.app
```

…or right-click the app and choose **Open**. Code signing needs an Apple Developer ID and is not set up in this repository.

---

## 🔄 CI/CD

[`.github/workflows/build-and-release.yml`](.github/workflows/build-and-release.yml) runs on every push and pull request to `main`/`master`/`dev`, on `v*` tags, and on manual dispatch.

**`test`** — a matrix across `windows-latest` and `macos-latest`, with `fail-fast: false` so a failure on one platform never hides the other's result. Runs under `QT_QPA_PLATFORM=offscreen`, since both runners are headless.

**`build-windows` / `build-macos`** — gated on `needs: test`, and skipped unless the run came from a `v*` tag or a manual dispatch. PyInstaller can't cross-compile, so each package costs a full dedicated runner and building one per push isn't worth it. For artifacts on demand, use **Actions → Run workflow** with `build_type: preview`; they land under **Artifacts** on the run summary and are kept 30 days.

**Releases** — run with `build_type: release` and a `release_tag`, or push a `v*` tag, to publish a GitHub Release with `.zip` and `.sha256` assets for both platforms.

> The macOS runner is Apple Silicon, so `xGen.app` is **arm64-only**. Supporting Intel Macs would need a second job on `macos-13`, or a universal2 build.

---

## 🧪 Running the tests

```bash
pytest -v
```

202 tests, pinning platform-specific behaviour on both sides, so the suite is meaningful on either OS. A few skip where they monkeypatch a module that only installs on one platform (`uiautomation`).

Two things worth knowing before adding tests:

**The driver dialect is pinned to Windows by default** (`tests/conftest.py`). Most of the suite asserts Windows behaviour against Windows-shaped XML, and since the dialect otherwise falls back to the *host* backend, that silently became Mac2 vocabulary on a macOS runner — seven tests failed for reasons unrelated to what they were checking. Tests about another driver override the default by setting `config.target_platform` or activating a dialect themselves.

**Don't let a test depend on the host's windows.** A headless runner has none open. Tests that need a target list stub `get_open_windows` rather than asking the machine.

---

## 📂 Project layout

```
xGen/
├── .github/workflows/         # CI matrix, packaging, releases
├── xgen/
│   ├── capture/               # Mouse/keyboard hooks, highlight overlay, transient capture
│   ├── core/
│   │   ├── driver_dialect.py  #   per-driver vocabulary, chosen from session capabilities
│   │   ├── session_manager.py #   Appium session lifecycle, reuse, heartbeat
│   │   └── ...                #   XPath generator, verifier, stability scorer, driver runner
│   ├── events/                # Decoupled application event bus
│   ├── platform/
│   │   ├── backend.py         #   the PlatformBackend protocol
│   │   ├── factory.py         #   the ONLY sys.platform check in the codebase
│   │   ├── windows_backend.py #   UI Automation / Win32
│   │   ├── mac_backend.py     #   Accessibility / Quartz / AppKit, via PyObjC
│   │   └── unsupported_backend.py
│   ├── ui/                    # PyQt6 panels, toolbar, dialogs, selector cards
│   ├── utils/                 # DPI math, rect geometry, window finder, logging
│   └── main.py                # Bootstrap & crash boundary
├── tests/                     # 202 unit and integration tests
├── build_exe.py               # PyInstaller driver
├── xgen.spec                  # Shared Windows/macOS build spec
└── pyproject.toml
```

---

## ⚠️ Known limitations

- **No multi-session merged tree on macOS.** Inspecting several applications at once would need one Appium session per app merged into a synthetic root — rejected as more confusion than it is worth. Switch targets instead.
- **The macOS bundle is unsigned**, so Gatekeeper warns on first launch and permission grants are invalidated by every rebuild (see above).
- **macOS builds are arm64-only.**
- **`walk_subtree` cannot resume from a window handle on macOS.** `AXUIElementRef` has no "reconstruct from an opaque integer" operation the way Windows' `ControlFromHandle` does.

---

## 📄 License

MIT. See [`LICENSE`](LICENSE).
