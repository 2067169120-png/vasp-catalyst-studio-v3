"""reactions 测试:化学式解析 + 反应描述校验(守恒/单调/步数)+ 预设库完整性。

TDD 口径:每个预设 validate 通过、字段齐全;故意破坏守恒/单调/电极须中文报错并点名。
"""
import copy
import re

import pytest

from vcstudio.project import reactions as R

_ALL = list(R.list_presets())


# ── 预设库完整性 ─────────────────────────────────────────────────────────────
def test_list_presets_has_all_families():
    assert set(R.list_presets()) == {
        'LIS_16E', 'LIS_ASSOC_LIS', 'LIS_ASSOC_LIS2', 'LIS_DISSOC',
        'ORR_4E', 'OER_4E', 'HER', 'CO2RR_TO_CO'}


def test_every_reaction_preset_has_non_cjk_english_display_metadata():
    for key, preset in R.list_presets().items():
        assert preset['name_en'] == key
        assert preset['description_en'].strip()
        assert not re.search(r'[\u3400-\u9fff]', preset['description_en']), key


def test_list_presets_returns_copy():
    d = R.list_presets()
    d.pop('HER')
    assert 'HER' in R.list_presets()          # 注册表不被外部改动污染


@pytest.mark.parametrize('name', _ALL)
def test_every_preset_validates(name):
    assert R.validate_spec(R.list_presets()[name]) is True


@pytest.mark.parametrize('name', _ALL)
def test_every_preset_has_required_fields(name):
    sp = R.list_presets()[name]
    for k in ('name', 'description', 'electrode', 'direction',
              'steps', 'reference_note', 'expected_u_eq'):
        assert k in sp, f'{name} 缺字段 {k}'
    assert sp['electrode'] in ('Li/Li+', 'RHE')
    assert sp['direction'] in ('reduction', 'oxidation')
    assert len(sp['steps']) >= 2
    assert sp['expected_u_eq'] is None or len(sp['expected_u_eq']) == 2


def test_get_preset_and_unknown_named():
    assert R.get_preset('HER')['name'] == 'HER'
    with pytest.raises(ValueError, match='未知预设'):
        R.get_preset('NOPE')


# ── 化学式解析 ───────────────────────────────────────────────────────────────
def test_parse_formula_basic():
    assert R.parse_formula('Li2S8') == {'Li': 2, 'S': 8}
    assert R.parse_formula('OOH') == {'O': 2, 'H': 1}
    assert R.parse_formula('H2O') == {'H': 2, 'O': 1}
    assert R.parse_formula('CO2') == {'C': 1, 'O': 2}
    assert R.parse_formula('*') == {}                 # 干净基底
    assert R.parse_formula('') == {}
    assert R.parse_formula('Li2S3*') == {'Li': 2, 'S': 3}   # 去吸附标记


def test_parse_formula_multiletter():
    assert R.parse_formula('LiS2') == {'Li': 1, 'S': 2}
    assert R.parse_formula('LiS') == {'Li': 1, 'S': 1}


def test_parse_formula_bad_raises_named():
    with pytest.raises(ValueError, match='无法解析'):
        R.parse_formula('2H')          # 数字打头
    with pytest.raises(ValueError, match='无法解析'):
        R.parse_formula('H-')          # 非法字符


# ── 校验:步数 / 电极 / 电子数单调 ────────────────────────────────────────────
def test_validate_min_steps():
    sp = copy.deepcopy(R.HER)
    sp['steps'] = sp['steps'][:1]
    with pytest.raises(ValueError, match='至少需要 2 个状态'):
        R.validate_spec(sp)


def test_validate_unknown_electrode():
    sp = copy.deepcopy(R.HER)
    sp['electrode'] = 'SHE'
    with pytest.raises(ValueError, match='未知参比电对'):
        R.validate_spec(sp)


def test_validate_unknown_direction():
    sp = copy.deepcopy(R.HER)
    sp['direction'] = 'sideways'
    with pytest.raises(ValueError, match='未知反应方向'):
        R.validate_spec(sp)


def test_validate_electron_not_monotonic():
    sp = copy.deepcopy(R.ORR_4E)
    sp['steps'][2]['n_electrons_cumulative'] = 0    # 第 3 态电子数降回
    with pytest.raises(ValueError, match='单调非降'):
        R.validate_spec(sp)


def test_validate_no_net_electron():
    sp = copy.deepcopy(R.HER)
    for st in sp['steps']:
        st['n_electrons_cumulative'] = 0
    with pytest.raises(ValueError, match='无净电子转移'):
        R.validate_spec(sp)


# ── 校验:逐步质量/电荷守恒(还原类 + 氧化类)──────────────────────────────────
def test_balance_break_reduction_named_step():
    sp = copy.deepcopy(R.LIS_16E)
    sp['steps'][2]['coadsorbates_or_gas'][0]['coef'] = 5   # Li2S6 步多算沉淀
    with pytest.raises(ValueError, match='第 2 步.*不守恒'):
        R.validate_spec(sp)


def test_balance_break_orr_missing_water():
    sp = copy.deepcopy(R.ORR_4E)
    sp['steps'][2]['coadsorbates_or_gas'] = []            # *O 步漏掉析出的 H2O
    with pytest.raises(ValueError, match='不守恒'):
        R.validate_spec(sp)


def test_balance_break_oxidation_oer():
    sp = copy.deepcopy(R.OER_4E)
    sp['steps'][1]['species'] = 'O*'                      # *OH 错配成 *O(氧化类)
    with pytest.raises(ValueError, match='不守恒'):
        R.validate_spec(sp)


def test_balance_break_assoc_path():
    sp = copy.deepcopy(R.LIS_ASSOC_LIS2)
    sp['steps'][1]['coadsorbates_or_gas'][0]['name'] = 'Li2S2'  # 析出物种改错
    with pytest.raises(ValueError, match='不守恒'):
        R.validate_spec(sp)


# ── 预设结构(反应族口径)────────────────────────────────────────────────────
@pytest.mark.parametrize('name', ['LIS_ASSOC_LIS', 'LIS_ASSOC_LIS2', 'LIS_DISSOC'])
def test_lis_assoc_paths_are_2e_from_li2s3(name):
    sp = R.list_presets()[name]
    assert sp['steps'][0]['label'] == 'Li2S3'
    ns = [s['n_electrons_cumulative'] for s in sp['steps']]
    assert ns[0] == 0 and ns[-1] == 2               # Li2S3 起点、2 电子
    assert sp['electrode'] == 'Li/Li+'


def test_co2rr_final_step_is_chemical_desorption():
    ns = [s['n_electrons_cumulative'] for s in R.CO2RR_TO_CO['steps']]
    assert ns[-2] == ns[-1]                          # 末步 Δn=0(*CO 脱附)


def test_rhe_reference_notes_document_conventions():
    for name in ('ORR_4E', 'OER_4E', 'HER', 'CO2RR_TO_CO'):
        note = R.list_presets()[name]['reference_note']
        assert '½G(H2)' in note                      # ½H2 口径写进 reference_note
    assert 'water trick' in R.ORR_4E['reference_note']
    assert '2.15' in R.LIS_16E['reference_note'] or R.LIS_16E['expected_u_eq'] == (2.15, 2.24)
