"""CI-NEB 作业生成端(Phase B 过渡态计算)——初/末态 → 标准 VASP NEB 目录树。

VASP NEB 与"一目录一作业"不同:一个作业含多个 image 子目录(00 初态、01..N 中间
插值 image、N+1 末态),根目录放共享的 INCAR/POTCAR/KPOINTS。本模块负责:

- ``interpolate_images``:两端分数坐标**线性插值**生成中间 image。逐原子取**最短镜像
  位移**(位移 wrap 到 [−0.5,0.5)),避免原子跨胞跳变(如 0.95→0.05 应走 +0.1 的短路径
  而非穿过整胞);插值后逐 image 查周期最小原子间距,重叠即拒绝(防插出病态几何)。
- ``build_neb_dir``:落标准 NEB 目录树 + 根 INCAR(在用户 INCAR 上**只补不改**:补
  IMAGES/SPRING/IBRION/LCLIMB 等 NEB 必需键,已存在的键一律保留用户值)+ job.yaml。
- ``neb_preflight``:提交前告警清单(端点弛豫/并行整除/ISYM/NSW)。

科学口径(见模块常量与 _incar_completions 注释):
- **IBRION**:缺省补 IBRION=1(VASP 内置准牛顿优化器驱动 NEB),不强制 IBRION=3+POTIM=0
  ——后者把优化交给 VTST 的 IOPT 优化器,须集群安装 VTST 补丁;POTIM 留 VASP 默认。
- **SPRING**:缺省 −5.0 eV/Å²(NEB 弹簧常数惯例)。
- **LCLIMB**:CI-NEB(爬坡镜像)开关,属 VTST 扩展标签;写入时加注释说明依赖(原生
  VASP 忽略此标签,退化为普通 NEB)。

纯 Python(不引 numpy 的 np.linalg 惰性子模块坑,与 slab_builder 同口径手写 3×3 求逆),
复用 poscar/structure_view/slab_builder 既有解析,不另造 parser。中文注释,英文标识符。
"""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path

from vcstudio.generate.poscar import parse_poscar_species
from vcstudio.generate.structure_view import parse_positions
from vcstudio.generate.slab_builder import min_interatomic_distance
from vcstudio.generate.incar_builder import parse_incar
from vcstudio.generate.kpoints import recommend_kpoints, kpoints_str
from vcstudio.shared import manifest as manifest_mod

# 续算/插值前几何健全阈值(Å):周期最小原子间距低于此值判原子重叠(< 最短化学键 H-H 0.74;
# 与 submitter.MIN_INTERATOMIC_OK 同口径)。
MIN_INTERATOMIC_OK = 0.7
DEFAULT_N_IMAGES = 5
DEFAULT_SPRING = -5.0
# 两端晶格差异容忍(Å):固定胞 NEB 要求各 image 同胞,端点胞明显不同时告警。
_CELL_TOL = 1e-3

_COMPLETION_BANNER = '# --- vcstudio NEB 自动补全(只补不改;已存在的键保留用户值)---'


# ── 纯 Python 3×3 求逆(避开 numpy.linalg 惰性子模块,与 slab_builder 同口径) ────────
def _inv3(m: list) -> list:
    """3×3 矩阵求逆(伴随矩阵/行列式)。奇异 → ValueError。"""
    (a, b, c), (d, e, f), (g, h, i) = m[0], m[1], m[2]
    A = e * i - f * h
    B = f * g - d * i
    C = d * h - e * g
    det = a * A + b * B + c * C
    if abs(det) < 1e-12:
        raise ValueError('晶格矢量退化(行列式≈0),无法求分数坐标')
    inv = 1.0 / det
    return [
        [A * inv, (c * h - b * i) * inv, (b * f - c * e) * inv],
        [B * inv, (a * i - c * g) * inv, (c * d - a * f) * inv],
        [C * inv, (b * g - a * h) * inv, (a * e - b * d) * inv],
    ]


def _cart_to_frac(coords: list, cell: list) -> list:
    """笛卡尔坐标 → 分数坐标(frac_j = Σ_k cart_k·inv[k][j];cell 行为晶格矢量)。"""
    inv = _inv3(cell)
    return [[c[0] * inv[0][j] + c[1] * inv[1][j] + c[2] * inv[2][j] for j in range(3)]
            for c in coords]


