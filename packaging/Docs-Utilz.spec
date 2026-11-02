import sys
from pathlib import Path

SRC = Path(SPECPATH).parent          # the project root, one level up
WIN = sys.platform.startswith("win")

icon = SPECPATH + ("/icon.ico" if WIN else "/icon.png")
if not Path(icon).exists():
    icon = None

# Exclude unused frameworks from the standalone executable.
EXCLUDE = [
    "matplotlib", "scipy", "pandas", "IPython", "jupyter", "notebook",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx", "sphinx", "pytest",
    "lib2to3", "pydoc_data", "test", "tests",
]

a = Analysis(
    [str(SRC / "docs_utilz" / "__main__.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=[],
    hiddenimports=["PIL._tkinter_finder", "docs_utilz.app", "docs_utilz.cli", "docs_utilz.engine", "docs_utilz.tools", "docs_utilz.devices", "docs_utilz.ui_theme"],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDE,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Docs-Utilz",
    debug=False,
    bootloader_ignore_signals=False,
    # Stripping bundled OpenBLAS can corrupt ELF alignment.
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # it is a GUI app; errors surface in the app's log
    disable_windowed_traceback=False,
    icon=icon,
)
