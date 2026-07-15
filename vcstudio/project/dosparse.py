"""vasprun.xml DOS 流式解析(C4)——只抓 <dos> 段,读完即停。

vasprun.xml 可达几十 MB(投影/波函数段占大头):ElementTree.iterparse 流式,
命中 `</dos>` 立即返回,绝不解析后面的大段。输入 file-like(测试 io.StringIO)。
坏 XML/截断/无 DOS 段 → ValueError(api 层兜成结构化 error)。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET


def parse_vasprun_dos(fileobj) -> dict:
    """file-like → ``{'efermi','energies','spin_up','spin_down'|None}``。

    结构(VASP 5/6 一致):``<dos><i name="efermi">…</i><total><array>…
    <set><set comment="spin 1"><r>E total integrated</r>…`` 。
    """
    efermi = None
    spins: list[list[list[float]]] = []   # [spin][row] -> [energy, total, ...]
    cur: list[list[float]] | None = None
    in_total = False
    try:
        for event, el in ET.iterparse(fileobj, events=('start', 'end')):
            if event == 'start':
                if el.tag == 'total':
                    in_total = True
                elif in_total and el.tag == 'set' and \
                        (el.get('comment') or '').startswith('spin'):
                    cur = []
                    spins.append(cur)
                continue
            # end 事件
            if el.tag == 'i' and el.get('name') == 'efermi':
                try:
                    efermi = float((el.text or '').strip())
                except ValueError:
                    pass
            elif el.tag == 'r' and in_total and cur is not None:
                toks = (el.text or '').split()
                if len(toks) >= 2:
                    try:
                        cur.append([float(toks[0]), float(toks[1])])
                    except ValueError:
                        pass
            elif el.tag == 'total':
                in_total = False
            elif el.tag == 'dos':
                break          # DOS 段读完,立即停——不碰后面的投影大段
            el.clear()         # 流式:释放已处理节点内存
    except ET.ParseError as e:
        raise ValueError(f'vasprun.xml 解析失败(文件损坏或截断):{e}')

    if not spins or not spins[0]:
        raise ValueError('该 vasprun.xml 无 DOS 数据(需静态/DOS 计算产出)')
    if efermi is None:
        raise ValueError('vasprun.xml 缺 efermi,无法对齐费米能级')
    return {
        'efermi': efermi,
        'energies': [row[0] for row in spins[0]],
        'spin_up': [row[1] for row in spins[0]],
        'spin_down': [row[1] for row in spins[1]] if len(spins) > 1 else None,
    }
