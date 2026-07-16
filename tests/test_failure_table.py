"""失败分类表同步守卫:docs/failure-taxonomy.md 必须与 diagnose.py 现状一致。

代码改了 FAILURE_TO_STATE/RESTARTABLE/_VASP_ERROR_TABLE 却忘记重新生成文档 → 本测试
失败,逼开发者运行 ``python tools/gen_failure_table.py`` 后再提交。杜绝文档漂移。
"""
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, 'tools', 'gen_failure_table.py')


def _load_tool():
    spec = importlib.util.spec_from_file_location('gen_failure_table', TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_doc_in_sync_with_code():
    tool = _load_tool()
    assert os.path.isfile(tool.DOC_PATH), \
        '缺 docs/failure-taxonomy.md,请运行 python tools/gen_failure_table.py'
    with open(tool.DOC_PATH, encoding='utf-8') as f:
        existing = f.read()
    assert existing == tool.render(), (
        'docs/failure-taxonomy.md 与 diagnose.py 不同步;'
        '请运行 python tools/gen_failure_table.py 重新生成后提交')


def test_check_mode_returns_zero():
    assert _load_tool().main(['--check']) == 0


def test_table_covers_every_class_and_signature():
    tool = _load_tool()
    from vcstudio.cluster import diagnose as dg
    with open(tool.DOC_PATH, encoding='utf-8') as f:
        doc = f.read()
    for cls in dg.FAILURE_TO_STATE:
        assert f'`{cls}`' in doc, f'分类 {cls} 未出现在表中'
    for _, label, _ in dg._VASP_ERROR_TABLE:
        assert f'`{label}`' in doc, f'VASP 错误签名 {label} 未出现在表中'
    # 计数与代码一致(README/paper 引用的数字来源)
    assert doc.count('| `') >= len(dg.FAILURE_TO_STATE) + len(dg._VASP_ERROR_TABLE)
