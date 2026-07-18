"""论文草稿骨架生成测试。

红线三连(设计宪法「草稿只组织真实数据」的可执行断言):
  ① 自动句子里的数字**只可能**来自输入(项目真实 ΔE / comparison);无一处编造;
  ② 机理/评价**形容词黑名单**绝不进文档(科学论断留 [作者补充]);
  ③ 占位符必在(标题/摘要机理句/引言/结论)。
另测:溯源锚存在;Methods 复用真实 INCAR;docx 可选依赖降级;draft_stats 诚实计数;英文骨架。
"""
import re
import sys

from vcstudio.project import manuscript_draft as md
from vcstudio.project import paper_data as pd
from vcstudio.shared import manifest as mm


# ── 测试工具 ──────────────────────────────────────────────────────────────────
def _auto_lines(text):
    """带溯源锚 <!--data:--> 的自动句子行。"""
    return [ln for ln in text.split('\n') if '<!--data:' in ln]


def _data_numbers(line, mask):
    """从一句自动文本抽「数据数字」:先去溯源锚 / 图号 / 化学名(体系/物种,含数字但非数据)。"""
    s = re.sub(r'<!--.*?-->', ' ', line)
    for token in sorted(mask, key=len, reverse=True):    # 长名优先,避免子串残留
        s = s.replace(token, ' ')
    s = re.sub(r'图\s*\d+[a-zA-Z]?', ' ', s)
    s = s.replace('−', '-')
    return [float(x) for x in re.findall(r'-?\d+\.?\d*', s)]


def _proj(tmp_path, name='Fe@N4'):
    return {'name': name, 'root': str(tmp_path),
            'members': {'configs': [], 'clean_slab': None, 'gas_ref': None}}


def _rows(*triples):
    return [{'name': n, 'species': sp, 'delta_e': d} for n, sp, d in triples]


def _comp():
    ref = {'entries': [{'system': 'Fe@N4', 'species': 'Li2S4', 'quantity': 'E_ads',
                        'ref_value': -1.71}]}
    computed = [{'system': 'Fe@N4', 'species': 'Li2S4', 'quantity': 'E_ads', 'value': -1.77}]
    return pd.compare_with_computed(ref, computed)


# ═══════════════════════════════════════════════════════════════════════════════
# 红线 ①:自动句子不含任何非输入数值
# ═══════════════════════════════════════════════════════════════════════════════
def test_redline_no_fabricated_numbers(monkeypatch, tmp_path):
    rows = _rows(('cfg_a', 'Li2S4', -1.77), ('cfg_b', 'Li2S6', -2.63), ('cfg_c', 'S8', -0.55))
    monkeypatch.setattr(md.adsorption, 'delta_e_rows', lambda p: {'rows': rows})
    comp = _comp()
    res = md.build_manuscript(_proj(tmp_path), comparison=comp, lang='zh')
    text = open(res['path'], encoding='utf-8').read()

    # allowed = 项目真实 ΔE + 构型计数 + comparison 各数值(全 round 2 位)
    allowed = {round(r['delta_e'], 2) for r in rows}
    allowed |= {float(len(rows)), float(comp['n'])}
    allowed |= {round(comp['mae'], 2), round(comp['rmse'], 2)}
    for p in comp['pairs'] + comp['worst']:
        allowed |= {round(p[k], 2) for k in ('ref', 'ours', 'delta', 'abs_delta')}
    # 化学名(含数字但非数据)不该被当数值:体系名 + 物种名
    mask = {'Fe@N4', 'Li2S4', 'Li2S6', 'S8'}

    auto = _auto_lines(text)
    assert auto, '应有带溯源锚的自动句子'
    for ln in auto:
        for num in _data_numbers(ln, mask):
            assert round(num, 2) in allowed, f'疑似编造数值 {num} 于自动句:{ln}'


# ═══════════════════════════════════════════════════════════════════════════════
# 红线 ②:机理/评价形容词黑名单绝不进文档
# ═══════════════════════════════════════════════════════════════════════════════
def test_redline_no_banned_mechanism_words(monkeypatch, tmp_path):
    rows = _rows(('cfg_a', 'Li2S4', -1.77), ('cfg_b', 'Li2S6', -2.63))
    monkeypatch.setattr(md.adsorption, 'delta_e_rows', lambda p: {'rows': rows})
    res = md.build_manuscript(_proj(tmp_path), comparison=_comp(), lang='zh')
    text = open(res['path'], encoding='utf-8').read()
    for word in md.BANNED_MECHANISM_WORDS:
        assert word not in text, f'黑名单评价词「{word}」不得出现在自动草稿里'


