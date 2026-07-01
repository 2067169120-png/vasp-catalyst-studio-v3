# VASP Catalyst Studio 桌面 GUI(EXE)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `vcs gen` 封装成自包含单文件 Windows EXE:两页界面(生成 + 集群连接),让计算化学用户方便配置、一键生成 VASP 四件套、一键打开输出目录。

**Architecture:** Tkinter 单窗口 + `ttk.Notebook` 两页。GUI 只做界面与事件编排,生成逻辑一律复用已测的 `vcstudio.generate.job_builder.build_job_dir()`,集群逻辑放独立 `vcstudio/cluster/`。所有可测逻辑(config/secrets/profiles/ssh_test/runner/纯helpers)脱离 Tk 单测;Tk 控件组装人工验收。PyInstaller `--onefile --windowed` 打包。

**Tech Stack:** Python ≥3.9、Tkinter(标准库)、PyYAML、paramiko(SSH)、keyring(密码加密存)、PyInstaller。**移除 numpy**。

## Global Constraints

- Python ≥ 3.9;Windows 为主要目标平台。
- 界面文案中文;标识符英文。
- **尊重用户 INCAR**:GUI 复用 `build_job_dir`,绝不重写生成/校验逻辑。
- **密码绝不明文写入任何 yaml**;只经 keyring 存,或每次现输(仅内存)。
- SSH 密钥只存**私钥文件路径**,绝不存密钥内容。
- 错误一律转友好中文文案显示,**绝不弹 Python traceback**、绝不让界面崩溃。
- `clusters.yaml`、`known_hosts`、`config.yaml`(落项目内时)、`_internal/`、`build/`、`dist/` 一律 gitignore。
- 已有生成核心签名(不改):`build_job_dir(poscar_path, incar, out_dir, *, calc_type='slab', kpoints=None, validate=True, lib_root=None, system_name='') -> {'ok','out_dir','warnings','kpoints','elements'}`;失败抛 `ValueError/PotcarError/OSError`。

---

### Task 1: 工程脚手架(依赖清单 + pytest + 入口 + 安装)

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

**Interfaces:**
- Produces: 可运行的 `python -m pytest`;`paramiko`、`keyring` 可导入;`vcs-gui` 入口声明就位。

- [ ] **Step 1: 改写 `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "vcstudio"
version = "0.1.0"
description = "VASP Catalyst Studio — 轻量化 DFT 自动化:POSCAR+用户INCAR → 输入四件套 / 多集群提交 / 零token监控 / LLM分析"
readme = "README.md"
requires-python = ">=3.9"
dependencies = [
    "PyYAML>=6.0",
    "paramiko>=3.0",
    "keyring>=24.0",
]

[project.optional-dependencies]
dev = ["pytest>=7"]

[project.scripts]
vcs = "vcstudio.cli.main:main"
vcs-gui = "vcstudio.gui.app:main"

[tool.setuptools.packages.find]
include = ["vcstudio*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2: 创建 `tests/__init__.py`(空文件)**

```python
```

- [ ] **Step 3: 创建 `tests/conftest.py`**

```python
"""pytest 公共夹具。当前留空,后续任务按需添加。"""
```

- [ ] **Step 4: 安装 dev 依赖(含新增运行时依赖 paramiko/keyring)**

Run: `pip install -e ".[dev]"`
Expected: 成功;安装 paramiko、keyring;不再需要 numpy。

- [ ] **Step 5: 校验环境**

Run: `python -c "import paramiko, keyring; print('deps ok')" && python -m pytest -q`
Expected: 打印 `deps ok`;pytest 输出 `no tests ran`(0 collected)。

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml tests/__init__.py tests/conftest.py
git commit -m "chore(gui): 脚手架 — 依赖(+paramiko/keyring,-numpy)、pytest、vcs-gui 入口"
```

---

### Task 2: 去掉 numpy 依赖(`kpoints.py` 改标准库)

**Files:**
- Modify: `vcstudio/generate/kpoints.py:28,33`
- Test: `tests/test_kpoints.py`

**Interfaces:**
- Produces: `recommend_kpoints(cell_vectors, calc_type='slab') -> list[int]`(签名不变,不再依赖 numpy)。

- [ ] **Step 1: 写失败测试 `tests/test_kpoints.py`**

```python
import importlib
import sys

from vcstudio.generate.kpoints import recommend_kpoints, kpoints_str


def test_molecule_is_gamma_only():
    assert recommend_kpoints([[10, 0, 0], [0, 10, 0], [0, 0, 10]], 'molecule') == [1, 1, 1]


def test_slab_forces_kz_1():
    # 2.87 Å 立方(bcc Fe):1/(0.03*2.87)=11.6 → ceil 12 → 偶数+1=13 → cap 9;slab kz=1
    assert recommend_kpoints([[2.87, 0, 0], [0, 2.87, 0], [0, 0, 2.87]], 'slab') == [9, 9, 1]


def test_bulk_all_three_directions():
    assert recommend_kpoints([[2.87, 0, 0], [0, 2.87, 0], [0, 0, 2.87]], 'bulk') == [9, 9, 9]


def test_large_cell_gives_one_k():
    # 20 Å 盒子:1/(0.03*20)=1.67 → ceil 2 → 偶数+1=3;确认长度用的是矢量模长
    assert recommend_kpoints([[20, 0, 0], [0, 20, 0], [0, 0, 20]], 'bulk') == [3, 3, 3]


def test_kpoints_module_does_not_import_numpy():
    sys.modules.pop('numpy', None)
    mod = importlib.reload(importlib.import_module('vcstudio.generate.kpoints'))
    mod.recommend_kpoints([[3, 0, 0], [0, 3, 0], [0, 0, 3]], 'bulk')
    assert 'numpy' not in sys.modules


def test_kpoints_str_format():
    assert kpoints_str([5, 5, 1]) == 'Automatic\n0\nGamma\n5 5 1\n0 0 0\n'
```

- [ ] **Step 2: 运行,确认失败**

Run: `python -m pytest tests/test_kpoints.py -v`
Expected: `test_kpoints_module_does_not_import_numpy` 失败(numpy 仍被导入)。

- [ ] **Step 3: 改 `kpoints.py` 去 numpy**

删除第 28 行 `import numpy as np`;把第 33 行

```python
    lengths = [np.linalg.norm(v) for v in cell_vectors[:3]]
```

改为:

```python
    lengths = [math.sqrt(sum(c * c for c in v[:3])) for v in cell_vectors[:3]]
```

(`import math` 已在文件顶部,无需新增。)

- [ ] **Step 4: 运行,确认通过**

Run: `python -m pytest tests/test_kpoints.py -v`
Expected: 6 passed。

- [ ] **Step 5: Commit**

```bash
git add vcstudio/generate/kpoints.py tests/test_kpoints.py
git commit -m "refactor(kpoints): 去 numpy 依赖,矢量模长改标准库 math(冻结瘦身)"
```

---

### Task 3: `config.py` 增加写入 + 冻结感知的可写路径

**Files:**
- Modify: `vcstudio/shared/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: 现有 `load_config(path=None) -> dict`、`find_config_path() -> Path|None`、`_DEFAULT`。
- Produces:
  - `user_config_dir() -> Path`(`%APPDATA%/vcstudio`,无 APPDATA 退回 `~/.config/vcstudio`)
  - `user_config_path() -> Path`
  - `writable_config_path() -> Path`
  - `save_config(cfg: dict, path=None) -> Path`
  - `set_potcar_lib_root(path_str: str, config_path=None) -> Path`

- [ ] **Step 1: 写失败测试 `tests/test_config.py`**

```python
import importlib

import vcstudio.shared.config as cfgmod


def _reload(monkeypatch, appdata):
    monkeypatch.setenv('APPDATA', str(appdata))
    monkeypatch.delenv('VCSTUDIO_CONFIG', raising=False)
    return importlib.reload(cfgmod)


def test_user_config_path_under_appdata(tmp_path, monkeypatch):
    m = _reload(monkeypatch, tmp_path)
    assert m.user_config_path() == tmp_path / 'vcstudio' / 'config.yaml'


def test_save_then_load_roundtrip(tmp_path, monkeypatch):
    m = _reload(monkeypatch, tmp_path)
    p = m.save_config({'potcar_lib_root': 'D:/lib', 'llm': {}, 'ssh': {}},
                      m.user_config_path())
    assert p.is_file()
    loaded = m.load_config(p)
    assert loaded['potcar_lib_root'] == 'D:/lib'


