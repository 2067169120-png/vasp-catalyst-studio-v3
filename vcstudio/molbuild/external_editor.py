"""外部结构编辑器联动(GaussView / Avogadro 等)——导出→用户手改→回读的往返闭环。

用户想在 GaussView/Avogadro 里手动摆原子/调构象:本模块把当前结构写成固定名的临时编辑
文件(xyz 或 mol),记录 mtime;用户用外部编辑器打开(open_with 探测→调用→降级)改完
保存后,check_reimport 靠 mtime 判是否动过,reimport 再把 xyz/mol **纯手写解析**回读
(不依赖 RDKit,回读永不因缺可选依赖失败)。

所有 IO / subprocess 异常统一转中文 error 不抛;subprocess 经 popen= / startfile= 注入
可替身(测试)。中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import subprocess
import tempfile

from vcstudio.molbuild import molinfo, smiles3d

# 固定编辑文件名(基名):便于用户/GUI 定位"当前正在外部编辑"的那份结构。
_EDIT_BASENAME = 'vcstudio_external_edit'


def export_for_editor(elements, coords, fmt: str = 'xyz', workdir=None) -> dict:
    """把结构写成临时编辑文件(固定名 vcstudio_external_edit.{xyz|mol})。

    workdir 缺省用系统临时目录。返回 ``{'ok','path','mtime','error'}``:格式非法 / 元素与
    坐标不等长 / 写盘失败 → ok=False + 中文 error(不抛)。mtime 供后续 check_reimport 比对。
    """
    fmt = str(fmt).lower()
    if fmt not in ('xyz', 'mol'):
        return {'ok': False, 'path': None, 'mtime': None,
                'error': f'未知导出格式 {fmt!r};可选 xyz/mol'}
    if len(list(elements)) != len(list(coords)):
        return {'ok': False, 'path': None, 'mtime': None, 'error': '元素数与坐标数不一致'}
    workdir = str(workdir) if workdir else tempfile.gettempdir()
    try:
        os.makedirs(workdir, exist_ok=True)
        path = os.path.join(workdir, f'{_EDIT_BASENAME}.{fmt}')
        text = (smiles3d.to_xyz(elements, coords) if fmt == 'xyz'
                else smiles3d.to_mol(elements, coords))
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        mtime = os.path.getmtime(path)
    except OSError as e:
        return {'ok': False, 'path': None, 'mtime': None, 'error': f'写编辑文件失败:{e}'}
    return {'ok': True, 'path': path, 'mtime': mtime, 'error': ''}


def check_reimport(path, last_mtime) -> dict:
    """比对编辑文件 mtime 判断用户是否改动过。返回 ``{'changed','mtime'}``。

    文件不存在 → changed=False, mtime=None。last_mtime 为 None(从未记录)且文件在 →
    视作已变化(changed=True)。
    """
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {'changed': False, 'mtime': None}
    changed = last_mtime is None or mtime > float(last_mtime)
    return {'changed': changed, 'mtime': mtime}


def reimport(path) -> dict:
    """回读编辑文件 → ``{'ok','elements','coords','error'}``。

    按扩展名或内容判定 xyz / mol(V2000),**纯手写解析**(不依赖 RDKit)。读盘失败 / 解析
    失败 → ok=False + 中文 error(不抛)。
    """
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
    except OSError as e:
        return {'ok': False, 'elements': [], 'coords': [], 'error': f'读取编辑文件失败:{e}'}
    ext = os.path.splitext(str(path))[1].lower()
    try:
        if ext == '.mol' or (ext != '.xyz' and _looks_like_mol(text)):
            elements, coords = parse_mol(text)
        else:
            elements, coords = parse_xyz(text)
    except ValueError as e:
        return {'ok': False, 'elements': [], 'coords': [], 'error': str(e)}
    return {'ok': True, 'elements': elements, 'coords': coords, 'error': ''}


def open_with(path, editor_exe=None, *, popen=None, startfile=None) -> dict:
    """用外部编辑器打开文件。返回 ``{'ok','error'}``。

    editor_exe 给定 → ``subprocess.Popen([exe, path])``;未给 → 平台默认(Windows
    os.startfile / 其它 xdg-open)降级。全部异常转中文 error(不抛)。popen / startfile
    可注入替身(测试)。
    """
    if not os.path.exists(path):
        return {'ok': False, 'error': f'文件不存在:{path}'}
    _popen = popen or subprocess.Popen
    try:
        if editor_exe:
            _popen([str(editor_exe), str(path)])
            return {'ok': True, 'error': ''}
        if os.name == 'nt':
            _startfile = startfile or getattr(os, 'startfile', None)
            if _startfile is None:
                return {'ok': False, 'error': '当前平台不支持 os.startfile'}
            _startfile(str(path))
        else:
            _popen(['xdg-open', str(path)])
        return {'ok': True, 'error': ''}
    except (OSError, ValueError) as e:
        return {'ok': False, 'error': f'打开外部编辑器失败:{e}'}


# ── 纯手写解析(xyz / mol V2000;不依赖 RDKit) ─────────────────────────────────

def _looks_like_mol(text: str) -> bool:
    """内容嗅探:含 V2000/V3000 标记或 'M  END' → 视作 MOL。"""
    head = text[:2048]
    return ('V2000' in head) or ('V3000' in head) or ('M  END' in text)


def _norm_element(tok: str) -> str:
    """XYZ/MOL 原子标记 → 规整元素符号:纯数字按原子序映射,否则规整大小写。"""
    s = str(tok).strip()
    if s.isdigit():
        return molinfo.element_symbol(int(s))
    return molinfo._norm_symbol(s)


def parse_xyz(text: str):
    """解析 XYZ 文本 → ``(elements, coords)``。行数/坐标非法 → ValueError。"""
    lines = text.splitlines()
    if not lines or not lines[0].strip():
        raise ValueError('XYZ 文件为空或首行缺原子数')
    try:
        n = int(lines[0].split()[0])
    except (ValueError, IndexError):
        raise ValueError(f'XYZ 首行原子数解析失败:{lines[0]!r}')
    if n < 0:
        raise ValueError(f'XYZ 原子数为负:{n}')
    elements: list[str] = []
    coords: list[list[float]] = []
    for ln in lines[2:]:                                       # 跳过原子数行 + 注释行
        toks = ln.split()
        if len(toks) < 4:
            continue
        try:
            xyz = [float(toks[1]), float(toks[2]), float(toks[3])]
        except ValueError:
            raise ValueError(f'XYZ 坐标行解析失败:{ln!r}')
        elements.append(_norm_element(toks[0]))
        coords.append(xyz)
        if len(elements) == n:
            break
    if len(elements) != n:
        raise ValueError(f'XYZ 原子数不符:声明 {n} 实得 {len(elements)}')
    return elements, coords


def parse_mol(text: str):
    """解析 MDL MOL V2000 文本 → ``(elements, coords)``(仅原子块)。非法 → ValueError。"""
    lines = text.splitlines()
    if len(lines) < 4:
        raise ValueError('MOL 文件行数不足(缺 counts 行)')
    counts = lines[3]
    n_atoms = None
    if len(counts) >= 3:
        try:
            n_atoms = int(counts[:3])
        except ValueError:
            n_atoms = None
    if n_atoms is None:
        try:
            n_atoms = int(counts.split()[0])
        except (ValueError, IndexError):
            raise ValueError(f'MOL counts 行原子数解析失败:{counts!r}')
    if n_atoms < 0:
        raise ValueError(f'MOL 原子数为负:{n_atoms}')
    atom_lines = lines[4:4 + n_atoms]
    if len(atom_lines) < n_atoms:
        raise ValueError(f'MOL 原子块不足:期望 {n_atoms} 行,实得 {len(atom_lines)}')
    elements: list[str] = []
    coords: list[list[float]] = []
    for ln in atom_lines:
        x, y, z, sym = _parse_mol_atom_line(ln)
        elements.append(_norm_element(sym))
        coords.append([x, y, z])
    return elements, coords


def _parse_mol_atom_line(ln: str):
    """MOL 原子行 → (x, y, z, symbol)。先按空白切分,失败退定宽(V2000 列位)。"""
    toks = ln.split()
    if len(toks) >= 4:
        try:
            return float(toks[0]), float(toks[1]), float(toks[2]), toks[3]
        except ValueError:
            pass
    try:                                                       # 定宽回退:x[0:10] y[10:20] z[20:30] sym[31:34]
        x, y, z = float(ln[0:10]), float(ln[10:20]), float(ln[20:30])
        sym = ln[31:34].strip()
        if not sym:
            raise ValueError('缺元素符号')
        return x, y, z, sym
    except (ValueError, IndexError):
        raise ValueError(f'MOL 原子行解析失败:{ln!r}')
