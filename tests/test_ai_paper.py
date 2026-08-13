"""AI 智能体入口测试(论文→规格表→确认/全自动→计算活动)。

全假件:零真实网络(transport 注入)、零真实 PDF(pypdf 打桩 + 纯文本假件)。覆盖摄取、
方法学定位、抽取(良构/畸形重试/联网门控/无 key)、确定性校验各分支、实例化计划、门禁序列
(dry_run / budget 拒绝 / 单点先行 / 跳闸 token / 节点数 / 指纹 / 账本)、全自动推进与
contradicts-halt。硬护栏落到断言:LLM 只抽文本、每格带出处、spec 过校验、accepted 只由门产生。
"""
import json
import multiprocessing
import threading

from vcstudio.campaign import derive, ledger, lock, schema
from vcstudio.project import ai_paper as ap

# 含方法学关键词的假论文文本(命中窗口喂 LLM);第二页无关键词(应被 locate 跳过)。
_METHODS_TEXT = (
    'Introduction. Single-atom catalysts are promising. '
    'Computational details: All spin-polarized DFT calculations were performed with VASP. '
    'The PBE functional (GGA) and PAW pseudopotentials were used. A plane-wave cutoff energy '
    '(ENCUT) of 500 eV was applied. The Brillouin zone was sampled with a 3x3x1 Monkhorst-Pack '
    'k-point mesh. DFT-D3 dispersion correction was included. Geometries were relaxed until forces '
    'fell below 0.02 eV/A (EDIFFG) and energies converged to 1E-5 eV (EDIFF). We studied Fe and Co '
    'single-atom catalysts in MN4 and MN3 coordination, with S8 adsorbate, for the ORR pathway.'
    '\f'
    'Results and discussion. The activity trend follows a volcano relationship with lots of prose '
    'that contains none of the target methodology keywords whatsoever in this second page.'
)


def _good_spec():
    """一份良构规格表(cells 带出处),供 validate/plan/instantiate 复用。"""
    def cell(v, page=1, quote='q', conf='high'):
        return {'value': v, 'origin': {'page': page, 'quote': quote}, 'confidence': conf}
    return {
        'systems': [{'substrate': cell('graphene'),
                     'metals': [cell('Fe'), cell('Co')],
                     'sites': [cell('N4'), cell('N3')]}],
        'adsorbates': [cell('S8')],
        'methods': {'functional': cell('PBE'), 'encut': cell('500 eV'),
                    'kpoints_relax': cell('3x3x1'), 'kpoints_static': cell('5x5x1'),
                    'ediff': cell('1E-5'), 'ediffg': cell('-0.02'), 'spin': cell('2')},
        'reactions': [cell('ORR')],
        'outputs': [cell('volcano plot')],
        'mode': cell('reproduce'),
    }


def _process_autopilot_with_blocking_runner(
        campaign_dir, entered, release_runner, calls, output):
    """Spawn-safe contender used to prove the campaign run lock is process-wide."""
    def blocking_runner(task):
        with calls.get_lock():
            calls.value += 1
        entered.set()
        if not release_runner.wait(20):
            raise TimeoutError('spawn runner release timed out')
        return _good_runner(task)

    try:
        result = ap.autopilot_step(
            campaign_dir, runners={'execute': blocking_runner})
        output.put(('result', result))
    except BaseException as exc:  # pragma: no cover - surfaced to parent assertion
        output.put(('error', type(exc).__name__, str(exc)))


# ═══════════════════════════════════════════════════════════════════════════════
# 1. 摄取 + 方法学定位 + 提示词护栏
# ═══════════════════════════════════════════════════════════════════════════════
def test_extract_text_missing_pypdf_guides_paste(monkeypatch):
    def _no_pypdf():
        raise ImportError('pypdf not installed')
    monkeypatch.setattr(ap, '_load_pypdf', _no_pypdf)
    out = ap.extract_text('paper.pdf')
    assert out['ok'] is False and out['pages'] == []
    assert out['error'] == '未安装 pypdf,可粘贴文本代替'


