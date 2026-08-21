@echo off
setlocal EnableExtensions

set "REPO_ROOT=%~dp0..\.."
pushd "%REPO_ROOT%" || exit /b 1

echo [VCS] Building the full Windows executable...
python -X utf8 packaging\build_exe.py --full
set "BUILD_EXIT=%ERRORLEVEL%"

if "%BUILD_EXIT%"=="0" (
  echo.
  echo [OK] Built: dist\VASP Catalyst Studio.exe
) else (
  echo.
  echo [ERROR] Packaging failed with exit code %BUILD_EXIT%.
)

popd
pause
exit /b %BUILD_EXIT%