def test_set_potcar_lib_root_preserves_other_keys(tmp_path, monkeypatch):
    m = _reload(monkeypatch, tmp_path)
    cfgpath = m.user_config_path()
    m.save_config({'potcar_lib_root': 'old', 'llm': {'x': 1}, 'ssh': {}}, cfgpath)
    m.set_potcar_lib_root('E:/newlib', cfgpath)
    loaded = m.load_config(cfgpath)
    assert loaded['potcar_lib_root'] == 'E:/newlib'
    assert loaded['llm'] == {'x': 1}  # 其他键不动


def test_writable_path_prefers_frozen_user_dir(tmp_path, monkeypatch):
    m = _reload(monkeypatch, tmp_path)
    monkeypatch.setattr(m.sys, 'frozen', True, raising=False)
    assert m.writable_config_path() == m.user_config_path()


def test_env_var_overrides_everything(tmp_path, monkeypatch):
    # 注意:editable 安装下"包旁 config.yaml"(仓库根)真实存在,会先于 user 路径命中,
    # 故不测 user 回退(不可控);改测 env 显式覆盖这条最高优先级、可控的路径。
    m = importlib.reload(cfgmod)
    target = tmp_path / 'custom.yaml'
    monkeypatch.setenv('VCSTUDIO_CONFIG', str(target))
    assert m.writable_config_path() == target
    assert m.find_config_path() == target
```

- [ ] **Step 2: 运行,确认失败**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL(`user_config_path` 等未定义 / `sys` 未导入)。

- [ ] **Step 3: 改 `config.py`**

在文件顶部 import 区加入 `import sys`(与现有 `import os`、`from pathlib import Path` 并列)。

把 `find_config_path` 改为在末尾增加 user 路径回退(其余不变):

```python
def find_config_path() -> Path | None:
    """按 env > cwd > 包旁 > 用户目录 顺序定位 config.yaml;都无 → None。"""
    env = os.environ.get('VCSTUDIO_CONFIG')
    if env:
        return Path(env)
    cwd = Path.cwd() / 'config.yaml'
    if cwd.is_file():
        return cwd
    beside = Path(__file__).resolve().parents[2] / 'config.yaml'  # shared → vcstudio → 包根
    if beside.is_file():
        return beside
    user = user_config_path()
    if user.is_file():
        return user
    return None
```

在 `get_potcar_lib_root` 之后追加:

```python
def user_config_dir() -> Path:
    """用户级配置目录:Windows %APPDATA%/vcstudio,否则 ~/.config/vcstudio。"""
    base = os.environ.get('APPDATA') or os.path.join(os.path.expanduser('~'), '.config')
    return Path(base) / 'vcstudio'


def user_config_path() -> Path:
    """用户级 config.yaml 路径(冻结版可写落点)。"""
    return user_config_dir() / 'config.yaml'


def writable_config_path() -> Path:
    """决定写配置落到哪:env 显式 > 冻结用用户目录 > 已有文件原地 > 用户目录兜底。"""
    env = os.environ.get('VCSTUDIO_CONFIG')
    if env:
        return Path(env)
    if getattr(sys, 'frozen', False):
        return user_config_path()
    found = find_config_path()
    if found is not None:
        return found
    return user_config_path()


def save_config(cfg: dict, path: str | os.PathLike | None = None) -> Path:
    """把配置 dict 写入 yaml(UTF-8)。path 缺省用 writable_config_path()。"""
    target = Path(path) if path is not None else writable_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, 'w', encoding='utf-8') as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    return target


def set_potcar_lib_root(path_str: str, config_path: str | os.PathLike | None = None) -> Path:
    """只更新 potcar_lib_root 并持久化,其余键原样保留。返回写入路径。"""
    resolved = Path(config_path) if config_path is not None else writable_config_path()
    cfg = load_config(resolved)
    cfg['potcar_lib_root'] = path_str
    return save_config(cfg, resolved)
```

- [ ] **Step 4: 运行,确认通过**

Run: `python -m pytest tests/test_config.py -v`
Expected: 5 passed。

- [ ] **Step 5: Commit**

```bash
git add vcstudio/shared/config.py tests/test_config.py
git commit -m "feat(config): 增 save_config/set_potcar_lib_root + 冻结感知可写路径(%APPDATA%)"
```

---

### Task 4: `shared/secrets.py` — keyring 密码封装

**Files:**
- Create: `vcstudio/shared/secrets.py`
- Test: `tests/test_secrets.py`

**Interfaces:**
- Produces:
  - `SERVICE: str = 'vcstudio-cluster'`
  - `available() -> bool`
  - `set_password(profile: str, password: str) -> bool`
  - `get_password(profile: str) -> str | None`
  - `delete_password(profile: str) -> None`

- [ ] **Step 1: 写失败测试 `tests/test_secrets.py`**

```python
import vcstudio.shared.secrets as secrets


class FakeKeyring:
    """内存假 keyring 后端。"""
    def __init__(self):
        self.store = {}
    def set_password(self, service, name, pw):
        self.store[(service, name)] = pw
    def get_password(self, service, name):
        return self.store.get((service, name))
    def delete_password(self, service, name):
        self.store.pop((service, name), None)


