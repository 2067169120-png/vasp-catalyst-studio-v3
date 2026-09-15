"""vcs 命令行入口。

M1 子命令:`gen` —— POSCAR + 用户 INCAR → VASP 输入四件套。
(submit/status/monitor/analyze 等推迟到 M2-M4。)
中文注释允许,英文标识符。
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys

from vcstudio.generate.job_builder import build_job_dir
from vcstudio.generate.potcar import PotcarError

_PYWEBVIEW_HINT = (
    '错误: 未安装 pywebview(Web 界面依赖)。请先安装:\n'
    '    pip install pywebview\n'
    '  (Windows 还需微软 Edge WebView2 Evergreen 运行时)\n'
    '或改用旧版 tkinter 界面:vcs gui --legacy')


def cmd_gen(args) -> int:
    if not os.path.isfile(args.incar):
        print(f'错误: INCAR 文件不存在: {args.incar}', file=sys.stderr)
        return 1

    kpts = None
    if args.kpoints:
        try:
            kpts = [int(x) for x in args.kpoints.split()]
        except ValueError:
            print(f'错误: --kpoints 需 3 个整数(如 "5 5 1"),得到: {args.kpoints!r}',
                  file=sys.stderr)
            return 1
        if len(kpts) != 3:
            print(f'错误: --kpoints 需恰好 3 个整数,得到: {args.kpoints!r}',
                  file=sys.stderr)
            return 1

    try:
        res = build_job_dir(
            args.poscar, args.incar, args.out,
            calc_type=args.calc_type, kpoints=kpts,
            validate=not args.no_validate, lib_root=args.lib_root)
    except (ValueError, PotcarError, OSError) as e:
        print(f'错误: {e}', file=sys.stderr)
        return 1

    for w in res['warnings']:
        print(f'警告: {w}', file=sys.stderr)
    print(f"已生成: {res['out_dir']}")
    print(f"元素: {res['elements']}  KPOINTS: {res['kpoints']}")

    # 落 job.yaml + 登记台账。失败只告警:绝不因台账问题撤销已生成的四件套。
    try:
        from vcstudio.shared import manifest
        from vcstudio.cluster import ledger
        manifest.create_from_build(res['out_dir'], res, poscar_path=args.poscar,
                                   validate=not args.no_validate)
        ledger.register(res['out_dir'])
        print('已写 job.yaml 并登记台账 (state=CREATED)')
    except Exception as e:
        print(f'警告: job.yaml/台账写入失败(不影响四件套): {e}', file=sys.stderr)
    return 0


def _launch_web_gui() -> int:
    """启动默认 Web GUI(pywebview + gui_web)。缺 pywebview → 中文安装提示,返回 1。

    先探测 webview 是否可导入(find_spec,不真 import,便于 mock 测试),缺失即给
    可操作提示并退出;就绪则委托 gui_web.__main__.main(内含窗口创建/启动兜底)。
    """
    if importlib.util.find_spec('webview') is None:
        print(_PYWEBVIEW_HINT, file=sys.stderr)
        return 1
    from vcstudio.gui_web.__main__ import main as web_main
    return web_main()


def _launch_legacy_gui() -> int:
    """启动旧版 tkinter GUI(vcs gui --legacy)。"""
    from vcstudio.gui.app import main as gui_main
    return gui_main()


def cmd_gui(args) -> int:
    """默认起 Web 界面;--legacy 走旧 tkinter。"""
    if getattr(args, 'legacy', False):
        return _launch_legacy_gui()
    return _launch_web_gui()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='vcs', description='VASP Catalyst Studio — 轻量化 DFT 自动化')
    sub = parser.add_subparsers(dest='command')

    g = sub.add_parser('gen', help='生成 VASP 输入四件套(INCAR/POTCAR/KPOINTS/POSCAR)')
    g.add_argument('--poscar', required=True, help='POSCAR 文件路径')
    g.add_argument('--incar', required=True, help='用户 INCAR 文件路径')
    g.add_argument('-o', '--out', required=True, help='输出作业目录')
    g.add_argument('--calc-type', default='slab',
                   choices=['molecule', 'slab', 'bulk'], help='计算类型(定 KPOINTS 策略)')
    g.add_argument('--kpoints', default=None, help='显式 KPOINTS,如 "5 5 1";缺省自动推荐')
    g.add_argument('--no-validate', action='store_true',
                   help='关闭 INCAR 校验补全(原文照抄,不追加)')
    g.add_argument('--lib-root', default=None, help='POTCAR 库根(覆盖 config)')
    g.set_defaults(func=cmd_gen)

    gui_p = sub.add_parser('gui', help='打开图形界面(默认 Web;--legacy 用旧 tkinter)')
    gui_p.add_argument('--legacy', action='store_true',
                       help='使用旧版 tkinter 界面(默认启动 Web 界面)')
    gui_p.set_defaults(func=cmd_gui)
    return parser


def _ensure_utf8_output():
    """尽量把 stdout/stderr 切到 UTF-8,避免中文在 Windows 控制台乱码。安全降级。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def main(argv=None) -> int:
    _ensure_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, 'command', None):
        parser.print_help(sys.stderr)
        return 2
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
