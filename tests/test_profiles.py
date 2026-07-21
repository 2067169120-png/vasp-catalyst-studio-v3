from vcstudio.cluster.profiles import (
    ClusterProfile, load_profiles, save_profiles,
)


def test_profile_defaults():
    p = ClusterProfile(name='c1')
    assert p.port == 22 and p.auth == 'key' and p.scheduler == 'Slurm'
    assert not hasattr(p, 'password')  # 密码绝不进模型


def test_save_load_roundtrip(tmp_path):
    path = tmp_path / 'clusters.yaml'
    profs = {
        'bj': ClusterProfile(name='bj', hostname='login.bj.edu.cn', username='me',
                             auth='key', key_path='C:/k/id_rsa', scheduler='Slurm'),
        'lab': ClusterProfile(name='lab', hostname='10.0.0.9', username='u',
                              auth='password', scheduler='PBS'),
    }
    save_profiles(profs, path)
    back = load_profiles(path)
    assert set(back) == {'bj', 'lab'}
    assert back['bj'].hostname == 'login.bj.edu.cn'
    assert back['lab'].auth == 'password' and back['lab'].scheduler == 'PBS'


def test_engine_commands_roundtrip_without_breaking_legacy_vasp(tmp_path):
    path = tmp_path / 'clusters.yaml'
    profile = ClusterProfile(
        name='multi', vasp_cmd='srun vasp_std',
        engine_commands={
            'gaussian': 'g16 < {input} > {stem}.log',
            'cp2k': 'srun cp2k.psmp -i {input} -o {stem}.out',
        })
    save_profiles({'multi': profile}, path)

    loaded = load_profiles(path)['multi']

    assert loaded.vasp_cmd == 'srun vasp_std'
    assert loaded.engine_commands == profile.engine_commands


def test_yaml_never_contains_password(tmp_path):
    path = tmp_path / 'clusters.yaml'
    save_profiles({'c': ClusterProfile(name='c', username='u')}, path)
    text = path.read_text(encoding='utf-8')
    assert 'password' not in text.lower()


def test_load_missing_file_returns_empty(tmp_path):
    assert load_profiles(tmp_path / 'nope.yaml') == {}