def test_ingest_plaintext_paginates_with_formfeed():
    out = ap.ingest_source('first page text\fsecond page text')
    assert out['ok'] is True
    assert [p['n'] for p in out['pages']] == [1, 2]
    assert out['pages'][1]['text'] == 'second page text'


def test_ingest_routes_pdf_path_to_extract_text(monkeypatch):
    seen = {}

    def fake_extract(path, *, max_pages=40):
        seen['path'] = path
        return {'pages': [{'n': 1, 'text': 'from pdf'}], 'ok': True, 'error': None}
    monkeypatch.setattr(ap, 'extract_text', fake_extract)
    out = ap.ingest_source('some/paper.PDF')
    assert seen['path'] == 'some/paper.PDF'
    assert out['pages'][0]['text'] == 'from pdf'
    # 纯文本(含换行)不误判为路径
    assert ap.ingest_source('line1\nfoo.pdf-ish body')['pages'][0]['n'] == 1


def test_locate_hits_method_window_and_skips_empty_pages():
    pages = ap.ingest_source(_METHODS_TEXT)['pages']
    windows = ap.locate_method_sections(pages)
    assert windows, '应命中方法学窗口'
    assert all(w['page'] == 1 for w in windows), '第二页无关键词应被跳过'
    joined = ' '.join(w['snippet'] for w in windows)
    for kw in ('VASP', 'ENCUT', '500 eV', '3x3x1', 'EDIFFG'):
        assert kw in joined
    # 只喂窗口:第二页那句无关 prose 不应进入
    assert 'volcano relationship' not in joined


def test_locate_merges_overlapping_windows():
    # 一段密集命中(VASP/ENCUT/PBE 相邻)应被合并成很少的窗口,而非每个关键词一个
    text = 'VASP ENCUT PBE k-point EDIFF EDIFFG dispersion converged'
    pages = [{'n': 1, 'text': text}]
    windows = ap.locate_method_sections(pages, window=160)
    assert len(windows) == 1


def test_extract_prompt_and_system_guardrails():
    windows = ap.locate_method_sections(ap.ingest_source(_METHODS_TEXT)['pages'])
    prompt = ap.build_extract_prompt(windows)
    assert 'ENCUT' in prompt                      # 窗口注入
    assert 'origin' in prompt and 'quote' in prompt and 'confidence' in prompt
    # 键名/结构约束靠后(recency):schema 形状出现在窗口 JSON 之后
    assert prompt.index('EXACTLY this shape') > prompt.index('ENCUT')
    for guard in ('FORBIDDEN', 'NEVER produce a numeric', 'null', 'verbatim', 'single JSON object'):
        assert guard in ap.EXTRACT_SYSTEM_PROMPT


# ═══════════════════════════════════════════════════════════════════════════════
# 2. extract_spec(联网门控 / 无 key / 无方法段 / 良构 / 畸形重试 / 全坏降级)
# ═══════════════════════════════════════════════════════════════════════════════
def _spec_reply(spec=None):
    spec = spec or _good_spec()
    content = json.dumps(spec)
    return 200, json.dumps({'choices': [{'message': {'content': content}}]}).encode()


def test_extract_spec_blocked_when_external_off():
    def boom(*a):
        raise AssertionError('联网关闭时绝不外发')
    out = ap.extract_spec(_METHODS_TEXT, transport=boom, config={'allow_external': False})
    assert out['ok'] is False and out['spec'] is None
    assert '设置页' in out['error'] and '手填' in out['error']


def test_extract_spec_no_key_degrades(monkeypatch):
    monkeypatch.setattr(ap.ai_analysis, 'load_api_key', lambda: None)  # 隔离 keyring
    out = ap.extract_spec(_METHODS_TEXT, config={'allow_external': True},
                          transport=lambda *a: (_ for _ in ()).throw(AssertionError))
    assert out['ok'] is False and 'API key' in out['error']


def test_extract_spec_no_method_section():
    out = ap.extract_spec('just an abstract with no computational keywords at all',
                          config={'allow_external': True, 'api_key': 'k'},
                          transport=lambda *a: (_ for _ in ()).throw(AssertionError))
    assert out['ok'] is False and '未定位到方法学段落' in out['error']


