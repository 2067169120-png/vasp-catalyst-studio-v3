#!/usr/bin/env python
"""从 diagnose.py 程序化生成 docs/failure-taxonomy.md(失败分类表)。

单一真相源:分类学与目标状态取自 ``diagnose.FAILURE_TO_STATE`` /
``diagnose.RESTARTABLE`` / ``diagnose._VASP_ERROR_TABLE``——代码改了这份表就变,
杜绝文档与代码漂移。人类可读的"触发证据"描述维护在本工具内(不改核心 diagnose)。

用法::

    python tools/gen_failure_table.py            # 生成/覆盖 docs/failure-taxonomy.md
    python tools/gen_failure_table.py --check     # 只校验:文件与代码是否同步(CI/测试用)

``--check`` 在文件缺失或内容过期时以非零码退出,并打印差异提示。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vcstudio.cluster import diagnose as dg  # noqa: E402

DOC_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'docs', 'failure-taxonomy.md')

# 各分类的人类可读"触发证据"(键 = diagnose 分类常量;缺失的类回落占位,行仍在,保同步)
_EVIDENCE = {
    dg.CONVERGED: 'OUTCAR 达到要求精度且能量物理合理(末离子步电子真收敛)',
    dg.NONCONVERGED: 'SCF/几何未收敛(NELM/NSW 耗尽);有输出、无硬崩信号、无收敛串',
    dg.WALLTIME: '墙钟耗尽(调度器 TIMEOUT;或裸退出码 137 且有部分输出,疑超墙钟被杀)',
    dg.ZBRENT: '离子步线搜索崩(ZBRENT: fatal / bracketing interval incorrect / can not reach accuracy)',
    dg.OOM: '内存耗尽(调度器 OOM;或日志 oom-kill / out of memory)',
    dg.CANCELLED: '作业被取消(调度器 CANCELLED)',
    dg.NODE_FAIL: '计算节点故障(调度器 NODE_FAIL)',
    dg.SIGSEGV: '段错误(SIGSEGV / segmentation fault;或 Intel-MPI 段错误末尾全 1 退出列)',
    dg.SILENT_EXIT: '退出码 0 但 OUTCAR 为空(沉默退出,疑输入/环境问题)',
    dg.NO_OUTPUT: '零输出(OUTCAR/OSZICAR 均缺失或空,启动即死;疑缺 POTCAR/输入错)',
    dg.BAD_ENERGY: '有收敛串但能量不合理(E≥0 或 |E|>1e4;疑结构重叠/SCF 发散)',
    dg.DISK_FULL: '磁盘满 / IO 错误(No space left / quota exceeded / I-O error / read-only fs)',
    dg.SCF_SLOSHING: '电子步震荡(某 SCF 块打满 NELM 且末步 |dE|>1e-2 eV);或收敛串为 NELM 耗尽假阳性',
    dg.USER_STOPPED: 'STOPCAR 人工叫停(OUTCAR 见 soft stop);非失败,由人决定续算/放弃',
    dg.UNKNOWN: '规则不覆盖(调度器泛化失败且无具体原因/日志签名)',
}

_HEADER = """\
<!-- 本文件由 tools/gen_failure_table.py 从 diagnose.py 自动生成,请勿手改。 -->
<!-- Auto-generated from diagnose.py by tools/gen_failure_table.py — do not edit by hand. -->

# 失败分类表 / Failure taxonomy

VASP 作业失败分类器(`vcstudio/cluster/diagnose.py`)把"作业没成功"变成**可行动的
原因**:综合 调度器终态 + 退出码 + OUTCAR/OSZICAR 完整性 + 日志硬崩签名 + 收敛串 +
能量物理合理性 → **单一分类 + 目标状态 + 是否可续算 + 证据**。规则不覆盖一律
`UNKNOWN` → 交人工(显式状态,绝不静默)。

本表由代码程序化生成:分类学与目标状态直接取自 `diagnose.FAILURE_TO_STATE` /
`diagnose.RESTARTABLE` / `diagnose._VASP_ERROR_TABLE`,与实现零漂移(`tests/`
有同步守卫,代码改了不重新生成即测试失败)。

- **作业分类**:{n_class} 类分类结果(含 `CONVERGED` 成功态),映射到
  **{n_states} 个目标状态**(`{states}`);其中 **{n_restart} 类可 CONTCAR 续算**。
- **VASP 内部错误签名**:{n_vasp} 条(字面串核对自 pymatgen Custodian
  VaspErrorHandler);命中即具体命名 + 标准补救提示,状态 `NEEDS_HUMAN`——本平台守
  方法学主权,**绝不自动改用户 INCAR**,只精确诊断并交人工。
"""


def _md_cell(s: str) -> str:
    """markdown 表格单元格转义:裸竖线 | 会截断列,转成 \\|。"""
    return str(s).replace('|', '\\|')


def _clean_pattern(rx) -> str:
    """正则 pattern → markdown 表格安全的简短签名串(| → /,截断,反引号包裹)。"""
    p = rx.pattern.replace('|', ' / ').replace('\\s*', ' ').replace('\\', '')
    p = ' '.join(p.split())
    if len(p) > 90:
        p = p[:88] + '…'
    return p


def render() -> str:
    states = sorted(set(dg.FAILURE_TO_STATE.values()))
    head = _HEADER.format(
        n_class=len(dg.FAILURE_TO_STATE), n_states=len(states),
        states=' / '.join(states), n_restart=len(dg.RESTARTABLE),
        n_vasp=len(dg._VASP_ERROR_TABLE))

    lines = [head, '', '## 作业失败分类 / Job classification', '',
             '| 分类 | 触发证据 | 目标状态 | 可否续算 |',
             '|---|---|---|---|']
    for cls, state in dg.FAILURE_TO_STATE.items():
        ev = _EVIDENCE.get(cls, '(见 diagnose.py 源码注释)')
        restart = '✅ 可 CONTCAR 续算' if cls in dg.RESTARTABLE else '—'
        lines.append(f'| `{cls}` | {_md_cell(ev)} | `{state}` | {restart} |')

    lines += ['', '## VASP 内部错误签名 / VASP internal-error signatures', '',
              '命中以下任一日志签名 → 具体命名 + 补救提示,目标状态一律 `NEEDS_HUMAN`'
              '(不可自动续算,靠改 INCAR 修复,交人工)。', '',
              '| 分类 | 触发证据(日志签名) | 补救提示 | 目标状态 |',
              '|---|---|---|---|']
    for rx, label, hint in dg._VASP_ERROR_TABLE:
        lines.append(f'| `{label}` | `{_clean_pattern(rx)}` | {_md_cell(hint)} | '
                     '`NEEDS_HUMAN` |')
    lines.append('')
    return '\n'.join(lines)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    content = render()
    if '--check' in argv:
        if not os.path.isfile(DOC_PATH):
            print(f'[check] 缺失:{DOC_PATH};请运行 python tools/gen_failure_table.py',
                  file=sys.stderr)
            return 1
        with open(DOC_PATH, 'r', encoding='utf-8') as f:
            existing = f.read()
        if existing != content:
            print('[check] docs/failure-taxonomy.md 与 diagnose.py 不同步;'
                  '请运行 python tools/gen_failure_table.py 重新生成', file=sys.stderr)
            return 1
        print('[check] docs/failure-taxonomy.md 与代码同步 ✓')
        return 0
    os.makedirs(os.path.dirname(DOC_PATH), exist_ok=True)
    with open(DOC_PATH, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f'已生成 {DOC_PATH}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
