# -*- mode: python ; coding: utf-8 -*-
#
# Shared PyInstaller spec for both Windows (.exe) and macOS (.app) builds —
# one file per the project's "fewer files" preference, branching on sys.platform
# (this file is executed as plain Python by PyInstaller) exactly like
# xgen/platform/factory.py does at runtime, rather than a second xgen_mac.spec.
import sys
from pathlib import Path

block_cipher = None
root_dir = Path('.').resolve()
is_macos = sys.platform == "darwin"

hiddenimports = [
    'xgen',
    'xgen.capture',
    'xgen.capture.inspect_mode',
    'xgen.capture.keyboard_hook',
    'xgen.capture.mouse_hook',
    'xgen.capture.overlay_window',
    'xgen.capture.transient_capture',
    'xgen.config',
    'xgen.core',
    'xgen.core.appium_compat',
    'xgen.core.driver_dialect',
    'xgen.core.driver_runner',
    'xgen.core.element_bridge',
    'xgen.core.session_manager',
    'xgen.core.stability_scorer',
    'xgen.core.tree_cache',
    'xgen.core.tree_fetcher',
    'xgen.core.tree_parser',
    'xgen.core.uia_bridge',
    'xgen.core.volatility_classifier',
    'xgen.core.xpath_generator',
    'xgen.core.xpath_verifier',
    'xgen.events',
    'xgen.events.event_bus',
    'xgen.platform',
    'xgen.platform.backend',
    'xgen.platform.factory',
    'xgen.platform.unsupported_backend',
    'xgen.ui',
    'xgen.ui.attribute_panel',
    'xgen.ui.crash_dialog',
    'xgen.ui.disambiguation_popup',
    'xgen.ui.legend_dialog',
    'xgen.ui.main_window',
    'xgen.ui.session_dialog',
    'xgen.ui.status_bar',
    'xgen.ui.toolbar',
    'xgen.ui.tree_panel',
    'xgen.ui.xpath_panel',
    'xgen.utils',
    'xgen.utils.dpi',
    'xgen.utils.logger',
    'xgen.utils.privilege',
    'xgen.utils.rect',
    'xgen.utils.xpath_escape',
    'PyQt6',
    'PyQt6.QtCore',
    'PyQt6.QtGui',
    'PyQt6.QtWidgets',
    'pynput',
    'lxml',
    'lxml.etree',
    'requests',
]

if is_macos:
    hiddenimports += [
        'xgen.platform.mac_backend',
        'pynput.keyboard._darwin',
        'pynput.mouse._darwin',
        'objc',
        'Quartz',
        'ApplicationServices',
        'AppKit',
    ]
else:
    hiddenimports += [
        'xgen.platform.windows_backend',
        'uiautomation',
        'pynput.keyboard._win32',
        'pynput.mouse._win32',
        'win32gui',
        'win32process',
        'win32api',
    ]

a = Analysis(
    ['xgen/main.py'],
    pathex=[str(root_dir)],
    binaries=[],
    datas=[('xgen_app_icon.svg', '.'), ('xgen/resources/*', 'xgen/resources')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='xGen',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # Windowed desktop application (no cmd prompt pop-up)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='xGen',
)

if is_macos:
    # Wraps the COLLECT output into a double-clickable xGen.app bundle.
    # NSHighResolutionCapable=True is what makes macOS report real (Retina)
    # point coordinates to the app instead of silently downscaling it — the
    # runtime counterpart of xgen/platform/mac_backend.py's coordinate-space
    # handling; leaving it out would make every native rect/cursor position
    # wrong on a Retina display even if the backend code itself is correct.
    #
    # UNVERIFIED: no code-signing identity or notarization step is configured
    # here (codesign_identity=None below) — without them this .app will show
    # Gatekeeper's "unidentified developer" warning on another Mac. That needs
    # an Apple Developer ID and credentials only the user can provide; tracked
    # as a follow-up, not attempted here.
    app = BUNDLE(
        coll,
        name='xGen.app',
        icon=None,
        bundle_identifier='xgen.xpath.inspector.v1',
        info_plist={
            'NSHighResolutionCapable': True,
            'NSAccessibilityUsageDescription': 'xGen uses Accessibility to inspect UI elements under the cursor.',
            'CFBundleShortVersionString': '1.0.0',
        },
    )