# ═══════════════════════════════════════════════════════════════════════════════
# 红线 ③:占位符必在(标题 / 摘要机理 / 引言 / 结论 / 机理讨论)
# ═══════════════════════════════════════════════════════════════════════════════
def test_redline_placeholders_present(monkeypatch, tmp_path):
    rows = _rows(('cfg_a', 'Li2S4', -1.77))
    monkeypatch.setattr(md.adsorption, 'delta_e_rows', lambda p: {'rows': rows})
    res = md.build_manuscript(_proj(tmp_path), comparison=_comp(), lang='zh')
    text = open(res['path'], encoding='utf-8').read()
    assert '[作者补充:论文标题]' in text
    assert '[作者补充:研究动机' in text                    # 摘要机理/意义占位
    assert '[作者撰写:研究背景' in text                    # 引言全占位
    assert '[作者补充:机理讨论]' in text                   # Results 每小节尾
    assert '[作者撰写:主要结论' in text                    # 结论全占位
    assert res['placeholders_count'] == text.count('[作者')
    assert res['placeholders_count'] >= 5


# ═══════════════════════════════════════════════════════════════════════════════
# 溯源锚 / 结构 / 数据句
# ═══════════════════════════════════════════════════════════════════════════════
def test_every_auto_sentence_has_provenance_anchor(monkeypatch, tmp_path):
    rows = _rows(('cfg_a', 'Li2S4', -1.77), ('cfg_b', 'Li2S6', -2.63))
    monkeypatch.setattr(md.adsorption, 'delta_e_rows', lambda p: {'rows': rows})
    res = md.build_manuscript(_proj(tmp_path), comparison=_comp(),
                              figures_manifest=[{'figure_id': '图2', 'title': '吸附能',
                                                 'kind': 'adsorption', 'job_dir': 'jobs/fe',
                                                 'panel': 'a'}], lang='zh')
    text = open(res['path'], encoding='utf-8').read()
    anchors = re.findall(r'<!--data: (.+?)-->', text)
    assert anchors and all('/' in a for a in anchors)          # 每锚形如 job_dir/figure_id
    assert any('图2' in a for a in anchors)                    # 用 manifest 的图号
    assert '各体系吸附能对比' in text or '吸附能' in text


def test_most_stable_and_range_sentences(monkeypatch, tmp_path):
    rows = _rows(('cfg_a', 'Li2S4', -1.77), ('cfg_b', 'Li2S6', -2.63), ('cfg_c', 'S8', -0.55))
    monkeypatch.setattr(md.adsorption, 'delta_e_rows', lambda p: {'rows': rows})
    res = md.build_manuscript(_proj(tmp_path), lang='zh')
    text = open(res['path'], encoding='utf-8').read()
    assert '最稳吸附构型' in text and 'Li2S6' in text          # 最负者 = 最稳
    assert '介于' in text                                      # 极值区间句


def test_sections_returned(monkeypatch, tmp_path):
    monkeypatch.setattr(md.adsorption, 'delta_e_rows', lambda p: {'rows': []})
    res = md.build_manuscript(_proj(tmp_path), lang='zh')
    assert res['sections'] == ['标题', '摘要', '引言', '方法', '结果与讨论', '结论', '参考文献']


# ═══════════════════════════════════════════════════════════════════════════════
# Methods 全自动(复用真实 INCAR)+ 降级
# ═══════════════════════════════════════════════════════════════════════════════
def _mk_member(base, name, energy, encut=520):
    d = base / name
    d.mkdir()
    m = mm.new_manifest(job_id=name, system='sys', task_type='relax', calc_type='slab',
                        inputs={'poscar_sha256': 'p' * 64, 'kpoints': [3, 3, 1],
                                'potcar': [{'element': 'C', 'variant': 'C',
                                            'titel': 'PAW_PBE C 08Apr2002', 'enmax': 400.0}]})
    m['kpoints'] = [3, 3, 1]
    mm.set_state(m, 'DONE')
    m['results'] = {'energy_e0_eV': energy}
    mm.save_manifest(d, m)
    (d / 'INCAR').write_text(f'ENCUT = {encut}\nGGA = PE\nISPIN = 2\n', encoding='utf-8')
    (d / 'KPOINTS').write_text('auto\n0\nGamma\n3 3 1\n', encoding='utf-8')
    (d / 'POSCAR').write_text(f'{name}\n1.0\n10 0 0\n0 10 0\n0 0 10\nC S\n1 1\nCart\n0 0 0\n1 1 1\n',
                              encoding='utf-8')
    return str(d)