def test_roundtrip_with_backend(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(secrets, 'keyring', fake)
    assert secrets.available() is True
    assert secrets.set_password('北京超算', 's3cret') is True
    assert secrets.get_password('北京超算') == 's3cret'
    secrets.delete_password('北京超算')
    assert secrets.get_password('北京超算') is None


def test_degrades_when_keyring_missing(monkeypatch):
    monkeypatch.setattr(secrets, 'keyring', None)
    assert secrets.available() is False
    assert secrets.set_password('x', 'y') is False
    assert secrets.get_password('x') is None
    secrets.delete_password('x')  # 不抛


def test_get_swallows_backend_errors(monkeypatch):
    class Boom:
        def get_password(self, *a): raise RuntimeError('backend down')
    monkeypatch.setattr(secrets, 'keyring', Boom())
    assert secrets.get_password('x') is None
```

- [ ] **Step 2: 运行,确认失败**

Run: `python -m pytest tests/test_secrets.py -v`
Expected: FAIL(模块不存在)。

- [ ] **Step 3: 创建 `vcstudio/shared/secrets.py`**

```python
"""集群密码的安全存取:走 keyring(Windows 凭据库)。绝不明文落 yaml。

keyring 不可用(未装/无后端)时全部安全降级:set 返回 False、get 返回 None,
由调用方转为"每次现输"。中文注释允许,英文标识符。
"""
from __future__ import annotations

try:
    import keyring  # type: ignore
except Exception:  # pragma: no cover - 环境相关
    keyring = None  # type: ignore

SERVICE = 'vcstudio-cluster'


def available() -> bool:
    """keyring 后端是否可用。"""
    return keyring is not None


def set_password(profile: str, password: str) -> bool:
    """存密码到凭据库,键=profile 名。成功 True;keyring 不可用/失败 False。"""
    if keyring is None:
        return False
    try:
        keyring.set_password(SERVICE, profile, password)
        return True
    except Exception:
        return False


def get_password(profile: str) -> str | None:
    """取密码;无/不可用/异常 → None。"""
    if keyring is None:
        return None
    try:
        return keyring.get_password(SERVICE, profile)
    except Exception:
        return None


def delete_password(profile: str) -> None:
    """删密码;不可用或不存在都静默。"""
    if keyring is None:
        return
    try:
        keyring.delete_password(SERVICE, profile)
    except Exception:
        pass
```

- [ ] **Step 4: 运行,确认通过**

Run: `python -m pytest tests/test_secrets.py -v`
Expected: 3 passed。

- [ ] **Step 5: Commit**

```bash
git add vcstudio/shared/secrets.py tests/test_secrets.py
git commit -m "feat(secrets): keyring 密码封装,不可用安全降级(绝不明文)"
```

---

### Task 5: `cluster/profiles.py` — 集群 profile 模型与 clusters.yaml 读写

**Files:**
- Create: `vcstudio/cluster/__init__.py`
- Create: `vcstudio/cluster/profiles.py`
- Test: `tests/test_profiles.py`

**Interfaces:**
- Consumes: `vcstudio.shared.config.user_config_dir()`。
- Produces:
  - `ClusterProfile`(dataclass;字段见下;**无 password 字段**)
  - `default_clusters_path() -> Path`
  - `load_profiles(path=None) -> dict[str, ClusterProfile]`
  - `save_profiles(profiles: dict[str, ClusterProfile], path=None) -> Path`

- [ ] **Step 1: 写失败测试 `tests/test_profiles.py`**

```python
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


def test_yaml_never_contains_password(tmp_path):
    path = tmp_path / 'clusters.yaml'
    save_profiles({'c': ClusterProfile(name='c', username='u')}, path)
    text = path.read_text(encoding='utf-8')
    assert 'password' not in text.lower()


def test_load_missing_file_returns_empty(tmp_path):
    assert load_profiles(tmp_path / 'nope.yaml') == {}
```

- [ ] **Step 2: 运行,确认失败**

Run: `python -m pytest tests/test_profiles.py -v`
Expected: FAIL(模块不存在)。

- [ ] **Step 3: 创建 `vcstudio/cluster/__init__.py`(空)**

```python
```

- [ ] **Step 4: 创建 `vcstudio/cluster/profiles.py`**

```python
"""集群连接 profile 模型 + clusters.yaml 读写(M2 集群区前置)。

安全底线:模型**不含密码字段**,故 yaml 天然不可能写出密码;密码只走 keyring
(见 vcstudio.shared.secrets)。字段对应 DPDispatcher 的 Machine 连接部分。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from pathlib import Path

import yaml

from vcstudio.shared.config import user_config_dir


@dataclass
class ClusterProfile:
    """一个集群的连接配置(不含密码)。auth ∈ {'key','password'}。"""
    name: str
    hostname: str = ''
    port: int = 22
    username: str = ''
    auth: str = 'key'            # 'key' | 'password'
    key_path: str = ''           # 私钥文件路径(仅路径,绝不存内容)
    use_jump: bool = False
    jump_host: str = ''
    jump_user: str = ''
    jump_port: int = 22
    remote_root: str = ''
    scheduler: str = 'Slurm'     # Slurm | PBS | LSF | Shell


def default_clusters_path() -> Path:
    """默认 clusters.yaml 路径:%APPDATA%/vcstudio/clusters.yaml。"""
    return user_config_dir() / 'clusters.yaml'


def load_profiles(path: str | os.PathLike | None = None) -> dict:
    """读 clusters.yaml → {name: ClusterProfile}。文件不存在 → {}。"""
    target = Path(path) if path is not None else default_clusters_path()
    if not target.is_file():
        return {}
    with open(target, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f) or {}
    out: dict = {}
    for name, d in (data.get('clusters') or {}).items():
        fields = {k: v for k, v in (d or {}).items() if k != 'name'}
        out[name] = ClusterProfile(name=name, **fields)
    return out


def save_profiles(profiles: dict, path: str | os.PathLike | None = None) -> Path:
    """把 {name: ClusterProfile} 写入 clusters.yaml(UTF-8)。返回写入路径。"""
    target = Path(path) if path is not None else default_clusters_path()
    out = {'clusters': {}}
    for name, p in profiles.items():
        d = asdict(p)
        d.pop('name', None)
        out['clusters'][name] = d
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, 'w', encoding='utf-8') as f:
        yaml.safe_dump(out, f, allow_unicode=True, sort_keys=False)
    return target
```

- [ ] **Step 5: 运行,确认通过**

Run: `python -m pytest tests/test_profiles.py -v`
Expected: 4 passed。

- [ ] **Step 6: Commit**

```bash
git add vcstudio/cluster/__init__.py vcstudio/cluster/profiles.py tests/test_profiles.py
git commit -m "feat(cluster): ClusterProfile 模型 + clusters.yaml 读写(模型不含密码)"
```

---

### Task 6: `cluster/ssh_test.py` — paramiko 测连接

**Files:**
- Create: `vcstudio/cluster/ssh_test.py`
- Test: `tests/test_ssh_test.py`

**Interfaces:**
- Consumes: `ClusterProfile`。
- Produces:
  - `ConnectionResult`(dataclass:`ok:bool, whoami:str='', scheduler:str='', message:str='', needs_trust:bool=False`)
  - `default_known_hosts_path() -> Path`
  - `check_connection(profile, password=None, *, trust_new=False, known_hosts_path=None, client_factory=None) -> ConnectionResult`
- 说明:`client_factory` 缺省用 `paramiko.SSHClient`,测试注入假客户端。`command -v sbatch qsub bsub` 探测调度器。跳板机用 direct-tcpip 通道作 `sock`。

- [ ] **Step 1: 写失败测试 `tests/test_ssh_test.py`**

```python
import pytest

from vcstudio.cluster.profiles import ClusterProfile
from vcstudio.cluster.ssh_test import check_connection, ConnectionResult


class FakeChannelFile:
    def __init__(self, text): self._t = text
    def read(self): return self._t.encode()


class FakeClient:
    """记录 connect 参数,按脚本回应 exec_command。"""
    def __init__(self, exec_map, raise_on_connect=None):
        self.exec_map = exec_map
        self.raise_on_connect = raise_on_connect
        self.connect_kwargs = None
        self.policy = None
    def set_missing_host_key_policy(self, p): self.policy = p
    def load_system_host_keys(self, *a): pass
    def load_host_keys(self, *a): pass
    def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        if self.raise_on_connect:
            raise self.raise_on_connect
    def exec_command(self, cmd):
        for key, val in self.exec_map.items():
            if key in cmd:
                return (None, FakeChannelFile(val), FakeChannelFile(''))
        return (None, FakeChannelFile(''), FakeChannelFile(''))
    def close(self): pass


def test_successful_key_auth_reports_whoami_and_scheduler():
    fake = FakeClient({'whoami': 'alice\n', 'command -v': '/usr/bin/sbatch\n'})
    prof = ClusterProfile(name='c', hostname='h', username='alice',
                          auth='key', key_path='/k/id_rsa')
    res = check_connection(prof, client_factory=lambda: fake, trust_new=True)
    assert res.ok is True
    assert res.whoami == 'alice'
    assert res.scheduler == 'Slurm'
    assert fake.connect_kwargs['key_filename'] == '/k/id_rsa'
    assert fake.connect_kwargs['hostname'] == 'h'


def test_password_auth_passes_password():
    fake = FakeClient({'whoami': 'bob\n', 'command -v': '/bin/qsub\n'})
    prof = ClusterProfile(name='c', hostname='h', username='bob', auth='password')
    res = check_connection(prof, password='pw', client_factory=lambda: fake, trust_new=True)
    assert res.ok is True and res.scheduler == 'PBS'
    assert fake.connect_kwargs['password'] == 'pw'


def test_auth_failure_is_friendly():
    import paramiko
    fake = FakeClient({}, raise_on_connect=paramiko.AuthenticationException())
    prof = ClusterProfile(name='c', hostname='h', username='x', auth='password')
    res = check_connection(prof, password='bad', client_factory=lambda: fake, trust_new=True)
    assert res.ok is False and '认证' in res.message


def test_unknown_host_needs_trust():
    import paramiko
    fake = FakeClient({}, raise_on_connect=paramiko.ssh_exception.SSHException('unknown'))
    prof = ClusterProfile(name='c', hostname='h', username='x', auth='key', key_path='/k')
    res = check_connection(prof, client_factory=lambda: fake, trust_new=False)
    assert res.ok is False and res.needs_trust is True
```

- [ ] **Step 2: 运行,确认失败**

Run: `python -m pytest tests/test_ssh_test.py -v`
Expected: FAIL(模块不存在)。

- [ ] **Step 3: 创建 `vcstudio/cluster/ssh_test.py`**

```python
"""集群连接测试:paramiko 真连 → whoami + 调度器探测。M2 提交前的"配好没"校验。

主机指纹策略(防 MITM):默认加载 known_hosts,遇未知主机不静默信任,返回
needs_trust=True 交界面确认;确认后 trust_new=True 再连并落 known_hosts。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import paramiko

from vcstudio.shared.config import user_config_dir

# 调度器 → 探测命令关键字(存在即判定)
_SCHED_PROBES = [('Slurm', 'sbatch'), ('PBS', 'qsub'), ('LSF', 'bsub')]


@dataclass
class ConnectionResult:
    ok: bool
    whoami: str = ''
    scheduler: str = ''
    message: str = ''
    needs_trust: bool = False


def default_known_hosts_path() -> Path:
    return user_config_dir() / 'known_hosts'


def _detect_scheduler(client) -> str:
    """跑一条 command -v 探测,返回 Slurm/PBS/LSF/Shell。"""
    probe = 'command -v sbatch qsub bsub 2>/dev/null'
    _in, out, _err = client.exec_command(probe)
    found = out.read().decode(errors='replace')
    for name, exe in _SCHED_PROBES:
        if exe in found:
            return name
    return 'Shell'


def check_connection(profile, password: str | None = None, *,
                    trust_new: bool = False,
                    known_hosts_path: str | os.PathLike | None = None,
                    client_factory=None) -> ConnectionResult:
    """连一次集群并回报结果。client_factory 缺省 paramiko.SSHClient(测试可注入)。"""
    factory = client_factory or paramiko.SSHClient
    kh = Path(known_hosts_path) if known_hosts_path is not None else default_known_hosts_path()

    client = factory()
    try:
        try:
            client.load_system_host_keys()
        except Exception:
            pass
        if kh.is_file():
            try:
                client.load_host_keys(str(kh))
            except Exception:
                pass
        client.set_missing_host_key_policy(
            paramiko.AutoAddPolicy() if trust_new else paramiko.RejectPolicy())

        connect_kwargs = dict(hostname=profile.hostname, port=int(profile.port),
                              username=profile.username, timeout=15,
                              allow_agent=False, look_for_keys=False)
        if profile.auth == 'key':
            connect_kwargs['key_filename'] = profile.key_path
        else:
            connect_kwargs['password'] = password

        if profile.use_jump and profile.jump_host:
            connect_kwargs['sock'] = _open_jump_channel(profile, password, factory)

        try:
            client.connect(**connect_kwargs)
        except paramiko.AuthenticationException:
            return ConnectionResult(ok=False, message='认证失败:用户名/密钥/密码不正确')
        except paramiko.ssh_exception.SSHException as e:
            # 未知主机指纹或 SSH 协议层错误 → 请界面确认信任
            return ConnectionResult(ok=False, needs_trust=not trust_new,
                              message=f'无法确认主机指纹或 SSH 错误:{e};确认后可信任重试')
        except (OSError, Exception) as e:  # 网络不可达等
            return ConnectionResult(ok=False, message=f'连接失败:{e}')

        _in, out, _err = client.exec_command('whoami')
        who = out.read().decode(errors='replace').strip()
        sched = _detect_scheduler(client)

        if trust_new:
            try:
                kh.parent.mkdir(parents=True, exist_ok=True)
                client.save_host_keys(str(kh))
            except Exception:
                pass

        return ConnectionResult(ok=True, whoami=who, scheduler=sched,
                          message=f'已连上 {profile.hostname},whoami={who},检测到 {sched}')
    finally:
        try:
            client.close()
        except Exception:
            pass


def _open_jump_channel(profile, password, factory):
    """经跳板机开 direct-tcpip 通道,作为目标连接的 sock。"""
    jump = factory()
    jump.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    jkwargs = dict(hostname=profile.jump_host, port=int(profile.jump_port),
                   username=profile.jump_user or profile.username, timeout=15,
                   allow_agent=False, look_for_keys=False)
    if profile.auth == 'key':
        jkwargs['key_filename'] = profile.key_path
    else:
        jkwargs['password'] = password
    jump.connect(**jkwargs)
    transport = jump.get_transport()
    return transport.open_channel(
        'direct-tcpip', (profile.hostname, int(profile.port)), ('127.0.0.1', 0))
```

- [ ] **Step 4: 运行,确认通过**

Run: `python -m pytest tests/test_ssh_test.py -v`
Expected: 4 passed。

- [ ] **Step 5: Commit**

```bash
git add vcstudio/cluster/ssh_test.py tests/test_ssh_test.py
git commit -m "feat(cluster): paramiko 测连接 — whoami+调度器探测,未知指纹需确认(防 MITM)"
```

---

### Task 7: `gui/runner.py` — 后台线程 + 队列

**Files:**
- Create: `vcstudio/gui/__init__.py`
- Create: `vcstudio/gui/runner.py`
- Test: `tests/test_runner.py`

**Interfaces:**
- Produces:
  - `submit(fn, *args, **kwargs) -> queue.Queue`(起 daemon 线程跑 fn,结果 `('ok', 返回值)` 或 `('error', 异常)` 入队)
  - `poll(q) -> tuple | None`(非阻塞取一条;空 → None)

- [ ] **Step 1: 写失败测试 `tests/test_runner.py`**

```python
from vcstudio.gui.runner import submit, poll


def test_submit_ok_puts_result():
    q = submit(lambda a, b: a + b, 2, 3)
    kind, val = q.get(timeout=3)
    assert kind == 'ok' and val == 5


def test_submit_error_puts_exception():
    def boom():
        raise ValueError('nope')
    q = submit(boom)
    kind, err = q.get(timeout=3)
    assert kind == 'error' and isinstance(err, ValueError)


def test_poll_empty_returns_none():
    import queue
    assert poll(queue.Queue()) is None
```

- [ ] **Step 2: 运行,确认失败**

Run: `python -m pytest tests/test_runner.py -v`
Expected: FAIL(模块不存在)。

- [ ] **Step 3: 创建 `vcstudio/gui/__init__.py`(空)**

```python
```

- [ ] **Step 4: 创建 `vcstudio/gui/runner.py`**

```python
"""后台线程 + 队列:让耗时活(生成/测连接)不冻界面。

用法:q = submit(fn, ...);Tk 里用 widget.after(100, ...) 轮询 poll(q)。
结果统一 ('ok', 返回值) 或 ('error', 异常)。中文注释允许,英文标识符。
"""
from __future__ import annotations

import queue
import threading


def submit(fn, *args, **kwargs) -> queue.Queue:
    """在 daemon 线程跑 fn(*args, **kwargs),结果入队并返回该队列。"""
    q: queue.Queue = queue.Queue()

    def worker():
        try:
            q.put(('ok', fn(*args, **kwargs)))
        except Exception as e:  # 任何异常都转消息,绝不让线程崩到界面
            q.put(('error', e))

    threading.Thread(target=worker, daemon=True).start()
    return q


def poll(q: queue.Queue):
    """非阻塞取一条结果;队列空返回 None。"""
    try:
        return q.get_nowait()
    except queue.Empty:
        return None
```

- [ ] **Step 5: 运行,确认通过**

Run: `python -m pytest tests/test_runner.py -v`
Expected: 3 passed。

- [ ] **Step 6: Commit**

```bash
git add vcstudio/gui/__init__.py vcstudio/gui/runner.py tests/test_runner.py
git commit -m "feat(gui): runner — 后台线程+队列,界面不冻"
```

---

### Task 8: `gui/logic.py` — 纯逻辑 helpers(脱离 Tk 可测)

**Files:**
- Create: `vcstudio/gui/logic.py`
- Test: `tests/test_gui_logic.py`

**Interfaces:**
- Consumes: `ClusterProfile`。
- Produces:
  - `parse_kpoints_field(text: str) -> list[int] | None`(空/"自动" → None;"5 5 1" → [5,5,1];非法抛 ValueError)
  - `validate_generate_inputs(poscar, incar, out_dir, lib_root) -> list[str]`
  - `profile_from_form(name: str, fields: dict) -> ClusterProfile`
  - `validate_cluster_inputs(profile: ClusterProfile, has_password: bool) -> list[str]`

- [ ] **Step 1: 写失败测试 `tests/test_gui_logic.py`**

```python
import pytest

from vcstudio.gui.logic import (
    parse_kpoints_field, validate_generate_inputs,
    profile_from_form, validate_cluster_inputs,
)
from vcstudio.cluster.profiles import ClusterProfile


def test_parse_kpoints_auto_and_empty():
    assert parse_kpoints_field('') is None
    assert parse_kpoints_field('  ') is None
    assert parse_kpoints_field('自动') is None
    assert parse_kpoints_field('auto') is None


def test_parse_kpoints_valid():
    assert parse_kpoints_field('5 5 1') == [5, 5, 1]


def test_parse_kpoints_bad_raises():
    with pytest.raises(ValueError):
        parse_kpoints_field('5 5')
    with pytest.raises(ValueError):
        parse_kpoints_field('a b c')


def test_validate_generate_inputs_collects_errors(tmp_path):
    errs = validate_generate_inputs('', '', '', '')
    assert any('赝势库' in e for e in errs)
    assert any('POSCAR' in e for e in errs)
    assert any('INCAR' in e for e in errs)
    assert any('输出' in e for e in errs)


def test_validate_generate_inputs_ok(tmp_path):
    p = tmp_path / 'POSCAR'; p.write_text('x')
    i = tmp_path / 'INCAR'; i.write_text('x')
    assert validate_generate_inputs(str(p), str(i), str(tmp_path / 'out'), 'D:/lib') == []


def test_profile_from_form_maps_fields():
    prof = profile_from_form('bj', {
        'hostname': 'h', 'port': '22', 'username': 'u', 'auth': 'key',
        'key_path': '/k', 'use_jump': False, 'jump_host': '', 'jump_user': '',
        'jump_port': '22', 'remote_root': '/r', 'scheduler': 'Slurm'})
    assert isinstance(prof, ClusterProfile)
    assert prof.name == 'bj' and prof.port == 22 and prof.remote_root == '/r'


def test_validate_cluster_inputs():
    ok = ClusterProfile(name='c', hostname='h', username='u', auth='key', key_path='/k')
    assert validate_cluster_inputs(ok, has_password=False) == []
    bad = ClusterProfile(name='', hostname='', username='', auth='password')
    errs = validate_cluster_inputs(bad, has_password=False)
    assert any('主机' in e for e in errs)
    assert any('密码' in e for e in errs)
```

- [ ] **Step 2: 运行,确认失败**

Run: `python -m pytest tests/test_gui_logic.py -v`
Expected: FAIL(模块不存在)。

- [ ] **Step 3: 创建 `vcstudio/gui/logic.py`**

```python
"""GUI 纯逻辑 helpers:字段解析/校验、表单↔profile 映射。**不 import tkinter**,可单测。

中文注释允许,英文标识符。
"""
from __future__ import annotations

import os

from vcstudio.cluster.profiles import ClusterProfile


def parse_kpoints_field(text: str):
    """KPOINTS 输入框 → [kx,ky,kz] 或 None(自动)。非法抛 ValueError(中文文案)。"""
    t = (text or '').strip()
    if t == '' or t.lower() in ('auto', '自动'):
        return None
    parts = t.split()
    try:
        nums = [int(x) for x in parts]
    except ValueError:
        raise ValueError('KPOINTS 需 3 个整数,如 "5 5 1"')
    if len(nums) != 3:
        raise ValueError('KPOINTS 需恰好 3 个整数,如 "5 5 1"')
    return nums


def validate_generate_inputs(poscar: str, incar: str, out_dir: str, lib_root: str) -> list:
    """生成前预检,返回错误文案列表(空=通过)。"""
    errs = []
    if not lib_root:
        errs.append('未设置赝势库(POTCAR lib)路径')
    if not poscar or not os.path.isfile(poscar):
        errs.append('POSCAR 文件不存在或未选择')
    if not incar or not os.path.isfile(incar):
        errs.append('INCAR 文件不存在或未选择')
    if not out_dir:
        errs.append('未设置输出目录')
    return errs


def profile_from_form(name: str, fields: dict) -> ClusterProfile:
    """把界面字段(字符串为主)映射成 ClusterProfile。port 容错为 int。"""
    def _int(v, default):
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return default
    return ClusterProfile(
        name=name,
        hostname=(fields.get('hostname') or '').strip(),
        port=_int(fields.get('port'), 22),
        username=(fields.get('username') or '').strip(),
        auth=fields.get('auth') or 'key',
        key_path=(fields.get('key_path') or '').strip(),
        use_jump=bool(fields.get('use_jump')),
        jump_host=(fields.get('jump_host') or '').strip(),
        jump_user=(fields.get('jump_user') or '').strip(),
        jump_port=_int(fields.get('jump_port'), 22),
        remote_root=(fields.get('remote_root') or '').strip(),
        scheduler=fields.get('scheduler') or 'Slurm',
    )


def validate_cluster_inputs(profile: ClusterProfile, has_password: bool) -> list:
    """集群配置预检(测连接/保存前)。has_password:是否已有可用密码。"""
    errs = []
    if not profile.name:
        errs.append('未填集群名称')
    if not profile.hostname:
        errs.append('未填主机名')
    if not profile.username:
        errs.append('未填用户名')
    if profile.auth == 'key':
        # 只校验"是否填了密钥路径";路径存在性交由实际连接时报错
        # (与测试契约一致:key_path 非空即视为已选择)。
        if not profile.key_path:
            errs.append('SSH 密钥文件未选择')
    else:
        if not has_password:
            errs.append('密码认证但未提供密码')
    if profile.use_jump and not profile.jump_host:
        errs.append('勾选了跳板机但未填跳板主机')
    return errs
```

- [ ] **Step 4: 运行,确认通过**

Run: `python -m pytest tests/test_gui_logic.py -v`
Expected: 7 passed。

- [ ] **Step 5: Commit**

```bash
git add vcstudio/gui/logic.py tests/test_gui_logic.py
git commit -m "feat(gui): 纯逻辑 helpers(kpoints 解析/输入校验/表单映射),脱离 Tk 可测"
```

---

### Task 9: `gui/widgets.py` + `gui/generate_tab.py` — 生成页

**Files:**
- Create: `vcstudio/gui/widgets.py`
- Create: `vcstudio/gui/generate_tab.py`

**Interfaces:**
- Consumes: `runner.submit/poll`、`logic.parse_kpoints_field/validate_generate_inputs`、`config.load_config/set_potcar_lib_root`、`build_job_dir`。
- Produces:
  - `widgets.FileRow(parent, label, mode)`(`mode ∈ {'openfile','dir'}`;`.get()/.set()`)
  - `widgets.LogBox(parent)`(`.write(text)`、`.clear()`)
  - `generate_tab.GenerateTab(ttk.Frame)`

- [ ] **Step 1: 创建 `vcstudio/gui/widgets.py`**

```python
"""复用 Tk 小组件:文件/目录选择行、只读日志框。中文注释允许,英文标识符。"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, filedialog


class FileRow(ttk.Frame):
    """一行:标签 + 输入框 + [浏览…]。mode='openfile' 选文件,'dir' 选目录。"""

    def __init__(self, parent, label: str, mode: str = 'openfile', width: int = 48):
        super().__init__(parent)
        self.mode = mode
        self.var = tk.StringVar()
        ttk.Label(self, text=label, width=14, anchor='e').grid(row=0, column=0, padx=4, pady=3)
        ttk.Entry(self, textvariable=self.var, width=width).grid(row=0, column=1, padx=4)
        ttk.Button(self, text='浏览…', command=self._browse).grid(row=0, column=2, padx=4)

    def _browse(self):
        if self.mode == 'dir':
            path = filedialog.askdirectory()
        else:
            path = filedialog.askopenfilename()
        if path:
            self.var.set(path)

    def get(self) -> str:
        return self.var.get()

    def set(self, value: str):
        self.var.set(value)


class LogBox(ttk.Frame):
    """只读多行日志框(带滚动条)。"""

    def __init__(self, parent, height: int = 12):
        super().__init__(parent)
        self.text = tk.Text(self, height=height, wrap='word', state='disabled')
        scroll = ttk.Scrollbar(self, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.grid(row=0, column=0, sticky='nsew')
        scroll.grid(row=0, column=1, sticky='ns')
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

    def write(self, msg: str):
        self.text.configure(state='normal')
        self.text.insert('end', msg + '\n')
        self.text.see('end')
        self.text.configure(state='disabled')

    def clear(self):
        self.text.configure(state='normal')
        self.text.delete('1.0', 'end')
        self.text.configure(state='disabled')
```

- [ ] **Step 2: 创建 `vcstudio/gui/generate_tab.py`**

```python
"""「生成」页:赝势库(持久化)+ POSCAR/INCAR/类型/KPOINTS/输出 → 一键生成 + 打开目录。

生成一律复用 build_job_dir(后台线程),错误转友好文案,绝不弹 traceback。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import sys

import tkinter as tk
from tkinter import ttk

from vcstudio.gui.widgets import FileRow, LogBox
from vcstudio.gui import runner
from vcstudio.gui.logic import parse_kpoints_field, validate_generate_inputs
from vcstudio.shared.config import load_config, set_potcar_lib_root
from vcstudio.generate.job_builder import build_job_dir
from vcstudio.generate.potcar import PotcarError


class GenerateTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=10)
        self._out_dir = ''
        self._build()
        self._load_lib()

    def _build(self):
        # 赝势库(全局,持久化)
        self.lib_row = FileRow(self, '赝势库 POTCAR', mode='dir')
        self.lib_row.grid(row=0, column=0, sticky='w')
        self.lib_row.var.trace_add('write', lambda *a: None)  # 变更即时可读

        ttk.Separator(self, orient='horizontal').grid(row=1, column=0, sticky='ew', pady=6)

        self.poscar_row = FileRow(self, 'POSCAR 结构')
        self.poscar_row.grid(row=2, column=0, sticky='w')
        self.incar_row = FileRow(self, 'INCAR 参数')
        self.incar_row.grid(row=3, column=0, sticky='w')

        opt = ttk.Frame(self)
        opt.grid(row=4, column=0, sticky='w', pady=3)
        ttk.Label(opt, text='计算类型', width=14, anchor='e').grid(row=0, column=0, padx=4)
        self.calc_var = tk.StringVar(value='slab')
        for i, t in enumerate(('molecule', 'slab', 'bulk')):
            ttk.Radiobutton(opt, text=t, value=t, variable=self.calc_var).grid(row=0, column=1 + i, padx=2)
        ttk.Label(opt, text='KPOINTS').grid(row=0, column=4, padx=(16, 2))
        self.kpts_var = tk.StringVar(value='自动')
        ttk.Entry(opt, textvariable=self.kpts_var, width=10).grid(row=0, column=5)

        self.out_row = FileRow(self, '输出目录', mode='dir')
        self.out_row.grid(row=5, column=0, sticky='w')

        self.validate_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self, text='INCAR 校验补全(缺 ENCUT/MAGMOM 自动补)',
                        variable=self.validate_var).grid(row=6, column=0, sticky='w', padx=18, pady=3)

        btns = ttk.Frame(self)
        btns.grid(row=7, column=0, pady=6)
        self.run_btn = ttk.Button(btns, text='▶ 一键生成', command=self._on_run)
        self.run_btn.grid(row=0, column=0, padx=6)
        self.open_btn = ttk.Button(btns, text='📂 打开输出文件夹', command=self._open_out, state='disabled')
        self.open_btn.grid(row=0, column=1, padx=6)

        ttk.Label(self, text='运行日志').grid(row=8, column=0, sticky='w')
        self.log = LogBox(self)
        self.log.grid(row=9, column=0, sticky='nsew', pady=3)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(9, weight=1)

    def _load_lib(self):
        try:
            self.lib_row.set(load_config().get('potcar_lib_root', ''))
        except Exception:
            pass

    def _persist_lib(self):
        lib = self.lib_row.get().strip()
        if lib:
            try:
                set_potcar_lib_root(lib)
            except Exception as e:
                self.log.write(f'⚠ 赝势库路径保存失败:{e}')

    def _on_run(self):
        self.log.clear()
        self._persist_lib()
        lib = self.lib_row.get().strip()
        poscar, incar, out = self.poscar_row.get().strip(), self.incar_row.get().strip(), self.out_row.get().strip()
        errs = validate_generate_inputs(poscar, incar, out, lib)
        if errs:
            for e in errs:
                self.log.write(f'❌ {e}')
            return
        try:
            kpts = parse_kpoints_field(self.kpts_var.get())
        except ValueError as e:
            self.log.write(f'❌ {e}')
            return

        self._out_dir = out
        self.run_btn.configure(state='disabled')
        self.log.write('⏳ 生成中…')
        q = runner.submit(build_job_dir, poscar, incar, out,
                          calc_type=self.calc_var.get(), kpoints=kpts,
                          validate=self.validate_var.get(), lib_root=lib)
        self.after(100, lambda: self._poll(q))

    def _poll(self, q):
        item = runner.poll(q)
        if item is None:
            self.after(100, lambda: self._poll(q))
            return
        kind, payload = item
        self.run_btn.configure(state='normal')
        if kind == 'ok':
            for w in payload['warnings']:
                self.log.write(f'⚠ {w}')
            self.log.write(f"✅ 已生成:{payload['out_dir']}")
            self.log.write(f"元素:{payload['elements']}   KPOINTS:{payload['kpoints']}")
            self.open_btn.configure(state='normal')
        else:
            self.log.write(f'❌ 错误:{payload}')  # ValueError/PotcarError/OSError 的中文文案

    def _open_out(self):
        if self._out_dir and os.path.isdir(self._out_dir):
            try:
                if sys.platform == 'win32':
                    os.startfile(self._out_dir)  # noqa
                else:
                    import subprocess
                    subprocess.Popen(['xdg-open', self._out_dir])
            except Exception as e:
                self.log.write(f'⚠ 无法打开目录:{e}')
