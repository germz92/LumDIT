# -*- mode: python ; coding: utf-8 -*-
# Build:  pyinstaller packaging/lumdit-macos.spec
# Output: dist/LumDIT.app
#
# For distribution outside your own machines, code-sign and notarize:
#   codesign --deep --force --options runtime --sign "Developer ID Application: ..." dist/LumDIT.app
#   xcrun notarytool submit ... ; xcrun stapler staple dist/LumDIT.app

from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent


def _version() -> str:
    import tomllib

    with open(ROOT / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)["project"]["version"]


VERSION = _version()

hiddenimports = (
    collect_submodules("lumdit")
    + collect_submodules("pymongo")
    + collect_submodules("bson")
    + collect_submodules("dns")
    + ["xxhash", "psutil", "pillow_heif", "rawpy", "av", "dotenv"]
)
binaries = collect_dynamic_libs("av") + collect_dynamic_libs("rawpy") + collect_dynamic_libs("pillow_heif")

a = Analysis(
    [str(ROOT / "lumdit" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=[],
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
    target_arch=None,  # set to "universal2" if all wheels support it
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="LumDIT")

app = BUNDLE(
    coll,
    name="LumDIT.app",
    icon=str(ROOT / "packaging" / "lumdit.icns") if (ROOT / "packaging" / "lumdit.icns").exists() else None,
    bundle_identifier="com.lumdit.app",
    info_plist={
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
        # Needed so the app can read cards in /Volumes and user folders.
        "NSRemovableVolumesUsageDescription": "LumDIT reads camera cards to back them up.",
        "NSDesktopFolderUsageDescription": "LumDIT writes productions to the folders you choose.",
        "NSDocumentsFolderUsageDescription": "LumDIT writes productions to the folders you choose.",
        "NSDownloadsFolderUsageDescription": "LumDIT writes productions to the folders you choose.",
        "NSAppleEventsUsageDescription": "LumDIT ejects cards after offload.",
    },
)