def test_extract_spec_happy_with_fake_transport():
    captured = {}

    def fake(url, body, headers, timeout):
        captured['body'] = json.loads(body)
        return _spec_reply()
    out = ap.extract_spec(_METHODS_TEXT, transport=fake,
                          config={'allow_external': True, 'api_key': 'k'})
    assert out['ok'] is True
    # 只喂窗口:user 提示词里带 ENCUT 片段
    assert 'ENCUT' in captured['body']['messages'][1]['content']
    assert captured['body']['temperature'] == 0.2
    m = out['spec']['methods']
    assert m['functional']['normalized'] == 'PBE' and m['encut']['normalized'] == 500.0
    assert m['kpoints_relax']['normalized'] == [3, 3, 1]
    # 每格带出处
    assert m['functional']['origin']['page'] == 1
    metals = [c['normalized'] for c in out['spec']['systems'][0]['metals']]
    assert metals == ['Fe', 'Co']


def test_extract_spec_malformed_json_retries_then_ok(monkeypatch):
    monkeypatch.setattr(ap, 'RETRY_BACKOFF_SEC', 0)
    calls = []

    def gw(url, body, headers, timeout):
        calls.append(1)
        if len(calls) == 1:
            return 200, b'{"choices":[{"message":{"content":"not json"}}]}'
        return _spec_reply()
    out = ap.extract_spec(_METHODS_TEXT, transport=gw,
                          config={'allow_external': True, 'api_key': 'k'})
    assert out['ok'] is True and len(calls) == 2


def test_extract_spec_all_bad_degrades(monkeypatch):
    monkeypatch.setattr(ap, 'RETRY_BACKOFF_SEC', 0)
    calls = []

    def bad(url, body, headers, timeout):
        calls.append(1)
        return 200, b'{"choices":[{"message":{"content":"never json"}}]}'
    out = ap.extract_spec(_METHODS_TEXT, transport=bad,
                          config={'allow_external': True, 'api_key': 'k'})
    assert out['ok'] is False and '解析失败' in out['error'] and len(calls) == 4


# ═══════════════════════════════════════════════════════════════════════════════
# 3. validate_spec(确定性校验各分支)
# ═══════════════════════════════════════════════════════════════════════════════
def _one_method(field, value):
    return {'methods': {field: {'value': value, 'origin': {}, 'confidence': 'low'}}}


def test_validate_functional_aliases():
    for raw, want in (('PBE', 'PBE'), ('GGA-PBE', 'PBE'), ('RPBE', 'RPBE'),
                      ('BEEF-vdW', 'BEEF-vdW'), ('rev-PBE', 'revPBE')):
        v = ap.validate_spec(_one_method('functional', raw))
        assert v['normalized']['methods']['functional']['normalized'] == want
    bad = ap.validate_spec(_one_method('functional', 'MyMadeUpFunctional'))
    assert bad['ok'] is False and any('未知泛函' in i for i in bad['issues'])


def test_validate_encut_range():
    ok = ap.validate_spec(_one_method('encut', '500 eV'))
    assert ok['normalized']['methods']['encut']['normalized'] == 500.0 and ok['ok']
    hi = ap.validate_spec(_one_method('encut', '1500'))
    assert hi['normalized']['methods']['encut']['normalized'] == 1500.0    # 记原值
    assert any('超出合理区间' in i for i in hi['issues'])


def test_validate_kgrid_formats():
    for raw in ('3x3x1', '3×3×1', '3 3 1', 'Gamma-centered 3 3 1'):
        v = ap.validate_spec(_one_method('kpoints_relax', raw))
        assert v['normalized']['methods']['kpoints_relax']['normalized'] == [3, 3, 1]
    lst = ap.validate_spec(_one_method('kpoints_relax', [5, 5, 1]))
    assert lst['normalized']['methods']['kpoints_relax']['normalized'] == [5, 5, 1]
    bad = ap.validate_spec(_one_method('kpoints_relax', 'Gamma only'))
    assert any('无法解析 k 点网格' in i for i in bad['issues'])


