"""批量报告测试:构造多态 job.yaml,验证聚合/渲染/落盘与 HTML 转义(纯函数,离线)。"""
from vcstudio.shared import manifest as mm
from vcstudio.project import report


def _job(tmp_path, name, state, *, system='sys', energy=None, diagnosis=None):
    d = tmp_path / name
    d.mkdir()
    m = mm.new_manifest(job_id=name, system=system, task_type='relax',
                        calc_type='slab', inputs={})
    if state != 'CREATED':
        mm.set_state(m, state)
    res = {}
    if energy is not None:
        res['energy_e0_eV'] = energy
    if diagnosis is not None:
        res['diagnosis'] = diagnosis
    m['results'] = res
    m['scheduler_job_id'] = '123'
    m['cluster'] = '1w'
    mm.save_manifest(d, m)
    return str(d)


def _sample(tmp_path):
    return [
        _job(tmp_path, 'a_done', 'DONE', energy=-435.6),
        _job(tmp_path, 'b_failed', 'FAILED', diagnosis={
            'failure_class': 'SIGSEGV', 'evidence': '日志命中 SIGSEGV 签名', 'restartable': False}),
        _job(tmp_path, 'c_unconv', 'UNCONVERGED', diagnosis={
            'failure_class': 'NONCONVERGED', 'evidence': '未见收敛串', 'restartable': True}),
        _job(tmp_path, 'd_human', 'NEEDS_HUMAN', diagnosis={
            'failure_class': 'BAD_ENERGY', 'evidence': 'E>0', 'restartable': False}),
        _job(tmp_path, 'e_running', 'RUNNING'),
    ]


def test_collect_jobs_extracts_fields(tmp_path):
    rows = report.collect_jobs(_sample(tmp_path))
    assert len(rows) == 5
    done = next(r for r in rows if r['name'] == 'a_done')
    assert done['state'] == 'DONE' and done['energy'] == -435.6 and done['cluster'] == '1w'
    seg = next(r for r in rows if r['name'] == 'b_failed')
    assert seg['failure_class'] == 'SIGSEGV' and seg['restartable'] is False


def test_collect_jobs_missing_manifest(tmp_path):
    (tmp_path / 'empty').mkdir()
    rows = report.collect_jobs([str(tmp_path / 'empty')])
    assert rows[0]['state'] == '缺 job.yaml'


def test_collect_jobs_corrupt_manifest_no_crash(tmp_path):
    """手改坏一份 job.yaml 不能拖垮整份报告(load_manifest 兜成 None)。"""
    good = _job(tmp_path, 'good', 'DONE', energy=-1.0)
    bad = tmp_path / 'bad'
    bad.mkdir()
    (bad / 'job.yaml').write_text('state: [unclosed\n\tbad tab', encoding='utf-8')
    rows = report.collect_jobs([good, str(bad)])                 # 不抛
    assert len(rows) == 2 and any(r['state'] == '缺 job.yaml' for r in rows)
    h = report.render_html(rows, report.summarize(rows))         # 报告整体仍生成
    assert h.startswith('<!doctype html>') and 'good' in h


def test_summarize_counts_and_problems(tmp_path):
    s = report.summarize(report.collect_jobs(_sample(tmp_path)))
    assert s['total'] == 5 and s['n_done'] == 1
    assert s['by_state']['FAILED'] == 1 and s['by_state']['RUNNING'] == 1
    assert s['n_problem'] == 3                       # FAILED + UNCONVERGED + NEEDS_HUMAN
    assert s['by_class']['SIGSEGV'] == 1 and s['by_class']['NONCONVERGED'] == 1
    names = {p['name'] for p in s['problems']}
    assert names == {'b_failed', 'c_unconv', 'd_human'}


def test_render_html_self_contained_and_has_content(tmp_path):
    rows = report.collect_jobs(_sample(tmp_path))
    h = report.render_html(rows, report.summarize(rows), title='测试报告')
    assert h.startswith('<!doctype html>') and h.rstrip().endswith('</html>')
    assert 'http://' not in h and 'https://' not in h    # 零外链,自包含
    assert 'src=' not in h                                # 无外部资源
    assert '测试报告' in h and 'SIGSEGV' in h
    assert 'a_done' in h and 'e_running' in h


def test_render_html_escapes_injection(tmp_path):
    d = _job(tmp_path, 'x', 'DONE', system='<script>alert(1)</script>', energy=-1.0)
    rows = report.collect_jobs([d])
    h = report.render_html(rows, report.summarize(rows))
    assert '<script>alert(1)</script>' not in h
    assert '&lt;script&gt;' in h


def test_render_markdown_tables(tmp_path):
    rows = report.collect_jobs(_sample(tmp_path))
    md = report.render_markdown(rows, report.summarize(rows))
    assert md.startswith('# ')
    assert '## 状态分布' in md and '## 问题作业' in md and '## 全部作业' in md
    assert 'SIGSEGV' in md


def test_render_html_no_problems_shows_ok(tmp_path):
    rows = report.collect_jobs([_job(tmp_path, 'only_done', 'DONE', energy=-1.0)])
    h = report.render_html(rows, report.summarize(rows))
    assert '无 — 全部作业均无失败' in h


def test_write_report_html_and_md(tmp_path):
    dirs = _sample(tmp_path)
    ph = report.write_report(dirs, tmp_path / 'out' / 'r.html', title='T')
    assert ph.is_file() and ph.read_text(encoding='utf-8').startswith('<!doctype html>')
    pm = report.write_report(dirs, tmp_path / 'out' / 'r.md', fmt='md')
    assert pm.is_file() and pm.read_text(encoding='utf-8').startswith('# ')


def test_render_html_embeds_delta_e(tmp_path):
    rows = report.collect_jobs([_job(tmp_path, 'a_done', 'DONE', energy=-1.0)])
    de = {'rows': [{'name': 'ads_h', 'state': 'DONE', 'e_config': -10.0,
                    'delta_e': -1.5, 'note': ''}]}
    h = report.render_html(rows, report.summarize(rows), delta_e=de)
    assert '吸附能 ΔE' in h and '-1.500000' in h
