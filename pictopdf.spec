# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：生成单文件 PicToPDF.exe（Windows）。

tkinterdnd2 的 tkdnd 平台库由 pyinstaller-hooks-contrib 的
hook-tkinterdnd2.py 自动收集，无需在此显式声明。
"""

a = Analysis(
    ["window.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PicToPDF",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
