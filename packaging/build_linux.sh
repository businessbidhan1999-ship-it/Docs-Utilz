#!/usr/bin/env bash
# Build in Debian 12 for compatibility with glibc 2.36 and newer.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
IMAGE="docker.io/library/python:3.12-slim-bookworm"
ENGINE="${ENGINE:-podman}"

echo "==> Building Linux executable in $IMAGE"
"$ENGINE" run --rm -v "$ROOT":/src:z -w /src "$IMAGE" bash -euo pipefail -c '
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends \
        python3-tk tk binutils libgl1 libglib2.0-0 >/dev/null

    python -m venv /build/venv
    . /build/venv/bin/activate
    pip install -q --upgrade pip
    # headless OpenCV: the app draws its own UI with tkinter and never calls
    # cv2.imshow, so the GUI half of OpenCV is dead weight and drags in Qt.
    pip install -q pyinstaller "PyMuPDF>=1.23" "opencv-python-headless>=4.8" \
        "numpy>=1.24" "Pillow>=10"

    pyinstaller --clean --noconfirm \
        --distpath /src/dist/linux --workpath /build/work \
        /src/packaging/Docs-Utilz.spec
    chmod +x /src/dist/linux/Docs-Utilz
'

echo
ls -lh "$ROOT/dist/linux/Docs-Utilz"
echo "==> Done: dist/linux/Docs-Utilz"