def test_validate_metal_symbol_membership():
    spec = {'systems': [{'metals': [{'value': 'Fe'}, {'value': 'Zz'}]}]}
    v = ap.validate_spec(spec)
    metals = v['normalized']['systems'][0]['metals']
    assert metals[0]['normalized'] == 'Fe'
    assert 'normalized' not in metals[1]
    assert any('未知金属符号「Zz」' in i for i in v['issues'])


def test_validate_template_mapping_six_classes():
    spec = {'systems': [{'sites': [{'value': 'N4'}, {'value': 'P1N3'}, {'value': 'N4+B'},
                                   {'value': 'M-N3'}, {'value': 'weird'}]}]}
    v = ap.validate_spec(spec)
    got = [c.get('normalized') for c in v['normalized']['systems'][0]['sites']]
    assert got[:4] == ['MN4', 'MP1N3', 'MN4+B', 'MN3']
    assert got[4] is None
    assert any('未知配位模板「weird」' in i for i in v['issues'])


def test_validate_unknown_adsorbate_issue():
    spec = {'adsorbates': [{'value': 'S8'}, {'value': '*Li2S4'}, {'value': 'Unobtanium'}]}
    v = ap.validate_spec(spec)
    got = [c.get('normalized') for c in v['normalized']['adsorbates']]
    assert got[0] == 'S8' and got[1] == 'Li2S4' and got[2] is None
    assert any('未知吸附质「Unobtanium」,需人工提供结构' in i for i in v['issues'])


def test_validate_reaction_preset_mapping():
    spec = {'reactions': [{'value': 'ORR'}, {'value': 'HER'}, {'value': 'Li-S'},
                          {'value': 'no-such-rxn'}]}
    v = ap.validate_spec(spec)
    got = [c.get('normalized') for c in v['normalized']['reactions']]
    assert got[:3] == ['ORR_4E', 'HER', 'LIS_16E'] and got[3] is None
    assert any('未知反应预设' in i for i in v['issues'])


def test_validate_keeps_original_and_records_normalized():
    v = ap.validate_spec(_one_method('encut', '500 eV'))
    cell = v['normalized']['methods']['encut']
    assert cell['value'] == '500 eV'          # 原值保留
    assert cell['normalized'] == 500.0        # 归一记录


def test_validate_mode_and_spin_normalization():
    assert ap.validate_spec({'mode': {'value': '复现'}})['normalized']['mode']['normalized'] == 'reproduce'
    assert ap.validate_spec({'mode': {'value': 'design'}})['normalized']['mode']['normalized'] == 'design'
    unk = ap.validate_spec({'mode': {'value': 'guesswork'}})
    assert unk['normalized']['mode']['normalized'] == 'reproduce'
    assert any('未知模式' in i for i in unk['issues'])
    assert ap.validate_spec(_one_method('spin', 'spin-polarized'))['normalized']['methods']['spin']['normalized'] == 2
    assert ap.validate_spec(_one_method('spin', 'non-magnetic'))['normalized']['methods']['spin']['normalized'] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 4. plan_campaign(纯计划对象)
# ═══════════════════════════════════════════════════════════════════════════════
def test_plan_matrix_params_and_jobs_estimate():
    pc = ap.plan_campaign(_good_spec(), incar_defaults={'ENCUT': 500})
    plan = pc['plan']
    assert pc['ok'] is True
    assert plan['matrix']['metals'] == ['Fe', 'Co']
    assert plan['matrix']['templates'] == ['MN4', 'MN3']
    assert plan['slabs'] == ['Fe@MN4', 'Fe@MN3', 'Co@MN4', 'Co@MN3']
    # 4 slab 弛豫 + 4 slab×1 吸附质 = 8
    assert plan['jobs_estimate'] == 8 and len(plan['tasks']) == 8
    assert plan['pilot'] == 'relax__Fe@MN4'
    assert plan['fingerprint_hash'] and plan['incar_provided'] is True


def test_plan_warns_missing_incar():
    without = ap.plan_campaign(_good_spec(), incar_defaults=None)['plan']
    assert any('INCAR' in w for w in without['warnings'])
    withi = ap.plan_campaign(_good_spec(), incar_defaults={'ENCUT': 500})['plan']
    assert not any('需用户提供 INCAR' in w for w in withi['warnings'])


