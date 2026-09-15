@echo off
setlocal EnableExtensions

set "REPO_ROOT=%~dp0..\.."
pushd "%REPO_ROOT%" || exit /b 1

echo [1/2] Removing reproducible caches and build intermediates...
for %%D in (build .pytest_cache .ruff_cache vcstudio.egg-info) do (
  if exist "%%D" rmdir /s /q "%%D"
)
for /d /r %%D in (__pycache__) do (
  if exist "%%D" rmdir /s /q "%%D"
)

echo [2/2] Preserving dist\ artifacts and results\ research outputs.
echo.
echo [OK] Generated caches were removed. Source, artifacts, and results were preserved.

popd
pause
exit /b 0
