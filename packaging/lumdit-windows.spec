# -*- mode: python ; coding: utf-8 -*-
# Build:  pyinstaller packaging/lumdit-windows.spec
# Output: dist/LumDIT/LumDIT.exe  (one-folder build; faster start-up than one-file)

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent

hiddenimports = (
    collect_submodules("lumdit")
    + collect_submodules("pymongo")
    + collect_submodules("bson")
    + collect_submodules("dns")
    + ["xxhash", "psutil", "pillow_heif", "rawpy", "av", "dotenv", "certifi"]
)
binaries = collect_dynamic_libs("av") + collect_dynamic_libs("rawpy") + collect_dynamic_libs("pillow_heif")
datas = [(str(ROOT / "lumdit" / "resources"), "lumdit/resources")] + collect_data_files("certifi")

a = Analysis(
    [str(ROOT / "lumdit" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LumDIT",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ROOT / "packaging" / "lumdit.ico") if (ROOT / "packaging" / "lumdit.ico").exists() else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="LumDIT",
)