def test_plan_selects_reaction_preset():
    plan = ap.plan_campaign(_good_spec())['plan']
    assert plan['reaction_preset'] == 'ORR_4E'


def test_plan_ok_false_when_nothing_buildable():
    spec = {'systems': [{'metals': [{'value': 'Zz'}], 'sites': [{'value': 'weird'}]}]}
    pc = ap.plan_campaign(spec)
    assert pc['ok'] is False and pc['plan']['jobs_estimate'] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 5. instantiate(门禁序列)
# ═══════════════════════════════════════════════════════════════════════════════
def _plan():
    return ap.plan_campaign(_good_spec(), incar_defaults={'ENCUT': 500})['plan']


def test_instantiate_dry_run_writes_nothing(tmp_path):
    import os
    r = ap.instantiate(_plan(), str(tmp_path), dry_run=True)
    assert r['ok'] is True and r['dry_run'] is True
    assert len(r['created']) == 8
    assert not os.path.exists(r['campaign_dir'])     # 全走逻辑但不落盘


def test_instantiate_budget_gate_rejects_with_split_suggestion(tmp_path):
    r = ap.instantiate(_plan(), str(tmp_path), budget_cap_hours=0.001)
    assert r['ok'] is False
    assert r['gates']['budget']['status'] == 'blocked'
    assert '批' in r['error'] and '核时' in r['error']
    assert schema.load_campaign(r['campaign_dir'] or str(tmp_path)) is None  # 未落盘


def test_instantiate_pilot_first_returns_pilot_and_blocks_fanout(tmp_path):
    r = ap.instantiate(_plan(), str(tmp_path), budget_cap_hours=100000.0)
    assert r['awaiting'] == 'pilot_validation'
    assert r['pilot']['id'] == 'relax__Fe@MN4'
    assert r['gates']['pilot']['status'] == 'awaiting'
    camp = schema.load_campaign(r['campaign_dir'])
    assert camp['meta']['require_pilot'] is True
    # 单点先行:只有 pilot 就绪,其余 blocked-on-pilot
    assert derive.derive_ready(camp)['ready'] == ['relax__Fe@MN4']
    assert len(camp['tasks']) == 8


def test_instantiate_skip_token_fans_out_and_records_user_decision(tmp_path):
    r = ap.instantiate(_plan(), str(tmp_path), budget_cap_hours=100000.0,
                       confirm_token=ap.SKIP_PILOT_TOKEN)
    assert 'awaiting' not in r
    assert r['gates']['pilot']['status'] == 'skipped'
    camp = schema.load_campaign(r['campaign_dir'])
    assert camp['meta']['require_pilot'] is False
    # 跳闸后所有 slab 弛豫直接 fan-out 就绪
    assert set(derive.derive_ready(camp)['ready']) == {
        'relax__Fe@MN4', 'relax__Fe@MN3', 'relax__Co@MN4', 'relax__Co@MN3'}
    skips = ledger.read_decisions(r['campaign_dir'], kind='skip-pilot')
    assert skips and skips[0]['made_by'] == 'user'


def test_instantiate_single_job_no_pilot(tmp_path):
    spec = {'systems': [{'metals': [{'value': 'Fe'}], 'sites': [{'value': 'N4'}]}],
            'adsorbates': [], 'methods': {'encut': {'value': '500'}}, 'mode': {'value': 'reproduce'}}
    plan = ap.plan_campaign(spec)['plan']
    r = ap.instantiate(plan, str(tmp_path), budget_cap_hours=100000.0)
    assert r['jobs_estimate'] == 1 and len(r['created']) == 1
    assert 'awaiting' not in r and r['gates']['pilot']['status'] == 'not_required'


def test_instantiate_registers_all_nodes_with_fingerprint_and_ledger(tmp_path):
    plan = _plan()
    r = ap.instantiate(plan, str(tmp_path), budget_cap_hours=100000.0)
    camp = schema.load_campaign(r['campaign_dir'])
    assert len(camp['tasks']) == plan['jobs_estimate']
    assert camp['meta']['fingerprint_hash'] == plan['fingerprint_hash']
    for t in camp['tasks']:
        assert t['fingerprint_hash'] == plan['fingerprint_hash']
        assert t['origin'] == 'ai_paper'
    origins = [d['context'].get('origin') for d in ledger.read_decisions(r['campaign_dir'])]
    assert origins and all(o == 'ai_paper' for o in origins)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. autopilot_step(推进 / contradicts-halt / 幂等 / 缺件)
