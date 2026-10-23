# PyInstaller spec for Plex Converter
# Usage: python -m PyInstaller build.spec --noconfirm --clean

import sys
from pathlib import Path
import tkinterdnd2
import customtkinter

_ROOT = Path(SPECPATH)  # noqa: F821 – SPECPATH injected by PyInstaller

# Bundle the entire tkinterdnd2 package so its DLL-picker finds its files.
# tkinterdnd2 resolves DLLs relative to its own __file__ at runtime.
_tkdnd2_pkg = Path(tkinterdnd2.__file__).parent
_ctk_pkg     = Path(customtkinter.__file__).parent

a = Analysis(
    [str(_ROOT / "app.py")],
    pathex=[str(_ROOT)],
    binaries=[],
    datas=[
        # tkinterdnd2: entire package including all platform DLLs
        (str(_tkdnd2_pkg), "tkinterdnd2"),
        # customtkinter: assets (fonts, images, themes)
        (str(_ctk_pkg), "customtkinter"),
    ],
    hiddenimports=[
        "customtkinter",
        "tkinterdnd2",
        "darkdetect",
        "packaging",
        "packaging.version",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["matplotlib", "numpy", "pandas", "scipy", "IPython", "jupyter"],
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="PlexConverter",
    debug=False,
    strip=False,
    upx=False,       # UPX can break DLL loading on some machines
    console=False,   # no console window
    icon=str(_ROOT / "assets" / "icon.ico"),
)
