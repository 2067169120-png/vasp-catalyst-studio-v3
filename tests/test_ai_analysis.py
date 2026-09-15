"""LLM 分析层测试:payload/提示词纯函数 + 假 transport 调用链 + 降级路径。"""
import json

from vcstudio.project import ai_analysis as ai

_ROWS = [{'name': 'ads_S8', 'delta_e': -4.1234567},
         {'name': 'ads_Li2S4', 'delta_e': -2.71},
         {'name': 'bad_cfg', 'delta_e': None}]


def test_build_payload_real_params_and_numbers():
    p = ai.build_payload(project_name='ZnTa', delta_rows=_ROWS,
                         incar_summary={'ENCUT': 500, 'EDIFF': 1e-5},
                         kpoints=[3, 3, 1])
    assert p['adsorption_energies_eV']['ads_S8'] == -4.1235      # 数字非字符串
    assert p['computational_parameters'] == {'ENCUT': 500, 'EDIFF': 1e-5}  # 真实参数
    assert p['incomplete'] == ['bad_cfg'] and p['kpoints'] == [3, 3, 1]
    assert 'negative = favorable' in p['sign_convention']


def test_prompt_and_system_guardrails():
    prompt = ai.build_prompt(ai.build_payload(project_name='p', delta_rows=_ROWS,
                                              incar_summary={}))
    assert 'no invented references' in prompt
    assert 'ads_S8' in prompt
    for guard in ('NEVER invent literature', 'no ZPE/entropy', 'more negative = stronger',
                  'analysis_zh', 'paragraph_en', 'caveats'):
        assert guard in ai.SYSTEM_PROMPT


def test_export_md_contains_both_prompts():
    md = ai.render_export_md(ai.build_payload(project_name='p', delta_rows=_ROWS,
                                              incar_summary={}))
    assert md.startswith('# VASP 结果分析提示词包')
    assert 'System prompt' in md and 'User prompt' in md and 'ads_S8' in md


def test_analyze_happy_with_fake_transport():
    seen = {}

    def fake(url, body, headers, timeout):
        seen['url'] = url
        seen['body'] = json.loads(body)
        reply = {'choices': [{'message': {'content': json.dumps({
            'analysis_zh': '吸附强度 S8 最强', 'paragraph_en': 'The adsorption...',
            'caveats': ['no ZPE'], 'confidence': 'high'})}}]}
        return 200, json.dumps(reply).encode()

    out = ai.analyze({'x': 1}, api_key='k', transport=fake, allow_external=True)
    assert out['ok'] and out['analysis_zh'] == '吸附强度 S8 最强'
    assert out['caveats'] == ['no ZPE'] and out['confidence'] == 'high'
    assert seen['body']['temperature'] == 0.2                    # 低温事实性
    assert seen['body']['messages'][0]['role'] == 'system'


def test_analyze_no_key_degrades(monkeypatch):
    monkeypatch.setattr(ai, 'load_api_key', lambda: None)   # 隔离 keyring(机器上可能存了真 key)
    out = ai.analyze({'x': 1}, api_key=None, allow_external=True,
                     transport=lambda *a: (_ for _ in ()).throw(AssertionError))
    assert not out['ok'] and 'API key' in out['error']


def test_analyze_skipped_when_external_disabled():
    # 联网门控默认关闭:未开启则不外发项目数据,返回 skipped 指引,transport 绝不触发
    out = ai.analyze({'x': 1}, api_key='k', allow_external=False,
                     transport=lambda *a: (_ for _ in ()).throw(AssertionError('不应外发')))
    assert not out['ok'] and out.get('skipped') is True
    assert '设置页' in out['error'] and '导出提示词包' in out['error']


def test_analyze_http_error_retries_then_fails():
    calls = []

    def bad(url, body, headers, timeout):
        calls.append(1)
        return 429, b'rate limited'

    out = ai.analyze({'x': 1}, api_key='k', transport=bad, allow_external=True)
    assert not out['ok'] and 'HTTP 429' in out['error'] and len(calls) == 4  # 重试预算(网关抖动)


def test_analyze_strips_rejected_params_and_extracts_fenced_json():
    """真实网关行为回放:400 拒 temperature → 剥参重试;Claude 回 ```json 围栏 → 提取。"""
    calls = []

    def gw(url, body, headers, timeout):
        calls.append(body.decode('utf-8'))
        if 'temperature' in calls[-1]:
            return 400, b'{"error":{"message":"`temperature` is deprecated for this model."}}'
        content = ('```json\n{"analysis_zh":"zh","paragraph_en":"en",'
                   '"caveats":["c1"],"confidence":"high"}\n```')
        import json as j
        return 200, j.dumps({'choices': [{'message': {'content': content}}]}).encode()

    out = ai.analyze({'x': 1}, api_key='k', transport=gw, allow_external=True)
    assert out['ok'] and out['analysis_zh'] == 'zh' and out['confidence'] == 'high'
    assert len(calls) == 2 and 'temperature' not in calls[-1]   # 剥参后第二次成功


