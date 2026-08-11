"""生成成功后“下一步”前端契约。

这里只检查 app.js/generate.js 的公开衔接点；JavaScript 语法由 node --check 覆盖。
"""
from pathlib import Path


ASSETS = Path(__file__).resolve().parents[1] / 'vcstudio' / 'gui_web' / 'assets'


def _source(name):
    return (ASSETS / name).read_text(encoding='utf-8')


def test_app_exposes_unified_navigation_and_next_step_prompt():
    app = _source('app.js')
    assert 'function activatePage(page, sourceLink, detail)' in app
    assert 'VCS.navigate = async function (page, options = {})' in app
    assert 'async function focusPendingJob(jobDir)' in app
    assert 'VCS.nextStep = function (' in app
    assert "source: 'next-step'" in app


def test_generate_success_guides_to_and_focuses_new_job_without_losing_feedback():
    generate = _source('generate.js')
    assert 'VCS.nextStep({' in generate
    assert "page: 'jobs'" in generate
    assert 'focusJobDir: r.job_dir' in generate
    assert '前往任务页并提交' in generate
    # 新引导为增量行为：保留原有的打开目录、toast 和台账刷新。
    assert "VCS.call('open_dir', r.job_dir)" in generate
    assert 'runtime.generate.run.four_files_created' in generate
    assert "'已生成四件套', 'Four-file input set generated'" in generate
    assert "typeof window.Jobs.reload === 'function'" in generate