```

- [ ] **Step 3: 冒烟自检(import 不报错)**

Run: `python -c "import vcstudio.gui.widgets, vcstudio.gui.generate_tab; print('import ok')"`
Expected: 打印 `import ok`(无 Tk 显示需求,仅导入)。

- [ ] **Step 4: 全量测试仍绿**

Run: `python -m pytest -q`
Expected: 之前所有测试 passed(本任务不新增测试,GUI 组装留待 Task 13 人工验收)。

- [ ] **Step 5: Commit**

```bash
git add vcstudio/gui/widgets.py vcstudio/gui/generate_tab.py
git commit -m "feat(gui): 生成页 — 赝势库持久化 + 一键生成(复用 build_job_dir)+ 打开输出目录"
```

---

### Task 10: `gui/cluster_tab.py` — 集群页(连接 + 测连接)

**Files:**
- Create: `vcstudio/gui/cluster_tab.py`

**Interfaces:**
- Consumes: `profiles.load_profiles/save_profiles/ClusterProfile`、`secrets`、`ssh_test.check_connection`、`runner`、`logic.profile_from_form/validate_cluster_inputs`、`widgets`。
- Produces: `cluster_tab.ClusterTab(ttk.Frame)`。

- [ ] **Step 1: 创建 `vcstudio/gui/cluster_tab.py`**

```python
"""「集群」页(M2 前置):多 profile 连接配置 + 测连接。提交按钮置灰待 M2。

密码绝不落 yaml:保存时经 keyring 存,keyring 不可用则弹框现输(仅内存)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog

from vcstudio.gui.widgets import LogBox
from vcstudio.gui import runner
from vcstudio.gui.logic import profile_from_form, validate_cluster_inputs
from vcstudio.cluster.profiles import load_profiles, save_profiles, ClusterProfile
from vcstudio.cluster import ssh_test
from vcstudio.shared import secrets


class ClusterTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=10)
        self.profiles = {}
        self.vars = {}
        self._build()
        self._reload_profiles()

    def _row(self, r, label, key, width=36):
        ttk.Label(self, text=label, width=14, anchor='e').grid(row=r, column=0, padx=4, pady=2)
        v = tk.StringVar()
        self.vars[key] = v
        ttk.Entry(self, textvariable=v, width=width).grid(row=r, column=1, columnspan=2, sticky='w', padx=4)

    def _build(self):
        top = ttk.Frame(self)
        top.grid(row=0, column=0, columnspan=3, sticky='w', pady=3)
        ttk.Label(top, text='集群配置', width=14, anchor='e').grid(row=0, column=0, padx=4)
        self.profile_var = tk.StringVar()
        self.profile_cb = ttk.Combobox(top, textvariable=self.profile_var, width=24, state='readonly')
        self.profile_cb.grid(row=0, column=1, padx=4)
        self.profile_cb.bind('<<ComboboxSelected>>', lambda e: self._fill_from_selected())
        ttk.Button(top, text='新建', command=self._new).grid(row=0, column=2, padx=2)
        ttk.Button(top, text='删除', command=self._delete).grid(row=0, column=3, padx=2)

        self._row(1, '集群名称', 'name', width=24)
        self._row(2, '主机名', 'hostname')
        self._row(3, '端口', 'port', width=8)
        self._row(4, '用户名', 'username', width=24)

        auth = ttk.Frame(self)
        auth.grid(row=5, column=0, columnspan=3, sticky='w')
        ttk.Label(auth, text='认证', width=14, anchor='e').grid(row=0, column=0, padx=4)
        self.auth_var = tk.StringVar(value='key')
        ttk.Radiobutton(auth, text='SSH 密钥', value='key', variable=self.auth_var,
                        command=self._sync_auth).grid(row=0, column=1)
        ttk.Radiobutton(auth, text='密码', value='password', variable=self.auth_var,
                        command=self._sync_auth).grid(row=0, column=2)

        keyf = ttk.Frame(self)
        keyf.grid(row=6, column=0, columnspan=3, sticky='w')
        ttk.Label(keyf, text='密钥文件', width=14, anchor='e').grid(row=0, column=0, padx=4)
        self.vars['key_path'] = tk.StringVar()
        ttk.Entry(keyf, textvariable=self.vars['key_path'], width=36).grid(row=0, column=1, padx=4)
        ttk.Button(keyf, text='浏览…', command=self._pick_key).grid(row=0, column=2)

        self._row(7, '远程工作目录', 'remote_root')

        sch = ttk.Frame(self)
        sch.grid(row=8, column=0, columnspan=3, sticky='w', pady=2)
        ttk.Label(sch, text='调度器', width=14, anchor='e').grid(row=0, column=0, padx=4)
        self.sched_var = tk.StringVar(value='Slurm')
        ttk.Combobox(sch, textvariable=self.sched_var, width=12, state='readonly',
                     values=['Slurm', 'PBS', 'LSF', 'Shell']).grid(row=0, column=1, padx=4)

        # 跳板机(可选)
        self.jump_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(self, text='经跳板机', variable=self.jump_var).grid(row=9, column=0, sticky='e', padx=4)
        jf = ttk.Frame(self)
        jf.grid(row=9, column=1, columnspan=2, sticky='w')
        ttk.Label(jf, text='主机').grid(row=0, column=0)
        self.vars['jump_host'] = tk.StringVar()
        ttk.Entry(jf, textvariable=self.vars['jump_host'], width=16).grid(row=0, column=1, padx=2)
        ttk.Label(jf, text='用户').grid(row=0, column=2)
        self.vars['jump_user'] = tk.StringVar()
        ttk.Entry(jf, textvariable=self.vars['jump_user'], width=10).grid(row=0, column=3, padx=2)
        ttk.Label(jf, text='端口').grid(row=0, column=4)
        self.vars['jump_port'] = tk.StringVar(value='22')
        ttk.Entry(jf, textvariable=self.vars['jump_port'], width=6).grid(row=0, column=5, padx=2)

        btns = ttk.Frame(self)
        btns.grid(row=10, column=0, columnspan=3, pady=6)
        ttk.Button(btns, text='💾 保存', command=self._save).grid(row=0, column=0, padx=4)
        self.test_btn = ttk.Button(btns, text='🔌 测试连接', command=self._on_test)
        self.test_btn.grid(row=0, column=1, padx=4)
        ttk.Button(btns, text='📤 提交任务 (M2上线)', state='disabled').grid(row=0, column=2, padx=4)

        ttk.Label(self, text='连接日志').grid(row=11, column=0, sticky='w')
        self.log = LogBox(self, height=6)
        self.log.grid(row=12, column=0, columnspan=3, sticky='nsew', pady=3)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(12, weight=1)
        self.vars['port'].set('22')

    # ---- profile 下拉 ----
    def _reload_profiles(self):
        self.profiles = load_profiles()
        self.profile_cb['values'] = list(self.profiles)
        if self.profiles:
            first = next(iter(self.profiles))
            self.profile_var.set(first)
            self._fill_from_selected()

    def _fill_from_selected(self):
        p = self.profiles.get(self.profile_var.get())
        if not p:
            return
        self.vars['name'].set(p.name)
        self.vars['hostname'].set(p.hostname)
        self.vars['port'].set(str(p.port))
        self.vars['username'].set(p.username)
        self.auth_var.set(p.auth)
        self.vars['key_path'].set(p.key_path)
        self.vars['remote_root'].set(p.remote_root)
        self.sched_var.set(p.scheduler)
        self.jump_var.set(p.use_jump)
        self.vars['jump_host'].set(p.jump_host)
        self.vars['jump_user'].set(p.jump_user)
        self.vars['jump_port'].set(str(p.jump_port))

    def _new(self):
        for k in ('name', 'hostname', 'username', 'key_path', 'remote_root',
                  'jump_host', 'jump_user'):
            self.vars[k].set('')
        self.vars['port'].set('22')
        self.vars['jump_port'].set('22')
        self.auth_var.set('key')
        self.sched_var.set('Slurm')
        self.jump_var.set(False)
        self.profile_var.set('')

    def _delete(self):
        name = self.profile_var.get()
        if name and name in self.profiles and messagebox.askyesno('删除', f'删除集群「{name}」?'):
            self.profiles.pop(name)
            secrets.delete_password(name)
            save_profiles(self.profiles)
            self._new()
            self.profile_cb['values'] = list(self.profiles)

    def _pick_key(self):
        path = filedialog.askopenfilename(title='选择 SSH 私钥')
        if path:
            self.vars['key_path'].set(path)

    def _sync_auth(self):
        pass  # 占位:如需按认证方式禁用控件,可在此切换 state

    def _current_profile(self) -> ClusterProfile:
        fields = {k: v.get() for k, v in self.vars.items()}
        fields['auth'] = self.auth_var.get()
        fields['scheduler'] = self.sched_var.get()
        fields['use_jump'] = self.jump_var.get()
        return profile_from_form(self.vars['name'].get().strip(), fields)

    def _get_password(self, prof, prompt_if_missing: bool) -> str | None:
        pw = secrets.get_password(prof.name)
        if pw is None and prompt_if_missing:
            pw = simpledialog.askstring('密码', f'输入 {prof.username}@{prof.hostname} 的密码:', show='*')
        return pw

    def _save(self):
        prof = self._current_profile()
        # 保存阶段不强制已有密码(可后填):校验非密码字段,密码类错误一律滤掉
        errs = [e for e in validate_cluster_inputs(prof, has_password=True) if '密码' not in e]
        if errs:
            self.log.write('❌ ' + '；'.join(errs))
            return
        # 密码认证:弹框收一次密码存 keyring(绝不写 yaml)
        if prof.auth == 'password':
            pw = simpledialog.askstring('密码', f'输入 {prof.username}@{prof.hostname} 的密码(存系统凭据库):', show='*')
            if pw:
                if not secrets.set_password(prof.name, pw):
                    self.log.write('⚠ 系统凭据库不可用,密码未保存,测连接时会现输')
        self.profiles[prof.name] = prof
        save_profiles(self.profiles)
        self.profile_cb['values'] = list(self.profiles)
        self.profile_var.set(prof.name)
        self.log.write(f'✅ 已保存集群「{prof.name}」(密码不写入 yaml)')

    def _on_test(self, trust_new=False):
        prof = self._current_profile()
        pw = self._get_password(prof, prompt_if_missing=(prof.auth == 'password')) if prof.auth == 'password' else None
        errs = validate_cluster_inputs(prof, has_password=(pw is not None))
        if errs:
            self.log.write('❌ ' + '；'.join(errs))
            return
        self.test_btn.configure(state='disabled')
        self.log.write('⏳ 连接中…')
        q = runner.submit(ssh_test.check_connection, prof, pw, trust_new=trust_new)
        self.after(150, lambda: self._poll_test(q))

    def _poll_test(self, q):
        item = runner.poll(q)
        if item is None:
            self.after(150, lambda: self._poll_test(q))
            return
        kind, payload = item
        self.test_btn.configure(state='normal')
        if kind == 'error':
            self.log.write(f'❌ 连接异常:{payload}')
            return
        res = payload
        if res.ok:
            self.log.write(f'✅ {res.message}')
        elif res.needs_trust:
            if messagebox.askyesno('未知主机', f'{res.message}\n\n是否信任该主机并重试?'):
                self._on_test(trust_new=True)
            else:
                self.log.write('已取消(未信任主机)')
        else:
            self.log.write(f'❌ {res.message}')
```

- [ ] **Step 2: 冒烟自检(import 不报错)**

Run: `python -c "import vcstudio.gui.cluster_tab; print('import ok')"`
Expected: 打印 `import ok`。

- [ ] **Step 3: 全量测试仍绿**

Run: `python -m pytest -q`
Expected: 全 passed(本任务不新增自动化测试)。

- [ ] **Step 4: Commit**

```bash
git add vcstudio/gui/cluster_tab.py
git commit -m "feat(gui): 集群页 — 多 profile 连接配置 + 测连接;密码走 keyring;提交按钮待 M2"
```

---

### Task 11: `gui/app.py` + 入口(Notebook 两页 / `python -m` / `vcs gui`)

**Files:**
- Create: `vcstudio/gui/app.py`
- Create: `vcstudio/gui/__main__.py`
- Modify: `vcstudio/cli/main.py`

**Interfaces:**
- Consumes: `GenerateTab`、`ClusterTab`。
- Produces: `app.main(argv=None) -> int`;`python -m vcstudio.gui`;`vcs gui` 子命令。

- [ ] **Step 1: 创建 `vcstudio/gui/app.py`**

```python
"""桌面 GUI 入口:单窗口 + 两页(生成 / 集群)。中文注释允许,英文标识符。"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from vcstudio.gui.generate_tab import GenerateTab
from vcstudio.gui.cluster_tab import ClusterTab


