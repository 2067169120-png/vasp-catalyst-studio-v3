"""conftest 隔离夹具的回归测试:确认所有默认用户级路径都落在 per-test tmp,
绝不指向真实 %APPDATA%/vcstudio(防 pytest 污染用户注册表/台账)。"""
import os

from vcstudio.cluster import ledger, profiles, ssh_test
from vcstudio.project import adsorption
from vcstudio.shared import config


def _real_user_dir() -> str:
    """独立复算真实用户目录(不经被打桩的 user_config_dir),作为"决不等于"的对照。"""
    base = os.environ.get('APPDATA') or os.path.join(os.path.expanduser('~'), '.config')
    return os.path.join(base, 'vcstudio')


def test_user_config_dir_isolated_into_tmp(tmp_path):
    expected = tmp_path / 'vcstudio_cfg'
    assert config.user_config_dir() == expected
    # 决不指向真实 %APPDATA%/vcstudio
    assert os.path.normpath(str(config.user_config_dir())) != os.path.normpath(_real_user_dir())


def test_all_default_paths_under_tmp(tmp_path):
    """五个 from-import 持引用的模块 default_*_path() 全部随之隔离(逐个改绑生效)。"""
    isolated = tmp_path / 'vcstudio_cfg'
    assert ledger.default_ledger_path() == isolated / 'jobs.json'
    assert adsorption.default_registry_path() == isolated / 'projects.json'
    assert profiles.default_clusters_path() == isolated / 'clusters.yaml'
    assert ssh_test.default_known_hosts_path() == isolated / 'known_hosts'
    assert config.user_config_path() == isolated / 'config.yaml'


def test_create_project_writes_registry_and_ledger_into_tmp(tmp_path):
    """走 test_advisor 同一条未 stub 路径:create_project 的注册表/台账落 tmp,不碰真实用户目录。"""
    lib = tmp_path / 'lib'
    (lib / 'C').mkdir(parents=True)
    (lib / 'C' / 'POTCAR').write_text(
        ' fake PAW_PBE C\n   TITEL  = PAW_PBE C 08Apr2002\n'
        '   ENMAX  =  273.214; ENMIN = 200.000 eV\n', encoding='utf-8')
    slab = tmp_path / 'slab.vasp'
    slab.write_text('C slab\n1.0\n10 0 0\n0 10 0\n0 0 14\nC\n1\nCartesian\n0 0 0\n',
                    encoding='utf-8')
    incar = tmp_path / 'INCAR'
    incar.write_text('ENCUT = 400\nISMEAR = 0\nNSW = 100\nIBRION = 2\n', encoding='utf-8')

    res = adsorption.create_project(tmp_path / 'proj', 'iso', clean_poscar=str(slab),
                                    config_poscars=[str(slab)], incar_path=str(incar),
                                    lib_root=str(lib))
    assert res['ok']
    # 注册表与台账都写进隔离目录,真实用户目录零污染
    reg = adsorption.default_registry_path()
    led = ledger.default_ledger_path()
    assert reg.parent == tmp_path / 'vcstudio_cfg' and reg.is_file()
    assert led.parent == tmp_path / 'vcstudio_cfg' and led.is_file()
    assert res['project_path'] in adsorption.list_projects()