def test_extract_json_variants():
    assert ai._extract_json('{"a":1}') == '{"a":1}'
    assert ai._extract_json('```json\n{"a":1}\n```') == '{"a":1}'
    assert ai._extract_json('前言 {"a":1} 后语') == '{"a":1}'
    assert ai._extract_json('no json here') == 'no json here'   # 原样(上层报解析失败)


def test_analyze_bad_json_degrades():
    def weird(url, body, headers, timeout):
        return 200, b'{"choices": [{"message": {"content": "not json"}}]}'

    out = ai.analyze({'x': 1}, api_key='k', transport=weird, allow_external=True)
    assert not out['ok'] and '解析失败' in out['error']


def test_catalyst_context_heuristics():
    """类型化机理语境(原版 _CATALYST_CONTEXT 框架移植,无编造数值)。"""
    assert 'single/dual-atom' in ai.catalyst_context(['Zn', 'N', 'C'])
    assert 'dichalcogenide' in ai.catalyst_context(['Mo', 'S'])
    assert 'transition-metal surface' in ai.catalyst_context(['Pt'])
    assert ai.catalyst_context(['Si', 'O']) == '' or 'oxide' in ai.catalyst_context(['Si', 'O'])
    assert ai.catalyst_context([]) == ''
    # catalyst_context 只在 payload 含电子结构数据(dos/bader)时才注入
    p = ai.build_payload(project_name='p', delta_rows=[], incar_summary={},
                         slab_elements=['Zn', 'N', 'C'], dos={'d_band_center': -1.2})
    assert 'catalyst_context' in p and 'd-orbital' in p['catalyst_context']


def test_build_payload_gates_electronic_context():
    """无 dos/bader → 不注入 catalyst_context;有 → 注入。"""
    p_plain = ai.build_payload(project_name='p', delta_rows=_ROWS, incar_summary={},
                               slab_elements=['Mo', 'S'])
    assert 'catalyst_context' not in p_plain and 'dos' not in p_plain
    p_bader = ai.build_payload(project_name='p', delta_rows=_ROWS, incar_summary={},
                               slab_elements=['Mo', 'S'], bader={'charge_M': 0.3})
    assert 'catalyst_context' in p_bader and p_bader['bader'] == {'charge_M': 0.3}


def test_prompt_gates_d_band_language():
    """payload 无 dos/bader → 提示词不含 d 带诱导语,反而明示仅基于能量;有则放行。"""
    p_plain = ai.build_payload(project_name='p', delta_rows=_ROWS, incar_summary={},
                               slab_elements=['Mo', 'S'])
    prompt_plain = ai.build_prompt(p_plain)
    assert 'you MAY discuss d-band-center' not in prompt_plain          # 诱导语缺席
    assert 'do NOT infer' in prompt_plain and 'ONLY energies' in prompt_plain

    p_elec = ai.build_payload(project_name='p', delta_rows=_ROWS, incar_summary={},
                              slab_elements=['Mo', 'S'], dos={'d_band_center': -1.5})
    prompt_elec = ai.build_prompt(p_elec)
    assert 'you MAY discuss d-band-center' in prompt_elec               # 有依据才放行


def test_prompt_preset_from_config(monkeypatch):
    """config llm.prompt_preset 生效;缺失则回落内置发刊级默认。"""
    monkeypatch.setattr(ai, '_config_llm', lambda: {'prompt_preset': 'CUSTOM_PRESET_XYZ'})
    prompt = ai.build_prompt(ai.build_payload(project_name='p', delta_rows=_ROWS,
                                              incar_summary={}))
    assert 'CUSTOM_PRESET_XYZ' in prompt
    monkeypatch.setattr(ai, '_config_llm', lambda: {})
    assert 'FOUR explicit parts' in ai.build_prompt(
        ai.build_payload(project_name='p', delta_rows=_ROWS, incar_summary={}))


def test_probe_ok_and_http_error():
    """连通性探测:200 → ok;非 200 → 错误含状态码;不外发项目数据。"""
    ok = ai.probe(api_key='k', base_url='http://x', model='m',
                  transport=lambda url, body, headers, timeout: (200, b'{}'))
    assert ok['ok'] is True
    bad = ai.probe(api_key='k', transport=lambda *a: (401, b'unauthorized'))
    assert bad['ok'] is False and '401' in bad['error']


def test_probe_no_key(monkeypatch):
    monkeypatch.setattr(ai, 'load_api_key', lambda: None)   # 隔离 keyring(机器上可能存了真 key)
    bad = ai.probe(api_key=None,
                   transport=lambda *a: (_ for _ in ()).throw(AssertionError('不应发请求')))
    assert bad['ok'] is False and '密钥' in bad['error']
