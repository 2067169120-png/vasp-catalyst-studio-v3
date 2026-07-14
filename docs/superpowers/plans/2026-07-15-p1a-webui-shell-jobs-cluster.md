# P1a — PyWebView 壳 + 任务页 + 集群页 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把桌面 GUI 的壳、任务页、集群页迁移到 PyWebView + HTML/CSS(B 版控制台风),旧 tkinter GUI 保持可用,新 GUI 以 `python -m vcstudio.gui_web` 独立入口运行。

**Architecture:** 三层:① `vcstudio/cluster/batch_ops.py` —— 把现在住在 `gui/jobs_tab.py` 底部的 5 个线程体函数(不碰 Tk 的纯编排)搬成 UI 无关模块,tkinter 与 web 两个 GUI 共用;② `vcstudio/gui_web/api.py` —— pywebview `js_api` 薄处理器(每个 JS 调用天然跑在独立线程,可直接同步调 batch_ops);③ `vcstudio/gui_web/assets/` —— 单页 HTML/CSS/JS,视觉规格照抄已定稿 mockup(`docs/superpowers/mockups/mockup_b_console.html`)。

**Tech Stack:** pywebview(已装)、原生 HTML/CSS/JS(零 npm)、pytest。

## Global Constraints(来自 spec,全任务生效)

- 中文注释允许,英文标识符;与用户对话中文,commit message 英文。
- UI 文案零 emoji;状态一律小圆点 pill;数据(作业号/能量/路径)等宽字体。
- 色板/字体栈/间距逐字取自 `docs/superpowers/mockups/mockup_b_console.html` 的 `:root` 变量,不新造颜色。
- 逻辑层(vcstudio/cluster、shared、project)只做"搬家不改行为";行为变更(如 DONE 自动拉回)属 P2,本计划禁止夹带。
- 所有资源离线(无 CDN);资源路径必须兼容 PyInstaller `sys._MEIPASS`。
- 每个 commit 必须带 pathspec(仓库有他人 WIP)。
- TDD:Python 部分先写失败测试;前端部分以浏览器人工验收清单代替单测。

---

### Task 1: batch_ops —— 线程体搬家(UI 无关化)

**Files:**
- Create: `vcstudio/cluster/batch_ops.py`
- Modify: `vcstudio/gui/jobs_tab.py`(底部 `_job_errors/_submit_batch/_fetch_batch/_filter_continuable/_continue_batch/_queue_detail/_tune_batch/_refresh_batch` 全部删除,改 `from vcstudio.cluster import batch_ops` 并把调用点 `_submit_batch` → `batch_ops.submit_batch` 等)
- Test: `tests/test_batch_ops.py`(新)

**Interfaces:**
- Consumes: `connection.open_client/close_quiet/ConnectError`、`submitter.*`、`manifest_mod.load_manifest`
- Produces(后续任务与 jobs_tab 都依赖,签名与现私有函数一致,仅去下划线):
  - `submit_batch(prof, pw, dirs, trust_new) -> dict`(键:needs_trust/message/results)
  - `fetch_batch(prof, pw, dirs, trust_new, files=submitter.FETCH_FILES) -> dict`
  - `continue_batch(prof, pw, dirs, trust_new) -> dict`
  - `refresh_batch(prof, pw, dirs, trust_new) -> dict`
  - `queue_detail(prof, pw, trust_new) -> dict`(键:needs_trust/message/jobs)
  - `tune_batch(prof, pw, job_dir, changes, trust_new, from_contcar=True) -> dict`
  - `filter_continuable(dirs) -> tuple[list, int]`

- [ ] **Step 1: 写失败测试**(先只测纯函数与模块形状,连接类函数由现有 tests/test_jobs_batch.py 迁移覆盖)

```python
# tests/test_batch_ops.py
"""batch_ops 是 UI 无关线程体:两个 GUI 共用,绝不 import tkinter。"""
import sys

from vcstudio.cluster import batch_ops


def test_no_tkinter_dependency():
    assert 'tkinter' not in sys.modules or True  # 见下:检查模块源
    import inspect
    src = inspect.getsource(batch_ops)
    assert 'tkinter' not in src


def test_filter_continuable_empty():
    eligible, skipped = batch_ops.filter_continuable([])
    assert eligible == [] and skipped == 0


def test_public_surface():
    for name in ('submit_batch', 'fetch_batch', 'continue_batch',
                 'refresh_batch', 'queue_detail', 'tune_batch'):
        assert callable(getattr(batch_ops, name))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_batch_ops.py -q`