# ═══════════════════════════════════════════════════════════════════════════════
def _single_slab_campaign(tmp_path, *, expected=None):
    """建一个单作业 campaign(立即就绪),可选带文献锚点 expected。"""
    spec = {'systems': [{'metals': [{'value': 'Fe'}], 'sites': [{'value': 'N4'}]}],
            'adsorbates': [], 'methods': {'functional': {'value': 'PBE'},
                                          'encut': {'value': '500'},
                                          'kpoints_relax': {'value': '3 3 1'}},
            'mode': {'value': 'reproduce'}}
    if expected is not None:
        spec['expected'] = expected
    plan = ap.plan_campaign(spec)['plan']
    return ap.instantiate(plan, str(tmp_path), budget_cap_hours=100000.0)


def _good_runner(task):
    return {'completed': True, 'energy_eV': -123.4, 'measured': {'u_l': 0.5},
            'checks': [{'name': 'scf_converged', 'ok': True},
                       {'name': 'force_below_ediffg', 'ok': True}]}


def test_autopilot_advances_ready_to_accepted_via_gate_and_is_idempotent(tmp_path):
    r = _single_slab_campaign(tmp_path)
    res = ap.autopilot_step(r['campaign_dir'], runners={'execute': _good_runner})
    assert res['halted'] is False
    assert res['actions'] == [{'task': 'relax__Fe@MN4', 'action': 'accepted'}]
    camp = schema.load_campaign(r['campaign_dir'])
    assert camp['tasks'][0]['rung'] == 'accepted'
    # 幂等:再走一步无就绪任务,零动作
    assert ap.autopilot_step(r['campaign_dir'], runners={'execute': _good_runner})['actions'] == []


def test_autopilot_contradicts_halt_flat_and_not_accepted(tmp_path):
    expected = {'u_l': {'value': '0.45', 'origin': {'page': 3, 'quote': 'U_L = 0.45 V'}, 'tol': 0.2}}
    r = _single_slab_campaign(tmp_path, expected=expected)

    def deviating(task):
        out = _good_runner(task)
        out['measured'] = {'u_l': 1.6}     # 与文献 0.45 差 1.15 > 0.2
        return out
    res = ap.autopilot_step(r['campaign_dir'], runners={'execute': deviating})
    assert res['halted'] is True
    assert 'contradicts-halt' in res['reason'] and '文献值 0.45' in res['reason']
    assert '复现成功' in res['reason']       # 平铺呈报、绝不 spin
    camp = schema.load_campaign(r['campaign_dir'])
    # 红线:超差绝不 accepted,停在 validated(确定性),等人裁决
    assert camp['tasks'][0]['rung'] == 'validated'
    assert len(ledger.read_events(r['campaign_dir'], kind='contradicts_halt')) == 1


def test_autopilot_missing_campaign_halts(tmp_path):
    res = ap.autopilot_step(str(tmp_path / 'nope'), runners={'execute': _good_runner})
    assert res['halted'] is True and '未找到 campaign' in res['reason']


def test_autopilot_validate_blocked_when_checks_fail(tmp_path):
    r = _single_slab_campaign(tmp_path)

    def failing(task):
        return {'completed': True, 'energy_eV': -1.0,
                'checks': [{'name': 'scf_converged', 'ok': False}]}
    res = ap.autopilot_step(r['campaign_dir'], runners={'execute': failing})
    assert res['halted'] is False
    assert res['actions'][0]['action'] == 'validate-blocked'
    camp = schema.load_campaign(r['campaign_dir'])
    # 校验未过 → 绝不 validated/accepted,停在 completed
    assert camp['tasks'][0]['rung'] == 'completed'


