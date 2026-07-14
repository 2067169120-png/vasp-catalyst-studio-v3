@echo off
rem 一键清理: 删除可再生的临时文件; 旧冒烟产物移入 _回收待删 供确认后手动删除
cd /d "%~dp0"

echo [1/3] 删除可再生垃圾 (build / __pycache__ / .pytest_cache / egg-info) ...
rmdir /s /q build 2>nul
rmdir /s /q .pytest_cache 2>nul
rmdir /s /q vcstudio.egg-info 2>nul
for /f "delims=" %%d in ('dir /ad /b /s __pycache__ 2^>nul') do rd /s /q "%%d" 2>nul

echo [2/3] 把旧测试产物移入 _回收待删 (不直接删, 请自查后手动清空) ...
if not exist _回收待删 mkdir _回收待删
if exist results\smoke move /y results\smoke _回收待删\smoke >nul 2>nul
if exist results\gui_test move /y results\gui_test _回收待删\gui_test >nul 2>nul

echo [3/3] 完成. 保留: tests\(在用测试套件) dist\(现用EXE) results\todo(你的真实作业)
echo.
echo [OK] 清理完毕. 确认 _回收待删 里没有要留的东西后, 手动删除该文件夹即可.
pause