Expected: FAIL(ModuleNotFoundError: vcstudio.cluster.batch_ops)

- [ ] **Step 3: 创建 batch_ops.py** —— 把 `vcstudio/gui/jobs_tab.py` 718 行以后的模块级函数**原样剪切**过来(含中文 docstring),仅两处改动:①函数名去前导下划线;②文件头 docstring:

```python
"""批量远程操作线程体(UI 无关):连接→逐作业操作→结果列表。

从 gui/jobs_tab.py 搬出,tkinter 与 gui_web 两个 GUI 共用。
本模块绝不 import tkinter/webview;paramiko 延迟导入。
返回统一 {'needs_trust': bool, 'message': str, 'results'|'jobs': list}。
中文注释允许,英文标识符。
"""
```

- [ ] **Step 4: jobs_tab.py 改为委托** —— 删除搬走的函数,顶部加 `from vcstudio.cluster import batch_ops`,调用点替换(`runner.submit(_submit_batch, ...)` → `runner.submit(batch_ops.submit_batch, ...)`,共 6 处;`_filter_continuable` → `batch_ops.filter_continuable` 1 处)。

- [ ] **Step 5: 全量测试**

Run: `python -m pytest tests/ -q`
Expected: 全过(tests/test_jobs_batch.py 原来 import jobs_tab 私有函数的用例,改 import batch_ops 同名公有函数——属于本步)

- [ ] **Step 6: Commit**

```bash
git add vcstudio/cluster/batch_ops.py vcstudio/gui/jobs_tab.py tests/test_batch_ops.py tests/test_jobs_batch.py
git commit -m "refactor(cluster): extract UI-agnostic batch_ops from jobs_tab thread bodies" -- vcstudio/cluster/batch_ops.py vcstudio/gui/jobs_tab.py tests/test_batch_ops.py tests/test_jobs_batch.py
```

---

### Task 2: 资源定位 + 窗口引导

**Files:**
- Create: `vcstudio/gui_web/__init__.py`(空)、`vcstudio/gui_web/resources.py`、`vcstudio/gui_web/__main__.py`
- Test: `tests/test_web_resources.py`

**Interfaces:**
- Produces: `resources.asset_dir() -> Path`(开发态 = 包内 assets/;打包态 = `sys._MEIPASS/vcstudio_assets`)、`resources.index_html() -> str`(绝对路径)

- [ ] **Step 1: 失败测试**

```python
# tests/test_web_resources.py
import sys
from pathlib import Path

from vcstudio.gui_web import resources


def test_asset_dir_dev_mode():
    d = resources.asset_dir()
    assert d.name == 'assets' and d.is_dir()


def test_asset_dir_frozen(monkeypatch, tmp_path):
    (tmp_path / 'vcstudio_assets').mkdir()
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    assert resources.asset_dir() == tmp_path / 'vcstudio_assets'


def test_index_html_points_into_asset_dir():
    p = Path(resources.index_html())
    assert p.name == 'index.html' and p.parent == resources.asset_dir()
```

- [ ] **Step 2: 确认失败** — `python -m pytest tests/test_web_resources.py -q` → ModuleNotFoundError

- [ ] **Step 3: 实现**

```python
# vcstudio/gui_web/resources.py
"""资源定位:开发态用包内 assets/,PyInstaller 单文件态用 _MEIPASS/vcstudio_assets。"""
from __future__ import annotations

import sys
from pathlib import Path


def asset_dir() -> Path:
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return Path(sys._MEIPASS) / 'vcstudio_assets'
    return Path(__file__).parent / 'assets'


def index_html() -> str:
    return str(asset_dir() / 'index.html')
```

```python
# vcstudio/gui_web/__main__.py
"""Web GUI 入口:python -m vcstudio.gui_web。"""
from __future__ import annotations

import webview

from vcstudio.gui_web.api import Api
from vcstudio.gui_web import resources


def main() -> int:
    api = Api()
    webview.create_window(
        'VASP Catalyst Studio', resources.index_html(), js_api=api,
        width=1180, height=800, min_size=(960, 640))
    webview.start()  # 默认 EdgeChromium(WebView2)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
```

