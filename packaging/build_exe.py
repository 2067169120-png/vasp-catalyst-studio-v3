"""Build a self-contained Windows executable with PyInstaller.

The executable cannot install Python packages into itself after it is frozen.  Optional
features therefore have to be selected *when the executable is built*:

``python packaging/build_exe.py``
    Build the normal, full Web executable. Charts (matplotlib/numpy), molecular
    editing (RDKit), Word/PDF report export and PDF reading are required and
    explicitly collected. A missing prerequisite is a build error; the script
    never silently emits a feature-incomplete executable.

``python packaging/build_exe.py --lite``
    Build a deliberately small executable without charts, numeric structure tools,
    RDKit, Word/PDF report export or PDF reading.

``python packaging/build_exe.py --no-charts``
    Preserve the historical meaning of this flag: omit matplotlib only, while keeping
    numpy, RDKit and document features.

``python packaging/build_exe.py --with-decimer``
    Additionally bundle DECIMER OCSR.  DECIMER/TensorFlow is intentionally excluded
    from both normal profiles unless this opt-in flag is present because it can make a
    one-file executable extremely large.

``--web`` remains a no-op alias for the default Web entry and ``--legacy`` selects the
old tkinter entry.  CLI flags are used instead of a hand-written spec so the recipe
stays compatible with PyInstaller 6.x.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENTRY = os.path.join(ROOT, 'vcstudio', 'gui', '__main__.py')
WEB_ENTRY = os.path.join(ROOT, 'vcstudio', 'gui_web', '__main__.py')
WEB_ASSETS = os.path.join(ROOT, 'vcstudio', 'gui_web', 'assets')
CONFIG_EXAMPLE = os.path.join(ROOT, 'config.example.yaml')

# These packages are not used by the shipped application.  Keeping the exclusions
# profile-independent also prevents an unrelated package in the build environment from
# unexpectedly inflating the executable.
EXCLUDES_ALWAYS = ('sklearn', 'scipy', 'pandas', 'pytest')
EXCLUDES_LITE = (
    'numpy', 'matplotlib', 'rdkit', 'PIL', 'docx', 'pypdf', 'reportlab',
)
EXCLUDES_DECIMER = ('DECIMER', 'decimer', 'tensorflow', 'keras')

# (import name, human-facing package name).  Core dependencies are required even for
# the lite build: otherwise the executable starts but cluster/config/keyring features
# fail later, which is exactly the partial-build failure this preflight prevents.
CORE_REQUIREMENTS = (
    ('PyInstaller', 'pyinstaller'),
    ('yaml', 'PyYAML'),
    ('paramiko', 'paramiko'),
    ('keyring', 'keyring'),
)
WEB_REQUIREMENTS = (('webview', 'pywebview'),)
FULL_REQUIREMENTS = (
    ('matplotlib', 'matplotlib'),
    ('numpy', 'numpy'),
    ('rdkit', 'rdkit'),
    ('docx', 'python-docx'),
    ('pypdf', 'pypdf'),
    ('reportlab', 'reportlab'),
)
DECIMER_REQUIREMENTS = (
    ('DECIMER', 'decimer'),
    ('tensorflow', 'tensorflow'),
)


class BuildConfigurationError(RuntimeError):
    """The requested build cannot safely be produced in this environment."""


@dataclass(frozen=True)
class BuildOptions:
    legacy: bool = False
    lite: bool = False
    no_charts: bool = False
    with_decimer: bool = False


def _has(mod: str) -> bool:
    """Return whether *mod* can be discovered without importing a heavy package."""
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _parse_args(args: Sequence[str]) -> BuildOptions:
    known = {'--web', '--legacy', '--full', '--lite', '--no-charts', '--with-decimer'}
    unknown = [arg for arg in args if arg not in known]
    if unknown:
        raise BuildConfigurationError(f"未知参数: {' '.join(unknown)}")
    if '--web' in args and '--legacy' in args:
        raise BuildConfigurationError('--web 与 --legacy 不能同时使用')

    lite = '--lite' in args
    no_charts = '--no-charts' in args
    if '--full' in args and (lite or no_charts):
        raise BuildConfigurationError('--full 与 --lite/--no-charts 不能同时使用')
    if lite and '--with-decimer' in args:
        raise BuildConfigurationError('--with-decimer 只支持完整版构建，不能与 --lite 同时使用')
    return BuildOptions(
        legacy='--legacy' in args,
        lite=lite,
        no_charts=no_charts,
        with_decimer='--with-decimer' in args,
    )


def _requirements(options: BuildOptions) -> tuple[tuple[str, str], ...]:
    required = list(CORE_REQUIREMENTS)
    if not options.legacy:
        required.extend(WEB_REQUIREMENTS)
    if not options.lite:
        required.extend(
            item for item in FULL_REQUIREMENTS
            if not (options.no_charts and item[0] == 'matplotlib')
        )
    if options.with_decimer:
        required.extend(DECIMER_REQUIREMENTS)
    return tuple(required)


def _install_hint(options: BuildOptions) -> str:
    extras = ['packaging']
    if not options.legacy:
        extras.append('gui')
    if not options.lite:
        extras.append('numeric' if options.no_charts else 'charts')
        extras.extend(('mol', 'docs'))
    if options.with_decimer:
        extras.append('ocsr')
    joined = ','.join(extras)
    executable = str(sys.executable)
    if os.name == 'nt' or (len(executable) > 2 and executable[1] == ':'
                           and executable[2] in {'\\', '/'}):
        # The build error is commonly copied into PowerShell. A quoted path is
        # only a string there unless the call operator is present.
        executable = f'& "{executable}"'
    elif any(ch.isspace() for ch in executable):
        executable = f'"{executable}"'
    return f'{executable} -m pip install -e ".[{joined}]"'


def _preflight(options: BuildOptions) -> None:
    missing = [label for mod, label in _requirements(options) if not _has(mod)]
    if missing:
        profile = ('轻量版' if options.lite else
                   '无原生出图版' if options.no_charts else '完整版')
        detail = '、'.join(missing)
        hint = _install_hint(options)
        lite_hint = '' if options.lite else (
            '\n若确实不需要论文出图和分子编辑，可显式构建轻量版:'
            '\n  python packaging/build_exe.py --lite'
        )
        raise BuildConfigurationError(
            f'{profile} EXE 缺少关键构建依赖: {detail}\n'
            f'请先在当前构建 Python 中安装完整依赖:\n  {hint}'
            f'{lite_hint}\n'
            '冻结 EXE 运行后不能再通过 pip 为自身补装这些模块。'
        )

    required_paths = [ENTRY if options.legacy else WEB_ENTRY]
    if not options.legacy:
        required_paths.append(WEB_ASSETS)
    missing_paths = [path for path in required_paths if not os.path.exists(path)]
    if missing_paths:
        raise BuildConfigurationError(
            '构建输入缺失，请从完整仓库运行脚本: ' + '、'.join(missing_paths)
        )


def build_command(args: Sequence[str] = ()) -> tuple[list[str], BuildOptions]:
    """Validate *args* and return the deterministic PyInstaller command.

    Keeping command construction separate from :func:`main` makes the packaging policy
    testable without launching a costly PyInstaller build.
    """
    options = _parse_args(args)
    _preflight(options)

    name = 'VASP Catalyst Studio Legacy' if options.legacy else 'VASP Catalyst Studio'
    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--onefile', '--windowed', '--clean', '--noconfirm',
        '--name', name,
        '--collect-submodules', 'keyring.backends',
        '--paths', ROOT,
        '--distpath', os.path.join(ROOT, 'dist'),
        '--workpath', os.path.join(ROOT, 'build'),
        '--specpath', os.path.join(ROOT, 'build'),
    ]

    excludes = list(EXCLUDES_ALWAYS)
    if options.lite:
        excludes.extend(EXCLUDES_LITE)
    else:
        # These packages use dynamic imports, data files and/or binary extensions;
        # PyInstaller's static import scan alone is insufficient.
        if options.no_charts:
            excludes.append('matplotlib')
        else:
            cmd += ['--collect-all', 'matplotlib']
        cmd += ['--collect-binaries', 'numpy']
        cmd += ['--collect-all', 'rdkit']
        cmd += ['--collect-all', 'docx']
        cmd += ['--collect-all', 'pypdf']
        cmd += ['--collect-all', 'reportlab']

    if options.with_decimer:
        cmd += ['--collect-all', 'DECIMER']
        cmd += ['--collect-all', 'tensorflow']
    else:
        excludes.extend(EXCLUDES_DECIMER)

    for mod in excludes:
        cmd += ['--exclude-module', mod]

    # Paramiko and pynacl load cryptographic backends dynamically.  Paramiko is a core
    # prerequisite; pynacl remains conditional because Paramiko can use other backends.
    cmd += ['--collect-submodules', 'paramiko']
    if _has('nacl'):
        cmd += ['--collect-all', 'nacl']

    if not options.legacy:
        cmd += ['--add-data', WEB_ASSETS + os.pathsep + 'vcstudio_assets']
        if os.path.isfile(CONFIG_EXAMPLE):
            cmd += ['--add-data', CONFIG_EXAMPLE + os.pathsep + '.']
        cmd += ['--collect-all', 'webview']
        # The EdgeChromium backend uses pythonnet on Windows.  It is a platform-specific
        # pywebview dependency, so collect it when the selected build environment has it.
        if _has('pythonnet') or _has('clr'):
            cmd += ['--collect-all', 'pythonnet']

    cmd.append(ENTRY if options.legacy else WEB_ENTRY)
    return cmd, options


def _usage() -> str:
    return (
        '用法: python packaging/build_exe.py '
        '[--web|--legacy] [--full|--lite|--no-charts] [--with-decimer]\n'
        '默认构建完整版 Web EXE（charts + RDKit + Word/PDF，不含 DECIMER）。\n'
        '--no-charts 仅关闭 matplotlib；--lite 还会关闭数值结构工具、分子编辑和文档能力。'
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if '--help' in args or '-h' in args:
        print(_usage())
        return 0
    try:
        cmd, options = build_command(args)
    except BuildConfigurationError as exc:
        print(f'构建前检查失败:\n{exc}', file=sys.stderr)
        return 2

    profile = ('轻量版' if options.lite else
               '无原生出图版' if options.no_charts else '完整版')
    decimer = '，含 DECIMER' if options.with_decimer else '，不含 DECIMER'
    print(f'构建配置: {profile}{decimer}')
    print('运行:', subprocess.list2cmdline(cmd))
    return subprocess.call(cmd, cwd=ROOT)


if __name__ == '__main__':
    raise SystemExit(main())