def _read_sd_flags(lines: list, natoms: int):
    """POSCAR 行列表 → 每原子 Selective dynamics 标志串列表;无 SD 行 → None。

    NEB 各 image 须沿用端点的冻结设置(slab NEB 冻结相同底层);端点带 SD 时逐原子
    透传其 'T T T'/'F F F' 标志到每个 image(缺列兜底 'T T T')。
    """
    has_sd = len(lines) > 7 and lines[7].strip()[:1].lower() == 's'
    if not has_sd:
        return None
    coord_start = 9  # 注释/缩放/三矢量/元素/计数/Selective dynamics/模式 = 前 9 行
    flags = []
    for k in range(natoms):
        idx = coord_start + k
        parts = lines[idx].split() if idx < len(lines) else []
        flags.append(' '.join(parts[3:6]) if len(parts) >= 6 else 'T T T')
    return flags


def _endpoint(text: str) -> dict:
    """解析一个端点 POSCAR → 物种/计数/晶格/分数坐标/SD 标志/头部行。

    复用 parse_positions(笛卡尔坐标 + 晶格,已处理 Direct/Cartesian/负缩放拒绝),
    再转分数坐标。VASP4/畸形 → ValueError(冒泡)。
    """
    syms, counts = parse_poscar_species(text)
    if not syms or not counts or len(syms) != len(counts):
        raise ValueError('POSCAR 缺元素/计数行(VASP4 或畸形),NEB 端点须为 VASP5 格式')
    p = parse_positions(text)                       # {'elements','coords'(笛卡尔),'cell'}
    frac = _cart_to_frac(p['coords'], p['cell'])
    lines = text.splitlines()
    natoms = sum(counts)
    return {
        'species': list(syms), 'counts': list(counts), 'cell': p['cell'],
        'frac': frac, 'sd': _read_sd_flags(lines, natoms),
        'header': [ln.rstrip('\n') for ln in lines[:7]],   # 注释/缩放/三矢量/元素/计数
    }


def _fmt_composition(ep: dict) -> str:
    """端点组成短串(供不一致报错点名),如 'C4 N1 Fe1'。"""
    return ' '.join(f'{s}{c}' for s, c in zip(ep['species'], ep['counts']))


def _write_image(ini_ep: dict, frac: list, idx: int, n_images: int) -> str:
    """按初态头部(晶格/物种固定)+ 插值分数坐标写一个 image 的 Direct POSCAR 文本。

    - 头部(注释/缩放/三矢量/元素/计数)取自初态,保固定胞与物种序;
    - 端点带 Selective dynamics → 逐原子透传标志(NEB 各 image 冻结设置一致);
    - 坐标写 Direct(分数);插值可能落在 [0,1) 外(保路径连续),VASP 可读。
    """
    header = list(ini_ep['header'])
    header[0] = f'{header[0].strip()} | NEB image {idx:02d}/{n_images + 1}'
    out = list(header)
    sd = ini_ep['sd']
    if sd is not None:
        out.append('Selective dynamics')
    out.append('Direct')
    for k, f in enumerate(frac):
        row = f'  {f[0]:.10f} {f[1]:.10f} {f[2]:.10f}'
        if sd is not None:
            row += f'  {sd[k]}'
        out.append(row)
    return '\n'.join(out) + '\n'


