"""vcs 命令行入口。

M1 子命令:`gen` —— POSCAR + 用户 INCAR → VASP 输入四件套。
(submit/status/monitor/analyze 等推迟到 M2-M4。)
中文注释允许,英文标识符。
"""
from __future__ import annotations

import argparse
import os
import sys

from vcstudio.generate.job_builder import build_job_dir
from vcstudio.generate.potcar import PotcarError


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
    return 0


def cmd_gui(args) -> int:
    from vcstudio.gui.app import main as gui_main
    return gui_main()


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

    gui_p = sub.add_parser('gui', help='打开图形界面(生成 + 集群配置)')
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
