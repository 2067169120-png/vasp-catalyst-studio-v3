"""Gaussian gjf 扩展测试:资源行 / SMD·PCM 溶剂 / PCM-Read 自定介电附加段顺序 /
Gen·GenECP 段完整语法与元素序 / per_element 缺失 warning / preview 同源 / 周期表建议。
"""
import glob
import os

import pytest

from vcstudio.engines import get_backend
from vcstudio.engines.calcspec import CalcSpec
from vcstudio.engines import gaussian
from vcstudio.engines.gaussian import build_gjf, preview
from tests.test_engines_calcspec import make_poscar

CUBE = [[12.0, 0, 0], [0, 12.0, 0], [0, 0, 12.0]]
MOL = make_poscar('water', CUBE, ['O', 'H'], [1, 2],
                  [[6.0, 6.0, 6.0], [6.96, 6.0, 6.0], [5.76, 6.93, 6.0]])
# 含过渡金属 Au 的混合体系(物种序 C H O Au)
GOLD = make_poscar('goldcluster', CUBE, ['C', 'H', 'O', 'Au'], [1, 1, 1, 1],
                   [[6, 6, 6], [6.9, 6, 6], [5.8, 6.9, 6], [6, 6, 7.5]])
# 全默认基组、物种序 O H C(测组内元素序 = 结构出现序)
OHC = make_poscar('ohc', CUBE, ['O', 'H', 'C'], [1, 1, 1],
                  [[6, 6, 6], [6.9, 6, 6], [5.8, 6.9, 6]])


def _spec(structure=MOL, **kw):
    base = dict(structure=structure, task='static', functional='PBE',
                periodic=False, charge=0, multiplicity=1)
    base.update(kw)
    return CalcSpec(**base)


def _route(spec):
    for ln in build_gjf(spec)[0].splitlines():
        if ln.lstrip().startswith('#'):
            return ln
    return ''


def _blocks(text):
    """gjf 全文 → 按空行切分的非空行块列表(附加段顺序断言用)。"""
    blocks, cur = [], []
    for ln in text.split('\n'):
        if ln.strip() == '':
            if cur:
                blocks.append(cur)
                cur = []
        else:
            cur.append(ln)
    if cur:
        blocks.append(cur)
    return blocks


# ── 资源行(%nprocshared / %mem / %chk) ───────────────────────────────────────
def test_resource_nproc_line():
    assert '%nprocshared=8' in build_gjf(_spec(extras={'nproc': 8}))[0].splitlines()


def test_resource_mem_gb_line():
    assert '%mem=16GB' in build_gjf(_spec(extras={'mem_gb': 16}))[0].splitlines()


def test_resource_mem_float_whole_no_decimal():
    assert '%mem=8GB' in build_gjf(_spec(extras={'mem_gb': 8.0}))[0].splitlines()


def test_resource_chk_default_present_when_absent():
    # 向后兼容:无 extras 也恒写 %chk(旧行为)
    assert '%chk=water.chk' in build_gjf(_spec())[0].splitlines()


def test_resource_chk_true():
    assert '%chk=water.chk' in build_gjf(_spec(extras={'chk': True}))[0].splitlines()


def test_resource_chk_named():
    assert '%chk=myrun.chk' in build_gjf(_spec(extras={'chk': 'myrun'}))[0].splitlines()


def test_resource_chk_named_strips_extension():
    assert '%chk=myrun.chk' in build_gjf(_spec(extras={'chk': 'myrun.chk'}))[0].splitlines()


def test_resource_chk_false_omits_line():
    text = build_gjf(_spec(extras={'chk': False}))[0]
    assert not any(ln.startswith('%chk') for ln in text.splitlines())


def test_resource_lines_order_nproc_mem_chk_before_route():
    text = build_gjf(_spec(extras={'nproc': 4, 'mem_gb': 8, 'chk': True}))[0]
    lines = text.splitlines()
    i_np = lines.index('%nprocshared=4')
    i_mem = lines.index('%mem=8GB')
    i_chk = lines.index('%chk=water.chk')
    i_route = next(k for k, ln in enumerate(lines) if ln.startswith('#'))
    assert i_np < i_mem < i_chk < i_route