同步创建占位 `vcstudio/gui_web/assets/index.html`(内容 `<!doctype html><title>vcstudio</title>`,Task 4 覆写)。注:Api 类 Task 3 才有,本任务先在 `__main__.py` 顶部 `from vcstudio.gui_web.api import Api` 会 ImportError——所以本任务创建最小 `api.py`:

```python
# vcstudio/gui_web/api.py
"""pywebview js_api:薄处理器。每个 JS 调用由 pywebview 派独立线程执行,可同步阻塞。"""
from __future__ import annotations


class Api:
    def ping(self) -> str:
        return 'pong'
```

- [ ] **Step 4: 测试过** — `python -m pytest tests/test_web_resources.py -q` → PASS
- [ ] **Step 5: Commit**

```bash
git add vcstudio/gui_web/ tests/test_web_resources.py
git commit -m "feat(gui_web): pywebview bootstrap + frozen-aware asset resolution" -- vcstudio/gui_web tests/test_web_resources.py
```

---

### Task 3: Api —— 集群 + 任务处理器(全部可注入、pytest 直测)

**Files:**
- Modify: `vcstudio/gui_web/api.py`(覆写)
- Test: `tests/test_web_api.py`

**Interfaces:**
- Consumes: `batch_ops.*`(Task 1)、`profiles.load_profiles/save_profiles/ClusterProfile`、`secrets.get_password/set_password`、`ssh_test.check_connection`、`ledger.load_all/unregister`、`manifest_mod.load_manifest`、`submitter.adopt_external_job/preflight/build_script_text`
- Produces(JS 侧按此签名调用,全部返回 JSON-safe dict):
  - `list_profiles() -> {'profiles': [dict], 'error': str|None}`
  - `save_profile(data: dict) -> {'ok': bool, 'error': str|None}`
  - `delete_profile(name: str) -> {'ok': bool}`
  - `test_connection(name, password, trust_new) -> {'ok','message','scheduler','needs_trust'}`(password 非空且成功 → 顺手 set_password 存 keyring)
  - `has_saved_password(name) -> {'saved': bool}`
  - `preview_script(name, job_dir) -> {'ok','text'|'error'}`
  - `list_jobs() -> {'jobs': [row], 'stale': [dir]}`,row 键:`dir,name,state,task,cluster,job_id,energy,diag,updated,steps,fmax`(取数逻辑照抄 `jobs_tab.reload` 的 manifest 解析,含 live 步数/警告)
  - `submit_jobs(dirs, name, password, trust_new)` / `fetch_jobs(dirs, name, password, trust_new, files)` / `continue_jobs(dirs, name, password, trust_new)` / `refresh_status(name, password, trust_new)` / `queue_detail(name, password, trust_new)` —— 一律先 `_resolve(name, password)` 拿 (prof, pw),再委托 batch_ops 同名函数,原样透传返回 dict;refresh_status 的目标 dirs 逻辑照抄 `jobs_tab._on_refresh_status`(台账里该集群 SUBMITTED/QUEUED/RUNNING)
  - `adopt_job(local_dir, name, job_id, remote_dir, job_name)` → submitter.adopt_external_job 包一层 try
  - `remove_jobs(dirs)` / `clean_stale()` → ledger.unregister
  - `open_dir(path) -> {'ok': bool}`(os.startfile,仅 win32)

依赖注入:`Api.__init__(self, *, profiles_mod=None, secrets_mod=None, ssh_test_mod=None, batch_ops_mod=None, ledger_mod=None, manifest_mod=None, submitter_mod=None)`,None 走真模块——测试全部注入假件,不碰网络。

- [ ] **Step 1: 失败测试**(核心用例;每个方法至少一个 happy + 一个错误路径)

