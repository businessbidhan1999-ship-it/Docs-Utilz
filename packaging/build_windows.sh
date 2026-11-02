#!/usr/bin/env bash
# Build Windows binaries under Wine; use build_windows.bat for native builds.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
IMAGE="docker.io/tobix/pywine:3.12"
ENGINE="${ENGINE:-podman}"

echo "==> Building Windows executable in $IMAGE (Wine)"
"$ENGINE" run --rm -v "$ROOT":/src:z -w /src "$IMAGE" sh -euc '
    wine python -m pip install -q --upgrade pip
    # numpy is held to 1.x *for this build only*. NumPy 2 calls crealf() in the
    # Windows C runtime, which Wine does not implement, so it cannot even be
    # imported here — and PyInstaller has to import a package to work out what
    # to bundle. Nothing about the finished .exe needs the older numpy; it is
    # purely a limit of building Windows binaries on Linux. build_windows.bat,
    # which runs on a real Windows machine, has no such restriction.
    wine python -m pip install -q pyinstaller "PyMuPDF>=1.23" \
        "opencv-python-headless>=4.8" "numpy<2" "Pillow>=10"

    wine python -m PyInstaller --clean --noconfirm \
        --distpath /src/dist/windows --workpath /tmp/work \
        /src/packaging/Docs-Utilz.spec
'

echo
ls -lh "$ROOT/dist/windows/Docs-Utilz.exe"
echo "==> Done: dist/windows/Docs-Utilz.exe"
