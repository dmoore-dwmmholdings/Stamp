# PyInstaller spec for Stamp - spec §11 M5, §3 packaging.
#
# Build with:   uv run pyinstaller packaging/stamp.spec --noconfirm
#
# OpenCascade arrives through the `OCP` extension module, which is a single very
# large binary with no Python submodules for PyInstaller's analyser to follow, so
# it has to be collected wholesale.  The same is true of manifold3d.  Everything
# else is ordinary.

import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files

BLOCK_CIPHER = None
ROOT = Path(SPECPATH).parent

# Read the version from the package rather than repeating it here.
VERSION = re.search(
    r'__version__ = "([^"]+)"',
    (ROOT / "src" / "stamp" / "__init__.py").read_text(encoding="utf-8"),
).group(1)

datas = []
binaries = []
hiddenimports = []

# OCP ships one enormous extension plus its OpenCascade resource files.
for package in ("OCP", "manifold3d"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

# svgelements and ezdxf carry data tables that are loaded at run time.
datas += collect_data_files("ezdxf")
datas += collect_data_files("trimesh")

# The window icon is loaded from inside the package at run time.
datas += [(str(ROOT / "src" / "stamp" / "resources" / name), "stamp/resources")
          for name in ("stamp.ico", "stamp.png")]

ICON = ROOT / "packaging" / "assets" / ("stamp.icns" if sys.platform == "darwin" else "stamp.ico")

hiddenimports += [
    "ocpsvg",
    "svgelements",
    "networkx",
    "lxml",
    "lxml._elementpath",
    "lxml.etree",
    "PySide6.QtSvg",
    "OCP.OpenGl",
]

# OCP exposes only the native window wrapper for the current platform. Asking
# PyInstaller for the Windows/X11 wrappers on macOS produces misleading missing
# module errors and can hide a real packaging failure.
if sys.platform == "win32":
    hiddenimports.append("OCP.WNT")
elif sys.platform == "darwin":
    hiddenimports.append("OCP.Cocoa")
else:
    hiddenimports.append("OCP.Xw")

# Things Stamp never imports.  Leaving them in roughly doubles the build.
excludes = [
    "tkinter",
    "matplotlib",
    "IPython",
    "jedi",
    "pytest",
    "sympy",
    "scipy",
    "sklearn",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.Qt3DCore",
    "PySide6.QtCharts",
    "PySide6.QtQuick",
    "PySide6.QtQml",
    "PySide6.QtMultimedia",
]

a = Analysis(
    [str(ROOT / "src" / "stamp" / "main.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=BLOCK_CIPHER,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=BLOCK_CIPHER)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Stamp",
    icon=str(ICON) if ICON.exists() else None,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX corrupts the OpenCascade binaries
    console=False,
    disable_windowed_traceback=False,
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
    upx=False,
    upx_exclude=[],
    name="Stamp",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Stamp.app",
        icon=str(ICON) if ICON.exists() else None,
        bundle_identifier="com.stamp.app",
        info_plist={
            "NSHighResolutionCapable": True,
            "CFBundleShortVersionString": VERSION,
        },
    )
