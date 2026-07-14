"""一键打包:PyInstaller 出单文件、无控制台窗口的 EXE 到 <repo>/dist/。

用法:
  python packaging/build_exe.py            # 新 pywebview 入口 → 'VASP Catalyst Studio'(默认)
  python packaging/build_exe.py --web      # 同上,无参默认的别名(保留肌肉记忆)
  python packaging/build_exe.py --legacy   # 旧 tkinter 入口 → 'VASP Catalyst Studio Legacy'

不手写 .spec(PyInstaller 6.x 的 spec 语法版本脆弱,EXE(onefile=True) 并非合法参数),
改用稳定的 CLI flags:--onefile --windowed。产物落仓库根 dist/,工作目录 build/,
与 .gitignore 的 build/ dist/ 对齐。keyring 后端是动态加载,用 --collect-submodules 收全。
默认(web)分支额外带入 gui_web/assets(目录名须与 resources.asset_dir 的 frozen 分支
'vcstudio_assets' 严格一致)并 --collect-all webview 收全 pywebview 的 clr-loader/bottle/js 资源。
P1b 后 web 成为默认打包入口,tkinter 降级为 --legacy 兜底。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENTRY = os.path.join(ROOT, 'vcstudio', 'gui', '__main__.py')
WEB_ENTRY = os.path.join(ROOT, 'vcstudio', 'gui_web', '__main__.py')
WEB_ASSETS = os.path.join(ROOT, 'vcstudio', 'gui_web', 'assets')

# 排除无关重包以瘦身(numpy 已从依赖移除,这里再兜底排除)
EXCLUDES = ['numpy', 'sklearn', 'scipy', 'PIL', 'matplotlib', 'pandas', 'pytest']


def main() -> int:
    # 无参 = web(默认);--web 保留为默认别名不破坏肌肉记忆;--legacy = 旧 tkinter 兜底
    legacy = '--legacy' in sys.argv[1:]
    name = 'VASP Catalyst Studio Legacy' if legacy else 'VASP Catalyst Studio'
    cmd = [sys.executable, '-m', 'PyInstaller',
           '--onefile', '--windowed', '--clean', '--noconfirm',
           '--name', name,
           '--collect-submodules', 'keyring.backends',
           '--paths', ROOT,
           '--distpath', os.path.join(ROOT, 'dist'),
           '--workpath', os.path.join(ROOT, 'build'),
           '--specpath', os.path.join(ROOT, 'build')]
    for m in EXCLUDES:
        cmd += ['--exclude-module', m]
    if not legacy:
        cmd += ['--add-data', WEB_ASSETS + os.pathsep + 'vcstudio_assets']
        cmd += ['--collect-all', 'webview']
    cmd.append(ENTRY if legacy else WEB_ENTRY)
    print('运行:', ' '.join(cmd))
    return subprocess.call(cmd, cwd=ROOT)


if __name__ == '__main__':
    raise SystemExit(main())