def main(argv=None) -> int:
    root = tk.Tk()
    root.title('VASP Catalyst Studio — 输入生成器')
    root.geometry('720x640')
    nb = ttk.Notebook(root)
    nb.add(GenerateTab(nb), text='生成')
    nb.add(ClusterTab(nb), text='集群')
    nb.pack(fill='both', expand=True)
    root.mainloop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
```

- [ ] **Step 2: 创建 `vcstudio/gui/__main__.py`**

```python
"""支持 `python -m vcstudio.gui`。"""
from vcstudio.gui.app import main

if __name__ == '__main__':
    raise SystemExit(main())
```

- [ ] **Step 3: 给 `cli/main.py` 加 `gui` 子命令**

在 `build_parser()` 里 `g.set_defaults(func=cmd_gen)` 之后、`return parser` 之前插入:

```python
    gui_p = sub.add_parser('gui', help='打开图形界面(生成 + 集群配置)')
    gui_p.set_defaults(func=cmd_gui)
```

在 `cmd_gen(...)` 函数之后新增:

```python
def cmd_gui(args) -> int:
    from vcstudio.gui.app import main as gui_main
    return gui_main()
```

- [ ] **Step 4: 冒烟自检 + 全量测试**

Run: `python -c "import vcstudio.gui.app; print('app import ok')" && python -m pytest -q`
Expected: 打印 `app import ok`;测试全 passed。

- [ ] **Step 5: 人工目视(有显示环境时)**

Run: `python -m vcstudio.gui`
Expected: 弹出窗口,顶部有「生成」「集群」两个标签页,可切换;关闭窗口退出。
(无显示环境则跳过,记为待 Task 13 验收。)

- [ ] **Step 6: Commit**

```bash
git add vcstudio/gui/app.py vcstudio/gui/__main__.py vcstudio/cli/main.py
git commit -m "feat(gui): app 入口 — Notebook 两页;python -m vcstudio.gui 与 vcs gui 子命令"
```

---

### Task 12: 打包(PyInstaller spec + 构建脚本)+ gitignore + 删残留

**Files:**
- Create: `packaging/vcstudio.spec`
- Create: `packaging/build_exe.py`
- Modify: `.gitignore`
- Delete(磁盘): `_internal/`(312MB 残留,未跟踪)

**Interfaces:**
- Produces: `dist/VASP Catalyst Studio.exe`(单文件)。

- [ ] **Step 1: 创建 `packaging/vcstudio.spec`**

```python
# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 配方:单文件、无控制台窗口。排除无关重包以瘦身。
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules('keyring.backends')