def interpolate_images(poscar_ini: str, poscar_fin: str, n_images: int) -> list:
    """两端分数坐标线性插值 → n_images 个中间 image 的 POSCAR 文本(逐 image 短镜像 wrap)。

    - n_images 为**中间** image 数(不含两端);返回列表长度 == n_images,对应 01..N。
    - 两端原子数/元素序/计数必须一致(NEB 要求原子一一对应),不一致 → ValueError 点名。
    - 逐原子位移取最短镜像(wrap 到 [−0.5,0.5)),防跨胞跳变;image k 分数坐标 =
      frac_ini + k/(n_images+1) · Δwrap。
    - 每个 image 查周期最小原子间距,< MIN_INTERATOMIC_OK → ValueError(插值路径原子重叠)。
    """
    if n_images < 1:
        raise ValueError(f'n_images 须为正整数(中间 image 数),收到 {n_images}')
    a = _endpoint(poscar_ini)
    b = _endpoint(poscar_fin)
    if a['species'] != b['species'] or a['counts'] != b['counts']:
        raise ValueError(
            f'初末态原子组成/顺序不一致:初态 [{_fmt_composition(a)}] '
            f'末态 [{_fmt_composition(b)}];NEB 要求两端原子一一对应(同元素序、同计数)')

    fa, fb = a['frac'], b['frac']
    n_atoms = len(fa)
    # 逐原子最短镜像位移:Δ = wrap(frac_fin − frac_ini) 到 [−0.5, 0.5)
    disp = []
    for k in range(n_atoms):
        d = [fb[k][c] - fa[k][c] for c in range(3)]
        d = [x - math.floor(x + 0.5) for x in d]
        disp.append(d)

    images = []
    for img in range(1, n_images + 1):
        t = img / (n_images + 1)
        frac_img = [[fa[k][c] + t * disp[k][c] for c in range(3)] for k in range(n_atoms)]
        text = _write_image(a, frac_img, img, n_images)
        d_min = min_interatomic_distance(text)
        if d_min < MIN_INTERATOMIC_OK:
            raise ValueError(
                f'插值路径存在原子重叠,请检查初末态原子对应关系'
                f'(image {img:02d} 周期最小间距 {d_min:.2f} Å < {MIN_INTERATOMIC_OK} Å)')
        images.append(text)
    return images


# ── 根 INCAR 补全(只补不改) ─────────────────────────────────────────────────────
def _as_int(v):
    try:
        if isinstance(v, bool):
            return None
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _incar_completions(incar_text: str, n_images: int, climbing: bool,
                       spring: float) -> tuple:
    """在用户 INCAR 上算 NEB 补全项(只补缺失键)→ (completion_block_text, warnings)。

    IMAGES 与目录布局强绑定:用户显式 IMAGES 与 n_images 冲突 → ValueError(防目录数
    与 INCAR 不符跑成错误作业)。IBRION/SPRING/LCLIMB 缺则补,已存在保留用户值。
    补全以"文末追加块"落地(原文一字不改,与 job_builder 决策1 同口径),LCLIMB 带
    VTST 依赖注释。
    """
    parsed = parse_incar(incar_text)
    present = set(parsed)
    warnings: list = []
    lines: list = []

    # IMAGES:与子目录数一致性最关键
    user_images = _as_int(parsed.get('IMAGES'))
    if 'IMAGES' in present:
        if user_images is not None and user_images != n_images:
            raise ValueError(
                f'用户 INCAR 显式 IMAGES={user_images} 与 n_images={n_images} 冲突;'
                f'NEB 目录布局(01..{n_images})须与 IMAGES 一致,请统一后再生成')
    else:
        lines.append(f'IMAGES = {n_images}')

    if 'SPRING' not in present:
        lines.append(f'SPRING = {spring:g}')

    if 'IBRION' not in present:
        lines.append(
            '# NEB 采用 IBRION=1(VASP 内置准牛顿优化器)+ POTIM 默认;若改用 VTST 的 IOPT')
        lines.append(
            '# 优化器(IBRION=3, POTIM=0, IOPT=1/2 等),集群须安装 VTST 补丁方可。')
        lines.append('IBRION = 1')
    else:
        warnings.append(
            f"用户 INCAR 已设 IBRION={parsed.get('IBRION')},保留;NEB 惯例可用 IBRION=1"
            f"(内置优化器)或 IBRION=3+POTIM=0(VTST IOPT,须装 VTST)。")

    # LCLIMB:CI-NEB 爬坡镜像开关(VTST 扩展标签),带依赖注释
    if 'LCLIMB' not in present:
        lines.append(
            '# LCLIMB 为 CI-NEB(爬坡镜像)开关,属 VTST(VASP Transition State Tools)扩展')
        lines.append(
            '# 标签;集群 VASP 须打 VTST 补丁方生效,原生 VASP 忽略此标签(退化为普通 NEB)。')
        lines.append(f'LCLIMB = {".TRUE." if climbing else ".FALSE."}')

    if not lines:
        return '', warnings
    block = _COMPLETION_BANNER + '\n' + '\n'.join(lines) + '\n'
    return block, warnings


def _render_neb_incar(incar_text: str, completion_block: str) -> str:
    """用户 INCAR 原文透传 + 追加补全块(决策1:一字不改用户 INCAR)。"""
    base = incar_text if incar_text.endswith('\n') else incar_text + '\n'
    if not completion_block:
        return base
    return base + '\n' + completion_block


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _cells_differ(c1: list, c2: list) -> bool:
    return any(abs(c1[i][j] - c2[i][j]) > _CELL_TOL for i in range(3) for j in range(3))


