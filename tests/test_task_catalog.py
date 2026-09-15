"""任务类型目录测试(generate.task_catalog):完整性/双语字段/builder/分类/查取。"""
import re

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
        for field in ('key', 'name_zh', 'name_en', 'category', 'category_en',
                      'description', 'description_en', 'builder_ref', 'requires',
                      'requires_en', 'outputs', 'outputs_en', 'next_action_en',
                      'figure'):
            assert field in t, f"{t.get('key')} 缺字段 {field}"


def test_every_english_field_is_nonempty_and_contains_no_cjk():
    cjk = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]')
    fields = ('name_en', 'category_en', 'description_en', 'requires_en', 'outputs_en',
              'next_action_en')
    for task in tc.CATALOG:
        for field in fields:
            value = task[field]
            assert isinstance(value, str) and value.strip(), f'{task["key"]}.{field} 为空'
            assert not cjk.search(value), f'{task["key"]}.{field} 残留中文:{value!r}'


def test_english_category_matches_chinese_category():
    assert set(tc.CATEGORY_EN) == set(tc.CATEGORIES)
    for task in tc.CATALOG:
        assert task['category_en'] == tc.CATEGORY_EN[task['category']]


def test_english_next_actions_are_specific_to_each_task():
    values = [task['next_action_en'] for task in tc.CATALOG]
    assert len(values) == len(set(values))
    assert 'imaginary-frequency quality gate' in tc.get_task('freq')['next_action_en']
    assert 'every NEB image' in tc.get_task('neb')['next_action_en']
    assert 'method consistency' in tc.get_task('vaspsol')['next_action_en']
    assert 'per-atom energy differences' in tc.get_task('conv_encut')['next_action_en']


def test_english_catalog_preserves_scientific_symbols_tags_and_key_files():
    expected_markers = {
        'relax': ('POSCAR', 'INCAR', 'CONTCAR/OUTCAR/OSZICAR'),
        'cellopt': ('ISIF=3', 'ENCUT × 1.3', 'CONTCAR'),
        'static': ('OUTCAR/CHGCAR/WAVECAR',),
        'adsorption_project': ('ΔE', 'ENCUT'),
        'spin_scan': ('NUPDOWN',),
        'dos_pdos': ('LORBIT=11', 'DOSCAR/vasprun.xml'),
        'bands': ('CHGCAR', 'ICHARG=11', 'E(k)', 'EIGENVAL/vasprun.xml'),
        'bader': ('LAECHG', 'AECCAR/CHGCAR → ACF.dat'),
        'chgdiff': ('Δρ=ρ(AB)−ρ(A)−ρ(B)', 'Δρ̄(z)', 'CHGDIFF.vasp'),
        'elf': ('LELF=.TRUE.', 'ELFCAR', 'VESTA'),
        'freq': ('IBRION=5', 'ZPE', 'ΔE', 'ΔG', 'OUTCAR'),
        'aimd': ('NVT', 'NVE', 'Nose–Hoover', 'OSZICAR/XDATCAR'),
        'neb': ('CI-NEB', 'OUTCAR'),
        'dimer': ('VTST', 'VASP', 'CONTCAR'),
        'eos': ('EOS', 'Birch–Murnaghan', 'V0/E0/B0/B0′', 'BM3'),
        'surface_energy': ('γ=(E_slab−N·E_bulk)/2A', 'γ (J/m²)'),
        'workfunction': ('LVTOT', 'LOCPOT', 'φ=vacuum level−E_F'),
        'formation_binding': ('E_form / E_bind (eV)',),
        'vaspsol': ('VASPsol', 'E_sol−E_vac', 'ΔE_solv'),
        'conv_encut': ('ENCUT', 'INCAR'),
        'conv_kmesh': ('KPOINTS', 'INCAR'),
        'conv_vacuum': ('slab c axis', 'INCAR'),
        'conv_thickness': ('job.yaml inputs.recipe',),
    }
    assert set(expected_markers) == {task['key'] for task in tc.CATALOG}
    for task in tc.CATALOG:
        english = ' '.join(task[field] for field in
                           ('name_en', 'description_en', 'requires_en', 'outputs_en'))
        for marker in expected_markers[task['key']]:
            assert marker in english, f'{task["key"]} 英文条目丢失 {marker!r}'


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
