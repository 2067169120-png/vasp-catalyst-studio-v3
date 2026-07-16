"""一键打包:PyInstaller 出单文件、无控制台窗口的 EXE 到 <repo>/dist/。

用法:
  python packaging/build_exe.py            # 新 pywebview 入口 → 'VASP Catalyst Studio'(默认)
  python packaging/build_exe.py --web      # 同上,无参默认的别名(保留肌肉记忆)
  python packaging/build_exe.py --legacy   # 旧 tkinter 入口 → 'VASP Catalyst Studio Legacy'
  python packaging/build_exe.py --no-charts  # 不打包 matplotlib/numpy(EXE 更小,原生出图不可用)

不手写 .spec(PyInstaller 6.x 的 spec 语法版本脆弱,EXE(onefile=True) 并非合法参数),
改用稳定的 CLI flags:--onefile --windowed。产物落仓库根 dist/,工作目录 build/,
与 .gitignore 的 build/ dist/ 对齐。keyring 后端是动态加载,用 --collect-submodules 收全。
默认(web)分支额外带入 gui_web/assets(目录名须与 resources.asset_dir 的 frozen 分支
'vcstudio_assets' 严格一致)并 --collect-all webview 收全 pywebview 的 clr-loader/bottle/js 资源。

出图引擎(2026-07-16):默认把 matplotlib/numpy 打进 EXE(原生论文级出图,项目页
「论文级出图」卡片依赖);--collect-data matplotlib 收 mpl-data(字体/色图/rc,缺了
import 即崩)。嫌大用 --no-charts 恢复旧瘦身行为(出图按钮会提示未安装可选依赖)。
pythonnet(clr)/paramiko 链按环境条件收集:装了才加 flag,防在未装机器上打包报错。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENTRY = os.path.join(ROOT, 'vcstudio', 'gui', '__main__.py')
WEB_ENTRY = os.path.join(ROOT, 'vcstudio', 'gui_web', '__main__.py')
WEB_ASSETS = os.path.join(ROOT, 'vcstudio', 'gui_web', 'assets')
CONFIG_EXAMPLE = os.path.join(ROOT, 'config.example.yaml')

# 无关重包始终排除;matplotlib/numpy 由 --no-charts 决定(见 main)
EXCLUDES_ALWAYS = ['sklearn', 'scipy', 'PIL', 'pandas', 'pytest']


def _has(mod: str) -> bool:
    """本机装了该包才加对应 collect flag(PyInstaller 对未安装包直接报错)。"""
    return importlib.util.find_spec(mod) is not None


def main() -> int:
    # 无参 = web(默认);--web 保留为默认别名不破坏肌肉记忆;--legacy = 旧 tkinter 兜底
    args = sys.argv[1:]
    legacy = '--legacy' in args
    no_charts = '--no-charts' in args
    name = 'VASP Catalyst Studio Legacy' if legacy else 'VASP Catalyst Studio'
    cmd = [sys.executable, '-m', 'PyInstaller',
           '--onefile', '--windowed', '--clean', '--noconfirm',
           '--name', name,
           '--collect-submodules', 'keyring.backends',
           '--paths', ROOT,
           '--distpath', os.path.join(ROOT, 'dist'),
           '--workpath', os.path.join(ROOT, 'build'),
           '--specpath', os.path.join(ROOT, 'build')]

    excludes = list(EXCLUDES_ALWAYS)
    if no_charts or not _has('matplotlib'):
        # 未装/显式关闭 → 维持旧瘦身行为;GUI 出图按钮会提示"未安装可选依赖"
        excludes += ['numpy', 'matplotlib']
        if not no_charts:
            print('提示:本机未安装 matplotlib,EXE 将不含原生出图引擎'
                  '(pip install matplotlib numpy 后重打包即可启用)')
    else:
        # 原生出图进包:mpl-data(字体/色图/matplotlibrc)必须显式收,缺了 import 即崩
        cmd += ['--collect-data', 'matplotlib']
    for m in excludes:
        cmd += ['--exclude-module', m]

    # SSH 链(paramiko→cryptography/bcrypt/nacl 有编译后端,自动分析偶有缺口):
    # 干净 Windows 上"连集群即 ImportError"的主凶,装了就显式收全
    if _has('paramiko'):
        cmd += ['--collect-submodules', 'paramiko']
    if _has('nacl'):
        cmd += ['--collect-all', 'nacl']

    if not legacy:
        cmd += ['--add-data', WEB_ASSETS + os.pathsep + 'vcstudio_assets']
        if os.path.isfile(CONFIG_EXAMPLE):   # 配置模板随包,首启可自取
            cmd += ['--add-data', CONFIG_EXAMPLE + os.pathsep + '.']
        cmd += ['--collect-all', 'webview']
        # pywebview 的 EdgeChromium 后端跑在 pythonnet(clr)上;不收全则窗口起不来
        if _has('pythonnet') or _has('clr'):
            cmd += ['--collect-all', 'pythonnet']
    cmd.append(ENTRY if legacy else WEB_ENTRY)
    print('运行:', ' '.join(cmd))
    return subprocess.call(cmd, cwd=ROOT)


if __name__ == '__main__':
    raise SystemExit(main())