```python
# tests/test_web_api.py
"""Api 处理器纯逻辑测试:注入假模块,零网络零 keyring。"""
import types

from vcstudio.gui_web.api import Api


def _fake_profiles(store):
    m = types.SimpleNamespace()
    m.load_profiles = lambda: dict(store)
    m.save_profiles = lambda p: store.clear() or store.update(p)
    from vcstudio.cluster.profiles import ClusterProfile
    m.ClusterProfile = ClusterProfile
    return m


def test_list_profiles_roundtrip():
    from vcstudio.cluster.profiles import ClusterProfile
    store = {'c1': ClusterProfile(name='c1', hostname='h', scheduler='PBS')}
    api = Api(profiles_mod=_fake_profiles(store))
    out = api.list_profiles()
    assert out['profiles'][0]['name'] == 'c1'
    assert out['profiles'][0]['scheduler'] == 'PBS'


def test_save_profile_rejects_blank_name():
    api = Api(profiles_mod=_fake_profiles({}))
    out = api.save_profile({'name': '  '})
    assert out['ok'] is False and '名称' in out['error']


def test_test_connection_saves_password_on_success():
    saved = {}
    secrets = types.SimpleNamespace(
        get_password=lambda n: None,
        set_password=lambda n, pw: saved.update({n: pw}))
    ssh = types.SimpleNamespace(check_connection=lambda prof, password, trust_new=False:
        types.SimpleNamespace(ok=True, message='ok', scheduler='PBS', needs_trust=False))
    from vcstudio.cluster.profiles import ClusterProfile
    store = {'c1': ClusterProfile(name='c1', hostname='h', auth='password')}
    api = Api(profiles_mod=_fake_profiles(store), secrets_mod=secrets, ssh_test_mod=ssh)
    out = api.test_connection('c1', 'pw123', False)
    assert out['ok'] is True and saved == {'c1': 'pw123'}


def test_submit_jobs_delegates_to_batch_ops():
    calls = {}
    bo = types.SimpleNamespace(submit_batch=lambda prof, pw, dirs, tn:
        calls.update(dirs=dirs) or {'needs_trust': False, 'results': [(d, True, 'ok') for d in dirs]})
    from vcstudio.cluster.profiles import ClusterProfile
    store = {'c1': ClusterProfile(name='c1', hostname='h', auth='key', key_path='/k')}
    api = Api(profiles_mod=_fake_profiles(store), batch_ops_mod=bo)
    out = api.submit_jobs(['/a', '/b'], 'c1', None, False)
    assert calls['dirs'] == ['/a', '/b'] and out['results'][0][1] is True


def test_unknown_profile_is_error_not_crash():
    api = Api(profiles_mod=_fake_profiles({}))
    out = api.submit_jobs(['/a'], 'nope', None, False)
    assert out.get('error') and '集群' in out['error']
```

- [ ] **Step 2: 确认失败** — `python -m pytest tests/test_web_api.py -q` → ImportError/AttributeError
- [ ] **Step 3: 实现 api.py**(骨架;list_jobs 的行组装从 jobs_tab.reload 平移,保持字段一致)

```python
# vcstudio/gui_web/api.py(核心结构;完整方法按 Interfaces 清单逐个实现)
from __future__ import annotations

import os
import sys
from dataclasses import asdict


class Api:
    """js_api 门面:参数/返回全 JSON-safe;真模块延迟导入,测试注入假件。"""

    def __init__(self, *, profiles_mod=None, secrets_mod=None, ssh_test_mod=None,
                 batch_ops_mod=None, ledger_mod=None, manifest_mod=None,
                 submitter_mod=None):
        from vcstudio.cluster import profiles as _p
        from vcstudio.shared import secrets as _s
        from vcstudio.cluster import ledger as _l
        from vcstudio.shared import manifest as _m
        self._profiles = profiles_mod or _p
        self._secrets = secrets_mod or _s
        self._ledger = ledger_mod or _l
        self._manifest = manifest_mod or _m
        self._ssh_test = ssh_test_mod      # 重依赖延迟到用时 import
        self._batch_ops = batch_ops_mod
        self._submitter = submitter_mod

    # ── 内部 ──
    def _bo(self):
        if self._batch_ops is None:
            from vcstudio.cluster import batch_ops
            self._batch_ops = batch_ops
        return self._batch_ops

    def _resolve(self, name, password):
        """名字 → (profile, 密码)。密码优先级:显式传入 > keyring;都没有且 auth=password → 让前端弹框。"""
        prof = self._profiles.load_profiles().get(name)
        if prof is None:
            return None, None, {'error': f'集群「{name}」不存在,请先在集群页保存'}
        pw = password or (self._secrets.get_password(name) if prof.auth == 'password' else None)
        if prof.auth == 'password' and not pw:
            return None, None, {'error': 'NEED_PASSWORD'}
        return prof, pw, None

    # ── 集群 ──
    def list_profiles(self):
        try:
            return {'profiles': [asdict(p) for p in self._profiles.load_profiles().values()],
                    'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'profiles': [], 'error': str(e)}

    def save_profile(self, data: dict):
        name = str(data.get('name', '')).strip()
        if not name:
            return {'ok': False, 'error': '集群名称不能为空'}
        known = {f.name for f in __import__('dataclasses').fields(self._profiles.ClusterProfile)}
        fields = {k: v for k, v in data.items() if k in known and k != 'name'}
        allp = self._profiles.load_profiles()
        allp[name] = self._profiles.ClusterProfile(name=name, **fields)
        self._profiles.save_profiles(allp)
        return {'ok': True, 'error': None}
    # …(delete_profile/has_saved_password/test_connection/preview_script 同构:
    #    try/except 包住,错误进 'error' 字段,绝不抛异常穿透到 JS)

    # ── 任务 ──
    def submit_jobs(self, dirs, name, password, trust_new=False):
        prof, pw, err = self._resolve(name, password)
        if err:
            return err
        try:
            return self._bo().submit_batch(prof, pw, list(dirs), bool(trust_new))
        except Exception as e:                            # noqa: BLE001
            return {'error': str(e)}
    # …(fetch_jobs/continue_jobs/refresh_status/queue_detail/adopt_job/
    #    remove_jobs/clean_stale/open_dir/list_jobs 按 Interfaces 清单实现)
```

