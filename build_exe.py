"""
xGen Standalone Executable Build Script.
Packages xGen into a standalone Windows (.exe) or macOS (.app) binary using
PyInstaller, driven by the single cross-platform xgen.spec (see its own
comment for why one shared spec file is used instead of a second one).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows consoles
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def build() -> None:
    root = Path(__file__).resolve().parent
    spec_file = root / "xgen.spec"
    dist_dir = root / "dist"
    build_dir = root / "build"

    target_desc = "macOS Application (.app)" if sys.platform == "darwin" else "Windows Executable (.exe)"
    print("=" * 60)
    print(f"Building xGen Production {target_desc}")
    print("=" * 60)

    if spec_file.exists():
        cmd = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            str(spec_file)
        ]
    else:
        entry_point = root / "xgen" / "main.py"
        cmd = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--name=xGen",
            "--onedir",
            "--windowed",
            "--noconfirm",
            "--clean",
            f"--distpath={dist_dir}",
            f"--workpath={build_dir}",
            f"--paths={root}",
            "--hidden-import=xgen",
            "--hidden-import=PyQt6",
            "--hidden-import=uiautomation",
            "--hidden-import=pynput.keyboard._win32",
            "--hidden-import=pynput.mouse._win32",
            "--hidden-import=win32gui",
            "--hidden-import=win32process",
            "--hidden-import=win32api",
            "--hidden-import=lxml",
            "--hidden-import=lxml.etree",
            "--hidden-import=requests",
            str(entry_point)
        ]

    print(f"Executing: {' '.join(cmd)}")
    res = subprocess.run(cmd, cwd=str(root))

    if res.returncode == 0:
        output_path = (dist_dir / "xGen.app") if sys.platform == "darwin" else (dist_dir / "xGen" / "xGen.exe")
        print("\n" + "=" * 60)
        print("[OK] Build Successful!")
        print(f"Output: {output_path}")
        print("=" * 60)
    else:
        print(f"\n[FAIL] Build failed with exit code {res.returncode}")
        sys.exit(res.returncode)


if __name__ == "__main__":
    build()
