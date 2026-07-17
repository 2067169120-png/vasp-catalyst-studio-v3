"""pywebview js_api:集群 + 任务处理器门面。

薄处理器:每个 JS 调用由 pywebview 派独立线程执行,可同步阻塞。
铁律:每个公开方法都返回 JSON-safe dict,异常一律 try/except 兜成
{'error': str(e)},绝不让异常穿透到 JS 侧。重模块(batch_ops/ssh_test/
submitter)延迟导入,测试注入假件即可全离线跑,不碰网络/keyring。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import asdict, fields as dc_fields

# 合法计算类型(决定 KPOINTS 网格);前端下拉与后端都以此为准
_CALC_TYPES = ('slab', 'bulk', 'molecule')

# 主题白名单(设置页三选);自动驾驶管线阶段序(pipeline_status 的 stage_index 取此序)
_THEMES = ('classic', 'paper', 'deep')
_STAGES = ('generate', 'submit', 'monitor', 'recover', 'analysis', 'report_done')
# 活跃(在队/在跑)状态与可续算终态:pipeline_tick/status 复用
_ACTIVE_STATES = ('UPLOADED', 'SUBMITTED', 'QUEUED', 'RUNNING')
_TERMINAL_FAIL = ('FAILED', 'UNCONVERGED')


def _norm_calc_type(x) -> str:
    """归一化前端传入的计算类型:非法/缺省一律回落 'slab'(保守且向后兼容)。"""
    s = str(x or '').strip().lower()
    return s if s in _CALC_TYPES else 'slab'


class Api:
    """js_api 门面:参数/返回全 JSON-safe;真模块延迟导入,测试注入假件。"""

    def __init__(self, *, profiles_mod=None, secrets_mod=None, ssh_test_mod=None,
                 batch_ops_mod=None, ledger_mod=None, manifest_mod=None,
                 submitter_mod=None, config_mod=None, job_builder_mod=None,
                 logic_mod=None, adsorption_mod=None, report_full_mod=None,
                 conv_mod=None, sview_mod=None, methods_mod=None, dialog_fn=None,
                 native_charts_mod=None, freeenergy_mod=None, ai_analysis_mod=None):
        from vcstudio.cluster import profiles as _p
        from vcstudio.shared import secrets as _s
        from vcstudio.cluster import ledger as _l
        from vcstudio.shared import manifest as _m
        from vcstudio.shared import config as _cfg
        from vcstudio.generate import job_builder as _jb
        from vcstudio.gui import logic as _logic
        from vcstudio.project import adsorption as _ads
        from vcstudio.cluster import convergence as _conv
        from vcstudio.generate import structure_view as _sview
        from vcstudio.generate import methods_text as _methods
        self._profiles = profiles_mod or _p
        self._secrets = secrets_mod or _s
        self._ledger = ledger_mod or _l
        self._manifest = manifest_mod or _m
        # 生成页依赖(纯模块,无网络/keyring,故与 ledger/manifest 一样即时导入)
        self._config = config_mod or _cfg
        self._job_builder = job_builder_mod or _jb
        self._logic = logic_mod or _logic
        # 项目页:adsorption 纯模块(无 matplotlib)即时导入;report_full 牵扯 charts
        # (matplotlib 相邻)故延迟到用时 import(同 batch_ops),测试注入假件即免真依赖。
        self._adsorption = adsorption_mod or _ads
        # 收敛解析/结构预览:纯函数模块(零 IO),即时导入,测试注入假件。
        self._conv = conv_mod or _conv
        self._sview = sview_mod or _sview
        self._methods = methods_mod or _methods
        self._dialog_fn = dialog_fn        # 测试注入假 dialog;None → 真走 webview
        self._ssh_test = ssh_test_mod      # 重依赖延迟到用时 import
        self._batch_ops = batch_ops_mod
        self._submitter = submitter_mod
        self._report_full = report_full_mod
        # 原生出图引擎(matplotlib 可选依赖)与自由能:延迟导入,测试注入假件
        self._native_charts = native_charts_mod
        self._freeenergy = freeenergy_mod
        # LLM 分析层(设置页/自动报告用):纯 stdlib 模块,延迟导入,测试注入假件
        self._ai_analysis = ai_analysis_mod

    # ── 桥活性探测(前端用来确认 js_api 已就绪) ──
    def ping(self) -> str:
        return 'pong'

    # ── 内部:重模块延迟加载 ──
    def _bo(self):
        if self._batch_ops is None:
            from vcstudio.cluster import batch_ops
            self._batch_ops = batch_ops
        return self._batch_ops

    def _sub(self):
        if self._submitter is None:
            from vcstudio.cluster import submitter
            self._submitter = submitter
        return self._submitter

    def _ssh(self):
        if self._ssh_test is None:
            from vcstudio.cluster import ssh_test
            self._ssh_test = ssh_test
        return self._ssh_test

    def _rf(self):
        """report_full 延迟加载(牵扯 charts/matplotlib 相邻,重):测试注入假件即免真依赖。"""
        if self._report_full is None:
            from vcstudio.project import report_full
            self._report_full = report_full
        return self._report_full

    def _nc(self):
        """原生出图引擎延迟加载(matplotlib/numpy 为可选依赖 charts)。"""
        if self._native_charts is None:
            from vcstudio.external import native_charts
            self._native_charts = native_charts
        return self._native_charts

    def _fe(self):
        if self._freeenergy is None:
            from vcstudio.project import freeenergy
            self._freeenergy = freeenergy
        return self._freeenergy

    def _ai(self):
        """LLM 分析层延迟加载(keyring/urllib 在其内部再延迟);测试注入假件即免真依赖。"""
        if self._ai_analysis is None:
            from vcstudio.project import ai_analysis
            self._ai_analysis = ai_analysis
        return self._ai_analysis

    def _resolve(self, name, password):
        """名字 → (profile, 密码, err_dict|None)。

        密码优先级:显式传入 > keyring;都没有且 auth=password → 让前端弹框
        (返回精确 token 'NEED_PASSWORD',前端据此匹配)。
        """
        prof = self._profiles.load_profiles().get(name)
        if prof is None:
            return None, None, {'error': f'集群「{name}」不存在,请先在集群页保存'}
        pw = password or (self._secrets.get_password(name) if prof.auth == 'password' else None)
        if prof.auth == 'password' and not pw:
            return None, None, {'error': 'NEED_PASSWORD'}
        return prof, pw, None

    # ── 集群 ─────────────────────────────────────────────────────────────────
    def list_profiles(self):
        try:
            return {'profiles': [asdict(p) for p in self._profiles.load_profiles().values()],
                    'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'profiles': [], 'error': str(e)}

    def save_profile(self, data: dict):
        try:
            name = str((data or {}).get('name', '')).strip()
            if not name:
                return {'ok': False, 'error': '集群名称不能为空'}
            known = {f.name for f in dc_fields(self._profiles.ClusterProfile)}
            fields = {k: v for k, v in data.items() if k in known and k != 'name'}
            allp = self._profiles.load_profiles()
            allp[name] = self._profiles.ClusterProfile(name=name, **fields)
            self._profiles.save_profiles(allp)
            return {'ok': True, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    def delete_profile(self, name: str):
        try:
            allp = self._profiles.load_profiles()
            if name not in allp:
                return {'ok': False}
            del allp[name]
            self._profiles.save_profiles(allp)
            return {'ok': True}
        except Exception:                                 # noqa: BLE001
            return {'ok': False}

    def test_connection(self, name, password, trust_new=False):
        try:
            prof = self._profiles.load_profiles().get(name)
            if prof is None:
                return {'ok': False, 'message': f'集群「{name}」不存在,请先在集群页保存',
                        'scheduler': '', 'needs_trust': False}
            # 前端在 keyring 已存密码时故意传 null;回退取 keyring 密码,
            # 否则 password=None 恒认证失败(存过密码后 retest 永远挂)。
            if prof.auth == 'password' and not password:
                password = self._secrets.get_password(name)
            res = self._ssh().check_connection(prof, password, trust_new=bool(trust_new))
            # 成功 + 显式给了密码 + 该 profile 走密码认证 → 顺手存 keyring
            if res.ok and password and prof.auth == 'password':
                self._secrets.set_password(name, password)
            return {'ok': bool(res.ok), 'message': res.message,
                    'scheduler': res.scheduler, 'needs_trust': bool(res.needs_trust)}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'message': str(e), 'scheduler': '', 'needs_trust': False}

    def has_saved_password(self, name):
        try:
            return {'saved': self._secrets.get_password(name) is not None}
        except Exception:                                 # noqa: BLE001
            return {'saved': False}

    def preview_script(self, name, job_dir):
        try:
            prof = self._profiles.load_profiles().get(name)
            if prof is None:
                return {'ok': False, 'error': f'集群「{name}」不存在,请先在集群页保存'}
            return {'ok': True, 'text': self._sub().build_script_text(prof, job_dir)}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    # ── 任务:台账列表(取数逻辑照抄 jobs_tab.reload) ──────────────────────
    def _project_role_map(self):
        """{规范化 job_dir → {'project': 项目名, 'role': 'clean'|'gas'|'config'}}。

        供 list_jobs 给作业行注入所属吸附能项目组;任何异常(注册表坏/单个
        project.yaml 畸形)兜成空映射或跳过该项目,绝不拖垮 list_jobs。
        """
        def norm(d):
            return os.path.normcase(os.path.normpath(str(d)))

        mapping = {}
        try:
            for pp in self._adsorption.list_projects():
                try:
                    proj = self._adsorption.load_project(pp)
                    if proj is None:
                        continue
                    pname = proj.get('name', '') or os.path.basename(
                        os.path.dirname(str(pp)))
                    mem = proj.get('members') or {}
                    if mem.get('clean_slab'):
                        mapping[norm(mem['clean_slab'])] = {'project': pname,
                                                            'role': 'clean'}
                    if mem.get('gas_ref'):
                        mapping[norm(mem['gas_ref'])] = {'project': pname,
                                                         'role': 'gas'}
                    for d in (mem.get('configs') or []):
                        if d:
                            mapping[norm(d)] = {'project': pname, 'role': 'config'}
                except Exception:                         # noqa: BLE001 单个坏项目跳过
                    continue
        except Exception:                                 # noqa: BLE001 注册表坏 → 全部不分组
            return {}
        return mapping

    def list_jobs(self):
        try:
            jobs, stale = [], []
            pmap = self._project_role_map()
            for job_dir, m in self._ledger.load_all():
                if m is None:
                    stale.append(job_dir)
                    continue
                state = m.get('state', '?')
                hist = m.get('state_history') or []
                updated = hist[-1].get('at', '') if hist else m.get('created_at', '')
                res = m.get('results') or {}
                energy = res.get('energy_e0_eV', '')
                dgn = res.get('diagnosis') or {}
                live = res.get('live') or {}
                diag = ''
                if dgn.get('failure_class') and state in ('FAILED', 'UNCONVERGED', 'NEEDS_HUMAN'):
                    diag = dgn['failure_class'] + ('♻可续算' if dgn.get('restartable') else '')
                elif state == 'RUNNING':
                    if live.get('warning'):
                        diag = '⚠' + str(live['warning'])[:24]
                    elif live.get('ionic_steps') is not None:
                        diag = f"{live['ionic_steps']}步" + (
                            f" |F|max={live['fmax']}" if live.get('fmax') else '')
                grp = pmap.get(os.path.normcase(os.path.normpath(job_dir)))
                jobs.append({
                    'dir': job_dir,
                    'name': os.path.basename(os.path.normpath(job_dir)),
                    'project': grp['project'] if grp else None,
                    'role': grp['role'] if grp else None,
                    'state': state,
                    'task': f"{m.get('task_type', '')}/{m.get('calc_type', '')}",
                    'cluster': m.get('cluster') or '',
                    'job_id': m.get('scheduler_job_id') or '',
                    'energy': f'{energy:.4f}' if isinstance(energy, float) else energy,
                    'diag': diag,
                    'updated': updated,
                    'steps': live.get('ionic_steps'),
                    'fmax': live.get('fmax'),
                })
            return {'jobs': jobs, 'stale': stale}
        except Exception as e:                            # noqa: BLE001
            return {'jobs': [], 'stale': [], 'error': str(e)}

    # ── 任务:远程动作(一律先 _resolve 再委托 batch_ops 同名函数) ─────────
    def _delegate(self, name, password, fn):
        """五个远程方法的公共壳:解析集群+密码 → 委托 batch_ops;任何异常兜成 error dict。

        _resolve 内部会读 load_profiles()/keyring,配置损坏或后端报错也须在此兜住,
        绝不穿透到 JS。
        """
        try:
            prof, pw, err = self._resolve(name, password)
            if err:
                return err
            return fn(prof, pw)
        except Exception as e:                            # noqa: BLE001 异常绝不穿透到 JS
            return {'error': str(e)}

    def submit_jobs(self, dirs, name, password, trust_new=False):
        return self._delegate(name, password,
                              lambda prof, pw: self._bo().submit_batch(
                                  prof, pw, list(dirs), bool(trust_new)))

    def fetch_jobs(self, dirs, name, password, trust_new=False, files=None):
        def _fetch(prof, pw):
            if files is None:
                return self._bo().fetch_batch(prof, pw, list(dirs), bool(trust_new))
            return self._bo().fetch_batch(prof, pw, list(dirs), bool(trust_new), files)
        return self._delegate(name, password, _fetch)

    def continue_jobs(self, dirs, name, password, trust_new=False):
        return self._delegate(name, password,
                              lambda prof, pw: self._bo().continue_batch(
                                  prof, pw, list(dirs), bool(trust_new)))

    def refresh_status(self, name, password, trust_new=False):
        def _refresh(prof, pw):
            # 目标 dirs 逻辑照抄 jobs_tab._on_refresh_status:
            # 台账里该集群 + 有作业号 + 状态 SUBMITTED/QUEUED/RUNNING
            targets = [d for d, m in self._ledger.load_all()
                       if m and m.get('scheduler_job_id') and m.get('cluster') == prof.name
                       and m.get('state') in ('SUBMITTED', 'QUEUED', 'RUNNING')]
            if not targets:
                return {'needs_trust': False, 'results': []}
            return self._bo().refresh_batch(prof, pw, targets, bool(trust_new))
        return self._delegate(name, password, _refresh)

    def queue_detail(self, name, password, trust_new=False):
        return self._delegate(name, password,
                              lambda prof, pw: self._bo().queue_detail(
                                  prof, pw, bool(trust_new)))

    def query_workdir(self, job_id, name, password, trust_new=False):
        """认领辅助:按作业号查远程工作目录(PBS qstat -f;查不到 workdir='')。"""
        return self._delegate(name, password,
                              lambda prof, pw: self._bo().workdir_lookup(
                                  prof, pw, str(job_id), bool(trust_new)))

    # ── 任务:认领外部作业 ──
    def _adopt_root_default(self):
        return os.path.join(os.path.expanduser('~'), 'vcstudio_jobs')

    def adopt_root_get(self):
        """认领本地根目录(未配置 → %USERPROFILE%\\vcstudio_jobs)。"""
        default = self._adopt_root_default()
        try:
            ui = self._config.get_ui_state()
            return {'root': (ui.get('adopt_root') or default)}
        except Exception:                                 # noqa: BLE001 读配置失败退默认
            return {'root': default}

    def adopt_root_set(self, path):
        """持久化认领本地根目录(config ui_state)。"""
        try:
            p = (path or '').strip()
            if not p:
                return {'ok': False, 'error': '认领根目录不能为空'}
            self._config.set_ui_state(adopt_root=p)
            return {'ok': True, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    def adopt_all(self, name, password, trust_new=False):
        """一键认领全部未纳入作业:一次连接完成 明细→补目录→落认领(委托 adopt_scan)。

        known_ids = 台账里所有带调度器作业号的作业(str);root 从 adopt_root_get。
        """
        def _scan(prof, pw):
            known_ids = {str(m['scheduler_job_id'])
                         for _d, m in self._ledger.load_all()
                         if m and m.get('scheduler_job_id')}
            root = self.adopt_root_get().get('root')
            return self._bo().adopt_scan(prof, pw, bool(trust_new), known_ids, root)
        return self._delegate(name, password, _scan)

    def adopt_job(self, local_dir, name, job_id, remote_dir, job_name=''):
        try:
            prof = self._profiles.load_profiles().get(name)
            if prof is None:
                return {'error': f'集群「{name}」不存在,请先在集群页保存'}
            self._sub().adopt_external_job(local_dir, prof, job_id, remote_dir, name=job_name)
            return {'ok': True}
        except Exception as e:                            # noqa: BLE001
            return {'error': str(e)}

    # ── 台账维护 ──
    def remove_jobs(self, dirs):
        try:
            for d in dirs:
                self._ledger.unregister(d)
            return {'ok': True}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    def clean_stale(self):
        try:
            stale = [d for d, m in self._ledger.load_all() if m is None]
            for d in stale:
                self._ledger.unregister(d)
            return {'ok': True, 'removed': len(stale)}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    # ── 生成页(镜像 gui/generate_tab 的调用面:预览/回填/一键生成/文件选择) ─────
    def gen_preview(self, poscar_path, incar_path, calc_type='slab'):
        """即时解析预览(镜像 generate_tab._refresh_preview):纯读、不写任何文件。

        summary = {'poscar','incar'} 两段中文摘要(logic.poscar_preview/incar_preview
        自身已把解析问题降级为友好文案);calc_type 由前端「计算类型」下拉传入
        (slab/bulk/molecule,非法值归 slab),影响 KPOINTS 预览;validate 取生成默认(开)。
        """
        try:
            poscar = (poscar_path or '').strip()
            incar = (incar_path or '').strip()
            calc = _norm_calc_type(calc_type)
            cfg = self._config.load_config()
            lib = cfg.get('potcar_lib_root', '') or ''
            pos_txt = self._logic.poscar_preview(poscar, calc)
            inc_txt = self._logic.incar_preview(incar, poscar, lib, True)
            return {'ok': True, 'summary': {'poscar': pos_txt, 'incar': inc_txt},
                    'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'summary': None, 'error': str(e)}

    def gen_state(self):
        """启动回填(镜像 generate_tab._load_state):最近 POSCAR/INCAR/输出目录 + 赝势库。"""
        try:
            cfg = self._config.load_config()
            ui = self._config.get_ui_state(cfg)
            return {'poscar': ui.get('last_poscar', '') or '',
                    'incar': ui.get('last_incar', '') or '',
                    'out_dir': ui.get('last_out', '') or '',
                    'lib_root': cfg.get('potcar_lib_root', '') or ''}
        except Exception as e:                            # noqa: BLE001
            return {'poscar': '', 'incar': '', 'out_dir': '', 'lib_root': '',
                    'error': str(e)}

    def conv_series(self, job_dir):
        """收敛过程序列(C1):读本地 job_dir/OSZICAR(+OUTCAR 若在)→ 逐离子步
        E0/ΔE/|F|max。缺 OSZICAR → 结构化 error(提示先拉取);解析异常兜成 error。
        纯读、不写、不联网;OUTCAR 缺失时降级由 convergence_series 用 notes 说明。
        """
        try:
            d = (job_dir or '').strip()
            osz_path = os.path.join(d, 'OSZICAR')
            if not d or not os.path.isfile(osz_path):
                return {'ok': False, 'series': None,
                        'error': '该作业尚无本地 OSZICAR,请先拉取该作业结果'}
            with open(osz_path, 'r', encoding='utf-8', errors='replace') as f:
                osz_txt = f.read()
            out_path = os.path.join(d, 'OUTCAR')
            out_txt = None
            if os.path.isfile(out_path):
                with open(out_path, 'r', encoding='utf-8', errors='replace') as f:
                    out_txt = f.read()
            series = self._conv.convergence_series(osz_txt, out_txt)
            return {'ok': True, 'series': series, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'series': None, 'error': str(e)}

    def struct_view(self, path, filename=None):
        """结构 3D 预览(C2):读本地 POSCAR/CONTCAR → XYZ + 间隙分析。

        - filename=None:path 本身即结构文件(生成页传选中的 POSCAR 路径)。
        - filename='AUTO':path 为 job_dir,依次试 CONTCAR、POSCAR(拉回后看弛豫结果)。
        - 其他 filename:os.path.join(path, filename)——路径拼接留后端,JS 不碰 os.sep。
        返回 {'ok','view'|None,'used','error'};缺文件/解析失败结构化 error,绝不抛。
        """
        try:
            base = (path or '').strip()
            if not base:
                return {'ok': False, 'view': None, 'used': None,
                        'error': '未提供结构文件路径'}
            if filename == 'AUTO':
                # 依次试 CONTCAR、POSCAR:非空但解析失败(如截断的 CONTCAR)也回退下一个
                errs = []
                for cand in ('CONTCAR', 'POSCAR'):
                    p = os.path.join(base, cand)
                    if not (os.path.isfile(p) and os.path.getsize(p) > 0):
                        continue
                    try:
                        with open(p, 'r', encoding='utf-8', errors='replace') as f:
                            content = f.read()
                        view = self._sview.structure_view(content)
                        return {'ok': True, 'view': view, 'used': cand, 'error': None}
                    except Exception as e:                # noqa: BLE001
                        errs.append(f'{cand}: {e}')
                return {'ok': False, 'view': None, 'used': None,
                        'error': ';'.join(errs) or '该作业目录下没有 CONTCAR/POSCAR,无法预览'}
            elif filename:
                target, used = os.path.join(base, filename), filename
            else:
                target, used = base, os.path.basename(base) or base
            if not os.path.isfile(target):
                return {'ok': False, 'view': None, 'used': None,
                        'error': f'结构文件不存在:{target}'}
            with open(target, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            view = self._sview.structure_view(content)
            return {'ok': True, 'view': view, 'used': used, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'view': None, 'used': None, 'error': str(e)}

    def dos_view(self, job_dir):
        """DOS 出图(C4):job_dir/vasprun.xml → 出版风格 SVG,同时落 job_dir/dos.svg。

        缺 vasprun.xml → error(拉回清单暂无它,需手动放置);解析失败 → error;
        dos.svg 写失败只 warnings 不挡显示。dosparse/charts 皆纯模块即时导入。
        """
        try:
            d = (job_dir or '').strip()
            xml_path = os.path.join(d, 'vasprun.xml')
            if not d or not os.path.isfile(xml_path):
                return {'ok': False, 'svg': None, 'saved': None, 'warnings': [],
                        'error': '该作业目录下没有 vasprun.xml(拉回清单暂不含它,'
                                 '请从集群手动下载后重试)'}
            from vcstudio.project import dosparse, charts
            with open(xml_path, 'r', encoding='utf-8', errors='replace') as f:
                dos = dosparse.parse_vasprun_dos(f)
            svg = charts.render_dos_svg(dos, title=os.path.basename(d) + ' DOS')
            warnings, saved = [], None
            try:
                out_path = os.path.join(d, 'dos.svg')
                with open(out_path, 'w', encoding='utf-8') as f:
                    f.write(svg)
                saved = out_path
            except Exception as e:                    # noqa: BLE001
                warnings.append(f'dos.svg 保存失败(仅影响落盘,不影响显示):{e}')
            return {'ok': True, 'svg': svg, 'saved': saved,
                    'warnings': warnings, 'error': None}
        except Exception as e:                        # noqa: BLE001
            return {'ok': False, 'svg': None, 'saved': None, 'warnings': [],
                    'error': str(e)}

    def methods_text(self, job_dir):
        """Methods 段生成(C3):读 job_dir 真实 INCAR/KPOINTS/POTCAR →
        {'ok','zh','en','bibtex','warnings'|'error'}。INCAR 缺 → error;
        KPOINTS/POTCAR 缺 → warnings 降级(extract_facts 内已明说)。纯读不写。
        """
        try:
            d = (job_dir or '').strip()
            inc_path = os.path.join(d, 'INCAR')
            if not d or not os.path.isfile(inc_path):
                return {'ok': False, 'zh': None, 'en': None, 'bibtex': None,
                        'warnings': [], 'error': '该作业目录下没有 INCAR,无法生成方法段'}

            def _read(name):
                p = os.path.join(d, name)
                if not os.path.isfile(p):
                    return None
                with open(p, 'r', encoding='utf-8', errors='replace') as f:
                    return f.read()

            r = self._methods.extract_facts(_read('INCAR'), _read('KPOINTS'),
                                            _read('POTCAR'))
            facts = r['facts']
            return {'ok': True,
                    'zh': self._methods.render_zh(facts),
                    'en': self._methods.render_en(facts),
                    'bibtex': self._methods.render_bibtex(facts),
                    'warnings': r['warnings'], 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'zh': None, 'en': None, 'bibtex': None,
                    'warnings': [], 'error': str(e)}

    def gen_run(self, poscar_path, incar_path, out_dir, lib_root, calc_type='slab'):
        """一键生成(镜像 generate_tab._on_run→build_job_dir→_write_manifest→ledger.register)。

        calc_type 由前端「计算类型」下拉传入(slab/bulk/molecule,非法值归 slab):决定
        KPOINTS 网格(slab 法向仅 1 个 k 点、molecule 为 Gamma 单点、bulk 三维网格)。
        此前 web 硬编码 slab,生成 bulk/molecule 会拿到错误 KPOINTS(缺口分析已指出)。
        校验开关取默认(开)、KPOINTS 仍自动推荐。
        job.yaml 与台账写入失败只追加警告,绝不撤销已生成的四件套(同 _write_manifest 口径)。
        """
        try:
            poscar = (poscar_path or '').strip()
            incar = (incar_path or '').strip()
            out = (out_dir or '').strip()
            lib = (lib_root or '').strip()
            # 持久化赝势库(_persist_lib:失败只告警,不挡生成)
            if lib:
                try:
                    self._config.set_potcar_lib_root(lib)
                except Exception:                         # noqa: BLE001
                    pass
            errs = self._logic.validate_generate_inputs(poscar, incar, out, lib)
            if errs:
                return {'ok': False, 'job_dir': None, 'warnings': [],
                        'error': ';'.join(errs)}
            validate, calc_type, kpts = True, _norm_calc_type(calc_type), None
            # 路径记忆:下次启动自动回填(失败静默,同 _on_run)
            try:
                self._config.set_ui_state(last_poscar=poscar, last_incar=incar,
                                          last_out=out)
            except Exception:                             # noqa: BLE001
                pass
            payload = self._job_builder.build_job_dir(
                poscar, incar, out, calc_type=calc_type, kpoints=kpts,
                validate=validate, lib_root=lib)
            warnings = list(payload.get('warnings') or [])
            # 落 job.yaml + 登记台账(_write_manifest:失败只告警)
            try:
                self._manifest.create_from_build(
                    payload['out_dir'], payload,
                    poscar_path=poscar, validate=validate)
                self._ledger.register(payload['out_dir'])
            except Exception as e:                        # noqa: BLE001
                warnings.append(f'job.yaml/台账写入失败(不影响四件套):{e}')
            return {'ok': True, 'job_dir': payload['out_dir'],
                    'warnings': warnings, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'job_dir': None, 'warnings': [], 'error': str(e)}

    # ── 吸附能项目页(镜像 gui/project_tab 调用面:列表/创建/ΔE/CSV/报告) ─────────
    def proj_list(self):
        """项目注册表 → [{path,name,n_members}](镜像 project_tab._reload_projects)。

        n_members 内联算(清洁表面 + 气相参考 + 构型族,镜像 project_tab._member_dirs):
        列表渲染绝不触碰 report_full(避免仅为计数拖入 matplotlib,matplotlib 坏时也不
        整体失败)。畸形/已移动的 project.yaml(load_project→None)或坏成员静默跳过。
        """
        try:
            projects = []
            for pp in self._adsorption.list_projects():
                try:
                    proj = self._adsorption.load_project(pp)
                    if proj is None:
                        continue
                    mem = proj.get('members') or {}
                    member_dirs = [d for d in ([mem.get('clean_slab'),
                                                mem.get('gas_ref')]
                                               + list(mem.get('configs') or [])) if d]
                    projects.append({
                        'path': pp,
                        'name': proj.get('name', '') or '',
                        'n_members': len(member_dirs),
                    })
                except Exception:                         # noqa: BLE001 单个坏项目不拖垮全表
                    continue
            return {'projects': projects, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'projects': [], 'error': str(e)}

    def proj_create(self, name, slab_path, config_paths, incar_path, gas_path,
                    out_root):
        """批量生成 清洁表面 + 构型族 +(可选)气相参考(镜像 project_tab._on_generate)。

        前置校验照抄 _on_generate;lib_root 从 config 读(失败静默)。坏构型隔离语义在
        adsorption 层已就位(create_project 的 errors 只记不拖垮全组)→ 此处把 build 警告
        与构型 errors 一并透传进 warnings,不整体失败;advisories 转成 "[级别] 文案" 列表
        (project_tab 展示口径)。gas_path 空 → ref_poscar=None(镜像可选气相参考)。
        """
        try:
            name = (name or '').strip()
            slab = (slab_path or '').strip()
            incar = (incar_path or '').strip()
            gas = (gas_path or '').strip()
            root = (out_root or '').strip()
            configs = [str(p).strip() for p in (config_paths or []) if str(p).strip()]
            errs = []
            if not name:
                errs.append('未填项目名')
            if not root:
                errs.append('未选输出根目录')
            if not slab or not os.path.isfile(slab):
                errs.append('清洁表面 POSCAR 不存在')
            if not incar or not os.path.isfile(incar):
                errs.append('共享 INCAR 不存在')
            if not configs:
                errs.append('至少添加一个吸附构型')
            if gas and not os.path.isfile(gas):
                errs.append('气相参考文件不存在')
            if errs:
                return {'ok': False, 'project_path': None, 'advisories': [],
                        'warnings': [], 'error': ';'.join(errs)}
            lib = ''
            try:
                lib = self._config.load_config().get('potcar_lib_root', '') or ''
            except Exception:                             # noqa: BLE001
                pass
            res = self._adsorption.create_project(
                os.path.join(root, name), name,
                clean_poscar=slab, config_poscars=configs,
                incar_path=incar, ref_poscar=(gas or None),
                lib_root=(lib or None))
            advisories = [f'[{pri}·{aname}] {msg}'
                          for pri, aname, msg in (res.get('advisories') or [])]
            warnings = []
            for member, _d, wlist in (res.get('generated') or []):
                for w in (wlist or []):
                    warnings.append(f'{member}:{w}')
            for member, msg in (res.get('errors') or []):
                warnings.append(f'{member}:{msg}')
            return {'ok': bool(res.get('ok')),
                    'project_path': res.get('project_path'),
                    'advisories': advisories, 'warnings': warnings, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'project_path': None, 'advisories': [],
                    'warnings': [], 'error': str(e)}

    def proj_delta(self, path):
        """项目 ΔE 汇总(镜像 project_tab._on_delta)。

        rows 忠实透传 delta_e_rows 的字段(name/state/e_config/delta_e/note);ΔE 门控语义
        原样——任一成员未 DONE 时对应行 delta_e=None 且 note 明说缺谁。note 顶层汇总清洁
        表面/气相参考状态(镜像 _on_delta 头部两行)。
        """
        try:
            proj = self._adsorption.load_project((path or '').strip())
            if proj is None:
                return {'ok': False, 'rows': [], 'note': '',
                        'error': '项目不存在或 project.yaml 已被移动'}
            s = self._adsorption.delta_e_rows(proj)
            slab_state, e_slab = s['slab']
            ref_state, e_ref = s['ref']
            rows = [{'name': r['name'], 'state': r['state'],
                     'e_config': r['e_config'], 'delta_e': r['delta_e'],
                     'note': r['note']} for r in (s.get('rows') or [])]
            parts = [f'清洁表面:{slab_state}']
            parts.append(f'气相参考:{ref_state}' if s.get('has_ref')
                         else '未设气相参考')
            return {'ok': True, 'rows': rows, 'note': ';'.join(parts),
                    'slab': {'state': slab_state, 'energy': e_slab},
                    'ref': {'state': ref_state, 'energy': e_ref,
                            'has_ref': bool(s.get('has_ref'))},
                    'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'rows': [], 'note': '', 'error': str(e)}

    def proj_export_csv(self, path, save_to):
        """ΔE 表导出 CSV(镜像 project_tab._on_export;utf-8-sig 语义在 adsorption 层)。"""
        try:
            proj = self._adsorption.load_project((path or '').strip())
            if proj is None:
                return {'ok': False, 'file': None,
                        'error': '项目不存在或 project.yaml 已被移动'}
            out = (save_to or '').strip()
            if not out:
                return {'ok': False, 'file': None, 'error': '未指定导出路径'}
            s = self._adsorption.delta_e_rows(proj)
            result = self._adsorption.export_csv(proj, s, out)
            return {'ok': True, 'file': str(result), 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'file': None, 'error': str(e)}

    def proj_report(self, path, save_to):
        """完整项目报告(镜像 project_tab._on_report);同步执行,耗时长在 JS 侧提示等待。

        load_project → 无成员作业防呆(report_full._member_dirs)→ generate_project_report
        (proj, save_to, config=load_config())。config 读失败降级为 {}(同 _on_report)。
        """
        try:
            proj = self._adsorption.load_project((path or '').strip())
            if proj is None:
                return {'ok': False, 'file': None,
                        'error': '项目不存在或 project.yaml 已被移动'}
            out = (save_to or '').strip()
            if not out:
                return {'ok': False, 'file': None, 'error': '未指定报告路径'}
            if not self._rf()._member_dirs(proj):
                return {'ok': False, 'file': None, 'error': '项目无成员作业'}
            try:
                cfg = self._config.load_config()
            except Exception:                             # noqa: BLE001
                cfg = {}
            result = self._rf().generate_project_report(proj, out, config=cfg)
            return {'ok': True, 'file': str(result), 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'file': None, 'error': str(e)}

    # ── 论文级出图(原生 matplotlib 引擎,不依赖 Origin/POV-Ray) ────────────────
    @staticmethod
    def _ads_short(member_name, proj_name) -> str:
        """构型成员名 → 吸附质短名:剥掉 create_project 的 '{项目名}_ads_' 前缀。"""
        prefix = f'{proj_name}_ads_'
        n = str(member_name)
        return n[len(prefix):] if n.startswith(prefix) else n

    def _proj_delta_data(self, proj):
        """项目 → (吸附质短名列表, ΔE 列表(None=未完成), delta 汇总 dict)。"""
        s = self._adsorption.delta_e_rows(proj)
        name = str(proj.get('name') or '')
        rows = s.get('rows') or []
        shorts = [self._ads_short(r['name'], name) for r in rows]
        des = [r['delta_e'] for r in rows]
        return shorts, des, s

    def _proj_fed(self, proj, summary):
        """项目 → Li-S 放电路径 fed;成功 (fed, None),失败 (None, 中文原因)。"""
        try:
            cfg = self._config.load_config()
        except Exception:                                 # noqa: BLE001
            cfg = {}
        mol_dir = (cfg or {}).get('lis_molecules_dir') or ''
        if not mol_dir or not os.path.isdir(str(mol_dir)):
            return None, '未配置分子库目录 lis_molecules_dir(config),无法算 ΔG 台阶'
        _state, e_slab = summary['slab']
        if e_slab is None:
            return None, '清洁表面未完成,无法算 ΔG 台阶'
        try:
            fed = self._fe().path_from_project_and_molecules(
                summary['rows'], e_slab=e_slab, molecules_dir=str(mol_dir))
            return fed, None
        except ValueError as e:
            return None, str(e)

    def proj_figures(self, path, kinds=None, save_to=None):
        """单项目论文级出图(原生引擎)。kinds ⊂ {'bar','table','ladder'},缺省全选。

        bar/table 只用已完成的 ΔE 行;ladder 需 config.lis_molecules_dir 分子库。
        某类图缺数据只记 skipped(kind+中文原因),不拖垮其他图。
        返回 {'ok','files','skipped','out_dir','error'}。
        """
        try:
            proj = self._adsorption.load_project((path or '').strip())
            if proj is None:
                return {'ok': False, 'files': [], 'skipped': [], 'out_dir': None,
                        'error': '项目不存在或 project.yaml 已被移动'}
            try:
                nc = self._nc()
            except ImportError:
                return {'ok': False, 'files': [], 'skipped': [], 'out_dir': None,
                        'error': '未安装 matplotlib/numpy(原生出图可选依赖):'
                                 'pip install matplotlib numpy 后重试'}
            kinds = [str(k) for k in (kinds or ['bar', 'table', 'ladder'])]
            shorts, des, summary = self._proj_delta_data(proj)
            pname = str(proj.get('name') or '') or '项目'
            out_dir = (save_to or '').strip() or os.path.join(
                str(proj.get('root') or os.path.dirname(str(path))), 'figures')
            os.makedirs(out_dir, exist_ok=True)

            files, skipped = [], []
            done = [(s, d) for s, d in zip(shorts, des) if d is not None]
            data = {'adsorbates': [s for s, _ in done],
                    'substrates': {pname: [d for _, d in done]}}
            for kind in kinds:
                if kind in ('bar', 'table') and not done:
                    skipped.append({'kind': kind,
                                    'reason': '无已完成的 ΔE(需构型+清洁表面+参考全 DONE)'})
                    continue
                if kind == 'bar':
                    files += nc.adsorption_bar(
                        data, os.path.join(out_dir, 'adsorption_bar.png'),
                        negative_up=True)
                elif kind == 'table':
                    files += nc.energy_matrix_table(
                        data, os.path.join(out_dir, 'delta_e_table.png'))
                elif kind == 'ladder':
                    fed, reason = self._proj_fed(proj, summary)
                    if fed is None:
                        skipped.append({'kind': 'ladder', 'reason': reason})
                        continue
                    title = (f'Li-S discharge path ($U_L$ = {fed["u_l"]:.2f} V)'
                             if fed.get('u_l') is not None else 'Li-S discharge path')
                    files += nc.free_energy_ladder(
                        [{'name': pname, 'G': [st['G'] for st in fed['steps']]}],
                        os.path.join(out_dir, 'free_energy_ladder.png'),
                        step_labels=[st['label'] for st in fed['steps']],
                        pds_index=fed.get('pds_index'), title=title)
                else:
                    skipped.append({'kind': kind, 'reason': '未知图类型'})
            return {'ok': True, 'files': files, 'skipped': skipped,
                    'out_dir': out_dir, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'files': [], 'skipped': [], 'out_dir': None,
                    'error': str(e)}

    def proj_compare_figures(self, paths, kinds=None, save_to=None):
        """多项目对比出图。kinds ⊂ {'heatmap','scaling','volcano'},缺省 heatmap。

        heatmap:催化剂(项目)×吸附质 ΔE 矩阵;scaling:两个共同吸附质 ΔE 线性标度
        (需 ≥3 个项目同时具备);volcano:x=共同吸附质 ΔE 描述符,y=各项目放电路径
        U_L(需分子库,≥3 点)。缺数据记 skipped 不拖垮其他图。
        """
        try:
            try:
                nc = self._nc()
            except ImportError:
                return {'ok': False, 'files': [], 'skipped': [], 'out_dir': None,
                        'error': '未安装 matplotlib/numpy(原生出图可选依赖):'
                                 'pip install matplotlib numpy 后重试'}
            kinds = [str(k) for k in (kinds or ['heatmap'])]
            projs = []
            for p in (paths or []):
                proj = self._adsorption.load_project(str(p or '').strip())
                if proj is None:
                    continue
                shorts, des, summary = self._proj_delta_data(proj)
                projs.append({'name': str(proj.get('name') or '') or '项目',
                              'proj': proj, 'summary': summary,
                              'de': dict(zip(shorts, des))})
            if len(projs) < 2:
                return {'ok': False, 'files': [], 'skipped': [], 'out_dir': None,
                        'error': '多项目对比至少需要选中 2 个有效项目'}
            cols: list = []
            for pr in projs:                     # 首见序并集
                for s in pr['de']:
                    if s not in cols:
                        cols.append(s)
            out_dir = (save_to or '').strip() or os.path.join(
                str(projs[0]['proj'].get('root') or '.'), 'compare_figures')
            os.makedirs(out_dir, exist_ok=True)

            files, skipped = [], []
            for kind in kinds:
                if kind == 'heatmap':
                    values = [[pr['de'].get(c) for c in cols] for pr in projs]
                    if not any(v is not None for row in values for v in row):
                        skipped.append({'kind': 'heatmap',
                                        'reason': '所有项目均无已完成的 ΔE'})
                        continue
                    files += nc.heatmap_matrix(
                        {'rows': [pr['name'] for pr in projs], 'cols': cols,
                         'values': values},
                        os.path.join(out_dir, 'delta_e_heatmap.png'))
                elif kind == 'scaling':
                    pair = self._best_scaling_pair(projs, cols)
                    if pair is None:
                        skipped.append({'kind': 'scaling',
                                        'reason': '不足 3 个项目同时具备两个共同吸附质的 ΔE'})
                        continue
                    a, b, xs, ys, labels = pair
                    files += nc.scaling_relation(
                        xs, ys, os.path.join(out_dir, 'scaling_relation.png'),
                        xlabel=f'$\\Delta E$({a}) (eV)',
                        ylabel=f'$\\Delta E$({b}) (eV)', labels=labels)
                elif kind == 'volcano':
                    pts, reason = self._volcano_points(projs, cols)
                    if pts is None:
                        skipped.append({'kind': 'volcano', 'reason': reason})
                        continue
                    sp, points = pts
                    files += nc.volcano_plot(
                        points, os.path.join(out_dir, 'volcano.png'),
                        descriptor_label=f'$\\Delta E$({sp}) (eV)',
                        activity_label='$U_L$ (V)')
                else:
                    skipped.append({'kind': kind, 'reason': '未知图类型'})
            return {'ok': True, 'files': files, 'skipped': skipped,
                    'out_dir': out_dir, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'files': [], 'skipped': [], 'out_dir': None,
                    'error': str(e)}

    @staticmethod
    def _best_scaling_pair(projs, cols):
        """选覆盖最好的两个吸附质:≥3 个项目同时有 ΔE → (a,b,xs,ys,labels);否则 None。"""
        best = None
        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                a, b = cols[i], cols[j]
                pts = [(pr['de'].get(a), pr['de'].get(b), pr['name']) for pr in projs]
                pts = [(x, y, n) for x, y, n in pts if x is not None and y is not None]
                if len(pts) >= 3 and (best is None or len(pts) > len(best[2])):
                    best = (a, b, pts)
        if best is None:
            return None
        a, b, pts = best
        return (a, b, [x for x, _, _ in pts], [y for _, y, _ in pts],
                [n for _, _, n in pts])

    def _volcano_points(self, projs, cols):
        """火山图数据:描述符=覆盖最好的共同吸附质 ΔE,活性=各项目 U_L。

        → ((species, points), None) 或 (None, 中文原因)。"""
        uls, reasons = {}, []
        for pr in projs:
            fed, reason = self._proj_fed(pr['proj'], pr['summary'])
            if fed is not None and fed.get('u_l') is not None:
                uls[pr['name']] = fed['u_l']
            elif reason:
                reasons.append(f"{pr['name']}: {reason}")
        if len(uls) < 3:
            why = ';'.join(reasons[:3]) or '有 U_L 的项目不足'
            return None, f'火山图需 ≥3 个项目具备放电路径 U_L(当前 {len(uls)} 个)。{why}'
        best_sp, best_pts = None, []
        for sp in cols:
            pts = [{'name': pr['name'], 'x': pr['de'].get(sp),
                    'y': uls.get(pr['name'])} for pr in projs]
            pts = [p for p in pts if p['x'] is not None and p['y'] is not None]
            if len(pts) > len(best_pts):
                best_sp, best_pts = sp, pts
        if len(best_pts) < 3:
            return None, '不足 3 个项目同时具备描述符 ΔE 与 U_L'
        return (best_sp, best_pts), None

    # ── 文件/目录选择(web 无原生 input;pywebview 延迟 import,测试注入 dialog_fn) ──
    def pick_file(self, kind='poscar'):
        try:
            if self._dialog_fn is not None:
                path = self._dialog_fn(kind)
            else:
                import webview                            # 延迟:测试永不 import
                res = webview.windows[0].create_file_dialog(webview.OPEN_DIALOG)
                path = res[0] if res else None
            return {'path': path or None}
        except Exception as e:                            # noqa: BLE001
            return {'path': None, 'error': str(e)}

    def pick_dir(self):
        try:
            if self._dialog_fn is not None:
                path = self._dialog_fn('dir')
            else:
                import webview                            # 延迟:测试永不 import
                res = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
                path = res[0] if res else None
            return {'path': path or None}
        except Exception as e:                            # noqa: BLE001
            return {'path': None, 'error': str(e)}

    # ── 打开本地目录(传文件路径 → 打开其所在目录) ──
    def open_dir(self, path):
        try:
            p = path
            # 传入文件路径(如报告 html / CSV)→ 打开其所在目录(输出反馈统一口径)
            if p and os.path.isfile(p):
                p = os.path.dirname(p)
            if not p or not os.path.isdir(p):
                return {'ok': False, 'error': '目录不存在'}
            if sys.platform == 'win32':
                os.startfile(p)  # noqa: S606
            else:
                import subprocess
                subprocess.Popen(['xdg-open', p])
            return {'ok': True}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    # ── 设置页(config 读写 + keyring 状态;全走注入的 config/ai_analysis,可测) ─────────
    @staticmethod
    def _parse_ideal_window(lo, hi):
        """两个数(可留空)→ [lo, hi] 或 None。都空 → None;非法数字 → ValueError。"""
        def _num(x):
            s = str(x if x is not None else '').strip()
            return None if s == '' else float(s)
        a, b = _num(lo), _num(hi)
        if a is None and b is None:
            return None
        if a is None or b is None:
            raise ValueError('ideal_window 需同时给两个数字,或都留空')
        return [a, b]

    def _ui_defaults(self, ui):
        """ui 小节 → 外观/自动化设置(带默认值)。"""
        return {
            'theme': ui.get('theme') if ui.get('theme') in _THEMES else 'classic',
            'autopilot': bool(ui.get('autopilot', True)),
            'poll_interval': int(ui.get('poll_interval', 10) or 10),
            'autopilot_continue': bool(ui.get('autopilot_continue', True)),
            'autopilot_fetch': bool(ui.get('autopilot_fetch', True)),
            'autopilot_report': bool(ui.get('autopilot_report', True)),
        }

    def settings_get(self):
        """设置页汇总读:LLM(不含密钥明文,仅 key_saved)/提示词/数据路径/外观自动化。"""
        try:
            cfg = self._config.load_config()
            llm = dict(cfg.get('llm') or {})
            ui = self._config.get_ui_state(cfg)
            iw = cfg.get('ideal_window')
            key_saved = False
            try:
                key_saved = self._ai().load_api_key() is not None
            except Exception:                             # noqa: BLE001 keyring 不可用 → 视作未设置
                key_saved = False
            preset = llm.get('prompt_preset')
            is_default = not (isinstance(preset, str) and preset.strip())
            return {
                'ok': True, 'error': None,
                'llm': {'base_url': llm.get('base_url', '') or '',
                        'model': llm.get('model', '') or '',
                        'allow_external': bool(llm.get('allow_external', False)),
                        'key_saved': bool(key_saved)},   # 绝不回显密钥,仅掩码状态
                'prompt': {'text': preset if not is_default else self._ai().DEFAULT_PROMPT_PRESET,
                           'is_default': is_default},
                'paths': {'potcar_lib_root': cfg.get('potcar_lib_root', '') or '',
                          'lis_molecules_dir': cfg.get('lis_molecules_dir', '') or '',
                          'ideal_window': list(iw) if isinstance(iw, (list, tuple)) else []},
                'ui': self._ui_defaults(ui),
            }
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    def llm_save(self, base_url, model, allow_external=None):
        """保存 LLM 端点/模型(可选联网开关)到 config.llm(密钥不走这里)。"""
        try:
            cfg = self._config.load_config()
            llm = dict(cfg.get('llm') or {})
            llm['base_url'] = (base_url or '').strip()
            llm['model'] = (model or '').strip()
            if allow_external is not None:
                llm['allow_external'] = bool(allow_external)
            cfg['llm'] = llm
            self._config.save_config(cfg)
            return {'ok': True, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    def llm_key_save(self, key):
        """密钥进 keyring(经 ai_analysis.save_api_key),绝不落 config/yaml。"""
        try:
            k = (key or '').strip()
            if not k:
                return {'ok': False, 'error': 'API 密钥不能为空'}
            self._ai().save_api_key(k)
            return {'ok': True, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    def llm_key_status(self):
        """密钥是否已存(仅掩码状态,绝不回显)。"""
        try:
            return {'saved': self._ai().load_api_key() is not None}
        except Exception:                                 # noqa: BLE001
            return {'saved': False}

    def llm_test(self, base_url='', model='', transport=None):
        """测试连接:用当前(未保存的表单)端点/模型 + keyring 密钥发一个极小请求。"""
        try:
            res = self._ai().probe(base_url=(base_url or '').strip(),
                                   model=(model or '').strip(), transport=transport)
            return {'ok': bool(res.get('ok')), 'error': res.get('error')}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    def prompt_get(self):
        """取生效中的分析提示词预设(config llm.prompt_preset,缺 → 内置默认)。"""
        try:
            llm = dict(self._config.load_config().get('llm') or {})
            preset = llm.get('prompt_preset')
            is_default = not (isinstance(preset, str) and preset.strip())
            return {'ok': True, 'error': None, 'is_default': is_default,
                    'text': preset if not is_default else self._ai().DEFAULT_PROMPT_PRESET}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'text': '', 'error': str(e)}

    def prompt_save(self, text):
        """保存自定义分析提示词到 config.llm.prompt_preset。"""
        try:
            if not (text or '').strip():
                return {'ok': False, 'error': '提示词不能为空'}
            cfg = self._config.load_config()
            llm = dict(cfg.get('llm') or {})
            llm['prompt_preset'] = text
            cfg['llm'] = llm
            self._config.save_config(cfg)
            return {'ok': True, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    def prompt_reset(self):
        """恢复默认:删除 config.llm.prompt_preset,返回内置默认文本。"""
        try:
            cfg = self._config.load_config()
            llm = dict(cfg.get('llm') or {})
            llm.pop('prompt_preset', None)
            cfg['llm'] = llm
            self._config.save_config(cfg)
            return {'ok': True, 'error': None, 'text': self._ai().DEFAULT_PROMPT_PRESET}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    def paths_save(self, potcar_lib_root='', lis_molecules_dir='',
                   ideal_window_lo=None, ideal_window_hi=None):
        """保存数据路径(赝势库 / Li-S 分子库 / 理想窗口)到 config 对应键。"""
        try:
            iw = self._parse_ideal_window(ideal_window_lo, ideal_window_hi)
            cfg = self._config.load_config()
            cfg['potcar_lib_root'] = (potcar_lib_root or '').strip()
            cfg['lis_molecules_dir'] = (lis_molecules_dir or '').strip()
            if iw is None:
                cfg.pop('ideal_window', None)
            else:
                cfg['ideal_window'] = iw
            self._config.save_config(cfg)
            return {'ok': True, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    def theme_set(self, name):
        """三选主题即时生效(写 config ui.theme;非法值回落 classic)。"""
        try:
            n = str(name or '').strip().lower()
            if n not in _THEMES:
                n = 'classic'
            self._config.set_ui_state(theme=n)
            return {'ok': True, 'theme': n, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    def autopilot_save(self, autopilot=None, poll_interval=None,
                       autopilot_continue=None, autopilot_fetch=None,
                       autopilot_report=None):
        """保存自动驾驶开关/间隔/子开关到 config ui.*(None 的字段不改)。"""
        try:
            kv = {}
            if autopilot is not None:
                kv['autopilot'] = bool(autopilot)
            if poll_interval is not None:
                pi = int(poll_interval)
                kv['poll_interval'] = pi if pi in (5, 10, 15) else 10
            if autopilot_continue is not None:
                kv['autopilot_continue'] = bool(autopilot_continue)
            if autopilot_fetch is not None:
                kv['autopilot_fetch'] = bool(autopilot_fetch)
            if autopilot_report is not None:
                kv['autopilot_report'] = bool(autopilot_report)
            self._config.set_ui_state(**kv)
            return {'ok': True, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    # ── 自动驾驶管线 ─────────────────────────────────────────────────────────────
    def _autopilot_cfg(self):
        try:
            ui = self._config.get_ui_state()
        except Exception:                                 # noqa: BLE001
            ui = {}
        d = self._ui_defaults(ui if isinstance(ui, dict) else {})
        return {'enabled': d['autopilot'], 'interval': d['poll_interval'],
                'cont': d['autopilot_continue'], 'fetch': d['autopilot_fetch'],
                'report': d['autopilot_report']}

    def _member_states(self, proj):
        """项目成员目录 → [{'dir','state','restartable','rounds'}](经注入的 manifest 读)。"""
        members = proj.get('members') or {}
        dirs = []
        if members.get('clean_slab'):
            dirs.append(members['clean_slab'])
        if members.get('gas_ref'):
            dirs.append(members['gas_ref'])
        dirs += [d for d in (members.get('configs') or []) if d]
        out = []
        for d in dirs:
            m = self._manifest.load_manifest(d)
            if m is None:
                out.append({'dir': d, 'state': None, 'restartable': False, 'rounds': 0})
                continue
            res = m.get('results') or {}
            dgn = res.get('diagnosis') or {}
            out.append({'dir': d, 'state': m.get('state'),
                        'restartable': bool(dgn.get('restartable')),
                        'rounds': int(res.get('continue_rounds', 0) or 0)})
        return out

    def _project_stage(self, states, has_marker):
        """成员状态 + 报告标记 → (stage, needs_human, recover_round)。规则见 pipeline_status。"""
        all_states = [s['state'] for s in states]
        needs_human = (any(s == 'NEEDS_HUMAN' for s in all_states)
                       or any(s['state'] in _TERMINAL_FAIL and not s['restartable']
                              for s in states))
        recover_round = max([s['rounds'] for s in states
                             if s['restartable'] and s['state'] in _TERMINAL_FAIL] or [0])
        if not states:
            stage = 'generate'
        elif any(s == 'CREATED' for s in all_states):
            stage = 'submit'
        elif any(s in _ACTIVE_STATES for s in all_states):
            stage = 'monitor'
        elif any(s['restartable'] and s['state'] in _TERMINAL_FAIL for s in states):
            stage = 'recover'
        elif all_states and all(s == 'DONE' for s in all_states):
            stage = 'report_done' if has_marker else 'analysis'
        elif any(s in _TERMINAL_FAIL for s in all_states):
            stage = 'recover'                             # 有终态失败但不可续算 → 卡恢复需人工
        else:
            stage = 'generate'
        return stage, needs_human, recover_round

    def pipeline_status(self):
        """每项目管线阶段:生成→提交→监控→恢复(n/3)→分析→报告完成;NEEDS_HUMAN 红旗。"""
        try:
            projs = []
            for pp in self._adsorption.list_projects():
                try:
                    proj = self._adsorption.load_project(pp)
                    if proj is None:
                        continue
                    states = self._member_states(proj)
                    has_marker = bool(proj.get('autopilot_report_done'))
                    stage, needs_human, rr = self._project_stage(states, has_marker)
                    projs.append({
                        'path': pp, 'name': proj.get('name', '') or '',
                        'stage': stage, 'stage_index': _STAGES.index(stage),
                        'stages': list(_STAGES), 'needs_human': needs_human,
                        'recover_round': rr,
                        'done': sum(1 for s in states if s['state'] == 'DONE'),
                        'total': len(states),
                    })
                except Exception:                         # noqa: BLE001 单个坏项目跳过
                    continue
            return {'ok': True, 'projects': projs, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'projects': [], 'error': str(e)}

    @staticmethod
    def _base(d):
        return os.path.basename(os.path.normpath(str(d)))

    def _tick_cluster(self, name, prof, pw, ap, events, errors):
        """单集群一轮:刷新在队作业(+ 可选续算 restartable / 拉回新 DONE)。成功返回 True。"""
        entries = list(self._ledger.load_all())
        before_done = {d for d, m in entries
                       if m and m.get('cluster') == name and m.get('state') == 'DONE'}
        targets = [d for d, m in entries
                   if m and m.get('scheduler_job_id') and m.get('cluster') == name
                   and m.get('state') in ('SUBMITTED', 'QUEUED', 'RUNNING')]
        if targets:
            res = self._bo().refresh_batch(prof, pw, targets, False)
            if res.get('needs_trust'):
                events.append({'kind': 'skip',
                               'text': f'集群「{name}」主机指纹未信任,跳过本轮'})
                return False
            for d, note in (res.get('results') or []):
                events.append({'kind': 'refresh', 'text': f'{self._base(d)}:{note}'})
        entries2 = list(self._ledger.load_all())
        # 续算:刷新后 restartable 终态且 attempts<3 → 自动续算(复用 filter_continuable)
        if ap['cont']:
            try:
                cdirs = [d for d, m in entries2 if m and m.get('cluster') == name]
                eligible, _sk = self._bo().filter_continuable(cdirs)
                if eligible:
                    cres = self._bo().continue_batch(prof, pw, eligible, False)
                    for d, ok, msg in (cres.get('results') or []):
                        events.append({'kind': 'continue', 'text': f'{self._base(d)}:{msg}'})
            except Exception as e:                        # noqa: BLE001 续算失败仅记 event 不中断
                events.append({'kind': 'continue', 'text': f'集群「{name}」续算跳过:{e}'})
        # 拉回:本轮新变 DONE 的作业(after − before)→ 轻量拉回
        if ap['fetch']:
            try:
                after_done = {d for d, m in entries2
                              if m and m.get('cluster') == name and m.get('state') == 'DONE'}
                newly = list(after_done - before_done)
                if newly:
                    fres = self._bo().fetch_batch(prof, pw, newly, False)
                    for d, ok, msg in (fres.get('results') or []):
                        events.append({'kind': 'fetch', 'text': f'{self._base(d)}:{msg}'})
            except Exception as e:                        # noqa: BLE001 拉回失败仅记 event 不中断
                events.append({'kind': 'fetch', 'text': f'集群「{name}」拉回跳过:{e}'})
        return True

    def _project_all_done(self, states):
        return bool(states) and all(s['state'] == 'DONE' for s in states)

    def _tick_reports(self, events, errors):
        """遍历项目:全员 DONE 且无报告标记 → 自动出图 + 报告 → 写标记 → report_done 事件。"""
        for pp in self._adsorption.list_projects():
            try:
                proj = self._adsorption.load_project(pp)
                if proj is None or proj.get('autopilot_report_done'):
                    continue
                states = self._member_states(proj)
                if not self._project_all_done(states):
                    continue
                name = proj.get('name', '') or self._base(os.path.dirname(str(pp)))
                root = proj.get('root') or os.path.dirname(str(pp))
                report_dir = os.path.join(root, 'report')
                os.makedirs(report_dir, exist_ok=True)
                fig = self.proj_figures(pp, ['bar', 'table', 'ladder'], report_dir)
                rep = self.proj_report(pp, os.path.join(report_dir, f'{name}_report.html'))
                if not rep.get('ok'):
                    errors.append(f'项目「{name}」自动报告失败:{rep.get("error")}')
                    continue
                # 安全的 yaml 读改写:原地打标记 + 落盘(落盘失败仅记 error,下拍再试)
                proj['autopilot_report_done'] = time.strftime('%Y-%m-%dT%H:%M:%S')
                try:
                    self._adsorption.save_project(root, proj)
                except Exception as e:                    # noqa: BLE001
                    errors.append(f'项目「{name}」报告标记落盘失败:{e}')
                events.append({'kind': 'report_done', 'project': name,
                               'report': rep.get('file'), 'figures_dir': fig.get('out_dir'),
                               'text': f'项目「{name}」报告已自动生成'})
            except Exception as e:                        # noqa: BLE001 单项目失败不拖垮其他
                errors.append(f'项目报告自动化异常:{e}')

    def pipeline_tick(self):
        """服务端一拍编排(幂等,全部复用现有方法):逐集群刷新/续算/拉回 + 自动报告。

        返回 {'ok','events':[{kind,text,...}],'errors':[...],'last_sync','synced'}。
        无凭据的 profile 记 skip event;各阶段失败只记 event/error,绝不抛到 JS。
        """
        events, errors, synced = [], [], 0
        ap = self._autopilot_cfg()
        try:
            profiles = self._profiles.load_profiles()
        except Exception as e:                            # noqa: BLE001
            profiles = {}
            errors.append(f'读取集群配置失败:{e}')
        for name, prof in profiles.items():
            try:
                if getattr(prof, 'auth', 'key') == 'password':
                    pw = self._secrets.get_password(name)
                    if not pw:
                        events.append({'kind': 'skip',
                                       'text': f'集群「{name}」无保存凭据,跳过本轮同步'})
                        continue
                else:
                    pw = None
                if self._tick_cluster(name, prof, pw, ap, events, errors):
                    synced += 1
            except Exception as e:                        # noqa: BLE001 单集群失败不拖垮其他
                errors.append(f'集群「{name}」同步失败:{e}')
        if ap['report']:
            try:
                self._tick_reports(events, errors)
            except Exception as e:                        # noqa: BLE001
                errors.append(f'报告自动化失败:{e}')
        return {'ok': True, 'events': events, 'errors': errors, 'synced': synced,
                'last_sync': time.strftime('%Y-%m-%d %H:%M:%S')}