# ── 溶剂 SCRF ─────────────────────────────────────────────────────────────────
def test_solvent_smd_route():
    assert 'SCRF=(SMD,Solvent=Water)' in _route(
        _spec(extras={'solvent': {'model': 'smd', 'name': 'water'}}))


def test_solvent_pcm_route():
    assert 'SCRF=(PCM,Solvent=Acetonitrile)' in _route(
        _spec(extras={'solvent': {'model': 'pcm', 'name': 'acetonitrile'}}))


def test_solvent_cpcm_route():
    assert 'SCRF=(CPCM,Solvent=Water)' in _route(
        _spec(extras={'solvent': {'model': 'cpcm', 'name': 'water'}}))


def test_solvent_name_capitalized():
    assert 'Solvent=Water' in _route(
        _spec(extras={'solvent': {'model': 'smd', 'name': 'water'}}))


def test_solvent_name_no_tail_section():
    text = build_gjf(_spec(extras={'solvent': {'model': 'smd', 'name': 'water'}}))[0]
    assert 'eps=' not in text and 'Generic' not in text


def test_solvent_pcm_read_eps_route():
    assert 'SCRF=(PCM,Solvent=Generic,Read)' in _route(
        _spec(extras={'solvent': {'model': 'pcm', 'eps': 78.39}}))


def test_solvent_pcm_read_eps_tail_section():
    text = build_gjf(_spec(extras={'solvent': {'model': 'pcm', 'eps': 78.39}}))[0]
    assert _blocks(text)[-1] == ['eps=78.39']


def test_solvent_pcm_read_eps_epsinf_tail():
    text = build_gjf(_spec(extras={'solvent':
                                   {'model': 'pcm', 'eps': 78.39, 'epsinf': 1.78}}))[0]
    assert _blocks(text)[-1] == ['eps=78.39', 'epsinf=1.78']


def test_solvent_unknown_model_warns():
    _, warns = build_gjf(_spec(extras={'solvent': {'model': 'wat', 'name': 'water'}}))
    assert any('SCRF 溶剂模型' in w for w in warns)


# ── 混合基组 Gen / GenECP ─────────────────────────────────────────────────────
def test_mixed_basis_gen_route_no_ecp():
    route = _route(_spec(extras={'mixed_basis':
                                 {'default': '6-31G(d)', 'per_element': {'O': 'def2-TZVP'}}}))
    assert ' Gen ' in f' {route} ' and 'GenECP' not in route and 'Pseudo=Read' not in route


def test_mixed_basis_genecp_route_with_pseudo():
    route = _route(_spec(structure=GOLD, extras={'mixed_basis': {
        'default': '6-31G(d)', 'per_element': {'Au': 'LANL2DZ'}, 'ecp_elements': ['Au']}}))
    assert 'GenECP' in route and 'Pseudo=Read' in route


def test_mixed_basis_section_syntax():
    text = build_gjf(_spec(structure=GOLD, extras={'mixed_basis': {
        'default': '6-31G(d)', 'per_element': {'Au': 'LANL2DZ'},
        'ecp_elements': ['Au']}}))[0]
    blocks = _blocks(text)
    # 基组段:默认组(C H O)在前,per_element 覆盖组(Au)在后,均以 **** 收尾
    assert ['C H O 0', '6-31G(d)', '****', 'Au 0', 'LANL2DZ', '****'] in blocks


def test_mixed_basis_ecp_section():
    text = build_gjf(_spec(structure=GOLD, extras={'mixed_basis': {
        'default': '6-31G(d)', 'per_element': {'Au': 'LANL2DZ'},
        'ecp_elements': ['Au']}}))[0]
    assert ['Au 0', 'LANL2DZ'] in _blocks(text)


def test_mixed_basis_element_order_follows_structure():
    # 物种序 O H C,全默认基组 → 单组,组内元素序 = 结构出现序
    text = build_gjf(_spec(structure=OHC,
                           extras={'mixed_basis': {'default': '6-31G(d)'}}))[0]
    assert 'O H C 0' in text


def test_mixed_basis_per_element_missing_warns():
    _, warns = build_gjf(_spec(extras={'mixed_basis': {
        'default': '6-31G(d)', 'per_element': {'Fe': 'SDD'}}}))
    assert any('per_element 元素' in w and 'Fe' in w for w in warns)


