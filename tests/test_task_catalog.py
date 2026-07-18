"""任务类型目录测试(generate.task_catalog):完整性/每条 builder_ref 可解析/分类过滤/查取。"""
import pytest

from vcstudio.generate import task_catalog as tc

# 任务书要求覆盖的计算种类 key(全通量目录必须齐):
_REQUIRED_KEYS = {
    'relax', 'cellopt', 'eos', 'static',
    'conv_encut', 'conv_kmesh', 'conv_vacuum', 'conv_thickness',
    'dos_pdos', 'bands', 'bader', 'chgdiff', 'elf',
    'workfunction', 'freq', 'aimd', 'neb', 'dimer',
    'surface_energy', 'formation_binding', 'adsorption_project',
    'spin_scan', 'vaspsol',
}


def test_catalog_covers_all_required_types():
    keys = {t['key'] for t in tc.CATALOG}
    missing = _REQUIRED_KEYS - keys
    assert not missing, f'目录缺少任务类型:{missing}'


def test_catalog_keys_unique():
    keys = [t['key'] for t in tc.CATALOG]
    assert len(keys) == len(set(keys)), '任务 key 有重复'


def test_every_entry_has_required_fields():
    for t in tc.CATALOG:
        for field in ('key', 'name_zh', 'category', 'description', 'builder_ref',
                      'requires', 'outputs', 'figure'):
            assert field in t, f"{t.get('key')} 缺字段 {field}"


def test_every_builder_ref_resolves():
    for t in tc.CATALOG:
        fn = tc.resolve_builder(t['builder_ref'])
        assert callable(fn), f"{t['key']} 的 builder_ref 不可调用"


def test_every_category_is_valid():
    for t in tc.CATALOG:
        assert t['category'] in tc.CATEGORIES, f"{t['key']} 分类非法:{t['category']}"


def test_all_five_categories_populated():
    used = {t['category'] for t in tc.CATALOG}
    assert used == set(tc.CATEGORIES), f'有分类为空:{set(tc.CATEGORIES) - used}'


def test_list_catalog_filters_by_category():
    conv = tc.list_catalog('收敛与校验')
    assert {t['key'] for t in conv} == {'conv_encut', 'conv_kmesh', 'conv_vacuum', 'conv_thickness'}


def test_list_catalog_unknown_category_raises():
    with pytest.raises(ValueError, match='分类'):
        tc.list_catalog('不存在的分类')


def test_list_catalog_all_when_none():
    assert len(tc.list_catalog()) == len(tc.CATALOG)


def test_get_task_returns_deepcopy():
    t = tc.get_task('bands')
    t['name_zh'] = 'MUTATED'
    assert tc.get_task('bands')['name_zh'] != 'MUTATED'    # 深拷贝,改不到源


def test_get_task_unknown_raises():
    with pytest.raises(KeyError):
        tc.get_task('no_such_task')


def test_resolve_builder_bad_format_raises():
    with pytest.raises(ValueError, match='module:function'):
        tc.resolve_builder('no_colon_here')


def test_resolve_builder_missing_func_raises():
    with pytest.raises(ValueError, match='无函数'):
        tc.resolve_builder('vcstudio.generate.task_catalog:nonexistent_func')