def neb_preflight(ini_text: str, fin_text: str, incar_text: str) -> list:
    """NEB 提交前告警清单(纯建议,不挡生成)。

    - 端点须为已弛豫构型(POSCAR 不含能量,恒提醒);
    - IMAGES 与并行度:总 MPI 进程数须能整除 IMAGES(每 image 等分核数);
    - ISYM 建议 0(避免镜像对称化致路径/受力异常);
    - NSW 建议 ≥200(过渡态优化通常需较多离子步)。
    """
    warnings: list = []
    warnings.append(
        'NEB 端点(00 与 N+1)必须是已弛豫的初/末态构型;端点未弛豫会污染整条 MEP 与能垒。')

    # 端点晶格一致性(固定胞 NEB)
    try:
        a, b = _endpoint(ini_text), _endpoint(fin_text)
        if a['species'] != b['species'] or a['counts'] != b['counts']:
            warnings.append(
                f'初末态原子组成不一致(初 [{_fmt_composition(a)}] / 末 [{_fmt_composition(b)}]),'
                f'NEB 要求两端原子一一对应。')
        elif _cells_differ(a['cell'], b['cell']):
            warnings.append('初末态晶格矢量不一致;NEB 为固定胞方法,请统一两端晶胞。')
    except ValueError:
        pass                                            # 解析问题交生成期显式报错

    parsed = parse_incar(incar_text)
    images = _as_int(parsed.get('IMAGES'))
    img_note = f'(当前 IMAGES={images})' if images is not None else ''
    warnings.append(
        f'并行提醒:总 MPI 进程数须能被 IMAGES 整除,每个 image 分到相等核数{img_note};'
        f'请核对 -np 与 KPAR/NCORE 设置与 IMAGES 匹配。')

    if _as_int(parsed.get('ISYM')) != 0:
        warnings.append('建议 NEB 关闭对称:ISYM=0(避免镜像间对称化导致路径/受力异常)。')

    nsw = _as_int(parsed.get('NSW'))
    if nsw is None or nsw < 200:
        warnings.append(
            f'建议 NSW≥200(过渡态优化通常需较多离子步;当前 '
            f'{"未设" if nsw is None else nsw})。')
    return warnings


def _frame_name(i: int) -> str:
    """image 子目录名:两位补零(标准 VASP NEB 布局 00/01/.../N+1)。"""
    return f'{i:02d}'


