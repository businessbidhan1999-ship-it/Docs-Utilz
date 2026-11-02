@echo off
REM Build the Windows executable natively, on any Windows PC with Python 3.9+
REM installed from python.org (tick "Add python.exe to PATH" in the installer).
REM
REM Double-click this file, or run it from the packaging folder. The result
REM lands in ..\dist\windows\Docs-Utilz.exe
setlocal
cd /d "%~dp0.."

echo ==^> Creating a build environment
python -m venv .buildenv || goto :fail
call .buildenv\Scripts\activate.bat

python -m pip install --upgrade pip
REM Headless OpenCV: the app draws its own interface and never uses cv2's.
python -m pip install pyinstaller "PyMuPDF>=1.23" "opencv-python-headless>=4.8" "numpy>=1.24" "Pillow>=10" || goto :fail

echo ==^> Building
python -m PyInstaller --clean --noconfirm ^
    --distpath dist\windows --workpath build\work ^
    packaging\Docs-Utilz.spec || goto :fail

echo.
echo ==^> Done: dist\windows\Docs-Utilz.exe
pause
exit /b 0

:fail
echo.
echo Build failed. Check the messages above.
pause
exit /b 1