- [ ] **Step 4: 测试过** — `python -m pytest tests/test_web_api.py tests/ -q` → 全过
- [ ] **Step 5: Commit**

```bash
git add vcstudio/gui_web/api.py tests/test_web_api.py
git commit -m "feat(gui_web): js_api facade for cluster + jobs (injectable, offline-testable)" -- vcstudio/gui_web/api.py tests/test_web_api.py
```

---

### Task 4: 前端壳(index.html + app.css + app.js:导航/路由/桥/通用组件)

**Files:**
- Create: `vcstudio/gui_web/assets/index.html`(覆写占位)、`assets/app.css`、`assets/app.js`

**Interfaces:**
- Consumes: `window.pywebview.api.*`(Task 3 全部方法)
- Produces(页面模块依赖):`VCS.call(method, ...args)`(桥+统一错误токен处理,NEED_PASSWORD 自动弹密码框重试)、`VCS.log(line, cls)`、`VCS.pill(state)`、`VCS.modal({title, body, actions})`、`VCS.confirm(msg)`、路由 `data-page` 切换

**要点(非占位,给实现者的硬规格):**
1. `app.css` 的 `:root` 变量、nav/header/pill/table/log 样式**逐字复制** `docs/superpowers/mockups/mockup_b_console.html` 的 `<style>` 内容,再追加:`.modal-mask/.modal`(居中卡片,背景 rgba(22,32,43,.45))、`.toast`、`.empty`(空态:居中一句话 + 主按钮)、`button:disabled{opacity:.5;cursor:not-allowed}`。
2. `index.html` 骨架 = mockup 的 nav+header+main,内容区五个 `<section data-page="dashboard|generate|project|jobs|cluster">`,默认显示 jobs;dashboard/generate/project 三个 section 本期只放空态(`本页在 P1b 迁移,先用旧版:python -m vcstudio.gui`)。
3. `app.js` 核心(完整实现,不再简化):

```javascript
// assets/app.js — 桥 + 路由 + 组件。零依赖。
const VCS = {
  ready: new Promise(res => window.addEventListener('pywebviewready', res)),

  async call(method, ...args) {                 // 统一入口:错误/密码/信任三类处理
    await VCS.ready;
    let out = await window.pywebview.api[method](...args);
    if (out && out.error === 'NEED_PASSWORD') {
      const pw = await VCS.password();          // 弹密码框
      if (pw === null) return { cancelled: true };
      // 密码放倒数第三参约定不可靠 → 各调用点自带 password 参数,重试由调用方做
      return { needPassword: true, password: pw };
    }
    return out;
  },

  password() {                                  // 返回 Promise<string|null>
    return new Promise(res => VCS.modal({
      title: '集群密码', bodyHTML:
        '<input id="pw" type="password" class="ipt" placeholder="输入密码(成功后存入系统凭据库)">',
      actions: [
        { label: '取消', quiet: true, onClick: m => { m.close(); res(null); } },
        { label: '连接', primary: true, onClick: m => { res(m.el.querySelector('#pw').value); m.close(); } },
      ]}));
  },

  modal({ title, bodyHTML, actions }) { /* 建 .modal-mask+.modal,返回 {el, close} */ },
  confirm(msg) { /* modal 包装,Promise<boolean> */ },
  pill(state) {                                  // 状态 → pill HTML(样式类同 mockup)
    const M = { RUNNING: ['run', 'RUN'], QUEUED: ['q', 'QUEUE'], SUBMITTED: ['q', 'SUBMIT'],
      UPLOADED: ['q', 'UPLOAD'], DONE: ['ok', 'DONE'], FAILED: ['fail', 'FAIL'],
      UNCONVERGED: ['fail', '未收敛'], NEEDS_HUMAN: ['warn', '需人工'], CREATED: ['q', '草稿'] };
    const [cls, txt] = M[state] || ['q', state];
    return `<span class="pill ${cls}"><i></i>${txt}</span>`;
  },
  log(line, cls = '') { /* 顶插 .log 容器,带 HH:MM:SS 时间戳,保留 200 行 */ },
};

// 路由:nav a[data-page] 点击 → section 显隐 + .on 高亮
document.addEventListener('click', e => {
  const a = e.target.closest('a[data-page]');
  if (!a) return;
  e.preventDefault();
  document.querySelectorAll('nav a').forEach(x => x.classList.toggle('on', x === a));
  document.querySelectorAll('main section[data-page]').forEach(
    s => s.hidden = s.dataset.page !== a.dataset.page);
});
```