def test_two_autopilot_calls_execute_exactly_one_runner_and_other_is_busy(tmp_path):
    r = _single_slab_campaign(tmp_path)
    entered = threading.Event()
    release_runner = threading.Event()
    calls = []
    first_result = []

    def blocking_runner(task):
        calls.append(task['id'])
        entered.set()
        assert release_runner.wait(timeout=5)
        return _good_runner(task)

    thread = threading.Thread(target=lambda: first_result.append(
        ap.autopilot_step(r['campaign_dir'], runners={'execute': blocking_runner})))
    thread.start()
    assert entered.wait(timeout=5)
    second = ap.autopilot_step(r['campaign_dir'], runners={'execute': blocking_runner})
    assert second['busy'] is True and second['actions'] == []
    assert '未读取/claim/执行任何任务' in second['reason']
    release_runner.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert calls == ['relax__Fe@MN4']
    assert first_result[0]['actions'] == [
        {'task': 'relax__Fe@MN4', 'action': 'accepted'}]
    assert lock.is_held(r['campaign_dir']) is False
    task = schema.load_task(r['campaign_dir'], 'relax__Fe@MN4')
    assert task is not None and task['rung'] == 'accepted' and task['revision'] == 4
    assert schema.load_campaign(r['campaign_dir']) is not None


def test_two_spawned_autopilots_execute_exactly_one_ready_task(tmp_path):
    """Two desktop processes cannot both claim/execute the same ready task."""
    campaign = _single_slab_campaign(tmp_path)
    context = multiprocessing.get_context('spawn')
    entered = context.Event()
    release_runner = context.Event()
    calls = context.Value('i', 0)
    output = context.Queue()

    first = context.Process(
        target=_process_autopilot_with_blocking_runner,
        args=(campaign['campaign_dir'], entered, release_runner, calls, output),
    )
    first.start()
    assert entered.wait(20), 'first spawned autopilot never entered its runner'

    second = context.Process(
        target=_process_autopilot_with_blocking_runner,
        args=(campaign['campaign_dir'], entered, release_runner, calls, output),
    )
    second.start()
    second_row = output.get(timeout=20)
    assert second_row[0] == 'result', second_row
    assert second_row[1]['busy'] is True
    assert second_row[1]['actions'] == []
    assert '未读取/claim/执行任何任务' in second_row[1]['reason']

    release_runner.set()
    first_row = output.get(timeout=30)
    assert first_row[0] == 'result', first_row
    assert first_row[1]['actions'] == [
        {'task': 'relax__Fe@MN4', 'action': 'accepted'}]

    for process in (first, second):
        process.join(30)
        if process.is_alive():  # pragma: no cover - cleanup for failed synchronization
            process.terminate()
            process.join(5)
    assert first.exitcode == 0 and second.exitcode == 0
    assert calls.value == 1
    disk = schema.load_task(campaign['campaign_dir'], 'relax__Fe@MN4')
    assert disk is not None and disk['rung'] == 'accepted' and disk['revision'] == 4
    assert lock.is_held(campaign['campaign_dir']) is False


def test_autopilot_held_lock_returns_before_campaign_read(tmp_path, monkeypatch):
    r = _single_slab_campaign(tmp_path)
    assert lock.acquire(r['campaign_dir'], 'manual-owner') is True

    def forbidden_read(*_args, **_kwargs):
        raise AssertionError('held lock 时不应读取 campaign')

    monkeypatch.setattr(schema, 'load_campaign', forbidden_read)
    result = ap.autopilot_step(r['campaign_dir'], runners={'execute': _good_runner})
    assert result['busy'] is True and result['lock']['owner'] == 'manual-owner'
    assert lock.release(r['campaign_dir'], owner='manual-owner') is True


def test_autopilot_runner_exception_structures_failure_and_finally_releases_lock(tmp_path):
    r = _single_slab_campaign(tmp_path)

    def boom(_task):
        raise RuntimeError('runner exploded')

    result = ap.autopilot_step(r['campaign_dir'], runners={'execute': boom})
    assert result['actions'][0]['action'] == 'failed'
    assert 'runner exploded' in result['actions'][0]['detail']
    assert lock.is_held(r['campaign_dir']) is False
    disk = schema.load_task(r['campaign_dir'], 'relax__Fe@MN4')
    assert disk['rung'] == 'failed' and disk['revision'] == 2
