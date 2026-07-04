"""ClusterProfile S2 扩字段测试:向后兼容载入 + 新字段 round-trip + 表单映射。"""
import yaml

from vcstudio.cluster.profiles import ClusterProfile, load_profiles, save_profiles
from vcstudio.gui.logic import profile_from_form


def test_old_yaml_loads_with_defaults(tmp_path):
    """S1 时代的 clusters.yaml(无资源字段)→ 新字段走默认,不炸。"""
    p = tmp_path / 'clusters.yaml'
    p.write_text(yaml.safe_dump({'clusters': {'old': {
        'hostname': 'h', 'port': 22, 'username': 'u', 'auth': 'key',
        'key_path': 'k', 'use_jump': False, 'jump_host': '', 'jump_user': '',
        'jump_port': 22, 'remote_root': '/w', 'scheduler': 'PBS',
    }}}), encoding='utf-8')
    prof = load_profiles(p)['old']
    assert prof.scheduler == 'PBS'
    assert prof.queue == '' and prof.ppn == 0 and prof.nodes == 1
    assert prof.walltime == '24:00:00' and prof.env_lines == []
    assert prof.script_mode == 'auto' and prof.template_path == ''


def test_future_unknown_keys_ignored(tmp_path):
    p = tmp_path / 'clusters.yaml'
    p.write_text(yaml.safe_dump({'clusters': {'x': {
        'hostname': 'h', 'from_the_future': 42,
    }}}), encoding='utf-8')
    prof = load_profiles(p)['x']
    assert prof.hostname == 'h' and not hasattr(prof, 'from_the_future')


def test_corrupt_yaml_returns_empty_not_crash(tmp_path):
    """损坏的 clusters.yaml 不能拖死 GUI 启动(ClusterTab/JobsTab __init__ 同步调用它)。"""
    p = tmp_path / 'clusters.yaml'
    p.write_text('clusters: [unclosed', encoding='utf-8')
    assert load_profiles(p) == {}


def test_wrong_shape_yaml_returns_empty(tmp_path):
    """YAML 合法但形状不对(顶层是 list / clusters 是 list)→ 空表,不抛 AttributeError。"""
    p = tmp_path / 'clusters.yaml'
    p.write_text('- just\n- a list\n', encoding='utf-8')
    assert load_profiles(p) == {}
    p.write_text('clusters: [a, b]\n', encoding='utf-8')
    assert load_profiles(p) == {}


def test_corrupt_entry_skipped_good_kept(tmp_path):
    """单条目损坏只跳过该条,其余照常载入。"""
    p = tmp_path / 'clusters.yaml'
    p.write_text(yaml.safe_dump({'clusters': {
        'bad': [1, 2],
        'good': {'hostname': 'h'},
    }}), encoding='utf-8')
    profs = load_profiles(p)
    assert 'bad' not in profs
    assert profs['good'].hostname == 'h'


def test_new_fields_roundtrip_and_no_password(tmp_path):
    p = tmp_path / 'clusters.yaml'
    prof = ClusterProfile(
        name='1w', hostname='h', scheduler='PBS', scheduler_bin='/opt/t/bin',
        queue='batch', nodes=1, ppn=12, walltime='48:00:00',
        env_lines=['source a', 'module load b'], vasp_cmd='mpirun vasp_std',
        script_mode='template', template_path='D:/t.sh')
    save_profiles({'1w': prof}, p)
    raw = p.read_text(encoding='utf-8')
    assert 'password' not in raw                       # 安全底线不因扩字段而破
    back = load_profiles(p)['1w']
    assert back == prof


def test_profile_from_form_maps_resources():
    fields = {
        'hostname': 'h', 'port': '22', 'username': 'u', 'auth': 'key',
        'key_path': 'k', 'use_jump': False, 'jump_host': '', 'jump_user': '',
        'jump_port': '22', 'remote_root': '/w', 'scheduler': 'PBS',
        'scheduler_bin': ' /opt/t/bin ', 'queue': ' batch ', 'nodes': '2',
        'ppn': '12', 'walltime': '', 'vasp_cmd': ' mpirun vasp ',
        'script_mode': 'auto', 'template_path': '',
        'env_lines': 'source a\n\n  module load b  \n',
    }
    prof = profile_from_form('1w', fields)
    assert prof.scheduler_bin == '/opt/t/bin' and prof.queue == 'batch'
    assert prof.nodes == 2 and prof.ppn == 12
    assert prof.walltime == '24:00:00'                 # 空 → 默认
    assert prof.env_lines == ['source a', 'module load b']
    assert prof.vasp_cmd == 'mpirun vasp'