def _resolve_potcar(potcar_fn, elements: list):
    """POTCAR 来源解析 → 文本 或 None。

    - callable:``potcar_fn(elements) -> text``(推荐;真用时传 build_potcar 闭包,
      离线测试注入假件);
    - 已存在文件路径:读其文本;
    - None:不写 POTCAR(返回 None,由调用方告警)。
    """
    if potcar_fn is None:
        return None
    if callable(potcar_fn):
        return potcar_fn(list(elements))
    if isinstance(potcar_fn, (str, os.PathLike)) and os.path.isfile(potcar_fn):
        with open(potcar_fn, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    raise ValueError('potcar_fn 须为 callable(elements)->text、POTCAR 文件路径或 None')


_README = """NEB 作业目录(vcstudio 生成)
================================

布局(标准 VASP NEB):
  00/POSCAR        初态(**须为已弛豫结构**)
  01..{n}/POSCAR     中间插值 image(线性插值 + 最短镜像 wrap)
  {last}/POSCAR        末态(**须为已弛豫结构**)
  INCAR / POTCAR / KPOINTS   全 image 共享(根目录)

要点:
  * 端点(00 与 {last})不参与优化,VASP 直接读其能量;必须是已收敛的初/末态,否则
    整条最小能量路径(MEP)与能垒不可信。
  * INCAR 已在用户设置上"只补不改"补齐 IMAGES/SPRING/IBRION/LCLIMB 等 NEB 必需键。
  * LCLIMB(CI-NEB 爬坡镜像)属 VTST 扩展标签,集群 VASP 须打 VTST 补丁方生效。
  * 提交后主输出在各 image 子目录(01..{n}/OUTCAR、OSZICAR),根目录无 OUTCAR。
"""


def build_neb_dir(out_dir, poscar_ini: str, poscar_fin: str, incar_text: str, *,
                  n_images: int = DEFAULT_N_IMAGES, climbing: bool = True,
                  spring: float = DEFAULT_SPRING, kpoints=None,
                  potcar_fn=None) -> dict:
    """生成标准 VASP NEB 目录树 + 根 INCAR/POTCAR/KPOINTS + job.yaml。

    布局:``00/POSCAR``(初)、``01..N/POSCAR``(插值)、``(N+1)/POSCAR``(末),根目录
    共享 INCAR/POTCAR/KPOINTS。INCAR 在用户设置上**只补不改**(见 _incar_completions)。

    Args:
        out_dir:   输出根目录(exist_ok)。
        poscar_ini/poscar_fin: 初/末态 POSCAR **文本**(须为已弛豫结构)。
        incar_text: 用户 INCAR 文本(原文透传 + 追加补全)。
        n_images:  中间 image 数(不含端点)。
        climbing:  True → CI-NEB(补 LCLIMB=.TRUE.);False → 普通 NEB(LCLIMB=.FALSE.)。
        spring:    弹簧常数 SPRING(eV/Å²,缺省 −5.0)。
        kpoints:   显式 [kx,ky,kz];None 则按初态晶胞推荐(slab 口径)。
        potcar_fn: POTCAR 提供者(callable(elements)->text / 文件路径 / None,见 _resolve_potcar)。

    Returns:
        ``{'job_dir','n_images','warnings'}``。端点不一致/插值重叠 → ValueError(冒泡)。
    """
    out_dir = Path(out_dir)
    # 插值(顺带完成端点一致性 + 重叠校验,失败在此冒泡)
    images = interpolate_images(poscar_ini, poscar_fin, n_images)
    ini_ep = _endpoint(poscar_ini)
    elements = ini_ep['species']

    # 根 INCAR(只补不改)
    completion_block, warnings = _incar_completions(incar_text, n_images, climbing, spring)
    incar_out = _render_neb_incar(incar_text, completion_block)

    # KPOINTS:显式优先,否则按初态晶胞推荐(NEB 多为 slab 催化体系)
    kpts = list(kpoints) if kpoints is not None else recommend_kpoints(ini_ep['cell'], 'slab')

    # POTCAR
    potcar_text = _resolve_potcar(potcar_fn, elements)
    if potcar_text is None:
        warnings.append('未提供 POTCAR(potcar_fn=None);提交前须在根目录补齐 POTCAR。')

    out_dir.mkdir(parents=True, exist_ok=True)
    # 根共享文件
    (out_dir / 'INCAR').write_text(incar_out, encoding='utf-8')
    (out_dir / 'KPOINTS').write_text(kpoints_str(kpts), encoding='utf-8')
    if potcar_text is not None:
        (out_dir / 'POTCAR').write_text(potcar_text, encoding='utf-8')
    (out_dir / 'README.txt').write_text(
        _README.format(n=n_images, last=_frame_name(n_images + 1)), encoding='utf-8')

    # image 子目录:00 初、01..N 插值、N+1 末
    frames_text = [poscar_ini] + images + [poscar_fin]
    for i, text in enumerate(frames_text):
        sub = out_dir / _frame_name(i)
        sub.mkdir(exist_ok=True)
        body = text if text.endswith('\n') else text + '\n'
        (sub / 'POSCAR').write_text(body, encoding='utf-8')

    warnings += neb_preflight(poscar_ini, poscar_fin, incar_out)

    # job.yaml(task_type='neb';记 n_images/climbing/两端来源哈希 溯源)
    inputs = {
        'n_images': n_images,
        'climbing': bool(climbing),
        'spring': spring,
        'elements': list(elements),
        'kpoints': list(kpts),
        'poscar_ini_sha256': _sha256_text(poscar_ini),
        'poscar_fin_sha256': _sha256_text(poscar_fin),
        'incar_source': 'user+neb_completion' if completion_block else 'user_verbatim',
    }
    system = ini_ep['header'][0].strip() or out_dir.name
    m = manifest_mod.new_manifest(
        job_id=f'{out_dir.resolve().name}-neb', system=system, task_type='neb',
        calc_type='slab', inputs=inputs, warnings=warnings)
    manifest_mod.save_manifest(out_dir, m)

    return {'job_dir': str(out_dir), 'n_images': n_images, 'warnings': warnings}
