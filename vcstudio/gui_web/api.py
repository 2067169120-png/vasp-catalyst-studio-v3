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
import tempfile
import time
import types
from dataclasses import asdict, fields as dc_fields

# 合法计算类型(决定 KPOINTS 网格);前端下拉与后端都以此为准
_CALC_TYPES = ('slab', 'bulk', 'molecule')

# 主题白名单(设置页三选);自动驾驶管线阶段序(pipeline_status 的 stage_index 取此序)
_THEMES = ('classic', 'paper', 'deep')
_STAGES = ('generate', 'submit', 'monitor', 'recover', 'analysis', 'report_done')
# 活跃(在队/在跑)状态与可续算终态:pipeline_tick/status 复用
_ACTIVE_STATES = ('UPLOADED', 'SUBMITTED', 'QUEUED', 'RUNNING')
_TERMINAL_FAIL = ('FAILED', 'UNCONVERGED')

# SAC 矩阵机时粗估系数(核时·原子⁻¹·作业⁻¹,数量级参考,可解释:Σ原子数 × 系数)
_SAC_EST_COEF = 0.8

# 图表预设 → 数据装配路线(render_figure_preset 据此从项目数据组装或降级 skipped):
#   _FIG_FROM_DELTA  能量学:柱状图/矩阵表/热图,取 delta_e_rows 的已完成 ΔE
#   _FIG_LADDER      电池/电化学:自由能台阶,取 freeenergy/reactions 路径
#   _FIG_MULTI       标度/火山:单项目无法出(需多催化剂对比),降级 skipped
#   _FIG_NEEDS_PARSE 电子结构/NEB/差分电荷/收敛测试:需对应解析产物,项目层无从组装 → skipped
_FIG_FROM_DELTA = ('adsorption_bar', 'energy_matrix_table', 'delta_e_heatmap')
_FIG_LADDER = ('free_energy_ladder', 'free_energy_ladder_multi')
_FIG_MULTI = ('scaling_relation', 'volcano')
_FIG_NEEDS_PARSE = {
    'pdos': 'PDOS 需电子结构静态计算的投影态密度解析产物;请在④页对 DONE 作业派生 PDOS 后单独出图。',
    'cohp': 'COHP 键强图需 LOBSTER 的 COHPCAR 解析产物;项目层暂无从组装。',
    'neb_profile': 'NEB 剖面需 CI-NEB 各像能量(过渡态搜索产物);项目层暂无从组装。',
    'charge_profile': '差分电荷面平均需 CHGDIFF 的面平均序列;请先派生差分电荷计算。',
    'convergence_curve': '收敛测试曲线需真实的截断能/K 点收敛历史值,该图型将在后续版本提供。',
}


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
                 native_charts_mod=None, freeenergy_mod=None, ai_analysis_mod=None,
                 freq_builder_mod=None, estatic_mod=None, sac_mods=None,
                 spin_mod=None, reactions_mod=None, campaign_mods=None,
                 scenarios_mod=None, i18n_mod=None, figure_presets_mod=None,
                 draftpack_mod=None, ai_paper_mod=None, engines_mod=None,
                 slab_builder_mod=None):
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
        from vcstudio.shared import scenarios as _scen
        from vcstudio.shared import i18n as _i18n_m
        from vcstudio.project import figure_presets as _figp
        from vcstudio.generate import slab_builder as _slabb
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
        # Phase A 新引擎(派生计算/SAC 矩阵/多自旋/通用反应/campaign):一律延迟导入,
        # 测试注入假件即全离线可测。sac_mods/campaign_mods 为"模块束"(SimpleNamespace/包)。
        self._freq_builder = freq_builder_mod
        self._estatic = estatic_mod
        self._sac_mods = sac_mods
        self._spin = spin_mod
        self._reactions = reactions_mod
        self._campaign = campaign_mods
        # v3.1 GUI 总集成:研究场景 / i18n / 图表预设 / slab_builder 为纯或轻模块,即时导入
        # (测试注入假件);draftpack/ai_paper/engines 牵扯较重依赖,延迟到用时 import。
        self._scenarios = scenarios_mod or _scen
        self._i18n = i18n_mod or _i18n_m
        self._figpresets = figure_presets_mod or _figp
        self._slab_builder = slab_builder_mod or _slabb
        self._draftpack = draftpack_mod
        self._ai_paper = ai_paper_mod
        self._engines = engines_mod

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

    def _fb(self):
        """频率作业生成端(F14)延迟加载。"""
        if self._freq_builder is None:
            from vcstudio.generate import freq_builder
            self._freq_builder = freq_builder
        return self._freq_builder

    def _es(self):
        """电子结构静态派生延迟加载。"""
        if self._estatic is None:
            from vcstudio.generate import estatic
            self._estatic = estatic
        return self._estatic

    def _sac(self):
        """SAC 建模束(sac_builder + molecules + sites)延迟加载(numpy 相邻,重)。"""
        if self._sac_mods is None:
            from vcstudio.generate import molecules, sac_builder, sites
            self._sac_mods = types.SimpleNamespace(
                sac_builder=sac_builder, molecules=molecules, sites=sites)
        return self._sac_mods

    def _sp(self):
        """多自旋并跑引擎(F3/F10)延迟加载。"""
        if self._spin is None:
            from vcstudio.project import spin_scan
            self._spin = spin_scan
        return self._spin

    def _rx(self):
        """通用反应预设库延迟加载。"""
        if self._reactions is None:
            from vcstudio.project import reactions
            self._reactions = reactions
        return self._reactions

    def _cmp(self):
        """campaign 文件式控制面延迟加载(不可用/未安装 → ImportError,调用方降级)。"""
        if self._campaign is None:
            import vcstudio.campaign as campaign
            self._campaign = campaign
        return self._campaign

    def _dp(self):
        """收尾流水线 Draft-Ready 延迟加载(牵扯 adsorption/methods_text/incar_builder)。"""
        if self._draftpack is None:
            from vcstudio.project import draftpack
            self._draftpack = draftpack
        return self._draftpack

    def _aip(self):
        """AI 论文智能体延迟加载(牵扯 campaign 全套 + ai_analysis)。"""
        if self._ai_paper is None:
            from vcstudio.project import ai_paper
            self._ai_paper = ai_paper
        return self._ai_paper

    def _eng(self):
        """多引擎适配层延迟加载(4 个 Backend + poscar/structure_view)。"""
        if self._engines is None:
            import vcstudio.engines as engines
            self._engines = engines
        return self._engines

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

    @staticmethod
    def _match_species_energy(species_bare, short_energies):
        """物种裸名 → 项目构型能量:短名精确匹配(或短名以物种名+分隔符起头)。

        取最稳(能量最低)的匹配。'OH' 不会误配 'OOH'(下一字符为字母);'O_top' 配 'O'。
        无匹配 → None。
        """
        low = str(species_bare).lower()
        if not low:
            return None
        best = None
        for short, e in short_energies.items():
            s = str(short).lower()
            hit = s == low or (s.startswith(low)
                               and (len(s) == len(low) or not s[len(low)].isalnum()))
            if hit and (best is None or e < best):
                best = e
        return best

    def _proj_fed_preset(self, proj, summary, preset_key):
        """通用反应预设台阶(F16):项目构型/分子能量 → free_energy_path。

        → (fed, None, 标题) 或 (None, 中文原因, 标题)。构型名映射为物种能量,
        缺失物种/分子能量集中报"缺 {物种} 的能量"。
        """
        try:
            spec = self._rx().get_preset(preset_key)
        except Exception as e:                            # noqa: BLE001 未知预设
            return None, f'未知反应预设「{preset_key}」:{e}', str(preset_key)
        ptitle = spec.get('description') or spec.get('name') or str(preset_key)
        # 分子/参考态能量:复用 Li-S 分子库扫描(mol_*/molecule_* 子目录 OSZICAR)
        try:
            cfg = self._config.load_config()
        except Exception:                                 # noqa: BLE001
            cfg = {}
        mol_dir = (cfg or {}).get('lis_molecules_dir') or ''
        mol_e = {}
        if mol_dir and os.path.isdir(str(mol_dir)):
            try:
                mol_e = self._fe().load_molecule_energies(str(mol_dir))
            except Exception:                             # noqa: BLE001 分子库坏 → 视作空
                mol_e = {}
        _state, e_slab = summary['slab']
        # 项目构型短名 → 最稳能量(仅 DONE)
        pname = str(proj.get('name') or '')
        short_e = {}
        for r in (summary.get('rows') or []):
            if r.get('e_config') is None or r.get('state') != 'DONE':
                continue
            short = self._ads_short(r['name'], pname)
            e = r['e_config']
            if short not in short_e or e < short_e[short]:
                short_e[short] = e
        energies, missing = {}, []

        def _need(key, energy):
            if energy is None:
                if key not in missing:
                    missing.append(key)
            else:
                energies[key] = energy

        for st in spec.get('steps') or []:
            sp = st.get('species', '')
            if sp in energies or sp in missing:
                continue
            bare = str(sp).rstrip('*')
            if bare == '':                    # 干净基底 '*' → 清洁表面能量
                _need(sp, e_slab)
            else:
                _need(sp, self._match_species_energy(bare, short_e))
        for st in spec.get('steps') or []:
            for g in (st.get('coadsorbates_or_gas') or []):
                nm = g.get('name')
                if nm and nm not in energies and nm not in missing:
                    _need(nm, mol_e.get(nm))
        # 参比电对定标所需分子(Li/Li+ 需 Li2S/S8;RHE 需 H2)
        for nm in (('Li2S', 'S8') if spec.get('electrode') == 'Li/Li+'
                   else ('H2',) if spec.get('electrode') == 'RHE' else ()):
            if nm not in energies and nm not in missing:
                _need(nm, mol_e.get(nm))
        if missing:
            return None, '缺 ' + '、'.join(missing) + ' 的能量(对应构型/分子需 DONE)', ptitle
        try:
            fed = self._fe().free_energy_path(spec, energies)
            return fed, None, ptitle
        except ValueError as e:
            return None, str(e), ptitle

    def proj_figures(self, path, kinds=None, save_to=None, preset_key=None):
        """单项目论文级出图(原生引擎)。kinds ⊂ {'bar','table','ladder'},缺省全选。

        bar/table 只用已完成的 ΔE 行;ladder 需 config.lis_molecules_dir 分子库。
        preset_key 为空(默认)→ ladder 走既有 Li-S 放电路径(向后兼容,行为完全不变);
        给非空反应预设 key(见 reaction_presets)→ ladder 改走通用 free_energy_path 引擎,
        项目构型名映射为物种能量,映射不上的物种记 skipped 原因"缺 {物种} 的能量"。
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
                    if preset_key:
                        fed, reason, ptitle = self._proj_fed_preset(
                            proj, summary, preset_key)
                    else:
                        fed, reason = self._proj_fed(proj, summary)
                        ptitle = 'Li-S discharge path'
                    if fed is None:
                        skipped.append({'kind': 'ladder', 'reason': reason})
                        continue
                    title = (f'{ptitle} ($U_L$ = {fed["u_l"]:.2f} V)'
                             if fed.get('u_l') is not None else ptitle)
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

    # ── Phase A 引擎:公共小工具 ────────────────────────────────────────────────
    @staticmethod
    def _rotations_tuple(n):
        """取向数 → 绕 z 采样角度(度)。1→(0,);2→(0,180);4→(0,90,180,270);其余均分。"""
        try:
            n = int(n or 1)
        except (TypeError, ValueError):
            n = 1
        n = max(1, n)
        if n == 1:
            return (0,)
        if n == 2:
            return (0, 180)
        if n == 4:
            return (0, 90, 180, 270)
        step = 360.0 / n
        return tuple(int(round(step * i)) for i in range(n))

    @staticmethod
    def _filter_sites(sites, sites_mode):
        """位点策略:'metal_top' 仅金属顶位;其余(全位点)返回全部。"""
        if str(sites_mode) == 'metal_top':
            return [s for s in sites if s.get('kind') == 'top_metal']
        return list(sites)

    @staticmethod
    def _poscar_natoms(text):
        """POSCAR 文本 → 原子数(取首个全整数行即计数行之和;解析不出 → 0)。"""
        for ln in (text or '').splitlines()[5:9]:
            parts = ln.split()
            if parts and all(p.isdigit() for p in parts):
                return sum(int(p) for p in parts)
        return 0

    def _job_from_text(self, poscar_text, incar_path, out_dir, lib):
        """把 POSCAR 文本经既有四件套链落 out_dir + 写 job.yaml + 入台账 →(job_dir, warnings)。

        build 失败向上抛(调用方记 skipped);job.yaml/台账写失败只并入 warnings,
        绝不撤销已生成的四件套(同 gen_run 口径)。
        """
        with tempfile.TemporaryDirectory() as td:
            ppath = os.path.join(td, 'POSCAR')
            with open(ppath, 'w', encoding='utf-8') as f:
                f.write(poscar_text)
            payload = self._job_builder.build_job_dir(
                ppath, incar_path, out_dir, calc_type='slab', lib_root=(lib or None))
        job_dir = payload['out_dir']
        warnings = list(payload.get('warnings') or [])
        try:
            self._manifest.create_from_build(
                job_dir, payload, poscar_path=os.path.join(job_dir, 'POSCAR'),
                validate=True)
            self._ledger.register(job_dir)
        except Exception as e:                            # noqa: BLE001
            warnings.append(f'job.yaml/台账写入失败(不影响四件套):{e}')
        return job_dir, warnings

    # ── 派生计算(作业页):频率(ZPE)/ 电子结构静态 ─────────────────────────────
    def derive_freq(self, job_dir, out_root=None):
        """派生频率作业(F14,ZPE):调 freq_builder 从 CONTCAR+INCAR 派生 → 入台账。

        out_root 缺省 → 与原作业同级;命名 {原名}_freq。
        返回 {'ok','job_dir','changes'(逐条改动 dict),'warnings','error'}。
        """
        try:
            d = (job_dir or '').strip()
            if not d or not os.path.isdir(d):
                return {'ok': False, 'job_dir': None, 'changes': [],
                        'warnings': [], 'error': '作业目录不存在'}
            base = os.path.basename(os.path.normpath(d))
            parent = (out_root or '').strip() or os.path.dirname(os.path.normpath(d))
            out_dir = os.path.join(parent, f'{base}_freq')
            res = self._fb().build_freq_job(d, out_dir)
            warnings = list(res.get('warnings') or [])
            try:
                self._ledger.register(res['out_dir'])
            except Exception as e:                        # noqa: BLE001
                warnings.append(f'台账登记失败(不影响已派生目录):{e}')
            return {'ok': True, 'job_dir': res['out_dir'],
                    'changes': list(res.get('changes') or []),
                    'warnings': warnings, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'job_dir': None, 'changes': [],
                    'warnings': [], 'error': str(e)}

    def derive_estatic(self, job_dir, kinds, out_root=None):
        """派生电子结构静态作业:kinds ⊂ {'pdos','bader','chgdiff'},每类一份 → 入台账。

        命名 {原名}_st_{kind}('_st' 为静态标记)。单类失败只记 skipped,不拖垮其他类。
        返回 {'ok','jobs':[{'kind','job_dir','changes','warnings'}],'skipped':[...],'error'}。
        """
        try:
            d = (job_dir or '').strip()
            if not d or not os.path.isdir(d):
                return {'ok': False, 'jobs': [], 'skipped': [],
                        'error': '作业目录不存在'}
            valid = ('pdos', 'bader', 'chgdiff')
            req = [str(k).strip().lower() for k in (kinds or []) if str(k).strip()]
            picks = [k for k in valid if k in req]        # 保序去重、只留合法
            skipped = [{'kind': k, 'reason': '不支持的静态类型(仅 pdos/bader/chgdiff)'}
                       for k in req if k not in valid]
            if not picks:
                return {'ok': False, 'jobs': [], 'skipped': skipped,
                        'error': '未选择有效的静态类型(pdos/bader/差分电荷)'}
            base = os.path.basename(os.path.normpath(d))
            parent = (out_root or '').strip() or os.path.dirname(os.path.normpath(d))
            es = self._es()
            jobs = []
            for kind in picks:
                out_dir = os.path.join(parent, f'{base}_st_{kind}')
                try:
                    res = es.build_static_job(d, out_dir, purpose=kind)
                except Exception as e:                    # noqa: BLE001 单类失败隔离
                    skipped.append({'kind': kind, 'reason': str(e)})
                    continue
                warnings = list(res.get('warnings') or [])
                try:
                    self._ledger.register(res['out_dir'])
                except Exception as e:                    # noqa: BLE001
                    warnings.append(f'台账登记失败:{e}')
                jobs.append({'kind': kind, 'job_dir': res['out_dir'],
                             'changes': list(res.get('changes') or []),
                             'warnings': warnings})
            return {'ok': bool(jobs), 'jobs': jobs, 'skipped': skipped, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'jobs': [], 'skipped': [], 'error': str(e)}

    # ── SAC 批量建模(生成页) ───────────────────────────────────────────────────
    def molecule_list(self):
        """内置分子库清单(SAC 吸附质 chips 取此)→
        {'ok','molecules':[{name,formula,spin_hint}],'error'}。"""
        try:
            mols = self._sac().molecules
            out = []
            for n in mols.list_molecules():
                try:
                    info = mols.molecule_info(n)
                except Exception:                         # noqa: BLE001
                    info = {}
                out.append({'name': n, 'formula': info.get('formula') or n,
                            'spin_hint': info.get('spin_hint')})
            return {'ok': True, 'molecules': out, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'molecules': [], 'error': str(e)}

    def sac_matrix_preview(self, metals, templates, adsorbates,
                           sites_mode='all', rotations=1):
        """SAC 候选矩阵预览 →
        {'ok','n_slabs','n_configs','n_total_jobs','estimate_note','names','error'}。

        金属 × 模板 × 吸附质 × 位点 × 取向。位点数按模板配位(与金属无关),故每模板建一
        样本 SAC 数位点。机时为粗估(Σ原子数 × 系数),文案注明"粗估"。
        """
        empty = {'ok': False, 'n_slabs': 0, 'n_configs': 0, 'n_total_jobs': 0,
                 'estimate_note': '', 'names': []}
        try:
            metals = [str(m).strip() for m in (metals or []) if str(m).strip()]
            templates = [str(t).strip() for t in (templates or []) if str(t).strip()]
            adsorbates = [str(a).strip() for a in (adsorbates or []) if str(a).strip()]
            if not metals or not templates:
                return {**empty, 'error': '请至少选择一个金属与一个模板'}
            sac = self._sac()
            rots = self._rotations_tuple(rotations)
            n_rot = len(rots)
            n_sites_by_t, natoms_by_t = {}, {}
            for t in templates:
                built = sac.sac_builder.build_sac(t, metals[0])
                sites = sac.sites.enumerate_sac_sites(
                    built['poscar'], built['site_indices'])
                n_sites_by_t[t] = len(self._filter_sites(sites, sites_mode))
                natoms_by_t[t] = self._poscar_natoms(built['poscar'])
            n_slabs = len(metals) * len(templates)
            n_ads = len(adsorbates)
            n_configs = sum(len(metals) * n_sites_by_t[t] * n_ads * n_rot
                            for t in templates)
            n_total = n_slabs + n_configs
            names = [f'{metal}@{t}_clean' for metal in metals for t in templates]
            names += [f'{metal}@{t}_ads_{ads}'
                      for metal in metals for t in templates for ads in adsorbates]
            names = names[:20]
            total_atoms = sum(
                len(metals) * natoms_by_t[t] * (1 + n_sites_by_t[t] * n_ads * n_rot)
                for t in templates)
            est = round(total_atoms * _SAC_EST_COEF)
            estimate_note = (
                f'粗估机时 ≈ {est} 核时(按 Σ原子数({total_atoms})× {_SAC_EST_COEF} '
                f'核时·原子⁻¹ 粗估:{n_slabs} 清洁面 + {n_configs} 吸附构型作业);'
                f'仅数量级参考,实际随体系/收敛差异大。')
            return {'ok': True, 'n_slabs': n_slabs, 'n_configs': n_configs,
                    'n_total_jobs': n_total, 'estimate_note': estimate_note,
                    'names': names, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {**empty, 'error': str(e)}

    def sac_matrix_generate(self, metals, templates, adsorbates, sites_mode='all',
                            rotations=1, incar_path='', out_root=''):
        """SAC 候选矩阵生成:逐个 build_sac → 清洁 slab 作业 + 每个吸附构型作业(用
        job_builder 四件套链,INCAR 用用户提供的)→ 入台账;按 slab 建吸附能项目(清洁面
        + 构型族);引擎可用则同步注册 campaign(记预估机时)。

        返回 {'ok','created','project_paths','skipped','campaign','error'}。
        """
        try:
            metals = [str(m).strip() for m in (metals or []) if str(m).strip()]
            templates = [str(t).strip() for t in (templates or []) if str(t).strip()]
            adsorbates = [str(a).strip() for a in (adsorbates or []) if str(a).strip()]
            incar = (incar_path or '').strip()
            root = (out_root or '').strip()
            errs = []
            if not metals or not templates:
                errs.append('请至少选择一个金属与一个模板')
            if not incar or not os.path.isfile(incar):
                errs.append('共享 INCAR 不存在')
            if not root:
                errs.append('未选输出根目录')
            if errs:
                return {'ok': False, 'created': 0, 'project_paths': [],
                        'skipped': [], 'campaign': None, 'error': ';'.join(errs)}
            lib = ''
            try:
                lib = self._config.load_config().get('potcar_lib_root', '') or ''
            except Exception:                             # noqa: BLE001
                pass
            sac = self._sac()
            rots = self._rotations_tuple(rotations)
            created, project_paths, skipped, tasks = 0, [], [], []
            for metal in metals:
                for template in templates:
                    label = f'{metal}@{template}'
                    try:
                        built = sac.sac_builder.build_sac(template, metal)
                    except Exception as e:                # noqa: BLE001 单体系建模失败隔离
                        skipped.append({'name': label, 'reason': f'建模失败:{e}'})
                        continue
                    base = f'{metal}_{template}'.replace('+', 'p')
                    proj_root = os.path.join(root, base)
                    try:
                        cdir, _cw = self._job_from_text(
                            built['poscar'], incar,
                            os.path.join(proj_root, f'{base}_clean'), lib)
                    except Exception as e:                # noqa: BLE001
                        skipped.append({'name': f'{label} 清洁面', 'reason': str(e)})
                        continue
                    created += 1
                    tasks.append({'id': os.path.basename(cdir), 'dir': cdir,
                                  'natoms': self._poscar_natoms(built['poscar'])})
                    try:
                        sites = self._filter_sites(sac.sites.enumerate_sac_sites(
                            built['poscar'], built['site_indices']), sites_mode)
                    except Exception as e:                # noqa: BLE001
                        skipped.append({'name': f'{label} 位点', 'reason': str(e)})
                        sites = []
                    config_dirs = []
                    for ads in adsorbates:
                        for site in sites:
                            try:
                                texts = sac.sites.place_adsorbate(
                                    built['poscar'], ads, site, rotations=rots)
                            except Exception as e:        # noqa: BLE001 拒绝/失败隔离
                                skipped.append(
                                    {'name': f'{ads}@{site.get("name")}',
                                     'reason': str(e)})
                                continue
                            for i, text in enumerate(texts):
                                deg = rots[i] if i < len(rots) else i
                                cfg_dir = os.path.join(
                                    proj_root,
                                    f'{base}_ads_{ads}_{site.get("name")}_r{deg}')
                                try:
                                    jd, _w = self._job_from_text(text, incar,
                                                                 cfg_dir, lib)
                                except Exception as e:    # noqa: BLE001
                                    skipped.append(
                                        {'name': os.path.basename(cfg_dir),
                                         'reason': str(e)})
                                    continue
                                config_dirs.append(jd)
                                tasks.append({'id': os.path.basename(jd), 'dir': jd,
                                              'natoms': self._poscar_natoms(text)})
                                created += 1
                    if config_dirs:
                        pp = self._save_sac_project(base, proj_root, cdir, config_dirs)
                        if pp:
                            project_paths.append(pp)
            campaign = self._register_sac_campaign(root, tasks)
            return {'ok': True, 'created': created, 'project_paths': project_paths,
                    'skipped': skipped, 'campaign': campaign, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'created': 0, 'project_paths': [],
                    'skipped': [], 'campaign': None, 'error': str(e)}

    def _save_sac_project(self, name, proj_root, clean_dir, config_dirs):
        """按 slab 建吸附能项目(清洁面 + 构型族)并登记注册表 → project.yaml 路径|None。"""
        try:
            proj = {'name': name, 'root': proj_root,
                    'members': {'clean_slab': clean_dir, 'gas_ref': None,
                                'configs': list(config_dirs)}}
            self._adsorption.save_project(proj_root, proj)
            pp = os.path.join(proj_root, 'project.yaml')
            try:
                self._adsorption.register_project(pp)
            except Exception:                             # noqa: BLE001 注册失败不致命
                pass
            return pp
        except Exception:                                 # noqa: BLE001 建项目失败不拖垮生成
            return None

    def _register_sac_campaign(self, root, tasks):
        """引擎可用则为本批生成注册 campaign(每作业一任务节点 + 记预估机时)→ 目录|None。

        campaign 不可用/失败一律降级为 None,绝不拖垮生成。
        """
        if not tasks:
            return None
        try:
            cmp = self._cmp()
        except ImportError:
            return None
        try:
            cid = 'sac-' + time.strftime('%Y%m%d-%H%M%S')
            nodes = [cmp.new_task(t['id'], 'relax', job_dir=t['dir']) for t in tasks]
            camp = cmp.init_campaign(
                root, cid, tasks=nodes, title=f'SAC 批量建模 {len(tasks)} 作业',
                hypothesis='SAC 候选矩阵筛选')
            cdir = camp['dir']
            for t in tasks:
                try:
                    est = cmp.estimate_job(t.get('natoms', 1), 1, 'relax', 32)
                    cmp.record_estimate(cdir, t['id'], est)
                except Exception:                         # noqa: BLE001 单条预估失败跳过
                    continue
            self._register_campaign_dir(cdir)
            return cdir
        except Exception:                                 # noqa: BLE001 注册失败降级
            return None

    def _register_campaign_dir(self, cdir):
        """把 campaign 目录记入 config ui.campaign_dirs(供仪表盘 campaign_list 发现)。"""
        try:
            ui = self._config.get_ui_state()
            dirs = list((ui or {}).get('campaign_dirs') or [])
            if cdir not in dirs:
                dirs.append(cdir)
                self._config.set_ui_state(campaign_dirs=dirs)
        except Exception:                                 # noqa: BLE001 记不进注册表不致命
            pass

    # ── 多自旋并跑(生成页 / 作业页) ────────────────────────────────────────────
    def spin_family_generate(self, poscar, incar, out_root):
        """多自旋并跑(F3):结构+INCAR → NM/LS/HS 家族作业组(spin_scan)→ 入台账。

        返回 {'ok','variants':[{name,job_dir,magmom,changes,warnings}],'error'}。
        """
        try:
            pos = (poscar or '').strip()
            inc = (incar or '').strip()
            root = (out_root or '').strip()
            errs = []
            if not pos or not os.path.isfile(pos):
                errs.append('POSCAR 文件不存在')
            if not inc or not os.path.isfile(inc):
                errs.append('INCAR 文件不存在')
            if not root:
                errs.append('未选输出根目录')
            if errs:
                return {'ok': False, 'variants': [], 'error': ';'.join(errs)}
            lib = ''
            try:
                lib = self._config.load_config().get('potcar_lib_root', '') or ''
            except Exception:                             # noqa: BLE001
                pass
            base_name = os.path.splitext(os.path.basename(pos))[0] or 'spin'
            with tempfile.TemporaryDirectory() as td:
                base_dir = os.path.join(td, base_name)
                payload = self._job_builder.build_job_dir(
                    pos, inc, base_dir, calc_type='slab', lib_root=(lib or None))
                try:
                    self._manifest.create_from_build(
                        payload['out_dir'], payload, poscar_path=pos, validate=True)
                except Exception:                         # noqa: BLE001 基座 manifest 失败不致命
                    pass
                variants = self._sp().build_spin_variants(base_dir, root)
            out = []
            for v in variants:
                warnings = list(v.get('warnings') or [])
                try:
                    self._ledger.register(v['out_dir'])
                except Exception as e:                    # noqa: BLE001
                    warnings.append(f'台账登记失败:{e}')
                out.append({'name': v.get('name'), 'job_dir': v.get('out_dir'),
                            'magmom': v.get('magmom'),
                            'changes': list(v.get('changes') or []),
                            'warnings': warnings})
            return {'ok': True, 'variants': out, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'variants': [], 'error': str(e)}

    def spin_family_compare(self, dirs):
        """同家族(_nm/_ls/_hs)全 DONE 后判自旋基态 + 磁矩审计(spin_scan)。

        返回 {'ok','ground'(pick_ground_state 结果),'audits':[{dir,name,...}],'error'}。
        """
        try:
            dirs = [str(d).strip() for d in (dirs or []) if str(d).strip()]
            if not dirs:
                return {'ok': False, 'ground': None, 'audits': [],
                        'error': '未提供作业目录'}
            sp = self._sp()
            ground = sp.pick_ground_state(dirs)
            audits = []
            for d in dirs:
                name = os.path.basename(os.path.normpath(d))
                outcar_path = os.path.join(d, 'OUTCAR')
                if not os.path.isfile(outcar_path):
                    audits.append({'dir': d, 'name': name, 'audited': False,
                                   'warning': '缺 OUTCAR,无法审计磁矩'})
                    continue
                with open(outcar_path, 'r', encoding='utf-8', errors='replace') as f:
                    outcar = f.read()
                m = self._manifest.load_manifest(d) or {}
                init = (m.get('inputs') or {}).get('spin_magmom')
                a = self._sp().audit_magmom(outcar, init)
                audits.append({'dir': d, 'name': name, **a})
            return {'ok': True, 'ground': ground, 'audits': audits, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'ground': None, 'audits': [], 'error': str(e)}

    # ── 通用反应预设(项目页 ΔG 台阶) ──────────────────────────────────────────
    def reaction_presets(self):
        """通用反应预设 → {'ok','presets':[{key,name,description}],'error'}(供 ΔG 台阶下拉)。"""
        try:
            presets = self._rx().list_presets()
            out = [{'key': key, 'name': spec.get('name') or key,
                    'description': spec.get('description') or ''}
                   for key, spec in presets.items()]
            return {'ok': True, 'presets': out, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'presets': [], 'error': str(e)}

    # ── campaign 最小接线(仪表盘) ─────────────────────────────────────────────
    def campaign_list(self):
        """仪表盘批次(campaign)统计 → {'ok','available','campaigns':[...],'error'}。

        每个 campaign {name,dir,n_tasks,states:{completed,validated,accepted}(累计口径,
        completed⊇validated⊇accepted),budget:{estimated,cap}}。campaign 模块不可用/无批次
        → available=False 或空列表(前端据此隐藏区块)。全程 try/except 降级,绝不拖垮仪表盘。
        """
        try:
            try:
                cmp = self._cmp()
            except ImportError:
                return {'ok': True, 'available': False, 'campaigns': [], 'error': None}
            try:
                ui = self._config.get_ui_state()
            except Exception:                             # noqa: BLE001
                ui = {}
            dirs = list((ui or {}).get('campaign_dirs') or [])
            campaigns = []
            for cdir in dirs:
                try:
                    camp = cmp.load_campaign(cdir)
                    if camp is None:
                        continue
                    summary = cmp.progress_summary(camp)
                    acc = int(summary.get('accepted', 0) or 0)
                    val = int(summary.get('validated', 0) or 0) + acc
                    comp = int(summary.get('completed', 0) or 0) + val
                    meta = camp.get('meta') or {}
                    try:
                        bud = cmp.load_budget(cdir)
                        estimated = round(float(sum(
                            float(v or 0.0)
                            for v in (bud.get('estimates') or {}).values())), 2)
                    except Exception:                     # noqa: BLE001 预算读失败给 0
                        estimated = 0.0
                    campaigns.append({
                        'name': meta.get('title') or meta.get('id')
                        or os.path.basename(str(cdir)),
                        'dir': cdir,
                        'n_tasks': int(summary.get('total', 0) or 0),
                        'states': {'completed': comp, 'validated': val,
                                   'accepted': acc},
                        'budget': {'estimated': estimated,
                                   'cap': meta.get('budget_core_hours')},
                    })
                except Exception:                         # noqa: BLE001 单个坏 campaign 跳过
                    continue
            return {'ok': True, 'available': True, 'campaigns': campaigns,
                    'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'available': False, 'campaigns': [], 'error': str(e)}

    # ── 研究场景(界面裁剪:显隐页面/卡片/图型/引擎/反应预设) ────────────────────
    @staticmethod
    def _scenario_view(sc):
        """场景 dict → 前端消费视图(JSON-safe;含显隐判定所需全部键)。"""
        return {
            'key': sc.get('key'), 'name': sc.get('name'),
            'description': sc.get('description'),
            'pages': list(sc.get('pages') or []),
            'cards': sc.get('cards') or {},
            'figure_preset_order': list(sc.get('figure_preset_order') or []),
            'reaction_presets': list(sc.get('reaction_presets') or []),
            'engines': list(sc.get('engines') or []),
            'defaults': sc.get('defaults') or {},
            'ai_context': sc.get('ai_context') or '',
        }

    def scenario_list(self):
        """全部内置研究场景(展示序)→ {'ok','scenarios':[view...],'error'}(首启场景选择模态用)。"""
        try:
            out = [self._scenario_view(s) for s in self._scenarios.list_scenarios()]
            return {'ok': True, 'scenarios': out, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'scenarios': [], 'error': str(e)}

    def scenario_get(self):
        """当前生效场景 + 是否已显式配置(config 无 ui.scenario → configured=False,首启弹模态)。"""
        try:
            try:
                cfg = self._config.load_config()
            except Exception:                             # noqa: BLE001
                cfg = {}
            ui = self._config.get_ui_state(cfg)
            configured = bool(isinstance(ui, dict) and ui.get('scenario'))
            sc = self._scenarios.active_scenario(cfg)
            return {'ok': True, 'configured': configured,
                    'scenario': self._scenario_view(sc), 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'configured': False, 'scenario': None, 'error': str(e)}

    def scenario_set(self, key):
        """切换研究场景(写 config ui.scenario)→ 返回新场景视图供前端即时 applyScenario。"""
        try:
            k = (key or '').strip()
            self._scenarios.set_scenario(k)
            sc = self._scenarios.get_scenario(k)
            return {'ok': True, 'key': sc.get('key'),
                    'scenario': self._scenario_view(sc), 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'scenario': None, 'error': str(e)}

    # ── 界面语言(i18n:zh 基准 + en 回落) ──────────────────────────────────────
    def lang_get(self):
        """当前界面语言 + 可选语言列表 → {'ok','lang','available','error'}。"""
        try:
            try:
                cfg = self._config.load_config()
            except Exception:                             # noqa: BLE001
                cfg = {}
            return {'ok': True, 'lang': self._i18n.current_lang(cfg),
                    'available': self._i18n.available_langs(), 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'lang': 'zh', 'available': [], 'error': str(e)}

    def lang_set(self, lang):
        """切换界面语言(写 config ui.lang);前端据返回值重拉 i18n_dict 换文案。"""
        try:
            lg = (lang or '').strip() or 'zh'
            self._i18n.set_lang(lg)
            return {'ok': True, 'lang': lg, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    def i18n_dict(self, lang):
        """某语言的完整词典(回落补齐后)→ {'ok','lang','dict','error'};供前端一次性注入替换。"""
        try:
            lg = (lang or '').strip() or 'zh'
            return {'ok': True, 'lang': lg,
                    'dict': self._i18n.export_for_js(lg), 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'lang': lang, 'dict': {}, 'error': str(e)}

    # ── 论文出图:图表预设画廊 + 一键出图(数据后端装配) ───────────────────────
    def figure_presets(self):
        """图表预设清单(含缩略 SVG + 参数 schema)→ 论文出图页画廊按分类渲染。"""
        try:
            fp = self._figpresets
            presets = []
            for p in fp.list_presets():
                key = p.get('key')
                try:
                    thumb = fp.get_preset(key).get('thumbnail_svg', '')
                except Exception:                         # noqa: BLE001 缩略缺失不致命
                    thumb = ''
                presets.append({
                    'key': key, 'name': p.get('name'), 'category': p.get('category'),
                    'description': p.get('description', ''),
                    'required_data': p.get('required_data', ''),
                    'thumbnail_svg': thumb,
                    'params_schema': p.get('params_schema') or {},
                })
            return {'ok': True, 'presets': presets,
                    'categories': fp.categories(), 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'presets': [], 'categories': [], 'error': str(e)}

    def render_figure_preset(self, key, project_path, params=None):
        """按图表预设 + 当前项目出图:后端从项目数据装配 → figure_presets.render_preset 出图。

        能量学类(柱/表/热图)取 delta_e_rows 的已完成 ΔE;台阶图取 freeenergy/reactions 路径
        (params.reaction_preset 为空 → Li-S 放电,向后兼容);标度/火山需多催化剂对比、电子结构/
        NEB/差分电荷需专门解析产物 → 项目层无从组装,返回中文 skipped 原因(绝不假装出图)。
        返回 {'ok','files','skipped':[{kind,reason}],'out_dir','provenance','error'}。
        """
        empty = {'ok': True, 'files': [], 'skipped': [], 'out_dir': None,
                 'provenance': None, 'error': None}
        try:
            k = str(key or '').strip()
            params = dict(params or {})
            fp = self._figpresets
            try:
                fp.get_preset(k)                          # 校验预设 key 存在(未知即抛)
            except Exception:                             # noqa: BLE001 未知预设
                return {**empty, 'ok': False, 'error': f'未知图表预设:{k}'}
            # 需专门解析产物的图型(电子结构/NEB/差分电荷/收敛):项目层无从组装 → skipped
            if k in _FIG_NEEDS_PARSE:
                return {**empty, 'skipped': [{'kind': k, 'reason': _FIG_NEEDS_PARSE[k]}]}
            # 标度/火山:单项目无法出(需多催化剂横比)→ skipped 明说去④页多项目对比
            if k in _FIG_MULTI:
                return {**empty, 'skipped': [{'kind': k,
                        'reason': '标度关系/火山图需多催化剂横向对比(≥3 组);'
                                  '请在④结果分析页用「多项目对比」出图。'}]}
            proj = self._adsorption.load_project((project_path or '').strip())
            if proj is None:
                return {**empty, 'ok': False,
                        'error': '项目不存在或 project.yaml 已被移动'}
            reaction_preset = params.pop('reaction_preset', None) or None
            save_to = (params.pop('save_to', None) or '').strip()
            shorts, des, summary = self._proj_delta_data(proj)
            pname = str(proj.get('name') or '') or '项目'
            out_dir = save_to or os.path.join(
                str(proj.get('root') or os.path.dirname(str(project_path))), 'figures')

            data, skipped = None, None
            if k in _FIG_FROM_DELTA:
                done = [(s, d) for s, d in zip(shorts, des) if d is not None]
                if not done:
                    skipped = '无已完成的 ΔE(需构型 + 清洁表面 + 参考全 DONE)'
                elif k == 'delta_e_heatmap':
                    data = {'rows': [pname], 'cols': [s for s, _ in done],
                            'values': [[d for _, d in done]]}
                else:
                    data = {'adsorbates': [s for s, _ in done],
                            'substrates': {pname: [d for _, d in done]}}
            elif k in _FIG_LADDER:
                if reaction_preset:
                    fed, reason, ptitle = self._proj_fed_preset(
                        proj, summary, reaction_preset)
                else:
                    fed, reason = self._proj_fed(proj, summary)
                    ptitle = 'Li-S discharge path'
                if fed is None:
                    skipped = reason
                else:
                    data = {'paths': [{'name': pname,
                                       'G': [st['G'] for st in fed['steps']]}],
                            'step_labels': [st['label'] for st in fed['steps']],
                            'pds_index': fed.get('pds_index')}
                    params.setdefault('title', ptitle)
            else:
                skipped = '该预设暂不支持从项目数据一键出图'

            if skipped is not None:
                return {**empty, 'skipped': [{'kind': k, 'reason': skipped}]}
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, k + '.png')
            files = fp.render_preset(k, data, out_path, **params)
            prov = None
            try:
                prov = fp.preset_provenance(
                    k, {'project': pname, 'source': 'delta_e_rows'}, params=params)
            except Exception:                             # noqa: BLE001 溯源失败不挡出图
                prov = None
            return {'ok': True, 'files': list(files), 'skipped': [],
                    'out_dir': out_dir, 'provenance': prov, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {**empty, 'ok': False, 'error': str(e)}

    # ── 一键成稿包(Draft-Ready 收尾流水线) ────────────────────────────────────
    def draft_ready(self, path, out):
        """一键成稿包:SI zip + 三线表 + 口径稽核 + Methods → {'ok','summary','issues','products'}。

        稽核不过/SI 缺关键输入仍产全套工件但 ok=False(DRAFT_READY.md 首行醒目);products 汇
        总全部落盘文件供前端列出 + open_dir。
        """
        try:
            proj = self._adsorption.load_project((path or '').strip())
            if proj is None:
                return {'ok': False, 'summary': None, 'issues': [], 'products': [],
                        'issues_total': 0, 'summary_path': None, 'out_dir': None,
                        'error': '项目不存在或 project.yaml 已被移动'}
            o = (out or '').strip()
            if not o:
                return {'ok': False, 'summary': None, 'issues': [], 'products': [],
                        'issues_total': 0, 'summary_path': None, 'out_dir': None,
                        'error': '未指定成稿包输出目录'}
            res = self._dp().draft_ready(proj, o)
            report = res.get('report') or {}
            products = []
            si = report.get('si_package') or {}
            if si.get('zip_path'):
                products.append(si['zip_path'])
            products += list((report.get('tables') or {}).get('files') or [])
            products += list((report.get('methods') or {}).get('files') or [])
            if res.get('summary_path'):
                products.append(res['summary_path'])
            return {'ok': bool(res.get('ok')), 'summary': res.get('summary'),
                    'issues': list(res.get('issues') or []),
                    'issues_total': int(res.get('issues_total', 0) or 0),
                    'products': products, 'summary_path': res.get('summary_path'),
                    'out_dir': o, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'summary': None, 'issues': [], 'products': [],
                    'issues_total': 0, 'summary_path': None, 'out_dir': None,
                    'error': str(e)}

    # ── AI 助手:论文 → 规格表 → 计划 → 实例化(转发 ai_paper) ─────────────────
    def ai_extract(self, source, transport=None):
        """论文/文本 → 可编辑规格表(LLM 只抽文本 + 每格带出处;allow_external 门在引擎内)。

        返回 {'ok','spec','issues','error'};transport 沿用 ai_analysis(测试注入假件离线可测)。
        """
        try:
            res = self._aip().extract_spec(source, transport=transport)
            return {'ok': bool(res.get('ok')), 'spec': res.get('spec'),
                    'issues': list(res.get('issues') or []),
                    'error': res.get('error')}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'spec': None, 'issues': [], 'error': str(e)}

    def ai_plan(self, spec):
        """规格表 → 实例化计划(纯计划对象,不落盘)→ {'ok','plan','jobs_estimate','est_core_hours'}。

        est_core_hours 经 dry-run instantiate 取机时预算闸估值(不写盘);warnings 透传计划告警。
        """
        try:
            res = self._aip().plan_campaign(spec)
            plan = res.get('plan') or {}
            est = None
            try:
                dry = self._aip().instantiate(plan, '(dry-run)', dry_run=True)
                est = ((dry.get('gates') or {}).get('budget') or {}).get(
                    'estimated_core_hours')
            except Exception:                             # noqa: BLE001 估值失败不挡计划
                est = None
            return {'ok': bool(res.get('ok')), 'plan': plan,
                    'jobs_estimate': plan.get('jobs_estimate'),
                    'est_core_hours': est,
                    'warnings': list(plan.get('warnings') or []), 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'plan': None, 'jobs_estimate': 0,
                    'est_core_hours': None, 'warnings': [], 'error': str(e)}

    def ai_instantiate(self, plan, out_root, opts=None):
        """计划 → campaign(门禁序列:机时闸 + 单点先行 + 写盘 + 账本;全自动与交互共用)。

        opts 透传 instantiate 的 confirm_token/budget_cap_hours/dry_run 等;成功且非 dry_run 则把
        campaign 目录记入仪表盘发现表。返回 {'ok','created','campaign_dir','gates','pilot','awaiting'}。
        """
        try:
            root = (out_root or '').strip()
            if not root:
                return {'ok': False, 'created': [], 'campaign_dir': None,
                        'gates': {}, 'error': '未指定输出根目录'}
            res = self._aip().instantiate(plan, root, **dict(opts or {}))
            if res.get('campaign_dir') and not res.get('dry_run'):
                self._register_campaign_dir(res['campaign_dir'])
            return {'ok': bool(res.get('ok')), 'created': list(res.get('created') or []),
                    'campaign_dir': res.get('campaign_dir'),
                    'gates': res.get('gates') or {}, 'pilot': res.get('pilot'),
                    'awaiting': res.get('awaiting'), 'error': res.get('error')}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'created': [], 'campaign_dir': None,
                    'gates': {}, 'error': str(e)}

    # ── 多引擎适配(生成页引擎选择器) ──────────────────────────────────────────
    _ENGINE_DISPLAY = {'vasp': 'VASP', 'cp2k': 'CP2K', 'gaussian': 'Gaussian',
                       'castep': 'CASTEP'}

    def engine_list(self, scenario_key=None):
        """已注册引擎清单(按场景标可见)→ {'ok','engines':[{key,name,visible,experimental}],'default'}。

        VASP 为主引擎(默认、非实验性、恒可见);CP2K/Gaussian/CASTEP 为文件级适配(实验性)。
        给 scenario_key 时按场景 engines 白名单标 visible(vasp 恒可见),否则全部 visible。
        """
        try:
            names = self._eng().available_engines()
            vis = None
            if scenario_key is not None:
                sc = self._scenarios.get_scenario(scenario_key)
                vis = set(sc.get('engines') or []) | {'vasp'}
            engines = [{'key': n, 'name': self._ENGINE_DISPLAY.get(n, n.upper()),
                        'experimental': n != 'vasp',
                        'visible': True if vis is None else (n in vis)}
                       for n in names]
            return {'ok': True, 'engines': engines, 'default': 'vasp', 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'engines': [], 'default': 'vasp', 'error': str(e)}

    def engine_generate(self, engine, params, out_dir):
        """非 VASP 引擎输入生成(文件级适配):简化参数表单 → CalcSpec → backend.generate_inputs。

        params:{poscar(结构文件路径),task,functional,cutoff_ev,kpoints([a,b,c]|None),spin,charge,
        periodic,dispersion,multiplicity}。validate 的自洽问题并入 issues(不静默),生成失败兜 error。
        返回 {'ok','files','warnings','issues','out_dir','error'}。
        """
        try:
            params = dict(params or {})
            eng = (engine or '').strip()
            out = (out_dir or '').strip()
            if not eng:
                return {'ok': False, 'files': [], 'warnings': [], 'issues': [],
                        'out_dir': None, 'error': '未指定引擎'}
            if not out:
                return {'ok': False, 'files': [], 'warnings': [], 'issues': [],
                        'out_dir': None, 'error': '未选输出目录'}
            poscar = (params.get('poscar') or '').strip()
            if not poscar or not os.path.isfile(poscar):
                return {'ok': False, 'files': [], 'warnings': [], 'issues': [],
                        'out_dir': None, 'error': '结构文件(POSCAR)不存在'}
            with open(poscar, 'r', encoding='utf-8', errors='replace') as f:
                structure = f.read()
            mods = self._eng()
            kpts = params.get('kpoints')
            if isinstance(kpts, (list, tuple)) and len(kpts) >= 3:
                try:
                    kpts = tuple(int(x) for x in kpts[:3])
                except (TypeError, ValueError):
                    kpts = None
            else:
                kpts = None
            cutoff = params.get('cutoff_ev')
            spec = mods.CalcSpec(
                structure=structure, task=(params.get('task') or 'relax'),
                functional=(params.get('functional') or 'PBE'),
                periodic=bool(params.get('periodic', True)),
                dispersion=(params.get('dispersion') or None),
                cutoff_ev=(float(cutoff) if cutoff not in (None, '') else None),
                kpoints=kpts, spin=bool(params.get('spin', False)),
                charge=int(params.get('charge') or 0),
                multiplicity=int(params.get('multiplicity') or 1))
            issues = list(mods.validate(spec))
            backend = mods.get_backend(eng)
            os.makedirs(out, exist_ok=True)
            res = backend.generate_inputs(spec, out)
            return {'ok': True, 'files': list(res.get('files') or []),
                    'warnings': list(res.get('warnings') or []),
                    'issues': issues, 'out_dir': out, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'files': [], 'warnings': [], 'issues': [],
                    'out_dir': None, 'error': str(e)}

    def engine_nonequiv(self, src, dst):
        """跨引擎方法不等价清单(不静默翻译)→ {'ok','report':[中文逐条],'error'}(生成页告警条)。"""
        try:
            report = self._eng().nonequivalence_report(src, dst)
            return {'ok': True, 'report': list(report), 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'report': [], 'error': str(e)}

    # ── 结构查看/编辑器(结构建模页;api 只做 解析/写出/固定层 纯转换,注入可测) ─────
    def _state_to_poscar(self, state, *, sd=None):
        """编辑器 JS 状态 {elements,coords,lattice} → (POSCAR 文本, perm)。

        按物种(首见序)分块以满足 VASP 计数契约;perm 为 state 原子序 → 写出序的映射,供固定层
        标志回填。sd 给定(每 state 原子 bool,True=冻结)且有真值时写 Selective dynamics。
        """
        elements = list((state or {}).get('elements') or [])
        coords = [list(c) for c in ((state or {}).get('coords') or [])]
        lattice = [list(r) for r in ((state or {}).get('lattice') or [])]
        if not elements or len(elements) != len(coords):
            raise ValueError('结构状态无效(元素数与坐标数不一致)')
        if len(lattice) != 3 or any(len(r) < 3 for r in lattice):
            raise ValueError('晶格矢量须为 3×3')
        uniq = []
        for e in elements:
            if e not in uniq:
                uniq.append(e)
        perm = [i for sp in uniq for i, e in enumerate(elements) if e == sp]
        counts = [sum(1 for e in elements if e == sp) for sp in uniq]
        use_sd = isinstance(sd, list) and any(bool(x) for x in sd)
        lines = ['vcstudio structure editor', '1.0']
        for r in lattice:
            lines.append('  ' + ' '.join(f'{float(x):.10f}' for x in r[:3]))
        lines.append('  ' + ' '.join(str(s) for s in uniq))
        lines.append('  ' + ' '.join(str(c) for c in counts))
        if use_sd:
            lines.append('Selective dynamics')
        lines.append('Cartesian')
        for idx in perm:
            x, y, z = coords[idx][:3]
            row = f'  {float(x):.10f} {float(y):.10f} {float(z):.10f}'
            if use_sd:
                fixed = idx < len(sd) and bool(sd[idx])
                row += '  ' + ('F F F' if fixed else 'T T T')
            lines.append(row)
        return '\n'.join(lines) + '\n', perm

    @staticmethod
    def _parse_sd_fixed(poscar_text):
        """带 Selective dynamics 的 POSCAR → 每原子是否冻结(首标志 F)的 bool 列表(写出序)。"""
        lines = poscar_text.splitlines()
        if not (len(lines) > 7 and lines[7].strip()[:1].lower() == 's'):
            return []
        try:
            natoms = sum(int(x) for x in lines[6].split())
        except (ValueError, IndexError):
            natoms = 0
        out = []
        for k in range(natoms):
            idx = 9 + k
            parts = lines[idx].split() if idx < len(lines) else []
            out.append(len(parts) >= 6 and parts[3].strip().upper() == 'F')
        return out

    def struct_load(self, path):
        """加载 POSCAR/CONTCAR → {'ok','struct':{elements,coords,lattice,natoms,formula},'vacuum'}。

        纯解析(复用 structure_view.parse_positions);真空厚度经 slab_builder。缺文件/畸形 → error。
        """
        try:
            p = (path or '').strip()
            if not p or not os.path.isfile(p):
                return {'ok': False, 'struct': None, 'vacuum': None,
                        'error': '结构文件不存在'}
            with open(p, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            parsed = self._sview.parse_positions(content)
            elements = parsed['elements']
            uniq = []
            for e in elements:
                if e not in uniq:
                    uniq.append(e)
            formula = ' '.join(f'{s}{sum(1 for e in elements if e == s)}' for s in uniq)
            vac = None
            try:
                vac = round(self._slab_builder.vacuum_thickness(content), 3)
            except Exception:                             # noqa: BLE001 真空计算失败不挡加载
                vac = None
            return {'ok': True, 'vacuum': vac,
                    'struct': {'elements': elements, 'coords': parsed['coords'],
                               'lattice': parsed['cell'], 'natoms': len(elements),
                               'formula': formula},
                    'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'struct': None, 'vacuum': None, 'error': str(e)}

    def struct_save(self, state, path):
        """编辑器状态 → POSCAR 落盘(Cartesian;state.fixed 有真值则写 Selective dynamics)。"""
        try:
            p = (path or '').strip()
            if not p:
                return {'ok': False, 'path': None, 'error': '未指定保存路径'}
            sd = (state or {}).get('fixed')
            text, _ = self._state_to_poscar(
                state, sd=sd if isinstance(sd, list) else None)
            os.makedirs(os.path.dirname(os.path.abspath(p)) or '.', exist_ok=True)
            with open(p, 'w', encoding='utf-8') as f:
                f.write(text)
            return {'ok': True, 'path': p, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'path': None, 'error': str(e)}

    def struct_fix_layers(self, state, n_layers):
        """冻结最底 n 层(调 slab_builder.fix_bottom_layers)→ 每原子冻结 bool(state 序)+ 真空厚度。

        n_layers ≤0 或 ≥ 总层数 → slab_builder 抛 ValueError,此处兜成 error。返回
        {'ok','fixed':[bool...],'fixed_count','vacuum','error'}。
        """
        try:
            n = int(n_layers)
            text, perm = self._state_to_poscar(state)
            new_text = self._slab_builder.fix_bottom_layers(text, n)
            built = self._parse_sd_fixed(new_text)
            fixed = [False] * len(perm)
            for built_i, state_i in enumerate(perm):
                if built_i < len(built):
                    fixed[state_i] = built[built_i]
            vac = None
            try:
                vac = round(self._slab_builder.vacuum_thickness(new_text), 3)
            except Exception:                             # noqa: BLE001
                vac = None
            return {'ok': True, 'fixed': fixed,
                    'fixed_count': sum(1 for x in fixed if x),
                    'vacuum': vac, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'fixed': None, 'fixed_count': 0,
                    'vacuum': None, 'error': str(e)}

    def struct_vacuum(self, state):
        """当前编辑器状态的 c 向真空层厚度(Å)→ {'ok','vacuum','error'}(编辑后实时刷新)。"""
        try:
            text, _ = self._state_to_poscar(state)
            return {'ok': True,
                    'vacuum': round(self._slab_builder.vacuum_thickness(text), 3),
                    'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'vacuum': None, 'error': str(e)}