def test_methods_auto_from_real_incar(tmp_path):
    slab = _mk_member(tmp_path, 'proj_slab_clean', -90.0)
    c1 = _mk_member(tmp_path, 'proj_ads_Li2S4_top', -92.5)
    proj = {'name': 'Fe@N4', 'root': str(tmp_path),
            'members': {'clean_slab': slab, 'gas_ref': None, 'configs': [c1]}}
    res = md.build_manuscript(proj, lang='zh')
    text = open(res['path'], encoding='utf-8').read()
    assert '## 方法' in text
    assert '520' in text and 'PBE' in text                     # 真实 INCAR 反映(非编造)
    assert '待确认' not in text                                # 有 DONE 成员 → 无降级占位


def test_methods_degrades_without_done_members(monkeypatch, tmp_path):
    monkeypatch.setattr(md.adsorption, 'delta_e_rows', lambda p: {'rows': []})
    res = md.build_manuscript(_proj(tmp_path), lang='zh')
    text = open(res['path'], encoding='utf-8').read()
    assert '待确认' in text                                    # 无 DONE 成员 → [待确认],不编造参数


# ═══════════════════════════════════════════════════════════════════════════════
# docx 可选依赖(可用则出 / 缺失则降级)+ draft_stats
# ═══════════════════════════════════════════════════════════════════════════════
def test_docx_written_when_available(monkeypatch, tmp_path):
    import os
    monkeypatch.setattr(md.adsorption, 'delta_e_rows', lambda p: {'rows': _rows(('a', 'S8', -0.5))})
    res = md.build_manuscript(_proj(tmp_path), fmt='docx', lang='zh')
    assert res['docx_available'] is True and res['docx_path'] and os.path.isfile(res['docx_path'])
    assert res['path'].endswith('.docx')


def test_docx_degrades_without_python_docx(monkeypatch, tmp_path):
    monkeypatch.setattr(md.adsorption, 'delta_e_rows', lambda p: {'rows': _rows(('a', 'S8', -0.5))})
    monkeypatch.setitem(sys.modules, 'docx', None)             # 令 `import docx` 抛 ImportError
    res = md.build_manuscript(_proj(tmp_path), fmt='docx', lang='zh')
    assert res['docx_available'] is False and res['docx_path'] is None
    assert res['path'].endswith('.md') and '降级' in res.get('note', '')


def test_draft_stats_honest_counts(monkeypatch, tmp_path):
    rows = _rows(('cfg_a', 'Li2S4', -1.77), ('cfg_b', 'Li2S6', -2.63))
    monkeypatch.setattr(md.adsorption, 'delta_e_rows', lambda p: {'rows': rows})
    res = md.build_manuscript(_proj(tmp_path), comparison=_comp(), lang='zh')
    stats = md.draft_stats(res['path'])
    assert stats['auto'] > 0 and stats['placeholder'] > 0
    assert stats['total'] == stats['auto'] + stats['placeholder']
    assert stats['placeholder'] == res['placeholders_count']
    assert 0.0 < stats['auto_ratio'] < 1.0


def test_draft_stats_missing_file():
    assert md.draft_stats('/no/such/file.md') == {'auto': 0, 'placeholder': 0,
                                                  'total': 0, 'auto_ratio': 0.0}


# ═══════════════════════════════════════════════════════════════════════════════
# 英文骨架
# ═══════════════════════════════════════════════════════════════════════════════
def test_english_skeleton(monkeypatch, tmp_path):
    monkeypatch.setattr(md.adsorption, 'delta_e_rows', lambda p: {'rows': _rows(('a', 'S8', -0.5))})
    res = md.build_manuscript(_proj(tmp_path), lang='en')
    text = open(res['path'], encoding='utf-8').read()
    assert res['path'].endswith('manuscript_en.md')
    assert '## Abstract' in text and '## Results and Discussion' in text
    assert '[TO BE WRITTEN' in text and res['placeholders_count'] >= 4
