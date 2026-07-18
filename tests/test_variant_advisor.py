"""材料变体推荐(确定性规则,无 LLM)测试。

硬护栏落到断言:母版识别 → 3d 全扫 + 同族 4d congener;配位环境变体;掺杂变体;**去重排除论文
已算过的组合**;matrix_spec 能真喂 sac_matrix;按优先级/预算分批(第一批 = 锚定体系近邻)。
"""
from vcstudio.generate import sac_builder
from vcstudio.project import variant_advisor as va


def _ref(systems_with_eads):
    """[(system, e_ads)] → reference_dataset。"""
    return {'entries': [{'system': s, 'species': 'Li2S4', 'quantity': 'E_ads', 'ref_value': e}
                        for s, e in systems_with_eads]}


# ═══════════════════════════════════════════════════════════════════════════════
# 1. 变体规则
# ═══════════════════════════════════════════════════════════════════════════════
def test_metal_swap_3d_scan_plus_4d_congener():
    res = va.suggest_variants(_ref([('Fe@N4', -1.5)]))
    swaps = {v['to'] for v in res['variants'] if v['kind'] == 'metal_swap'}
    # 3d 全扫(除 Fe)+ 同族 4d congener Ru
    assert {'Sc', 'Ti', 'V', 'Cr', 'Mn', 'Co', 'Ni', 'Cu', 'Zn'} <= swaps
    assert 'Ru' in swaps                                        # 同族 4d
    assert 'Fe' not in swaps                                    # 不换成自己
    # 所有 metal_swap 保持母版模板 MN4
    assert all(v['template'] == 'MN4' for v in res['variants'] if v['kind'] == 'metal_swap')


def test_excludes_paper_already_computed_combos():
    # 论文已算 Fe@N4 与 Co@N4 → 这两个 (metal, MN4) 组合不得再出现在变体里
    res = va.suggest_variants(_ref([('Fe@N4', -1.8), ('Co@N4', -1.2)]))
    combos = {(v['metal'], v['template']) for v in res['variants']}
    assert ('Fe', 'MN4') not in combos and ('Co', 'MN4') not in combos


def test_coordination_swap_heteroatom_templates():
    res = va.suggest_variants(_ref([('Fe@N4', -1.5)]))
    coord = {v['to'] for v in res['variants'] if v['kind'] == 'coordination_swap'}
    assert {'MP1N3', 'MS1N3', 'MB1N3'} <= coord                 # N4 → 杂原子配位
    assert all(v['metal'] == 'Fe' for v in res['variants'] if v['kind'] == 'coordination_swap')


def test_dopant_variant_boron():
    res = va.suggest_variants(_ref([('Fe@N4', -1.5)]))
    dop = [v for v in res['variants'] if v['kind'] == 'dopant']
    assert dop and dop[0]['to'] == 'MN4+B' and dop[0]['metal'] == 'Fe'


def test_matrix_spec_feeds_sac_matrix():
    res = va.suggest_variants(_ref([('Fe@N4', -1.5)]))
    ms = res['matrix_spec']
    assert ms['metals'] and ms['templates']
    # 真喂 sac_matrix:能建出 metal × template 的结构(命名 M@T)
    built = sac_builder.sac_matrix(ms['metals'][:2], ms['templates'][:1])
    assert built and all('@' in b['name'] for b in built)


def test_accepts_ai_paper_systems_cell_form():
    # ai_paper 归一 spec 的 systems(cell 带 normalized)也能识别母版
    spec_systems = {'systems': [{'metals': [{'normalized': 'Fe'}, {'normalized': 'Co'}],
                                 'sites': [{'normalized': 'MN4'}]}]}
    res = va.suggest_variants(spec_systems)
    assert res['variants']
    combos = {(v['metal'], v['template']) for v in res['variants']}
    assert ('Fe', 'MN4') not in combos and ('Co', 'MN4') not in combos     # 已算过排除


def test_accepts_system_name_list():
    res = va.suggest_variants(['Fe@N4'])
    assert any(v['metal'] == 'Ru' for v in res['variants'])


def test_empty_spec_table_no_variants():
    res = va.suggest_variants({'entries': []})
    assert res['variants'] == [] and res['matrix_spec'] == {'metals': [], 'templates': []}


# ═══════════════════════════════════════════════════════════════════════════════
# 2. 锚定「文献最优体系」+ 分批
# ═══════════════════════════════════════════════════════════════════════════════
def test_anchor_is_strongest_adsorption_system():
    # Fe@N4 吸附更强(更负)→ 锚定;其同族 congener Ru 记近邻(priority 0)
    res = va.suggest_variants(_ref([('Ni@N4', -0.8), ('Fe@N4', -1.9)]))
    ru = [v for v in res['variants'] if v['metal'] == 'Ru']
    assert ru and ru[0]['priority'] == 0 and ru[0]['parent'] == 'Fe@MN4'
    # Ni(非锚定母版)的 congener Pd 不记近邻
    pd_ = [v for v in res['variants'] if v['metal'] == 'Pd']
    assert pd_ and pd_[0]['priority'] != 0


def test_campaign_plan_first_batch_is_anchor_neighborhood():
    res = va.suggest_variants(_ref([('Fe@N4', -1.9)]))
    plan = va.variant_campaign_plan(res['variants'])
    assert plan['n_jobs'] == len(res['variants'])
    assert plan['batches'][0]['priority'] == 0                  # 第一批 = 锚定近邻
    assert '近邻' in plan['batches'][0]['reason']
    # 近邻批含同族 congener + 配位/掺杂,不含 3d 远扫金属(如 Sc)
    b0 = set(plan['batches'][0]['jobs'])
    assert 'Ru@MN4' in b0 and 'Sc@MN4' not in b0


def test_campaign_plan_budget_splits_batches():
    res = va.suggest_variants(_ref([('Fe@N4', -1.9)]))
    unsplit = va.variant_campaign_plan(res['variants'])
    per_job = unsplit['estimate_hours'] / max(unsplit['n_jobs'], 1)
    tight = va.variant_campaign_plan(res['variants'], budget_cap_hours=per_job * 2)
    assert len(tight['batches']) > len(unsplit['batches'])      # 预算收紧 → 切更多小批
    assert all(b['n_jobs'] <= 2 for b in tight['batches'])      # 每小批 ≤ 上限容量
    assert '机时上限' in tight['note']


def test_campaign_plan_hours_scale_with_jobs():
    res = va.suggest_variants(_ref([('Fe@N4', -1.9)]))
    plan = va.variant_campaign_plan(res['variants'])
    per = va.budget.estimate_job(va._SLAB_NATOMS, va._DEFAULT_NK, 'relax', va._DEFAULT_CORES)
    assert abs(plan['estimate_hours'] - per * plan['n_jobs']) < 1e-6