def test_mixed_basis_ecp_missing_warns_and_falls_back_to_gen():
    route = _route(_spec(extras={'mixed_basis': {
        'default': '6-31G(d)', 'ecp_elements': ['Pt']}}))
    text, warns = build_gjf(_spec(extras={'mixed_basis': {
        'default': '6-31G(d)', 'ecp_elements': ['Pt']}}))
    assert 'GenECP' not in route              # Pt 不在结构 → 无 ecp → Gen
    assert any('ecp_elements 元素' in w and 'Pt' in w for w in warns)


# ── 附加输入区顺序(坐标 → 基组 → ECP → SCRF-Read;段间单空行) ─────────────────
def test_additional_sections_order_coords_basis_ecp_scrf():
    text = build_gjf(_spec(structure=GOLD, functional='B3LYP', extras={
        'nproc': 8, 'mem_gb': 16, 'chk': True,
        'solvent': {'model': 'pcm', 'eps': 78.39, 'epsinf': 1.78},
        'mixed_basis': {'default': '6-31G(d)', 'per_element': {'Au': 'LANL2DZ'},
                        'ecp_elements': ['Au']}}))[0]
    blocks = _blocks(text)
    # 坐标块(含电荷多重度)→ 基组段 → ECP 段 → SCRF-Read 段,末段为自定介电
    coords = next(i for i, b in enumerate(blocks) if b[0].startswith('0 1'))
    basis = blocks.index(['C H O 0', '6-31G(d)', '****', 'Au 0', 'LANL2DZ', '****'])
    ecp = blocks.index(['Au 0', 'LANL2DZ'])
    scrf = blocks.index(['eps=78.39', 'epsinf=1.78'])
    assert coords < basis < ecp < scrf
    assert scrf == len(blocks) - 1


def test_gjf_ends_with_blank_line():
    assert build_gjf(_spec())[0].endswith('\n\n')


# ── preview 与 generate_inputs 同源一致 ───────────────────────────────────────
def test_preview_returns_full_gjf_str():
    out = preview(_spec())
    assert isinstance(out, str) and out.startswith('%') and '#P' in out


def test_preview_equals_generated_file(tmp_path):
    spec = _spec(structure=GOLD, extras={
        'nproc': 4, 'mem_gb': 8,
        'solvent': {'model': 'smd', 'name': 'water'},
        'mixed_basis': {'default': '6-31G(d)', 'per_element': {'Au': 'LANL2DZ'},
                        'ecp_elements': ['Au']}})
    get_backend('gaussian').generate_inputs(spec, str(tmp_path))
    written = open(glob.glob(os.path.join(str(tmp_path), '*.gjf'))[0],
                   encoding='utf-8').read()
    assert preview(spec) == written


def test_preview_periodic_still_raises():
    slab = make_poscar('slab', [[5, 0, 0], [0, 5, 0], [0, 0, 15]], ['Fe'], [1],
                       [[0, 0, 5.0]])
    with pytest.raises(ValueError, match='仅支持分子'):
        preview(_spec(structure=slab, periodic=True, cutoff_ev=400))


# ── PERIODIC_TABLE_GROUPS 周期表建议 ──────────────────────────────────────────
def test_periodic_table_has_86_elements():
    assert len(gaussian.PERIODIC_TABLE_GROUPS['elements']) == 86


def test_periodic_table_z_symbol_consistent():
    els = gaussian.PERIODIC_TABLE_GROUPS['elements']
    assert els[0] == {'z': 1, 'symbol': 'H', 'category': 'main_group',
                      'basis_suggestion': ['6-31G(d)', 'def2-SVP']}
    assert els[-1]['z'] == 86 and els[-1]['symbol'] == 'Rn'


def test_periodic_table_note_marks_starting_point():
    assert '起点' in gaussian.PERIODIC_TABLE_GROUPS['note']


def test_periodic_table_transition_metal_suggestion():
    assert gaussian.suggest_basis('Fe') == ['LANL2DZ', 'SDD']
    assert gaussian.suggest_basis('Au') == ['LANL2DZ', 'SDD']


def test_periodic_table_main_group_suggestion():
    assert gaussian.suggest_basis('C') == ['6-31G(d)', 'def2-SVP']


def test_periodic_table_lanthanide_suggestion():
    assert gaussian.suggest_basis('Ce') == ['SDD']


def test_periodic_table_unknown_symbol_empty():
    assert gaussian.suggest_basis('Xx') == []