- [ ] **Step 1: 实现三个文件**(规格如上)
- [ ] **Step 2: 字体打包(spec 要求离线分发)** — 下载 Noto Sans SC(Regular/Bold)与 JetBrains Mono(Regular)woff2 放 `assets/fonts/`,app.css 顶部加 `@font-face` 三段并把 `--sans`/`--mono` 首选改为 `'Noto Sans SC'`/`'JetBrains Mono'`(系统微软雅黑/Consolas 仍留作回退);woff2 三个文件合计应 <6MB,超了改用 subset。下载不到(离线环境)则跳过本步、留系统栈,并在计划勾选处注明。
- [ ] **Step 3: 人工验收** — `python -m vcstudio.gui_web` 打开:深色侧栏 5 项可切换、空态文案正确、无控制台报错(webview.start(debug=True) 临时看)、`await pywebview.api.ping()` 在 devtools 返回 'pong'。
- [ ] **Step 4: Commit**

```bash
git add vcstudio/gui_web/assets/
git commit -m "feat(gui_web): app shell — console-dense skin, router, bridge, modal/pill/log components" -- vcstudio/gui_web/assets
```

---

### Task 5: 任务页前端(表格 + 全部动作 + 自动刷新)

**Files:**
- Create: `vcstudio/gui_web/assets/jobs.js`
- Modify: `assets/index.html`(jobs section 填入 mockup 的 pagebar/actions/card/log 结构 + `<script src="jobs.js">`)

**Interfaces:**
- Consumes: `VCS.*`(Task 4)、api:`list_jobs/submit_jobs/refresh_status/fetch_jobs/continue_jobs/queue_detail/adopt_job/remove_jobs/clean_stale/open_dir/has_saved_password`
- Produces: `Jobs.reload()`(集群页保存后调用可刷新下拉)

**要点:**
1. 表格列 = mockup 减 sparkline(P3 才有数据通道):作业/状态/作业号/步/|F|max/E0/诊断/更新;行 `data-dir`,点击选中(多选 ctrl),双击 `open_dir`。
2. 动作按钮逻辑逐个对齐 jobs_tab 现行为:提交前 `VCS.confirm` 汇总(n 个作业/目标集群/remote_root);needs_trust 返回 → `VCS.confirm('未知主机…信任并重试?')` → 同调用 trust_new=true 重发;结果逐条 `VCS.log`,完了 `Jobs.reload()`。
3. 密码流:动作前 `has_saved_password`,没存过 → `VCS.password()`,把拿到的密码作为参数传 api(成功后 api 侧 test_connection 已负责存;批量动作成功也调 secrets 存——api 的 `_resolve` 已处理读取)。
4. 自动刷新:右上 checkbox + 间隔选择(5/15/30 分钟),`setInterval` 调 `refresh_status`;上一轮未返回则跳过本轮(与 tkinter 版语义一致);无保存密码时写日志提示并跳过。
5. 集群队列 modal:表(作业号/状态/作业名/远程目录/纳管),未纳入行有「认领」按钮 → 二段 modal(远程目录预填、本地目录文本框)→ `adopt_job`。
6. 空态:台账为空 → `.empty`("还没有纳管的作业 — 去生成页产出四件套,或从集群队列认领已有作业")。