a = Analysis(
    ['../vcstudio/gui/__main__.py'],
    pathex=['..'],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports + ['paramiko'],
    excludes=['numpy', 'sklearn', 'scipy', 'PIL', 'matplotlib',
              'pandas', 'pytest', 'tkinter.test'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='VASP Catalyst Studio',
    console=False,          # --windowed
    onefile=True,
    disable_windowed_traceback=False,
)
```

- [ ] **Step 2: 创建 `packaging/build_exe.py`**

```python
"""一键打包:调 PyInstaller 用 vcstudio.spec 出单文件 EXE 到 dist/。

用法:python packaging/build_exe.py
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = os.path.join(HERE, 'vcstudio.spec')


def main() -> int:
    cmd = [sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm', SPEC]
    print('运行:', ' '.join(cmd))
    return subprocess.call(cmd, cwd=HERE)


if __name__ == '__main__':
    raise SystemExit(main())
```

- [ ] **Step 3: 更新 `.gitignore`(在文件末尾追加)**

```gitignore
# 打包产物 / 残留
_internal/
/build/
/dist/
*.spec.bak

# 用户本地敏感/机器相关配置(绝不入 git)
clusters.yaml
known_hosts
/config.local.yaml
```

- [ ] **Step 4: 删掉 312MB 残留 `_internal/`**

Run: `rm -rf _internal`
Expected: 目录消失(它是未跟踪的旧 PyInstaller 产物,已被上一步 gitignore 覆盖)。
> ⚠️ 执行前口头向用户确认一次(见 Global Constraints:不静默删除)。

- [ ] **Step 5: 打包出 EXE**

Run: `python packaging/build_exe.py`
Expected: 结束后存在 `packaging/dist/VASP Catalyst Studio.exe`(onefile 模式产物在 spec 所在目录的 `dist/`)。

- [ ] **Step 6: Commit(spec + 脚本 + gitignore;dist/ 不入 git)**

```bash
git add packaging/vcstudio.spec packaging/build_exe.py .gitignore
git commit -m "build(gui): PyInstaller 单文件配方 + 构建脚本;gitignore 残留/产物/敏感配置"
```

---

### Task 13: 最终验收(真打 EXE,跑生成 5 例 + 测连接)

**Files:** 无(人工验收 + 记录)。

REQUIRED SUB-SKILL 提示:本任务用 `superpowers:verification-before-completion` 心态执行——亲自运行、观察行为,不臆测。

- [ ] **Step 1: 确认残留已清、产物已出**

Run: `ls "packaging/dist/" && test ! -d _internal && echo "_internal 已删"`
Expected: 列出 `VASP Catalyst Studio.exe`;打印 `_internal 已删`。

- [ ] **Step 2: 双击 EXE,验「生成」页(用冒烟测试那套真实输入)**

手动:双击 `packaging/dist/VASP Catalyst Studio.exe`。
- 赝势库设为 `E:/V2.0.0/results/inputs/potpaw54/potpaw54/potpaw54/potpaw_PBE/paw_pbe`;关掉再开,确认被记住。
- 3 个正向:①molecule + `molecule_Li2S.vasp`+`mol_Li2S/INCAR`;②bulk + `Fe.vasp`+极简 INCAR;③slab + `Pt_surface_001_ads_top.vasp`。各点[一键生成]→日志出「已生成/元素/KPOINTS/警告」,输出目录含 INCAR/POTCAR/KPOINTS/POSCAR 四件套;点[打开输出文件夹]能在资源管理器打开。
- 2 个负向:①INCAR 里 ENCUT=100 → 日志红字「ENMAX 超过 ENCUT」;②含 H/O 的 POSCAR → 「元素未登记」。界面不崩、不弹 traceback。
Expected: 全部符合;与 CLI 冒烟测试结果一致。

- [ ] **Step 3: 验「集群」页**

手动:
- [新建]→填名称/主机/用户名/密钥或密码/远程目录/调度器→[保存];打开 `%APPDATA%/vcstudio/clusters.yaml` 确认**无 password 字段**。
- 多建一个 profile,下拉切换字段随之变化。
- [测试连接]:对一台真集群(或本地 sshd)连,日志出 whoami + 调度器;首次未知主机弹「是否信任」→确认后连上。
- [提交任务] 为灰、标注 M2。
Expected: 符合;密码不落 yaml。

- [ ] **Step 4: 全量自动化测试收尾**

Run: `python -m pytest -q`
Expected: 全 passed。

- [ ] **Step 5: 记录验收结论(可选提交一条 docs)**

在 PR/提交说明里写明:5 例生成一致、集群密码不落 yaml、EXE 单文件双击可用、`_internal/` 已清。

---

## Self-Review(计划自查结论)

- **Spec 覆盖**:两页界面(生成 Task 9 / 集群 Task 10-11)、复用 build_job_dir(Task 9)、config 持久化+冻结路径(Task 3)、密钥+密码且密码走 keyring 绝不明文(Task 4/10)、密钥只存路径(Task 5 模型无密码字段)、测连接+未知指纹确认(Task 6)、去 numpy(Task 2)、paramiko/keyring 依赖(Task 1)、集群页本版只做连接、提交置灰、资源参数推迟(Task 10)、打包单文件+删 _internal+gitignore(Task 12)、错误友好不弹 traceback(Task 9/10 的 poll 分支)、验收矩阵(Task 13)。全部有对应任务。
- **占位符**:无 TBD/TODO;每个代码步给了完整实现。
- **类型一致**:`build_job_dir` 返回键(warnings/out_dir/elements/kpoints)在 Task 9 一致使用;`ClusterProfile` 字段在 profiles/logic/ssh_test/cluster_tab 一致;`ConnectionResult`(ok/whoami/scheduler/message/needs_trust)在 ssh_test 与 cluster_tab 一致;`submit/poll` 在 runner 与两个 tab 一致。
