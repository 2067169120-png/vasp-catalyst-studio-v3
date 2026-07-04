@echo off
rem 一键重新打包 EXE(需本机已安装 pyinstaller: pip install pyinstaller)
cd /d "%~dp0"
python packaging\build_exe.py
if %errorlevel%==0 (
  echo.
  echo [OK] 打包成功: dist\VASP Catalyst Studio.exe
) else (
  echo.
  echo [X] 打包失败, 请把上面的报错发给 Claude
)
pause