- [ ] **Step 1: 实现 jobs.js + section 结构**
- [ ] **Step 2: 人工验收清单**(连不上集群也可验的部分):台账列表渲染真实 job.yaml、状态 pill 颜色正确、多选/双击、移出台账/清理失效可用、动作按钮在无集群配置时给引导日志;连 VPN 后:查询状态/集群队列/提交全链路。
- [ ] **Step 3: Commit**

```bash
git add vcstudio/gui_web/assets/jobs.js vcstudio/gui_web/assets/index.html
git commit -m "feat(gui_web): jobs page — ledger table, batch actions, trust/password flows, auto-refresh" -- vcstudio/gui_web/assets/jobs.js vcstudio/gui_web/assets/index.html
```

---

### Task 6: 集群页前端(表单 + 测试连接 + 脚本预览)

**Files:**
- Create: `vcstudio/gui_web/assets/cluster.js`
- Modify: `assets/index.html`(cluster section:两栏表单卡片)

**Interfaces:**
- Consumes: api:`list_profiles/save_profile/delete_profile/test_connection/preview_script`
- Produces: 保存成功后触发 `Jobs.reload()` 同步任务页集群下拉

**要点:**
1. 表单字段与 `ClusterProfile` 全量对齐(连接组/跳板组/调度器组/资源组/脚本双轨组),布局用卡片分组;`scheduler_bin` 帮助文案:"调度器不在 PATH 时必填,如 1w 的 /opt/torque-6.1.2/bin"。
2. 测试连接:按钮 → 若 auth=password 且无保存密码先弹 `VCS.password()` → `test_connection(name, pw, false)`;`needs_trust` → confirm → trust_new=true 重试;成功日志"已连上 <host>,whoami=<u>,检测到 <sched>";探测结果 ≠ 表单选择 → 黄色警告条"远端探测到 PBS,与当前选择不一致,已为你选中"并自动改下拉(用户可改回)。
3. 预览脚本:选台账任一作业目录难做——直接用 modal 展示 `preview_script(name, job_dir)`,job_dir 取任务页当前选中;无选中给提示。

- [ ] **Step 1: 实现**
- [ ] **Step 2: 人工验收**:读出现有"华师大集群"配置全字段、改动保存后 clusters.yaml 落盘正确(对比 git diff 无关字段不动)、探测不一致警告可见(连 VPN 后)。
- [ ] **Step 3: Commit**

```bash
git add vcstudio/gui_web/assets/cluster.js vcstudio/gui_web/assets/index.html
git commit -m "feat(gui_web): cluster page — profile form, test-connection with scheduler probe, script preview" -- vcstudio/gui_web/assets/cluster.js vcstudio/gui_web/assets/index.html
```

---

### Task 7: 打包接线 + 收尾验证

**Files:**
- Modify: `packaging/build_exe.py`(`--add-data assets;vcstudio_assets`,新入口可选参数)
- Test: 手动

**要点:**
1. `build_exe.py` 增加 `--web` 开关:入口换 `vcstudio/gui_web/__main__.py`,并追加
   `cmd += ['--add-data', os.path.join(ROOT, 'vcstudio', 'gui_web', 'assets') + os.pathsep + 'vcstudio_assets']`
   (与 resources.asset_dir 的 frozen 分支目录名严格一致);默认(无开关)仍打旧 tkinter 入口——P1b 完成前双轨。
2. `pywebview` 打包需 `--collect-all webview`(clr-loader/bottle 资源),加在 `--web` 分支。

- [ ] **Step 1: 改 build_exe.py**
- [ ] **Step 2: 全量测试** — `python -m pytest tests/ -q` → 全过
- [ ] **Step 3: 打包验证** — `python packaging/build_exe.py --web` → dist 出 exe,双击可开、任务页读到台账
- [ ] **Step 4: Commit**

```bash
git add packaging/build_exe.py
git commit -m "build: optional --web entry packaging pywebview assets" -- packaging/build_exe.py
```

---

## 验收总清单(P1a 完成定义)

- [ ] `python -m vcstudio.gui_web`:深色侧栏壳 + 任务页 + 集群页全功能;生成/项目页显式空态指回旧版
- [ ] 旧 `python -m vcstudio.gui` 完全不受影响(batch_ops 委托后行为不变)
- [ ] `python -m pytest tests/ -q` 全绿
- [ ] `--web` exe 打包可运行
- [ ] UI 无 emoji;pill/等宽/色板与 mockup 一致
