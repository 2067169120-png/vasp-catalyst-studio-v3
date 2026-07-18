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
                 slab_builder_mod=None, molbuild_mods=None, gaussian_mod=None,
                 quick_submit_mod=None, local_runner_mod=None, connection_mod=None,
                 multiwfn_mod=None, vmd_mod=None, aimd_mod=None,
                 task_catalog_mod=None, u_library_mod=None, conv_scan_mod=None,
                 bands_mod=None, cell_opt_mod=None, eos_mod=None,
                 workfunction_mod=None, surface_energy_mod=None, dimer_mod=None,
                 auto_figures_mod=None, campaign_templates_mod=None,
                 solvation_mod=None, paper_data_mod=None, variant_advisor_mod=None,
                 manuscript_draft_mod=None, bands_parse_mod=None, deps_runner=None,
                 neb_builder_mod=None, references_mod=None, chgdiff_mod=None,
                 incar_builder_mod=None):
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
        # 分子计算全流程总装(结构建模页分子建模区 / ②Gaussian 分子面板 / ③本机运行·文件管理 /
        # ⑤波函数分析 / ④AIMD 派生):全部重/可选依赖(rdkit/decimer/paramiko/Multiwfn/VMD)延迟
        # 导入,测试注入假件即全离线可测。
        self._molbuild = molbuild_mods            # {ocsr,smiles3d,molinfo,external_editor} 束
        self._gaussian = gaussian_mod             # engines.gaussian(任务表/周期表/preview)
        self._quick_submit = quick_submit_mod
        self._local_runner = local_runner_mod
        self._connection = connection_mod
        self._multiwfn = multiwfn_mod
        self._vmd = vmd_mod
        self._aimd = aimd_mod
        # v3.1 GUI 总装(全 DFT 任务目录 / 一键出图管线 / AI 三能力 / starpivot 对齐):
        # 全部只读引擎,一律延迟导入,测试注入假件即全离线可测(不碰 numpy/matplotlib/网络)。
        self._task_catalog = task_catalog_mod       # generate.task_catalog(计算类型目录)
        self._u_library = u_library_mod             # project.u_library(DFT+U 建议表)
        self._conv_scan = conv_scan_mod             # generate.conv_scan(收敛扫描系列)
        self._bands = bands_mod                     # generate.bands_builder(能带派生)
        self._cell_opt = cell_opt_mod               # generate.cell_opt(变胞弛豫派生)
        self._eos = eos_mod                         # project.eos(EOS 系列 + BM 拟合)
        self._workfunction = workfunction_mod       # project.workfunction(功函数派生/解析)
        self._surface_energy = surface_energy_mod   # project.surface_energy(表面能计算器)
        self._dimer = dimer_mod                     # generate.dimer_builder(Dimer 派生)
        self._auto_figures = auto_figures_mod       # project.auto_figures(场景感知一键出图)
        self._campaign_tpl = campaign_templates_mod  # project.campaign_templates(活动模板)
        self._solvation = solvation_mod             # generate.solvation(溶剂化复合物)
        self._paper_data = paper_data_mod           # project.paper_data(数据表提取/文献对照)
        self._variant_advisor = variant_advisor_mod  # project.variant_advisor(材料变体)
        self._manuscript = manuscript_draft_mod     # project.manuscript_draft(论文骨架)
        self._bands_parse = bands_parse_mod         # project.bands(EIGENVAL/带隙解析)
        self._deps_runner = deps_runner             # 依赖安装后台执行器(subprocess 注入)
        self._deps_job = None                       # deps_install 后台任务句柄(轮询用)
        # QA 接线修复(NEB/形成能·结合能/差分电荷/VASPsol):全只读引擎,延迟导入,注入式可测。
        self._neb_builder = neb_builder_mod         # generate.neb_builder(NEB 目录树)
        self._references = references_mod            # project.references(结合能/形成能/σ)
        self._chgdiff = chgdiff_mod                 # project.chgdiff(差分电荷三静态 + Δρ 合成)
        self._incar_builder = incar_builder_mod     # generate.incar_builder(VASPsol 键)

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

    def _mb(self):
        """分子建模引擎束(ocsr/smiles3d/molinfo/external_editor)延迟加载(rdkit/decimer 可选)。"""
        if self._molbuild is None:
            from vcstudio.molbuild import external_editor, molinfo, ocsr, smiles3d
            self._molbuild = types.SimpleNamespace(
                ocsr=ocsr, smiles3d=smiles3d, molinfo=molinfo,
                external_editor=external_editor)
        return self._molbuild

    def _gauss(self):
        """engines.gaussian 子模块延迟加载(GAUSSIAN_TASKS/PERIODIC_TABLE_GROUPS/preview)。"""
        if self._gaussian is None:
            from vcstudio.engines import gaussian
            self._gaussian = gaussian
        return self._gaussian

    def _qs(self):
        """任意输入批量提交建作业延迟加载。"""
        if self._quick_submit is None:
            from vcstudio.cluster import quick_submit
            self._quick_submit = quick_submit
        return self._quick_submit

    def _lr(self):
        """本机作业运行器延迟加载。"""
        if self._local_runner is None:
            from vcstudio.cluster import local_runner
            self._local_runner = local_runner
        return self._local_runner

    def _conn(self):
        """可复用 SSH 连接延迟加载(paramiko 在其内部再延迟;文件管理/远程波函数用)。"""
        if self._connection is None:
            from vcstudio.cluster import connection
            self._connection = connection
        return self._connection

    def _mw(self):
        """Multiwfn 波函数分析驱动延迟加载。"""
        if self._multiwfn is None:
            from vcstudio.external import multiwfn_driver
            self._multiwfn = multiwfn_driver
        return self._multiwfn

    def _vmd_(self):
        """VMD 批渲染驱动延迟加载。"""
        if self._vmd is None:
            from vcstudio.external import vmd_driver
            self._vmd = vmd_driver
        return self._vmd

    def _aimd_(self):
        """AIMD 作业派生端延迟加载。"""
        if self._aimd is None:
            from vcstudio.generate import aimd_builder
            self._aimd = aimd_builder
        return self._aimd

    def _tc(self):
        """计算类型目录(task_catalog)延迟加载。"""
        if self._task_catalog is None:
            from vcstudio.generate import task_catalog
            self._task_catalog = task_catalog
        return self._task_catalog

    def _ul(self):
        """DFT+U 建议库延迟加载。"""
        if self._u_library is None:
            from vcstudio.project import u_library
            self._u_library = u_library
        return self._u_library

    def _cs(self):
        """收敛扫描系列引擎延迟加载。"""
        if self._conv_scan is None:
            from vcstudio.generate import conv_scan
            self._conv_scan = conv_scan
        return self._conv_scan

    def _bd(self):
        """能带派生端延迟加载。"""
        if self._bands is None:
            from vcstudio.generate import bands_builder
            self._bands = bands_builder
        return self._bands

    def _co(self):
        """变胞弛豫派生端延迟加载。"""
        if self._cell_opt is None:
            from vcstudio.generate import cell_opt
            self._cell_opt = cell_opt
        return self._cell_opt

    def _eos_(self):
        """EOS 系列 + BM 拟合引擎延迟加载(numpy 相邻,重)。"""
        if self._eos is None:
            from vcstudio.project import eos
            self._eos = eos
        return self._eos

    def _wf(self):
        """功函数派生/解析引擎延迟加载。"""
        if self._workfunction is None:
            from vcstudio.project import workfunction
            self._workfunction = workfunction
        return self._workfunction

    def _se(self):
        """表面能计算器延迟加载(纯函数)。"""
        if self._surface_energy is None:
            from vcstudio.project import surface_energy
            self._surface_energy = surface_energy
        return self._surface_energy

    def _dm(self):
        """Dimer 过渡态派生端延迟加载。"""
        if self._dimer is None:
            from vcstudio.generate import dimer_builder
            self._dimer = dimer_builder
        return self._dimer

    def _af(self):
        """场景感知一键出图引擎延迟加载(matplotlib 相邻,重)。"""
        if self._auto_figures is None:
            from vcstudio.project import auto_figures
            self._auto_figures = auto_figures
        return self._auto_figures

    def _ct(self):
        """计算活动模板引擎延迟加载。"""
        if self._campaign_tpl is None:
            from vcstudio.project import campaign_templates
            self._campaign_tpl = campaign_templates
        return self._campaign_tpl

    def _sv(self):
        """溶剂化复合物建模引擎延迟加载(numpy 相邻,重)。"""
        if self._solvation is None:
            from vcstudio.generate import solvation
            self._solvation = solvation
        return self._solvation

    def _pd(self):
        """文献数据表提取 / 对照引擎延迟加载。"""
        if self._paper_data is None:
            from vcstudio.project import paper_data
            self._paper_data = paper_data
        return self._paper_data

    def _va(self):
        """材料变体推荐引擎延迟加载。"""
        if self._variant_advisor is None:
            from vcstudio.project import variant_advisor
            self._variant_advisor = variant_advisor
        return self._variant_advisor

    def _md(self):
        """论文骨架生成引擎延迟加载(python-docx 可选)。"""
        if self._manuscript is None:
            from vcstudio.project import manuscript_draft
            self._manuscript = manuscript_draft
        return self._manuscript

    def _bp(self):
        """能带/带隙解析引擎延迟加载。"""
        if self._bands_parse is None:
            from vcstudio.project import bands as bands_parse
            self._bands_parse = bands_parse
        return self._bands_parse

    def _neb(self):
        """NEB 过渡态目录生成端延迟加载(纯 python)。"""
        if self._neb_builder is None:
            from vcstudio.generate import neb_builder
            self._neb_builder = neb_builder
        return self._neb_builder

    def _refs(self):
        """参考态/稳定性判据引擎(结合能/形成能/σ)延迟加载。"""
        if self._references is None:
            from vcstudio.project import references
            self._references = references
        return self._references

    def _chg(self):
        """差分电荷工作流引擎(三静态派生 + Δρ 网格代数)延迟加载。"""
        if self._chgdiff is None:
            from vcstudio.project import chgdiff
            self._chgdiff = chgdiff
        return self._chgdiff

    def _ib(self):
        """INCAR 顾问引擎(VASPsol 键等)延迟加载(纯 python)。"""
        if self._incar_builder is None:
            from vcstudio.generate import incar_builder
            self._incar_builder = incar_builder
        return self._incar_builder

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

    def gen_run(self, poscar_path, incar_path, out_dir, lib_root, calc_type='slab',
                extra_keywords=None, solvation=None):
        """一键生成(镜像 generate_tab._on_run→build_job_dir→_write_manifest→ledger.register)。

        calc_type 由前端「计算类型」下拉传入(slab/bulk/molecule,非法值归 slab):决定
        KPOINTS 网格(slab 法向仅 1 个 k 点、molecule 为 Gamma 单点、bulk 三维网格)。
        此前 web 硬编码 slab,生成 bulk/molecule 会拿到错误 KPOINTS(缺口分析已指出)。
        校验开关取默认(开)、KPOINTS 仍自动推荐。extra_keywords(每行一条 ``KEY = VALUE``)
        生成后追加到 INCAR 末(自定义关键词,如 LREAL/NCORE);解析不出的行原样保留。
        solvation(可选,默认关,向后兼容):真值/{'enabled':True,'eb_k':78.4}→ 调
        incar_builder.vaspsol_keys 把 LSOL/EB_K 追加进 INCAR,并把「需 VASPsol 补丁编译、
        否则标准 VASP 静默给真空结果」的 advisory 一并进 warnings(绝不假装已溶剂化)。
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
            # 自定义关键词:追加到生成的 INCAR 末(失败只告警,不撤销四件套)
            extra_lines = [ln.strip() for ln in str(extra_keywords or '').splitlines()
                           if ln.strip()]
            if extra_lines:
                try:
                    incar_out = os.path.join(payload['out_dir'], 'INCAR')
                    with open(incar_out, 'a', encoding='utf-8') as f:
                        f.write('\n# === vcstudio 自定义关键词 ===\n')
                        f.write('\n'.join(extra_lines) + '\n')
                    warnings.append(f'已追加 {len(extra_lines)} 条自定义关键词到 INCAR')
                except Exception as e:                    # noqa: BLE001
                    warnings.append(f'自定义关键词追加失败(不影响四件套):{e}')
            # VASPsol 隐式溶剂化(默认关):勾选则把 LSOL/EB_K 追加进 INCAR + advisory 进 warnings
            sol = solvation if isinstance(solvation, dict) else (
                {'enabled': True} if solvation else {})
            if sol.get('enabled'):
                try:
                    ib = self._ib()
                    eb_k = float(sol.get('eb_k', 78.4) or 78.4)
                    keys = ib.vaspsol_keys(True, eb_k=eb_k)
                    incar_out = os.path.join(payload['out_dir'], 'INCAR')
                    with open(incar_out, 'a', encoding='utf-8') as f:
                        f.write('\n# === VASPsol 隐式溶剂化(需 VASPsol 补丁编译的 VASP)===\n')
                        f.write('\n'.join(self._incar_lines_from(keys)) + '\n')
                    warnings.append(f'已启用 VASPsol 隐式溶剂化(EB_K={eb_k:g}),追加 LSOL/EB_K 到 INCAR')
                    adv = getattr(ib, 'VASPSOL_ADVISORY', '')
                    if adv:
                        warnings.append(adv)
                except Exception as e:                    # noqa: BLE001
                    warnings.append(f'VASPsol 键追加失败(不影响四件套):{e}')
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

    def save_text(self, path, text):
        """把文本写到指定路径(②预览区「保存预览到文件」等用)→ {'ok','path','error'}。"""
        try:
            p = (path or '').strip()
            if not p:
                return {'ok': False, 'path': None, 'error': '未指定保存路径'}
            with open(p, 'w', encoding='utf-8') as f:
                f.write(str(text if text is not None else ''))
            return {'ok': True, 'path': p, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'path': None, 'error': str(e)}

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
                'figures': {  # 出图偏好(期刊风格 / 自动出图 / 多面板)
                    'journal_style': (str(ui.get('journal_style') or 'nature').lower()
                                      if str(ui.get('journal_style') or 'nature').lower()
                                      in self._JOURNAL_STYLES else 'nature'),
                    'auto_figures': bool(ui.get('auto_figures', True)),
                    'multi_panel': bool(ui.get('multi_panel', True))},
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
                fig = self._auto_figures_for_project(proj, pp, report_dir)
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
                nfig = len(fig.get('files') or [])
                extra = f'(一键出图 {nfig} 张)' if fig.get('engine') == 'auto_figures' else ''
                events.append({'kind': 'report_done', 'project': name,
                               'report': rep.get('file'), 'figures_dir': fig.get('out_dir'),
                               'engine': fig.get('engine'), 'n_figures': nfig,
                               'text': f'项目「{name}」报告已自动生成{extra}'})
            except Exception as e:                        # noqa: BLE001 单项目失败不拖垮其他
                errors.append(f'项目报告自动化异常:{e}')

    # ── 一键出图接线:全 DONE 项目自动出图升级(auto_figures 场景感知 + 期刊风格) ──
    def _af_scenario(self, proj):
        """项目 → auto_figures 场景(lis/general)。研究场景为 lis(锂硫)→ lis,否则 general。"""
        try:
            cfg = self._config.load_config()
            sc = self._scenarios.active_scenario(cfg)
            if str((sc or {}).get('key')) == 'lis':
                return 'lis'
        except Exception:                                 # noqa: BLE001
            pass
        return 'general'

    def _af_molecules_dir(self):
        try:
            return (self._config.load_config().get('lis_molecules_dir') or '').strip() or None
        except Exception:                                 # noqa: BLE001
            return None

    def _auto_figures_for_project(self, proj, pp, out_dir):
        """全 DONE 项目一键出图:优先 auto_figures.run_auto_figures(场景感知 + 期刊风格 + 多面板),
        引擎不可用(缺 matplotlib)/偏好关闭 → 回退 proj_figures(bar/table/ladder)。"""
        prefs = self.figure_prefs_get()
        if prefs.get('auto_figures', True):
            try:
                r = self._af().run_auto_figures(
                    proj, self._af_scenario(proj), out_dir,
                    journal=prefs.get('journal_style', 'nature'),
                    adsorption_mod=self._adsorption, molecules_dir=self._af_molecules_dir(),
                    compose_panel=bool(prefs.get('multi_panel', True)))
                if r.get('ok'):
                    return {'out_dir': r.get('out_dir'), 'files': list(r.get('files') or []),
                            'panel': r.get('panel'), 'manifest': list(r.get('manifest') or []),
                            'engine': 'auto_figures'}
            except Exception:                             # noqa: BLE001 引擎不可用 → 回退
                pass
        fig = self.proj_figures(pp, ['bar', 'table', 'ladder'], out_dir)
        return {'out_dir': fig.get('out_dir'), 'files': [], 'panel': None,
                'manifest': [], 'engine': 'proj_figures'}

    # ── campaign 全链推进接线:消费 next_derivations 自动派生下一阶段作业(自动驾驶时) ──
    def _tick_campaigns(self, events, errors):
        """扫已登记 campaign,消费 next_derivations 自动派生下一阶段作业并 mark_derived(幂等)。

        依赖 campaign_templates(不可用则整体跳过);每目录/每项失败只记 event/error,绝不抛出。
        """
        try:
            ct = self._ct()
        except Exception:                                 # noqa: BLE001 模块不可用 → 跳过
            return
        try:
            ui = self._config.get_ui_state()
        except Exception:                                 # noqa: BLE001
            ui = {}
        for cdir in list((ui or {}).get('campaign_dirs') or []):
            try:
                pending = ct.next_derivations(cdir)
            except Exception as e:                        # noqa: BLE001
                errors.append(f'批次「{self._base(cdir)}」推进扫描失败:{e}')
                continue
            for item in pending:
                src = item.get('src_dir')
                derive = item.get('derive')
                if not src or not os.path.isdir(str(src)):
                    continue
                try:
                    if derive == 'estatic':
                        r = self.derive_estatic(src, item.get('kinds') or ['pdos'])
                        ok = bool(r.get('ok'))
                    elif derive == 'freq':
                        r = self.derive_freq(src)
                        ok = bool(r.get('ok'))
                    else:
                        continue
                    if ok:
                        try:
                            ct.mark_derived(cdir, item.get('src_id'), derive)
                        except Exception as e:            # noqa: BLE001 记事件失败下拍再试
                            errors.append(f'批次派生标记失败:{e}')
                        events.append({'kind': 'derive',
                                       'text': f'{self._base(src)}:自动派生 {derive} '
                                               f'({self._base(cdir)})'})
                except Exception as e:                    # noqa: BLE001 单项失败不拖垮其他
                    errors.append(f'批次自动派生失败({self._base(src)}/{derive}):{e}')

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
        # campaign 全链推进:自动派生下一阶段作业(自动驾驶续算或拉回任一子开关开启时)
        if ap['cont'] or ap['fetch']:
            try:
                self._tick_campaigns(events, errors)
            except Exception as e:                        # noqa: BLE001
                errors.append(f'批次全链推进失败:{e}')
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

    def _spec_from_params(self, mods, params):
        """params → (CalcSpec, err|None)。结构取 params['structure'](内联文本)或 poscar 路径;

        extras(Gaussian 分子面板透传:nproc/mem_gb/chk/basis/solvent/mixed_basis/gaussian_task/
        td_nstates/irc_maxpoints/modredundant …)原样并入 spec.extras(见 CalcSpec.extras 契约)。
        engine_generate/engine_preview 共用同一装配源。
        """
        params = dict(params or {})
        structure = params.get('structure')
        if not (isinstance(structure, str) and structure.strip()):
            poscar = (params.get('poscar') or '').strip()
            if not poscar or not os.path.isfile(poscar):
                return None, '结构文件(POSCAR)不存在'
            with open(poscar, 'r', encoding='utf-8', errors='replace') as f:
                structure = f.read()
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
            multiplicity=int(params.get('multiplicity') or 1),
            extras=dict(params.get('extras') or {}))
        return spec, None

    def engine_generate(self, engine, params, out_dir):
        """非 VASP 引擎输入生成(文件级适配):简化参数表单 → CalcSpec → backend.generate_inputs。

        params:{poscar(结构文件路径)|structure(内联文本),task,functional,cutoff_ev,
        kpoints([a,b,c]|None),spin,charge,periodic,dispersion,multiplicity,extras(引擎私有透传,
        Gaussian 分子面板参数走此)}。validate 的自洽问题并入 issues(不静默),生成失败兜 error。
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
            mods = self._eng()
            spec, serr = self._spec_from_params(mods, params)
            if serr:
                return {'ok': False, 'files': [], 'warnings': [], 'issues': [],
                        'out_dir': None, 'error': serr}
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

    def engine_preview(self, engine, params):
        """引擎输入实时预览(不落用户盘):CalcSpec → 输入全文文本 + 字符数(②预览区用)。

        Gaussian 走 gaussian.preview(spec)(分子面板主用);其余引擎经临时目录 generate_inputs
        回读首个产物文本。validate 自洽问题并入 issues。返回
        {'ok','text','chars','warnings','issues','error'}。
        """
        try:
            eng = (engine or '').strip().lower()
            if not eng:
                return {'ok': False, 'text': '', 'chars': 0, 'warnings': [],
                        'issues': [], 'error': '未指定引擎'}
            mods = self._eng()
            spec, serr = self._spec_from_params(mods, params)
            if serr:
                return {'ok': False, 'text': '', 'chars': 0, 'warnings': [],
                        'issues': [], 'error': serr}
            issues = list(mods.validate(spec))
            eng_norm = {'g16': 'gaussian', 'g09': 'gaussian'}.get(eng, eng)
            warnings: list = []
            if eng_norm == 'gaussian':
                text = self._gauss().preview(spec)
            else:
                tmp = tempfile.mkdtemp(prefix='vcs_engine_preview_')
                try:
                    res = mods.get_backend(eng).generate_inputs(spec, tmp)
                    warnings = list(res.get('warnings') or [])
                    files = list(res.get('files') or [])
                    text = ''
                    if files and os.path.isfile(files[0]):
                        with open(files[0], 'r', encoding='utf-8', errors='replace') as f:
                            text = f.read()
                finally:
                    import shutil
                    shutil.rmtree(tmp, ignore_errors=True)
            return {'ok': True, 'text': text, 'chars': len(text),
                    'warnings': warnings, 'issues': issues, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'text': '', 'chars': 0, 'warnings': [],
                    'issues': [], 'error': str(e)}

    def gauss_tasks(self):
        """Gaussian 任务种类全家桶(9 种)→ {'ok','tasks':[{key,name,note}],'error'}(②任务下拉)。"""
        try:
            tasks = self._gauss().GAUSSIAN_TASKS
            out = [{'key': k, 'name': v.get('name_zh', k), 'note': v.get('note', '')}
                   for k, v in tasks.items()]
            return {'ok': True, 'tasks': out, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'tasks': [], 'error': str(e)}

    def gauss_periodic_table(self):
        """Gaussian 混合基组周期表数据源(1-86 号 + 类别 + 每元素基组建议)→ {'ok','table','error'}。"""
        try:
            pt = self._gauss().PERIODIC_TABLE_GROUPS
            return {'ok': True, 'error': None, 'table': {
                'note': pt.get('note', ''),
                'categories': list(pt.get('categories') or []),
                'elements': list(pt.get('elements') or [])}}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'table': None, 'error': str(e)}

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

    # ── ①结构建模页·分子建模区(图片识别 → SMILES → 3D 建模 → 外部编辑器往返) ─────
    _MOL_BOX = 15.0                                       # 分子装盒边长(Å;编辑器晶格 + 保存 POSCAR)

    @staticmethod
    def _mol_struct(elements, coords, formula):
        """elements/coords + 分子式 → 编辑器 struct dict(立方盒晶格,便于 3D 渲染/POSCAR 导出)。"""
        box = Api._MOL_BOX
        return {'elements': list(elements), 'coords': [list(c) for c in coords],
                'lattice': [[box, 0.0, 0.0], [0.0, box, 0.0], [0.0, 0.0, box]],
                'natoms': len(list(elements)), 'formula': formula}

    def mol_ocsr_probe(self):
        """探测 DECIMER(图片 OCSR)可用性 → {'ok','available','detail','error'}(卡顶提示条)。"""
        try:
            r = self._mb().ocsr.probe()
            return {'ok': True, 'available': bool(r.get('available')),
                    'detail': r.get('detail', ''), 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'available': False, 'detail': '', 'error': str(e)}

    def mol_image_to_smiles(self, image_path):
        """分子结构图片 → SMILES(DECIMER)→ {'ok','smiles','elapsed_ms','error'}。"""
        try:
            p = (image_path or '').strip()
            if not p:
                return {'ok': False, 'smiles': '', 'elapsed_ms': 0.0, 'error': '未选择图片文件'}
            r = self._mb().ocsr.image_to_smiles(p)
            return {'ok': bool(r.get('ok')), 'smiles': r.get('smiles', ''),
                    'elapsed_ms': r.get('elapsed_ms', 0.0), 'error': r.get('error') or None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'smiles': '', 'elapsed_ms': 0.0, 'error': str(e)}

    def mol_smiles_svg(self, smiles, width=420, height=300):
        """SMILES → 2D 键线式 SVG(RDKit)→ {'ok','svg','error'}(结构图预览)。"""
        try:
            s = (smiles or '').strip()
            if not s:
                return {'ok': False, 'svg': '', 'error': 'SMILES 为空'}
            r = self._mb().ocsr.smiles_svg(s, width=int(width or 420), height=int(height or 300))
            return {'ok': bool(r.get('ok')), 'svg': r.get('svg', ''),
                    'error': r.get('error') or None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'svg': '', 'error': str(e)}

    def mol_smiles_to_3d(self, smiles, forcefield='auto'):
        """SMILES → 3D 结构(RDKit ETKDG + MMFF/UFF)→ 编辑器 struct + 电荷/多重度提示。

        返回 {'ok','struct','charge','multiplicity_hint','warnings','error'}。struct 载入现有编辑器。
        """
        try:
            s = (smiles or '').strip()
            if not s:
                return {'ok': False, 'struct': None, 'charge': 0,
                        'multiplicity_hint': None, 'warnings': [], 'error': 'SMILES 为空'}
            r = self._mb().smiles3d.smiles_to_3d(s, forcefield=(forcefield or 'auto'))
            if not r.get('ok'):
                return {'ok': False, 'struct': None, 'charge': 0, 'multiplicity_hint': None,
                        'warnings': list(r.get('warnings') or []), 'error': r.get('error')}
            return {'ok': True, 'error': None,
                    'struct': self._mol_struct(r['elements'], r['coords'], r['formula']),
                    'charge': r.get('charge', 0),
                    'multiplicity_hint': r.get('multiplicity_hint'),
                    'warnings': list(r.get('warnings') or [])}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'struct': None, 'charge': 0, 'multiplicity_hint': None,
                    'warnings': [], 'error': str(e)}

    def mol_info(self, elements, coords=None, charge=0):
        """分子属性(化学式/原子数/电子数/分子量/建议多重度)→ {'ok', …, 'error'}(属性面板)。"""
        try:
            els = list(elements or [])
            if not els:
                return {'ok': False, 'error': '无原子(先建模或载入结构)'}
            r = self._mb().molinfo.mol_summary(els, coords, int(charge or 0))
            out = {'ok': True, 'error': None}
            out.update(r)
            return out
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    def mol_export_editor(self, elements, coords, fmt='xyz', workdir=None):
        """把当前结构写成外部编辑器临时文件(固定名)→ {'ok','path','mtime','error'}(GaussView/Avogadro)。"""
        try:
            els = list(elements or [])
            cds = [list(c) for c in (coords or [])]
            r = self._mb().external_editor.export_for_editor(
                els, cds, fmt=(fmt or 'xyz'), workdir=((workdir or '').strip() or None))
            return {'ok': bool(r.get('ok')), 'path': r.get('path'),
                    'mtime': r.get('mtime'), 'error': r.get('error') or None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'path': None, 'mtime': None, 'error': str(e)}

    def mol_open_with(self, path, editor_exe=None):
        """用外部编辑器(或平台默认)打开文件 → {'ok','error'}。editor_exe 空则走 xdg-open/startfile。"""
        try:
            p = (path or '').strip()
            if not p:
                return {'ok': False, 'error': '未指定文件路径'}
            exe = (editor_exe or '').strip() or None
            r = self._mb().external_editor.open_with(p, editor_exe=exe)
            return {'ok': bool(r.get('ok')), 'error': r.get('error') or None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    def mol_check_reimport(self, path, last_mtime=None):
        """比对外部编辑文件 mtime 判是否改动过 → {'ok','changed','mtime','error'}(自动检测轮询)。"""
        try:
            p = (path or '').strip()
            if not p:
                return {'ok': False, 'changed': False, 'mtime': None, 'error': '未指定文件路径'}
            r = self._mb().external_editor.check_reimport(p, last_mtime)
            return {'ok': True, 'changed': bool(r.get('changed')),
                    'mtime': r.get('mtime'), 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'changed': False, 'mtime': None, 'error': str(e)}

    def mol_reimport(self, path):
        """回读外部编辑结果(xyz/mol 纯手写解析)→ {'ok','struct','error'}(载入编辑器)。"""
        try:
            p = (path or '').strip()
            if not p or not os.path.isfile(p):
                return {'ok': False, 'struct': None, 'error': '编辑文件不存在'}
            r = self._mb().external_editor.reimport(p)
            if not r.get('ok'):
                return {'ok': False, 'struct': None, 'error': r.get('error')}
            els, cds = r['elements'], r['coords']
            formula = self._mb().molinfo.formula(els)
            return {'ok': True, 'error': None, 'struct': self._mol_struct(els, cds, formula)}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'struct': None, 'error': str(e)}

    # ── ③提交页·快速批量提交 / 本机运行 / 文件管理 ─────────────────────────────────
    def quick_submit_build(self, files, out_root, job_prefix=''):
        """任意输入文件(.gjf/.com/.inp/.cell/VASP 目录)→ 逐个建轻量作业目录 + 入台账。

        返回 {'ok','jobs':[{dir,name,engine,files,hint,registered}],'skipped':[{file,reason}],'error'}。
        hint 为该引擎的集群运行命令模板提示(quick_submit.submit_hint)。
        """
        try:
            fs = [str(f) for f in (files or []) if str(f).strip()]
            root = (out_root or '').strip()
            if not fs:
                return {'ok': False, 'jobs': [], 'skipped': [], 'error': '未选择任何输入文件'}
            if not root:
                return {'ok': False, 'jobs': [], 'skipped': [], 'error': '未选输出根目录'}
            qs = self._qs()
            res = qs.build_quick_jobs(fs, root, job_prefix=(job_prefix or ''))
            if not res.get('ok'):
                return {'ok': False, 'jobs': [], 'skipped': list(res.get('skipped') or []),
                        'error': res.get('error') or '建作业失败'}
            jobs = []
            for j in (res.get('jobs') or []):
                registered = True
                try:
                    self._ledger.register(j['dir'])
                except Exception:                         # noqa: BLE001 登记失败不挡已建目录
                    registered = False
                jobs.append({'dir': j['dir'], 'name': j['name'], 'engine': j['engine'],
                             'files': list(j.get('files') or []),
                             'hint': qs.submit_hint(j.get('engine', '')),
                             'registered': registered})
            return {'ok': True, 'jobs': jobs, 'skipped': list(res.get('skipped') or []),
                    'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'jobs': [], 'skipped': [], 'error': str(e)}

    def jobs_cancel_batch(self, dirs, name, password, trust_new=False):
        """批量取消集群作业(逐作业 qdel/scancel + 回写 manifest FAILED/用户取消)。

        返回 {'ok','cancelled':[job_id],'failed':[{job_id,reason}],'needs_trust','error'}。
        """
        try:
            ds = [str(d) for d in (dirs or []) if str(d).strip()]
            if not ds:
                return {'ok': False, 'cancelled': [], 'failed': [],
                        'needs_trust': False, 'error': '未选择要取消的作业'}
            prof, pw, err = self._resolve(name, password)
            if err:
                return err
            res = self._bo().cancel_batch(prof, ds, password=pw, trust_new=bool(trust_new))
            return {'ok': bool(res.get('ok')), 'cancelled': list(res.get('cancelled') or []),
                    'failed': list(res.get('failed') or []),
                    'needs_trust': bool(res.get('needs_trust')), 'error': res.get('error')}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'cancelled': [], 'failed': [],
                    'needs_trust': False, 'error': str(e)}

    _LOCAL_INPUT_EXTS = ('.gjf', '.com', '.inp', '.gau')

    def _find_local_input(self, job_dir):
        """作业目录内首个本机引擎输入文件(.gjf/.com/.inp/.gau);无 → None。"""
        import glob
        for ext in self._LOCAL_INPUT_EXTS:
            cands = sorted(glob.glob(os.path.join(job_dir, '*' + ext)))
            if cands:
                return cands[0]
        return None

    def _build_local_cmd(self, job_dir, tmpl):
        """命令模板 + 作业目录输入 → argv 列表。占位符 {input}/{output} 替换,否则末尾追加输入名。

        缺 {input} 占位符也无可识别输入文件 → 直接按模板拆分(用户模板可自含输入)。
        """
        import shlex
        inp = self._find_local_input(job_dir)
        inp_base = os.path.basename(inp) if inp else ''
        out_base = (os.path.splitext(inp_base)[0] + '.log') if inp_base else 'output.log'
        if '{input}' in tmpl or '{output}' in tmpl:
            filled = tmpl.replace('{input}', inp_base).replace('{output}', out_base)
            return shlex.split(filled)
        parts = shlex.split(tmpl)
        if inp_base:
            parts.append(inp_base)
        return parts

    def local_run_start(self, job_dir, cmd_template):
        """本机启动作业(quick/gaussian):按命令模板 + 目录输入文件 → local_runner.start。

        返回 {'ok','pid','cmd','error'}。cmd_template 为设置页存的本地软件命令(如 g16 或
        'g16 {input} {output}')。
        """
        try:
            d = (job_dir or '').strip()
            if not d or not os.path.isdir(d):
                return {'ok': False, 'pid': None, 'cmd': [], 'error': '作业目录不存在'}
            tmpl = (cmd_template or '').strip()
            if not tmpl:
                return {'ok': False, 'pid': None, 'cmd': [],
                        'error': '未配置本机运行命令模板(请在设置页填写,如 g16)'}
            cmd = self._build_local_cmd(d, tmpl)
            if not cmd:
                return {'ok': False, 'pid': None, 'cmd': [], 'error': '命令模板为空'}
            lr = self._lr()
            job = lr.LocalJob(cmd=cmd, cwd=d, log_file=os.path.join(d, 'local_run.log'))
            res = lr.start(job)
            return {'ok': bool(res.get('ok')), 'pid': res.get('pid'),
                    'cmd': cmd, 'error': res.get('error') or None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'pid': None, 'cmd': [], 'error': str(e)}

    def local_run_status(self, job_dir):
        """查询本机作业状态 → {'ok','state','pid','exit_code','log_tail','error'}(轮询)。"""
        try:
            d = (job_dir or '').strip()
            if not d:
                return {'ok': False, 'state': 'NOT_STARTED', 'pid': None,
                        'exit_code': None, 'log_tail': '', 'error': '未指定作业目录'}
            r = self._lr().status(d)
            return {'ok': True, 'state': r.get('state'), 'pid': r.get('pid'),
                    'exit_code': r.get('exit_code'), 'log_tail': r.get('log_tail', ''),
                    'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'state': 'NOT_STARTED', 'pid': None,
                    'exit_code': None, 'log_tail': '', 'error': str(e)}

    def local_run_cancel(self, job_dir):
        """停止本机作业(杀进程树/进程组)→ {'ok','error'}。"""
        try:
            d = (job_dir or '').strip()
            if not d:
                return {'ok': False, 'error': '未指定作业目录'}
            r = self._lr().cancel(d)
            return {'ok': bool(r.get('ok')), 'error': r.get('error') or None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    @staticmethod
    def _sftp_is_dir(mode):
        """SFTP st_mode → 是否目录(缺/异常 → False)。"""
        import stat as _stat
        try:
            return bool(_stat.S_ISDIR(int(mode or 0)))
        except (TypeError, ValueError):
            return False

    def remote_ls(self, name, password, remote_path='.', trust_new=False):
        """列远端目录(SFTP listdir_attr)→ {'ok','path','entries':[{name,size,mtime,is_dir}],
        'needs_trust','error'}(文件管理子卡)。"""
        try:
            prof, pw, err = self._resolve(name, password)
            if err:
                return err
            rpath = (remote_path or '.').strip() or '.'
            conn = self._conn()
            try:
                client, jump = conn.open_client(prof, pw, trust_new=bool(trust_new))
            except conn.ConnectError as e:
                return {'ok': False, 'path': rpath, 'entries': [],
                        'needs_trust': bool(getattr(e, 'needs_trust', False)), 'error': str(e)}
            try:
                sftp = client.open_sftp()
                attrs = sftp.listdir_attr(rpath)
                entries = []
                for a in attrs:
                    entries.append({
                        'name': getattr(a, 'filename', ''),
                        'size': int(getattr(a, 'st_size', 0) or 0),
                        'mtime': int(getattr(a, 'st_mtime', 0) or 0),
                        'is_dir': self._sftp_is_dir(getattr(a, 'st_mode', 0))})
                sftp.close()
            finally:
                conn.close_quiet(client, jump)
            entries.sort(key=lambda e: (not e['is_dir'], e['name'].lower()))
            return {'ok': True, 'path': rpath, 'entries': entries,
                    'needs_trust': False, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'path': remote_path, 'entries': [],
                    'needs_trust': False, 'error': str(e)}

    def remote_fetch_file(self, name, password, remote_path, local_dir, trust_new=False):
        """下载远端单个文件到本地目录(SFTP get)→ {'ok','local_path','needs_trust','error'}。"""
        try:
            rpath = (remote_path or '').strip()
            ldir = (local_dir or '').strip()
            if not rpath:
                return {'ok': False, 'local_path': None, 'needs_trust': False,
                        'error': '未指定远端文件'}
            if not ldir:
                return {'ok': False, 'local_path': None, 'needs_trust': False,
                        'error': '未指定本地保存目录'}
            prof, pw, err = self._resolve(name, password)
            if err:
                return err
            os.makedirs(ldir, exist_ok=True)
            local_path = os.path.join(ldir, os.path.basename(rpath.rstrip('/')))
            conn = self._conn()
            try:
                client, jump = conn.open_client(prof, pw, trust_new=bool(trust_new))
            except conn.ConnectError as e:
                return {'ok': False, 'local_path': None,
                        'needs_trust': bool(getattr(e, 'needs_trust', False)), 'error': str(e)}
            try:
                sftp = client.open_sftp()
                sftp.get(rpath, local_path)
                sftp.close()
            finally:
                conn.close_quiet(client, jump)
            return {'ok': True, 'local_path': local_path, 'needs_trust': False, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'local_path': None, 'needs_trust': False, 'error': str(e)}

    # ── ⑤波函数分析页(外部工具探测 / 分析 / 渲染 / 极值) ──────────────────────────
    _WAVEFN_TOOL_KEYS = ('multiwfn', 'vmd', 'gaussview', 'gaussian')
    _TOOL_WHICH = {'gaussview': ('gview', 'gview.exe', 'GaussView'),
                   'gaussian': ('g16', 'g09', 'g16.exe', 'g09.exe')}

    def _tool_paths(self):
        """config.tool_paths(外部工具路径记忆)→ dict(缺 → {})。"""
        try:
            tp = self._config.load_config().get('tool_paths')
            return dict(tp) if isinstance(tp, dict) else {}
        except Exception:                                 # noqa: BLE001
            return {}

    def _probe_which(self, key, exe):
        """gaussview/gaussian 简易探测(给定路径 isfile 直用,否则候选名 PATH 查)→ probe dict。"""
        import shutil
        if exe and os.path.isfile(exe):
            return {'available': True, 'path': exe, 'detail': f'使用指定路径:{exe}'}
        for cand in ((exe,) if exe else ()) + self._TOOL_WHICH.get(key, ()):
            found = shutil.which(cand)
            if found:
                return {'available': True, 'path': found, 'detail': f'在 PATH 找到:{found}'}
        label = 'GaussView' if key == 'gaussview' else '本地 Gaussian(g16/g09)'
        return {'available': False, 'path': None,
                'detail': f'未找到 {label}:请在上方填写可执行文件路径,或将其加入 PATH。'}

    def wavefn_probe(self, tools=None):
        """探测波函数分析外部工具(Multiwfn/VMD/GaussView/本地 Gaussian)→
        {'ok','tools':{key:{available,path,detail}},'error'}。"""
        try:
            paths = self._tool_paths()
            want = [str(t).lower() for t in tools] if tools else list(self._WAVEFN_TOOL_KEYS)
            out = {}
            for key in want:
                exe = (paths.get(key) or '').strip() or None
                if key == 'multiwfn':
                    out[key] = self._mw().probe(exe)
                elif key == 'vmd':
                    out[key] = self._vmd_().probe(exe)
                elif key in ('gaussview', 'gaussian'):
                    out[key] = self._probe_which(key, exe)
                else:
                    out[key] = {'available': False, 'path': None,
                                'detail': f'未知工具 {key!r}'}
            return {'ok': True, 'tools': out, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'tools': {}, 'error': str(e)}

    def wavefn_scenes(self):
        """波函数分析项(Multiwfn)+ 可视化场景(VMD)目录 → {'ok','analyses','scenes','error'}(chips)。"""
        try:
            analyses = [{'key': k, 'name': v.get('name', k), 'note': v.get('note', '')}
                        for k, v in self._mw().ANALYSES.items()]
            scenes = [{'key': k, 'name': v.get('name', k),
                       'files': list(v.get('files') or ()), 'note': v.get('note', '')}
                      for k, v in self._vmd_().SCENES.items()]
            return {'ok': True, 'analyses': analyses, 'scenes': scenes, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'analyses': [], 'scenes': [], 'error': str(e)}

    def wavefn_run(self, wavefn_file, analyses, params=None, exe=None, workdir=None):
        """本机逐项跑 Multiwfn 分析 → {'ok','results':[{analysis,ok,outputs,stdout_tail,elapsed_s,
        script,extrema?,error}],'error'}。Multiwfn 缺失 → 各项 error + 可复制 stdin 脚本(script)。"""
        try:
            wf = (wavefn_file or '').strip()
            keys = [str(a).strip() for a in (analyses or []) if str(a).strip()]
            if not wf:
                return {'ok': False, 'results': [], 'error': '未选择波函数文件'}
            if not keys:
                return {'ok': False, 'results': [], 'error': '未选择分析项'}
            paths = self._tool_paths()
            mw_exe = (exe or paths.get('multiwfn') or '').strip() or None
            wd = (workdir or '').strip() or None
            mw = self._mw()
            p = dict(params or {})
            results = []
            for k in keys:
                r = mw.run(wf, k, exe=mw_exe, workdir=wd, params=p)
                entry = {'analysis': k, 'ok': bool(r.get('ok')),
                         'outputs': list(r.get('outputs') or []),
                         'stdout_tail': r.get('stdout_tail', ''),
                         'elapsed_s': r.get('elapsed_s', 0.0),
                         'script': r.get('script', ''), 'error': r.get('error') or None}
                if k in ('esp_extrema', 'alie_extrema') and r.get('stdout_tail'):
                    try:
                        entry['extrema'] = mw.extrema_parse(r['stdout_tail'])
                    except Exception:                     # noqa: BLE001 解析失败不挡该项返回
                        pass
                results.append(entry)
            return {'ok': any(e['ok'] for e in results), 'results': results, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'results': [], 'error': str(e)}

    def wavefn_run_remote(self, wavefn_file, analyses, name, password, remote_dir,
                          remote_exe='Multiwfn', trust_new=False, params=None):
        """(实验性)远程集群跑 Multiwfn:上传波函数 → 远端逐项执行 → 取回 stdout。

        返回 {'ok','results':[{analysis,ok,stdout_tail,error}],'experimental':True,'needs_trust','error'}。
        任一步失败给中文说明。复杂路径(产物取回/大文件)后续版本完善。
        """
        try:
            import shlex
            wf = (wavefn_file or '').strip()
            keys = [str(a).strip() for a in (analyses or []) if str(a).strip()]
            rdir = (remote_dir or '').strip()
            if not wf or not os.path.isfile(wf):
                return {'ok': False, 'results': [], 'experimental': True,
                        'needs_trust': False, 'error': '波函数文件不存在'}
            if not keys:
                return {'ok': False, 'results': [], 'experimental': True,
                        'needs_trust': False, 'error': '未选择分析项'}
            if not rdir:
                return {'ok': False, 'results': [], 'experimental': True,
                        'needs_trust': False, 'error': '未指定远端工作目录'}
            prof, pw, err = self._resolve(name, password)
            if err:
                return err
            conn = self._conn()
            mw = self._mw()
            rexe = (remote_exe or 'Multiwfn').strip() or 'Multiwfn'
            try:
                client, jump = conn.open_client(prof, pw, trust_new=bool(trust_new))
            except conn.ConnectError as e:
                return {'ok': False, 'results': [], 'experimental': True,
                        'needs_trust': bool(getattr(e, 'needs_trust', False)), 'error': str(e)}
            results = []
            try:
                sftp = client.open_sftp()
                base = os.path.basename(wf)
                remote_wf = rdir.rstrip('/') + '/' + base
                sftp.put(wf, remote_wf)
                for k in keys:
                    spec = mw.ANALYSES.get(k)
                    if spec is None:
                        results.append({'analysis': k, 'ok': False, 'stdout_tail': '',
                                        'error': f'未知分析项 {k!r}'})
                        continue
                    try:
                        script = spec['stdin_script'](dict(params or {}))
                    except ValueError as e:
                        results.append({'analysis': k, 'ok': False, 'stdout_tail': '',
                                        'error': str(e)})
                        continue
                    script_name = f'_wfn_{k}.txt'
                    with sftp.open(rdir.rstrip('/') + '/' + script_name, 'w') as f:
                        f.write(script)
                    cmd = (f'cd {shlex.quote(rdir)} && {rexe} '
                           f'{shlex.quote(base)} < {script_name}')
                    _in, out, _err = client.exec_command(cmd, timeout=1800)
                    text = out.read().decode('utf-8', errors='replace')
                    code = out.channel.recv_exit_status()
                    tail = '\n'.join(text.splitlines()[-40:])
                    results.append({'analysis': k, 'ok': code == 0, 'stdout_tail': tail,
                                    'error': None if code == 0 else f'远端退出码 {code}'})
                sftp.close()
            finally:
                conn.close_quiet(client, jump)
            return {'ok': any(r.get('ok') for r in results), 'results': results,
                    'experimental': True, 'needs_trust': False, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'results': [], 'experimental': True,
                    'needs_trust': False, 'error': str(e)}

    def wavefn_render(self, scene, files, out_png, params=None, exe=None):
        """VMD 批渲染一个可视化场景 → {'ok','png','tcl','stdout_tail','error'}。VMD 缺失 → 回传 tcl。"""
        try:
            sc = (scene or '').strip()
            if not sc:
                return {'ok': False, 'png': None, 'tcl': '', 'error': '未选择渲染场景'}
            outp = (out_png or '').strip()
            if not outp:
                return {'ok': False, 'png': None, 'tcl': '', 'error': '未指定输出 PNG 路径'}
            vmd_exe = (exe or self._tool_paths().get('vmd') or '').strip() or None
            r = self._vmd_().render(sc, dict(files or {}), outp,
                                    exe=vmd_exe, params=dict(params or {}))
            return {'ok': bool(r.get('ok')), 'png': r.get('png'), 'tcl': r.get('tcl', ''),
                    'stdout_tail': r.get('stdout_tail', ''), 'error': r.get('error') or None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'png': None, 'tcl': '', 'error': str(e)}

    def wavefn_extrema(self, wavefn_file, kind='esp_extrema', exe=None, workdir=None):
        """查询分子表面极值点(ESP/ALIE)→ {'ok','minima','maxima','script','error'}。

        跑 Multiwfn 定量分子表面分析并解析极小/极大点(反应位点);Multiwfn 缺失 → 回传 stdin 脚本。
        """
        try:
            wf = (wavefn_file or '').strip()
            if not wf:
                return {'ok': False, 'minima': [], 'maxima': [], 'script': '',
                        'error': '未选择波函数文件'}
            k = (kind or 'esp_extrema').strip()
            if k not in ('esp_extrema', 'alie_extrema'):
                return {'ok': False, 'minima': [], 'maxima': [], 'script': '',
                        'error': f'不支持的极值类型 {k!r}(仅 esp_extrema/alie_extrema)'}
            mw_exe = (exe or self._tool_paths().get('multiwfn') or '').strip() or None
            mw = self._mw()
            r = mw.run(wf, k, exe=mw_exe, workdir=((workdir or '').strip() or None))
            ex = mw.extrema_parse(r.get('stdout_tail', '')) if r.get('stdout_tail') \
                else {'minima': [], 'maxima': []}
            return {'ok': bool(r.get('ok')), 'minima': list(ex.get('minima') or []),
                    'maxima': list(ex.get('maxima') or []), 'script': r.get('script', ''),
                    'error': r.get('error') or None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'minima': [], 'maxima': [], 'script': '', 'error': str(e)}

    # ── 外部工具路径(设置页 / 波函数页 / 外部编辑器卡 共用记忆) ──────────────────
    def tool_paths_get(self):
        """读外部工具路径记忆 → {'ok','paths':{key:path},'error'}。"""
        try:
            return {'ok': True, 'paths': self._tool_paths(), 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'paths': {}, 'error': str(e)}

    def tool_paths_set(self, paths):
        """合并更新外部工具路径记忆到 config.tool_paths → {'ok','paths','error'}(空串=清除该键值)。"""
        try:
            incoming = dict(paths or {})
            cfg = self._config.load_config()
            tp = dict(cfg.get('tool_paths') or {})
            for k, v in incoming.items():
                key = str(k).strip()
                if not key:
                    continue
                tp[key] = (str(v).strip() if v is not None else '')
            cfg['tool_paths'] = tp
            self._config.save_config(cfg)
            return {'ok': True, 'paths': tp, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'paths': {}, 'error': str(e)}

    # ── ④结果分析页·AIMD 派生(从完成弛豫作业一键派生 AIMD 作业) ────────────────────
    def derive_aimd(self, job_dir, ensemble='nvt', temp_k=300.0, temp_end_k=None,
                    steps=10000, potim_fs=1.0, encut=None, out_root=None):
        """从完成弛豫的作业目录派生 AIMD 作业(NVT/NVE)→ 入台账。命名 {原名}_aimd。

        返回 {'ok','job_dir','changes','warnings','error'}。系综非法/缺 INCAR → ok=False + 中文 error。
        """
        try:
            d = (job_dir or '').strip()
            if not d or not os.path.isdir(d):
                return {'ok': False, 'job_dir': None, 'changes': [],
                        'warnings': [], 'error': '作业目录不存在'}
            base = os.path.basename(os.path.normpath(d))
            parent = (out_root or '').strip() or os.path.dirname(os.path.normpath(d))
            out_dir = os.path.join(parent, f'{base}_aimd')
            kw = dict(ensemble=(ensemble or 'nvt'), temp_k=float(temp_k),
                      steps=int(steps), potim_fs=float(potim_fs))
            if temp_end_k not in (None, ''):
                kw['temp_end_k'] = float(temp_end_k)
            if encut not in (None, ''):
                kw['encut'] = float(encut)
            res = self._aimd_().build_aimd_job(d, out_dir, **kw)
            if not res.get('ok'):
                return {'ok': False, 'job_dir': None, 'changes': [],
                        'warnings': list(res.get('warnings') or []), 'error': res.get('error')}
            warnings = list(res.get('warnings') or [])
            try:
                self._ledger.register(res['job_dir'])
            except Exception as e:                        # noqa: BLE001
                warnings.append(f'台账登记失败(不影响已派生目录):{e}')
            return {'ok': True, 'job_dir': res['job_dir'],
                    'changes': list(res.get('changes') or []),
                    'warnings': warnings, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'job_dir': None, 'changes': [],
                    'warnings': [], 'error': str(e)}

    # ══════════════════════════════════════════════════════════════════════════
    # 一、全 DFT 任务目录(②生成输入页):计算类型目录 + U 建议 + 派生分发 + 任务解析
    # ══════════════════════════════════════════════════════════════════════════
    # 计算类型 key → estatic build_static_job 的 purpose(电子结构静态派生一族)。
    # 注意:'chgdiff' 不在此表——差分电荷不是单个静态,而是 AB/A/B 三冻结几何静态,
    # 单独走 chgdiff.build_chgdiff_jobs(见 derive_task 的 chgdiff 分支),名副其实。
    _DERIVE_ELECTRONIC = {'static': 'esp', 'dos_pdos': 'pdos', 'bader': 'bader',
                          'elf': 'elf'}

    @staticmethod
    def _task_badge(builder_ref):
        """按 builder_ref 分类任务性质徽标:作业生成 / 结果计算器 / INCAR 顾问(P2 卡片区分)。

        - INCAR 顾问:incar_builder 系(如 vaspsol_keys 只给键、不建作业);
        - 结果计算器:从已完成作业算标量的纯函数(surface_energy / binding_energy / formation_energy);
        - 作业生成:其余(建作业目录/系列的 builder,create_project 等)。
        """
        ref = str(builder_ref or '')
        mod, _, func = ref.partition(':')
        if 'incar_builder' in mod:
            return 'INCAR 顾问'
        if func in ('surface_energy', 'binding_energy', 'formation_energy'):
            return '结果计算器'
        return '作业生成'

    def task_catalog(self):
        """计算类型目录(五分类 23 项)→ {'ok','categories','tasks':[{key,name_zh,category,
        description,requires,outputs,figure,builder_ref,kind_badge}],'error'}。前端据此渲染卡片
        网格与参数表单;kind_badge 按 builder_ref 分「作业生成/结果计算器/INCAR 顾问」区分。"""
        try:
            tc = self._tc()
            tasks = [{'key': t['key'], 'name_zh': t.get('name_zh', t['key']),
                      'category': t.get('category', ''),
                      'description': t.get('description', ''),
                      'requires': t.get('requires', ''),
                      'outputs': t.get('outputs', ''), 'figure': t.get('figure'),
                      'builder_ref': t.get('builder_ref', ''),
                      'kind_badge': self._task_badge(t.get('builder_ref', ''))}
                     for t in tc.list_catalog()]
            return {'ok': True, 'categories': list(tc.CATEGORIES),
                    'tasks': tasks, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'categories': [], 'tasks': [], 'error': str(e)}

    def u_suggest(self, elements):
        """DFT+U 建议表:按元素查 U 库 → {'ok','suggestions':[{element,u,l,orbital,source,note}],
        'missing':[库内无经验 U 的元素],'incar_keys':LDAU 系列键(按输入序),'error'}。

        库内未登记的元素**不编造 U**(计入 missing);incar_keys 供「应用到 INCAR」直接写入。
        """
        try:
            els = [str(e).strip() for e in (elements or []) if str(e).strip()]
            ul = self._ul()
            sugg = list(ul.suggest_u(els))
            have = {s['element'] for s in sugg}
            keys = {}
            try:
                keys = ul.ldau_keys(sugg, els)
            except Exception:                             # noqa: BLE001 键组装失败不挡建议表
                keys = {}
            return {'ok': True, 'suggestions': sugg,
                    'missing': [e for e in els if e not in have],
                    'incar_keys': keys, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'suggestions': [], 'missing': [],
                    'incar_keys': {}, 'error': str(e)}

    def _derive_ret(self, key, dirs, changes, warnings, *, series=None, extra=None):
        """派生统一返回:逐目录入台账(失败并入 warnings,不撤销已生成目录)。"""
        warns = list(warnings or [])
        for d in dirs:
            try:
                self._ledger.register(d)
            except Exception as e:                        # noqa: BLE001
                warns.append(f'台账登记失败({os.path.basename(str(d))}):{e}')
        out = {'ok': True, 'key': key, 'job_dirs': [str(d) for d in dirs],
               'series': series, 'changes': list(changes or []),
               'warnings': warns, 'error': None}
        if extra:
            out.update(extra)
        return out

    @staticmethod
    def _series_dirs(res):
        """系列 build 返回 → (dirs 列表, series 视图)。"""
        series = list(res.get('series') or [])
        dirs = [s.get('dir') for s in series if s.get('dir')]
        return dirs, series

    def derive_task(self, key, src_dir, params=None):
        """按计算类型 key 从源作业目录派生下一步作业(分发到对应 builder)→ 入台账。

        支持:cellopt / static / dos_pdos / bader / chgdiff / elf / bands / eos /
        workfunction / dimer / freq / aimd / conv_encut / conv_kmesh / conv_vacuum /
        conv_thickness。返回 {'ok','key','job_dirs':[...],'series':[...]|None,'changes',
        'warnings','error'}。不可派生的 key(如吸附能项目/表面能计算器)→ ok=False + 中文说明。
        """
        try:
            k = str(key or '').strip()
            d = (src_dir or '').strip()
            if not d or not os.path.isdir(d):
                return {'ok': False, 'key': k, 'job_dirs': [], 'series': None,
                        'changes': [], 'warnings': [], 'error': '源作业目录不存在'}
            p = dict(params or {})
            base = os.path.basename(os.path.normpath(d))
            parent = (p.get('out_root') or '').strip() or os.path.dirname(
                os.path.normpath(d))

            if k == 'cellopt':
                out_dir = os.path.join(parent, f'{base}_cellopt')
                res = self._co().build_cellopt_job(
                    d, out_dir, bump_encut=bool(p.get('bump_encut', True)))
                return self._derive_ret(k, [res['out_dir']], res.get('changes'),
                                        res.get('warnings'))
            if k in self._DERIVE_ELECTRONIC:
                out_dir = os.path.join(parent, f'{base}_{k}')
                res = self._es().build_static_job(
                    d, out_dir, purpose=self._DERIVE_ELECTRONIC[k])
                return self._derive_ret(k, [res['out_dir']], res.get('changes'),
                                        res.get('warnings'))
            if k == 'chgdiff':
                # 差分电荷:名副其实拆 AB/A/B 三冻结几何静态(此前误接单静态,拿不到 Δρ)。
                idx = p.get('adsorbate_indices') or p.get('indices')
                ads = [int(i) for i in idx] if idx else []
                if not ads:
                    return {'ok': False, 'key': k, 'job_dirs': [], 'series': None,
                            'changes': [], 'warnings': [],
                            'error': ('差分电荷需指定吸附质原子序号(1 起,对齐 POSCAR),'
                                      '据此拆 AB/仅表面 A/仅吸附质 B 三冻结几何静态;'
                                      '请在参数里填「吸附质原子序号」。')}
                out_root = os.path.join(parent, f'{base}_chgdiff')
                res = self._chg().build_chgdiff_jobs(d, out_root, ads)
                dirs = [str(v) for v in (res.get('dirs') or {}).values()]
                changes = [f'差分电荷三静态已拆:{", ".join(os.path.basename(x) for x in dirs)}'
                           f'(AB 全原子 / A 仅表面 / B 仅吸附质,同冻结几何)']
                warns = []
                for r in (res.get('results') or {}).values():
                    warns += list((r or {}).get('warnings') or [])
                return self._derive_ret(k, dirs, changes, warns,
                                        extra={'chgdiff_dirs': res.get('dirs')})
            if k == 'bands':
                out_dir = os.path.join(parent, f'{base}_bands')
                res = self._bd().build_bands_job(
                    d, out_dir, lattice=(p.get('lattice') or None),
                    npoints=int(p.get('npoints', 40) or 40))
                return self._derive_ret(k, [res['out_dir']], res.get('changes'),
                                        res.get('warnings'),
                                        extra={'lattice': res.get('lattice')})
            if k == 'workfunction':
                out_dir = os.path.join(parent, f'{base}_wf')
                res = self._wf().build_workfunction_job(
                    d, out_dir, add_dipole=str(p.get('add_dipole', 'auto')))
                return self._derive_ret(k, [res['out_dir']], res.get('changes'),
                                        res.get('warnings'),
                                        extra={'dipole': res.get('dipole')})
            if k == 'dimer':
                out_dir = os.path.join(parent, f'{base}_dimer')
                kw = {}
                if p.get('amplitude') not in (None, ''):
                    kw['amplitude'] = float(p['amplitude'])
                if p.get('displaced_poscar'):
                    kw['displaced_poscar'] = str(p['displaced_poscar'])
                res = self._dm().build_dimer_job(d, out_dir, **kw)
                return self._derive_ret(k, [res['out_dir']], res.get('changes'),
                                        res.get('warnings'),
                                        extra={'modecar_method': res.get('modecar_method')})
            if k == 'eos':
                out_root = os.path.join(parent, f'{base}_eos')
                kw = {}
                if p.get('scales'):
                    kw['scales'] = [float(x) for x in p['scales']]
                res = self._eos_().build_eos_series(d, out_root, **kw)
                dirs, series = self._series_dirs(res)
                return self._derive_ret(k, dirs, [], res.get('warnings'), series=series)
            if k in ('conv_encut', 'conv_kmesh', 'conv_vacuum', 'conv_thickness'):
                return self._derive_conv(k, d, parent, base, p)
            if k == 'freq':
                r = self.derive_freq(d, out_root=parent)
                return {'ok': r['ok'], 'key': k,
                        'job_dirs': [r['job_dir']] if r.get('job_dir') else [],
                        'series': None, 'changes': r.get('changes') or [],
                        'warnings': r.get('warnings') or [], 'error': r.get('error')}
            if k == 'aimd':
                r = self.derive_aimd(
                    d, ensemble=str(p.get('ensemble', 'nvt')),
                    temp_k=float(p.get('temp_k', 300.0) or 300.0),
                    temp_end_k=p.get('temp_end_k'), steps=int(p.get('steps', 10000) or 10000),
                    potim_fs=float(p.get('potim_fs', 1.0) or 1.0),
                    encut=p.get('encut'), out_root=parent)
                return {'ok': r['ok'], 'key': k,
                        'job_dirs': [r['job_dir']] if r.get('job_dir') else [],
                        'series': None, 'changes': r.get('changes') or [],
                        'warnings': r.get('warnings') or [], 'error': r.get('error')}
            return {'ok': False, 'key': k, 'job_dirs': [], 'series': None,
                    'changes': [], 'warnings': [],
                    'error': f'任务类型 {k!r} 不支持一键派生(如吸附能项目/表面能计算器请走专用流程)'}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'key': str(key), 'job_dirs': [], 'series': None,
                    'changes': [], 'warnings': [], 'error': str(e)}

    def _derive_conv(self, k, d, parent, base, p):
        """收敛扫描系列派生(encut/kmesh/vacuum/thickness):默认值兜底,系列各作业入台账。

        conv_thickness 诚实化:build_slab_thickness_series 无 slab_builder_fn 时返回空作业 + note
        (裸 CONTCAR 无米勒面/终止面信息,无法再生不同层数 slab);此前 note 被吞掉、前端误报
        成功。现把 note 并入 warnings,并在 extra 透传 note 供前端按「0 作业 + note」显示 warn 级
        说明(而非成功 toast)——绝不假成功。
        """
        cs = self._cs()
        out_root = os.path.join(parent, f'{base}_{k}')
        if k == 'conv_encut':
            vals = [int(x) for x in (p.get('values') or [])] or None
            res = (cs.build_encut_series(d, out_root, values=vals) if vals
                   else cs.build_encut_series(d, out_root))
        elif k == 'conv_kmesh':
            meshes = p.get('meshes') or [[3, 3, 1], [5, 5, 1], [7, 7, 1]]
            res = cs.build_kmesh_series(d, out_root, meshes)
        elif k == 'conv_vacuum':
            vacs = [float(x) for x in (p.get('vacuums') or [10, 12, 15, 18])]
            res = cs.build_vacuum_series(d, out_root, vacs)
        else:  # conv_thickness
            layers = [int(x) for x in (p.get('layers') or [3, 4, 5])]
            res = cs.build_slab_thickness_series(d, out_root, layers)
        dirs, series = self._series_dirs(res)
        warns = list(res.get('warnings') or [])
        note = res.get('note')
        # note(层厚需从建 slab 流程发起)不吞掉:并入 warnings,并单列 extra 供前端判 warn。
        if note and note not in warns:
            warns.append(note)
        return self._derive_ret(k, dirs, [], warns, series=series,
                                extra={'note': note})

    # ── 输入读取小工具(NEB / 计算器共用;优先 CONTCAR) ─────────────────────────
    @staticmethod
    def _read_struct_text(job_dir):
        """作业目录结构文本:优先 CONTCAR(已弛豫),回落 POSCAR;都无 → None。"""
        for name in ('CONTCAR', 'POSCAR'):
            p = os.path.join(job_dir, name)
            if os.path.isfile(p):
                try:
                    with open(p, encoding='utf-8', errors='replace') as f:
                        return f.read()
                except OSError:
                    return None
        return None

    @staticmethod
    def _read_named_text(job_dir, name):
        """读作业目录下指定文件文本;缺文件/读失败 → None。"""
        p = os.path.join(job_dir, name)
        if os.path.isfile(p):
            try:
                with open(p, encoding='utf-8', errors='replace') as f:
                    return f.read()
            except OSError:
                return None
        return None

    @staticmethod
    def _fmt_incar_value(v):
        """INCAR 值格式化:bool→.TRUE./.FALSE.,float→紧凑,其余原样。"""
        if isinstance(v, bool):
            return '.TRUE.' if v else '.FALSE.'
        if isinstance(v, float):
            return f'{v:g}'
        return str(v)

    @classmethod
    def _incar_lines_from(cls, keys):
        """INCAR 键 dict → ['KEY = VALUE', ...](供预览/追加)。"""
        return [f'{k} = {cls._fmt_incar_value(v)}' for k, v in (keys or {}).items()]

    @staticmethod
    def _species_counts(text):
        """POSCAR/CONTCAR 文本 → {元素: 计数} dict;解析失败 → {}。"""
        try:
            from vcstudio.generate.poscar import parse_poscar_species
            syms, counts = parse_poscar_species(text)
            out = {}
            for s, c in zip(syms or [], counts or []):
                out[s] = out.get(s, 0) + int(c)
            return out
        except Exception:                                 # noqa: BLE001
            return {}

    # ── P0-1 NEB 过渡态接通(始/末态目录 → 标准 NEB 目录树) ──────────────────────
    # 注:nimages/out_root 为位置或关键字参(非 keyword-only)——前端经 pywebview 桥按位置
    # 传参,keyword-only 会断桥;与 gen_run/wavefn_run 等 JS 面向方法同口径。
    def derive_neb(self, start_dir, end_dir, nimages=5, out_root=None):
        """NEB 过渡态接通:始/末态目录 → neb_builder.build_neb_dir(插值 + 多 image 目录树)→ 入台账。

        - start_dir/end_dir:已弛豫的初/末态作业目录(读 CONTCAR 优先、否则 POSCAR)。
        - INCAR 取自 start_dir(否则 end_dir);两者皆缺 → 中文错误(NEB 须用户电子学设置)。
        - POTCAR:start_dir 有则透传其路径,否则 build 侧告警、提交前补齐。
        返回 {'ok','job_dir','n_images','changes','warnings','error'}。端点不一致/插值重叠等 →
        error(引擎 ValueError 冒泡),绝不假成功。
        """
        try:
            s = (start_dir or '').strip()
            e = (end_dir or '').strip()
            if not s or not os.path.isdir(s):
                return {'ok': False, 'job_dir': None, 'n_images': 0, 'changes': [],
                        'warnings': [], 'error': '始态作业目录不存在'}
            if not e or not os.path.isdir(e):
                return {'ok': False, 'job_dir': None, 'n_images': 0, 'changes': [],
                        'warnings': [], 'error': '末态作业目录不存在'}
            ini = self._read_struct_text(s)
            fin = self._read_struct_text(e)
            if not ini:
                return {'ok': False, 'job_dir': None, 'n_images': 0, 'changes': [],
                        'warnings': [], 'error': '始态目录缺 CONTCAR/POSCAR,无法作 NEB 初态'}
            if not fin:
                return {'ok': False, 'job_dir': None, 'n_images': 0, 'changes': [],
                        'warnings': [], 'error': '末态目录缺 CONTCAR/POSCAR,无法作 NEB 末态'}
            incar = self._read_named_text(s, 'INCAR') or self._read_named_text(e, 'INCAR')
            if not incar:
                return {'ok': False, 'job_dir': None, 'n_images': 0, 'changes': [],
                        'warnings': [],
                        'error': 'NEB 须用户 INCAR(始/末态目录均无 INCAR);请先在始态目录放置 INCAR'}
            try:
                n = int(nimages)
            except (TypeError, ValueError):
                n = 5
            base = os.path.basename(os.path.normpath(s))
            parent = (out_root or '').strip() or os.path.dirname(os.path.normpath(s))
            out_dir = os.path.join(parent, f'{base}_neb')
            potcar_path = os.path.join(s, 'POTCAR')
            potcar_fn = potcar_path if os.path.isfile(potcar_path) else None
            res = self._neb().build_neb_dir(
                out_dir, ini, fin, incar, n_images=n, potcar_fn=potcar_fn)
            warns = list(res.get('warnings') or [])
            try:
                self._ledger.register(res['job_dir'])
            except Exception as ex:                       # noqa: BLE001
                warns.append(f'台账登记失败(不影响已生成 NEB 目录):{ex}')
            changes = [f'已生成标准 NEB 目录树:00 初态 + {res["n_images"]} 个中间 image + 末态,'
                       f'根目录共享 INCAR/POTCAR/KPOINTS(INCAR 只补不改补齐 IMAGES/SPRING 等)']
            return {'ok': True, 'job_dir': res['job_dir'], 'n_images': res['n_images'],
                    'changes': changes, 'warnings': warns, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'job_dir': None, 'n_images': 0, 'changes': [],
                    'warnings': [], 'error': str(e)}

    # ── P0-2 形成能/结合能计算器 ───────────────────────────────────────────────
    # 位置或关键字参(非 keyword-only):前端经 pywebview 桥按位置传参,keyword-only 会断桥。
    def formation_binding_calc(self, sac_dir, substrate_dir=None,
                               atom_energies=None, chem_pots=None):
        """形成能/结合能计算器:读各作业能量 → references.binding_energy/formation_energy + σ 稳定性。

        - sac_dir:单原子催化剂(SAC)作业目录(读 OSZICAR 末 E0 作 E_sac,读 CONTCAR/POSCAR 组成)。
        - substrate_dir:含空位、未嵌金属的基底作业目录(E_substrate);Eb 与 Ef 都相对它。
        - atom_energies:{元素: 孤立原子能量 eV}(结合能用金属原子能;缺 → 无法算 Eb 的中文提示)。
        - chem_pots:{元素: 化学势 μ eV/atom}(形成能用;counts 取 SAC−基底 组成差)。
        返回 {'ok','e_sac','e_substrate','metal','counts','binding_energy','formation_energy',
        'sigma','stable','stability_note','hints','warnings','error'}。缺量只提示、绝不编造。
        """
        try:
            sd = (sac_dir or '').strip()
            if not sd or not os.path.isdir(sd):
                return {'ok': False, 'e_sac': None, 'e_substrate': None, 'metal': None,
                        'counts': {}, 'binding_energy': None, 'formation_energy': None,
                        'sigma': None, 'stable': None, 'stability_note': None,
                        'hints': [], 'warnings': [], 'error': 'SAC 作业目录不存在'}
            e_sac = self._osz_energy(sd)
            if e_sac is None:
                return {'ok': False, 'e_sac': None, 'e_substrate': None, 'metal': None,
                        'counts': {}, 'binding_energy': None, 'formation_energy': None,
                        'sigma': None, 'stable': None, 'stability_note': None,
                        'hints': [], 'warnings': [],
                        'error': 'SAC 作业无 OSZICAR / 未跑完,取不到 E_sac 能量'}
            refs = self._refs()
            hints, warnings = [], []
            sac_counts = self._species_counts(self._read_struct_text(sd) or '')

            # 基底能量 + 组成
            e_sub, sub_counts = None, {}
            sub = (substrate_dir or '').strip()
            if sub and os.path.isdir(sub):
                e_sub = self._osz_energy(sub)
                sub_counts = self._species_counts(self._read_struct_text(sub) or '')
                if e_sub is None:
                    hints.append('基底作业无 OSZICAR / 未跑完,取不到 E_substrate;结合能/形成能暂缺。')

            # 组成差 counts(SAC − 基底);金属 = 差值为正且在 atom_energies 中的元素
            delta = {}
            if sac_counts and sub_counts:
                for el in set(sac_counts) | set(sub_counts):
                    d = sac_counts.get(el, 0) - sub_counts.get(el, 0)
                    if d != 0:
                        delta[el] = d
            ae = {}
            for k, v in (atom_energies or {}).items():
                try:
                    ae[str(k)] = float(v)
                except (TypeError, ValueError):
                    continue
            added = [el for el, d in delta.items() if d > 0 and el in ae]
            metal = None
            if len(added) == 1:
                metal = added[0]
            elif added:
                metal = added[0]
            elif len(ae) == 1:
                metal = next(iter(ae))

            # 结合能 Eb + σ 稳定性
            eb, sigma, stable, stab_note = None, None, None, None
            if e_sub is None and sub:
                pass                                       # 已在上面提示
            elif not sub:
                hints.append('结合能需基底作业(含空位、未嵌金属)的能量;请选「基底作业目录」。')
            elif not ae:
                hints.append('结合能需金属孤立原子能量;请填「金属原子能量」(元素→eV)。')
            elif metal is None:
                hints.append('无法判定嵌入的金属元素(SAC 与基底组成差不明确);'
                             '请确认所选作业,或让金属原子能量只含目标金属。')
            else:
                eb = refs.binding_energy(e_sac, e_sub, ae[metal])
                try:
                    ecoh = refs.cohesive_energy(metal)
                    verdict = refs.stability_verdict(eb, ecoh)
                    sigma, stable, stab_note = verdict['sigma'], verdict['stable'], verdict['note']
                except ValueError as ve:
                    hints.append(f'稳定性 σ 需金属内聚能:{ve}')

            # 形成能 Ef(以基底为参考,counts 取组成差,μ 取 chem_pots)
            ef = None
            cp = {}
            for k, v in (chem_pots or {}).items():
                try:
                    cp[str(k)] = float(v)
                except (TypeError, ValueError):
                    continue
            if e_sub is None:
                if not sub:
                    hints.append('形成能需参考态(基底)作业能量;请选基底作业目录。')
            elif not delta:
                hints.append('形成能需 SAC 与基底的组成差(判掺入/移除的物种);'
                             '请确认两作业结构可解析组成。')
            elif not cp:
                hints.append('形成能需各变动物种的化学势 μ(eV/atom);请填「化学势」。')
            else:
                try:
                    ef = refs.formation_energy(e_sac, e_sub, cp, delta)
                except ValueError as ve:
                    hints.append(f'形成能:{ve}')

            return {'ok': (eb is not None) or (ef is not None),
                    'e_sac': e_sac, 'e_substrate': e_sub, 'metal': metal,
                    'counts': delta, 'binding_energy': eb, 'formation_energy': ef,
                    'sigma': sigma, 'stable': stable, 'stability_note': stab_note,
                    'hints': hints, 'warnings': warnings, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'e_sac': None, 'e_substrate': None, 'metal': None,
                    'counts': {}, 'binding_energy': None, 'formation_energy': None,
                    'sigma': None, 'stable': None, 'stability_note': None,
                    'hints': [], 'warnings': [], 'error': str(e)}

    # ── P0-4 差分电荷合成(三作业 CHGCAR → CHGDIFF.vasp + 面平均) ──────────────────
    def compute_chgdiff(self, ab_dir, a_dir, b_dir, out_dir=None):
        """差分电荷合成:三作业 CHGCAR → chgdiff.compute_chgdiff 出 CHGDIFF.vasp + 面平均 Δρ̄(z)。

        返回 {'ok','out','max','min','n_grid','profile':{'z','rho','axis'},'error'}。
        任一 CHGCAR 缺失/网格或晶格不一致/原子数不守恒 → 中文 error(引擎硬校验冒泡),不静默错算。
        """
        try:
            paths = {}
            for tag, dd in (('AB', ab_dir), ('A', a_dir), ('B', b_dir)):
                d = (dd or '').strip()
                if not d or not os.path.isdir(d):
                    return {'ok': False, 'out': None, 'max': None, 'min': None,
                            'n_grid': 0, 'profile': {}, 'error': f'{tag} 作业目录不存在'}
                cp = os.path.join(d, 'CHGCAR')
                if not os.path.isfile(cp):
                    return {'ok': False, 'out': None, 'max': None, 'min': None,
                            'n_grid': 0, 'profile': {},
                            'error': f'{tag} 作业目录缺 CHGCAR;差分电荷需三体系各自自洽 CHGCAR,'
                                     '请先跑完三个静态作业。'}
                paths[tag] = cp
            out = (out_dir or '').strip() or os.path.dirname(paths['AB'])
            os.makedirs(out, exist_ok=True)
            out_path = os.path.join(out, 'CHGDIFF.vasp')
            chg = self._chg()
            res = chg.compute_chgdiff(paths['AB'], paths['A'], paths['B'], out_path)
            profile = {}
            try:
                profile = chg.plane_averaged(out_path, 'z')
            except Exception:                             # noqa: BLE001 面平均失败不挡主产物
                profile = {}
            return {'ok': True, 'out': res['out'], 'max': res['max'], 'min': res['min'],
                    'n_grid': res['n_grid'], 'profile': profile, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'out': None, 'max': None, 'min': None, 'n_grid': 0,
                    'profile': {}, 'error': str(e)}

    # ── P0-3 VASPsol 隐式溶剂化预览(将写入的键 + 补丁编译 warning) ─────────────────
    def vaspsol_preview(self, eb_k=78.4, enabled=True):
        """VASPsol 隐式溶剂化预览:incar_builder.vaspsol_keys → 将写入的 INCAR 键 + 补丁编译 warning。

        返回 {'ok','keys','incar_lines','warning','error'}。供②生成页「高级」区在勾选前后展示
        (标准 VASP 无 VASPsol 会**静默**给真空结果的陷阱一并提醒,绝不假装已溶剂化)。
        """
        try:
            ib = self._ib()
            keys = ib.vaspsol_keys(bool(enabled), eb_k=float(eb_k))
            return {'ok': True, 'keys': keys, 'incar_lines': self._incar_lines_from(keys),
                    'warning': getattr(ib, 'VASPSOL_ADVISORY', ''), 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'keys': {}, 'incar_lines': [], 'warning': '', 'error': str(e)}

    # ── ④结果分析页·任务解析(按 task_type 自动解析 + 出图) ──────────────────────
    @staticmethod
    def _osz_energy(job_dir):
        """OSZICAR 末态自洽能 E0 → float|None(未跑完/缺文件 → None)。"""
        try:
            p = os.path.join(job_dir, 'OSZICAR')
            if not os.path.isfile(p):
                return None
            e = None
            with open(p, encoding='utf-8', errors='replace') as f:
                for ln in f:
                    if 'E0=' in ln:
                        try:
                            e = float(ln.split('E0=')[1].split()[0])
                        except (IndexError, ValueError):
                            pass
            return e
        except Exception:                                 # noqa: BLE001
            return None

    @staticmethod
    def _poscar_volume(job_dir):
        """CONTCAR/POSCAR 晶胞体积(Å³)→ float|None(scale>0 时乘 scale³)。"""
        try:
            for name in ('CONTCAR', 'POSCAR'):
                p = os.path.join(job_dir, name)
                if not os.path.isfile(p):
                    continue
                lines = open(p, encoding='utf-8', errors='replace').read().splitlines()
                if len(lines) < 5:
                    continue
                scale = float(lines[1].split()[0])
                a = [[float(x) for x in lines[2].split()[:3]],
                     [float(x) for x in lines[3].split()[:3]],
                     [float(x) for x in lines[4].split()[:3]]]
                det = (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
                       - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
                       + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))
                return abs(det) * (scale ** 3 if scale > 0 else 1.0)
            return None
        except Exception:                                 # noqa: BLE001
            return None

    @staticmethod
    def _outcar_efermi(job_dir):
        """OUTCAR 费米能 E-fermi → float|None。"""
        try:
            import re as _re
            p = os.path.join(job_dir, 'OUTCAR')
            if not os.path.isfile(p):
                return None
            ef = None
            with open(p, encoding='utf-8', errors='replace') as f:
                for ln in f:
                    if 'E-fermi' in ln:
                        m = _re.search(r'E-fermi\s*:\s*([-\d.]+)', ln)
                        if m:
                            ef = float(m.group(1))
            return ef
        except Exception:                                 # noqa: BLE001
            return None

    def _infer_task_kind(self, d):
        """从 manifest task_type 或目录内容推断解析类型(conv/eos/bands/workfunction)。"""
        try:
            m = self._manifest.load_manifest(d)
            tt = str((m or {}).get('task_type') or '').lower()
            if 'eos' in tt:
                return 'eos'
            if 'band' in tt:
                return 'bands'
            if 'work' in tt or tt == 'workfunction':
                return 'workfunction'
            if tt in ('encut', 'kmesh', 'vacuum', 'thickness') or 'conv' in tt:
                return 'conv'
        except Exception:                                 # noqa: BLE001
            pass
        try:
            subs = [x for x in os.listdir(d) if os.path.isdir(os.path.join(d, x))]
        except OSError:
            subs = []
        if any(x.startswith('eos_') for x in subs):
            return 'eos'
        if any(x.split('_')[0] in ('encut', 'kmesh', 'vac', 'thick', 'slab') for x in subs):
            return 'conv'
        if os.path.isfile(os.path.join(d, 'LOCPOT')):
            return 'workfunction'
        if os.path.isfile(os.path.join(d, 'EIGENVAL')):
            return 'bands'
        return ''

    def analyze_task(self, job_dir, kind=None):
        """任务解析:按 task_type(或推断)自动调解析器 + 出图 → {'ok','kind','result',
        'figure','summary','files','error'}。支持 conv 曲线 / EOS 拟合 / 能带带隙 / 功函数。"""
        try:
            d = (job_dir or '').strip()
            if not d or not os.path.isdir(d):
                return {'ok': False, 'kind': None, 'result': None, 'figure': None,
                        'summary': '', 'files': [], 'error': '作业目录不存在'}
            k = (kind or '').strip().lower() or self._infer_task_kind(d)
            if k.startswith('conv') or k in ('encut', 'kmesh', 'vacuum', 'thickness'):
                return self._analyze_conv(d)
            if k == 'eos':
                return self._analyze_eos(d)
            if k in ('bands', 'band'):
                return self._analyze_bands(d)
            if k in ('workfunction', 'work_function', 'wf'):
                return self._analyze_workfunction(d)
            return {'ok': False, 'kind': k or None, 'result': None, 'figure': None,
                    'summary': '', 'files': [],
                    'error': '无法识别任务类型(支持 conv/eos/bands/workfunction;可显式传 kind)'}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'kind': None, 'result': None, 'figure': None,
                    'summary': '', 'files': [], 'error': str(e)}

    def _analyze_conv(self, d):
        subs = sorted(os.path.join(d, x) for x in os.listdir(d)
                      if os.path.isdir(os.path.join(d, x))
                      and x.split('_')[0] in ('encut', 'kmesh', 'vac', 'thick', 'slab'))
        dirs = subs or [d]
        cs = self._cs()
        res = cs.analyze_series(dirs)
        fig = None
        try:
            pts = [p for p in (res.get('points') or []) if p.get('energy') is not None]
            if pts:
                out_png = os.path.join(d, 'convergence.png')
                cs.conv_plot(pts, out_png, converged_at=res.get('converged_at'))
                fig = out_png
        except Exception:                                 # noqa: BLE001 出图失败不挡解析
            fig = None
        return {'ok': True, 'kind': 'conv', 'result': res, 'figure': fig,
                'summary': res.get('note', ''),
                'files': [fig] if fig else [], 'error': None}

    def _analyze_eos(self, d):
        subs = sorted(os.path.join(d, x) for x in os.listdir(d)
                      if os.path.isdir(os.path.join(d, x)) and x.startswith('eos_'))
        dirs = subs or [d]
        vols, ens = [], []
        for jd in dirs:
            v, e = self._poscar_volume(jd), self._osz_energy(jd)
            if v is not None and e is not None:
                vols.append(v)
                ens.append(e)
        if len(vols) < 3:
            return {'ok': False, 'kind': 'eos', 'result': None, 'figure': None,
                    'summary': '', 'files': [],
                    'error': f'EOS 拟合需 ≥3 个含体积/能量的作业(现有 {len(vols)} 个,尚未跑完?)'}
        eos = self._eos_()
        fit = eos.fit_birch_murnaghan(vols, ens)
        fig = None
        try:
            out_png = os.path.join(d, 'eos.png')
            eos.eos_plot([{'volume': v, 'energy': e} for v, e in zip(vols, ens)],
                         fit, out_png)
            fig = out_png
        except Exception:                                 # noqa: BLE001
            fig = None
        summary = (f"V0 = {fit.get('v0'):.3f} Å³,E0 = {fit.get('e0'):.4f} eV,"
                   f"B0 = {fit.get('b0_gpa'):.1f} GPa,B0' = {fit.get('b0_prime'):.2f},"
                   f"R² = {fit.get('r2'):.4f}") if fit else ''
        return {'ok': True, 'kind': 'eos', 'result': fit, 'figure': fig,
                'summary': summary, 'files': [fig] if fig else [], 'error': None}

    def _analyze_bands(self, d):
        src = None
        for name in ('EIGENVAL', 'vasprun.xml'):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                src = open(p, encoding='utf-8', errors='replace').read()
                break
        if src is None:
            return {'ok': False, 'kind': 'bands', 'result': None, 'figure': None,
                    'summary': '', 'files': [], 'error': '未找到 EIGENVAL / vasprun.xml'}
        bp = self._bp()
        ef = self._outcar_efermi(d)
        data = bp.parse_bands(src, efermi=ef)
        gap = data.get('gap') or {}
        fig = None
        try:
            out_png = os.path.join(d, 'band.png')
            bp.band_plot(data, out_png, efermi=ef)
            fig = out_png
        except Exception:                                 # noqa: BLE001
            fig = None
        if gap.get('metal'):
            summary = '金属性(无带隙)。'
        elif gap.get('value') is not None:
            summary = (f"带隙 = {gap['value']:.3f} eV"
                       f"({'直接' if gap.get('direct') else '间接'}带隙)。")
        else:
            summary = gap.get('note', '未能判定带隙。')
        return {'ok': True, 'kind': 'bands', 'result': {'gap': gap}, 'figure': fig,
                'summary': summary, 'files': [fig] if fig else [], 'error': None}

    def _analyze_workfunction(self, d):
        locpot = os.path.join(d, 'LOCPOT')
        if not os.path.isfile(locpot):
            return {'ok': False, 'kind': 'workfunction', 'result': None, 'figure': None,
                    'summary': '', 'files': [], 'error': '未找到 LOCPOT'}
        wf = self._wf()
        ef = self._outcar_efermi(d)
        if ef is None:
            return {'ok': False, 'kind': 'workfunction', 'result': None, 'figure': None,
                    'summary': '', 'files': [], 'error': '未能从 OUTCAR 读到费米能 E-fermi'}
        planar = wf.parse_locpot_planar(locpot)
        res = wf.work_function(planar['v_planar'], planar['z'], ef)
        fig = None
        try:
            out_png = os.path.join(d, 'work_function.png')
            wf.wf_plot(planar['v_planar'], planar['z'], ef, out_png,
                       vacuum_level=res.get('vacuum_level'), phi=res.get('phi'))
            fig = out_png
        except Exception:                                 # noqa: BLE001
            fig = None
        phi = res.get('phi')
        summary = (f"功函数 φ = {phi:.3f} eV(真空能级 {res.get('vacuum_level'):.3f} eV,"
                   f"E_F = {ef:.3f} eV)。") if phi is not None else res.get('note', '')
        return {'ok': True, 'kind': 'workfunction', 'result': res, 'figure': fig,
                'summary': summary, 'files': [fig] if fig else [], 'error': None}

    def surface_energy_calc(self, slab_dir, bulk_dir, e_bulk_per_atom=None, area=None):
        """表面能计算器:选 slab + bulk 作业 → γ (J/m²)。面积从 slab POSCAR 自动算。

        e_bulk_per_atom 缺省时由 bulk 作业的 OSZICAR 能与原子数现算;area 缺省时由 slab POSCAR
        的 a×b 叉积面积算。返回 {'ok','gamma_jm2','area_a2','e_slab','n_slab','e_bulk_per_atom',
        'note','error'}。
        """
        try:
            sd = (slab_dir or '').strip()
            if not sd or not os.path.isdir(sd):
                return {'ok': False, 'gamma_jm2': None, 'error': 'slab 作业目录不存在'}
            e_slab = self._osz_energy(sd)
            if e_slab is None:
                return {'ok': False, 'gamma_jm2': None, 'error': 'slab 作业无可解析能量(未跑完?)'}
            n_slab = self._poscar_natoms(self._read_poscar_text(sd))
            if not n_slab:
                return {'ok': False, 'gamma_jm2': None, 'error': '无法从 slab POSCAR 读原子数'}
            se = self._se()
            if area in (None, ''):
                area = se.area_from_poscar(self._read_poscar_text(sd))
            e_bpa = e_bulk_per_atom
            if e_bpa in (None, ''):
                bd = (bulk_dir or '').strip()
                if not bd or not os.path.isdir(bd):
                    return {'ok': False, 'gamma_jm2': None,
                            'error': 'bulk 作业目录不存在(或直接填体相每原子能)'}
                e_bulk = self._osz_energy(bd)
                n_bulk = self._poscar_natoms(self._read_poscar_text(bd))
                if e_bulk is None or not n_bulk:
                    return {'ok': False, 'gamma_jm2': None,
                            'error': 'bulk 作业无可解析能量/原子数'}
                e_bpa = e_bulk / n_bulk
            gamma = se.surface_energy(float(e_slab), int(n_slab), float(e_bpa), float(area))
            return {'ok': True, 'gamma_jm2': float(gamma), 'area_a2': float(area),
                    'e_slab': float(e_slab), 'n_slab': int(n_slab),
                    'e_bulk_per_atom': float(e_bpa),
                    'note': f'γ = (E_slab − N·E_bulk)/2A = {gamma:.4f} J/m²', 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'gamma_jm2': None, 'error': str(e)}

    @staticmethod
    def _read_poscar_text(job_dir):
        for name in ('CONTCAR', 'POSCAR'):
            p = os.path.join(job_dir, name)
            if os.path.isfile(p):
                return open(p, encoding='utf-8', errors='replace').read()
        return ''

    # ══════════════════════════════════════════════════════════════════════════
    # 二、一键出图管线接线:活动模板 + 溶剂化复合物 + 出图偏好
    # ══════════════════════════════════════════════════════════════════════════
    def campaign_templates(self):
        """计算活动模板清单 → {'ok','templates':[{key,name_zh,description,figures_scenario,
        n_stages,analyses}],'error'}(①结构建模 SAC 矩阵卡下拉)。"""
        try:
            tpl = self._ct().list_templates()
            out = [{'key': k, 'name_zh': v.get('name_zh', k),
                    'description': v.get('description', ''),
                    'figures_scenario': v.get('figures_scenario'),
                    'n_stages': v.get('n_stages', 0),
                    'analyses': list(v.get('analyses') or [])}
                   for k, v in tpl.items()]
            return {'ok': True, 'templates': out, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'templates': [], 'error': str(e)}

    def campaign_instantiate(self, template_key, matrix_spec, out_root, title=None):
        """据模板 + 体系矩阵生成全链 DAG(落 campaign)→ 入仪表盘发现表。

        返回 {'ok','campaign_dir','stages','n_jobs','estimate','figures_scenario','error'}。
        """
        try:
            root = (out_root or '').strip()
            if not root:
                return {'ok': False, 'campaign_dir': None, 'error': '未指定输出根目录'}
            spec = dict(matrix_spec or {})
            if not (spec.get('systems') or []):
                return {'ok': False, 'campaign_dir': None,
                        'error': '体系矩阵至少需要一个 system(体系/催化剂)'}
            res = self._ct().instantiate(str(template_key or ''), spec, root,
                                         title=(title or None))
            cdir = res.get('campaign_dir')
            if cdir:
                self._register_campaign_dir(cdir)
            return {'ok': True, 'campaign_dir': cdir, 'stages': res.get('stages') or {},
                    'n_jobs': res.get('n_jobs', 0), 'estimate': res.get('estimate') or {},
                    'figures_scenario': res.get('figures_scenario'), 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'campaign_dir': None, 'error': str(e)}

    def solvent_presets(self):
        """溶剂配比预设清单 → {'ok','presets':[{key,recipe,label}],'error'}(溶剂化小卡下拉)。"""
        try:
            sv = self._sv()
            presets = [{'key': k, 'recipe': [[nm, n] for nm, n in v],
                        'label': '、'.join(f'{n}×{nm}' for nm, n in v)}
                       for k, v in sv.SOLVENT_PRESETS.items()]
            return {'ok': True, 'presets': presets, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'presets': [], 'error': str(e)}

    def build_solvated(self, core='Li2S3', solvents='lis_electrolyte', box=18.0,
                       min_sep=2.5, seed=42, save_to=None):
        """组装显式溶剂化复合物(核 + 溶剂 → 立方盒 POSCAR)→ {'ok','poscar','n_atoms','note',
        'saved_to','error'}。solvents 可为预设 key 或 [[名,个数],...];save_to 给了则落盘。"""
        try:
            sv = self._sv()
            if isinstance(solvents, (list, tuple)) and not isinstance(solvents, str):
                recipe = [(str(nm), int(cnt)) for nm, cnt in solvents]
            else:
                recipe = str(solvents)
            res = sv.build_solvated_complex(
                core=str(core or 'Li2S3'), solvents=recipe,
                box=float(box), min_sep=float(min_sep), seed=int(seed))
            saved = None
            dest = (save_to or '').strip()
            if dest:
                with open(dest, 'w', encoding='utf-8') as f:
                    f.write(res['poscar'])
                saved = dest
            # 另写一份到临时 POSCAR,供前端「载入编辑器」经 struct_load 读取
            temp_path = None
            try:
                fd, temp_path = tempfile.mkstemp(prefix='vcs_solv_', suffix='_POSCAR')
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    f.write(res['poscar'])
            except Exception:                             # noqa: BLE001 临时文件失败不致命
                temp_path = None
            return {'ok': True, 'poscar': res['poscar'], 'n_atoms': res.get('n_atoms'),
                    'note': res.get('note', ''), 'saved_to': saved,
                    'temp_path': temp_path, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'poscar': None, 'n_atoms': 0, 'note': '',
                    'saved_to': None, 'error': str(e)}

    _JOURNAL_STYLES = ('nature', 'acs', 'prb')

    def figure_prefs_get(self):
        """出图偏好读:期刊风格 / 自动出图开关 / 多面板开关(config ui.*,带默认)。"""
        try:
            ui = self._config.get_ui_state()
        except Exception:                                 # noqa: BLE001
            ui = {}
        js = str((ui or {}).get('journal_style') or 'nature').lower()
        return {'ok': True,
                'journal_style': js if js in self._JOURNAL_STYLES else 'nature',
                'auto_figures': bool((ui or {}).get('auto_figures', True)),
                'multi_panel': bool((ui or {}).get('multi_panel', True)),
                'error': None}

    def figure_prefs_save(self, journal_style=None, auto_figures=None, multi_panel=None):
        """保存出图偏好到 config ui.*(None 字段不改;非法期刊回落 nature)。"""
        try:
            kv = {}
            if journal_style is not None:
                js = str(journal_style).lower()
                kv['journal_style'] = js if js in self._JOURNAL_STYLES else 'nature'
            if auto_figures is not None:
                kv['auto_figures'] = bool(auto_figures)
            if multi_panel is not None:
                kv['multi_panel'] = bool(multi_panel)
            self._config.set_ui_state(**kv)
            return {'ok': True, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    # ══════════════════════════════════════════════════════════════════════════
    # 三、AI 助手三能力:数据对照 / 材料变体 / 论文草稿
    # ══════════════════════════════════════════════════════════════════════════
    def ai_extract_tables(self, source, transport=None):
        """论文正文 → 结构化文献数据表(LLM 只誊抄 + 确定性校验)→ {'ok','tables':[{label,kind,
        columns,rows:[{system,species,value_ev,page_hint}]}],'error'}。仅供对照,绝不回流计算。"""
        try:
            text = (source or '')
            if not str(text).strip():
                return {'ok': False, 'tables': [], 'error': '未提供论文文本'}
            res = self._pd().extract_data_tables(str(text), transport=transport)
            return {'ok': bool(res.get('ok')), 'tables': list(res.get('tables') or []),
                    'error': res.get('error')}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'tables': [], 'error': str(e)}

    def _project_computed(self, proj):
        """项目 → 计算侧对照集 [{system,species,quantity:'E_ads',value}](取各物种最稳 ΔE)。"""
        rows = (self._adsorption.delta_e_rows(proj) or {}).get('rows') or []
        name = str(proj.get('name') or '')
        out = []
        for r in rows:
            if r.get('is_most_stable') and r.get('delta_e') is not None:
                out.append({'system': name, 'species': r.get('species') or r.get('name'),
                            'quantity': 'E_ads', 'value': float(r['delta_e'])})
        return out

    def ai_compare(self, project_path, reference):
        """项目计算值 × 文献参考(体系×物种×量对齐)→ MAE/RMSE/最差3项/对照表(纯确定性)。

        reference 为 ai_extract_tables 的 tables(或已归一参考集)。返回 {'ok','n','mae','rmse',
        'worst','pairs','unmatched','summary','error'};无对齐项 ok=True 但 n=0 + 说明。
        """
        try:
            proj = self._adsorption.load_project((project_path or '').strip())
            if proj is None:
                return {'ok': False, 'n': 0, 'error': '项目不存在或 project.yaml 已移动'}
            pd = self._pd()
            ref = reference
            if isinstance(reference, list):
                ref = pd.build_reference_dataset(reference)
            elif isinstance(reference, dict) and 'entries' not in reference:
                ref = pd.build_reference_dataset(reference.get('tables') or [])
            computed = self._project_computed(proj)
            cmp = pd.compare_with_computed(ref, computed)
            return {'ok': True, 'n': cmp.get('n', 0), 'mae': cmp.get('mae'),
                    'rmse': cmp.get('rmse'), 'worst': list(cmp.get('worst') or []),
                    'pairs': list(cmp.get('pairs') or []),
                    'unmatched': list(cmp.get('unmatched') or []),
                    'summary': cmp.get('summary_zh', ''), 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'n': 0, 'error': str(e)}

    def ai_write_validation(self, project_path, reference, out=None):
        """把文献对照写成 validation.md(项目 report/ 下或指定路径)→ {'ok','path','n','error'}。"""
        try:
            proj = self._adsorption.load_project((project_path or '').strip())
            if proj is None:
                return {'ok': False, 'path': None, 'error': '项目不存在或 project.yaml 已移动'}
            pd = self._pd()
            ref = reference
            if isinstance(reference, list):
                ref = pd.build_reference_dataset(reference)
            elif isinstance(reference, dict) and 'entries' not in reference:
                ref = pd.build_reference_dataset(reference.get('tables') or [])
            cmp = pd.compare_with_computed(ref, self._project_computed(proj))
            md = pd.mae_report_md(cmp)
            dest = (out or '').strip()
            if not dest:
                root = proj.get('root') or os.path.dirname((project_path or '').strip())
                rdir = os.path.join(root, 'report')
                os.makedirs(rdir, exist_ok=True)
                dest = os.path.join(rdir, 'validation.md')
            with open(dest, 'w', encoding='utf-8') as f:
                f.write(md)
            return {'ok': True, 'path': dest, 'n': cmp.get('n', 0), 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'path': None, 'error': str(e)}

    def ai_variants(self, spec, budget_cap_hours=None):
        """材料变体推荐:母版规格表 → 变体列表 + 分批预算 + 可喂 sac_matrix 的矩阵谱。

        返回 {'ok','variants':[{kind,from,to,metal,template,parent,priority,rationale_zh}],
        'matrix_spec':{metals,templates},'plan':{n_jobs,estimate_hours,batches,note},'error'}。
        """
        try:
            va = self._va()
            sugg = va.suggest_variants(spec or {})
            variants = list(sugg.get('variants') or [])
            cap = None if budget_cap_hours in (None, '') else float(budget_cap_hours)
            plan = va.variant_campaign_plan(variants, budget_cap_hours=cap)
            return {'ok': True, 'variants': variants,
                    'matrix_spec': sugg.get('matrix_spec') or {'metals': [], 'templates': []},
                    'plan': plan, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'variants': [], 'matrix_spec': {},
                    'plan': {}, 'error': str(e)}

    def ai_manuscript(self, project_path, fmt='markdown', reference=None):
        """生成论文骨架:Methods 全自动 + Results 逐图数据句 + 占位待补 → 诚实展示自动/占位比。

        返回 {'ok','path','md_path','docx_path','docx_available','sections','stats':{auto,
        placeholder,total,auto_ratio},'note','error'}。fmt='docx' 时缺 python-docx 降级只出 .md。
        """
        try:
            proj = self._adsorption.load_project((project_path or '').strip())
            if proj is None:
                return {'ok': False, 'path': None, 'error': '项目不存在或 project.yaml 已移动'}
            comparison = None
            if reference is not None:
                cr = self.ai_compare(project_path, reference)
                if cr.get('ok') and cr.get('n'):
                    comparison = {'pairs': cr.get('pairs'), 'mae': cr.get('mae'),
                                  'rmse': cr.get('rmse'), 'n': cr.get('n'),
                                  'worst': cr.get('worst'), 'unmatched': cr.get('unmatched'),
                                  'summary_zh': cr.get('summary')}
            md = self._md()
            res = md.build_manuscript(proj, comparison=comparison,
                                      fmt=str(fmt or 'markdown'))
            stats = {}
            try:
                stats = md.draft_stats(res.get('md_path') or res.get('path'))
            except Exception:                             # noqa: BLE001 统计失败不挡产物
                stats = {}
            return {'ok': bool(res.get('ok')), 'path': res.get('path'),
                    'md_path': res.get('md_path'), 'docx_path': res.get('docx_path'),
                    'docx_available': bool(res.get('docx_available')),
                    'sections': list(res.get('sections') or []),
                    'placeholders_count': res.get('placeholders_count'),
                    'stats': stats, 'note': res.get('note'), 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'path': None, 'error': str(e)}

    # ══════════════════════════════════════════════════════════════════════════
    # 四、starpivot 细节对齐:依赖状态/安装 + 概览核时 + 波函数分组/散点/远程渲染
    # ══════════════════════════════════════════════════════════════════════════
    # 可后台 pip 安装的组件白名单(避免任意包注入;pip 为真实包名序列)
    _DEPS_INSTALLABLE = {
        'rdkit': {'pip': ('rdkit',), 'note': '分子建模(SMILES↔3D、2D 键线式)'},
        'decimer': {'pip': ('decimer',),
                    'note': '图片识别 OCSR(含 TensorFlow 模型,约数百 MB)'},
        'matplotlib': {'pip': ('matplotlib', 'numpy'), 'note': '论文级出图引擎'},
        'python-docx': {'pip': ('python-docx',), 'note': '论文骨架导出 .docx'},
        'pypdf': {'pip': ('pypdf',), 'note': 'PDF 论文文本提取'},
    }

    @staticmethod
    def _pkg_present(name):
        try:
            import importlib.util
            return importlib.util.find_spec(name) is not None
        except Exception:                                 # noqa: BLE001
            return False

    def deps_status(self):
        """依赖状态汇总(侧栏依赖状态区)→ {'ok','deps':[{key,name,available,detail,installable,
        note}],'error'}。RDKit/DECIMER/matplotlib 按 import 探测;Multiwfn/VMD 走各自 probe。"""
        try:
            deps = []
            py = [('rdkit', 'RDKit', 'rdkit'), ('decimer', 'DECIMER', 'decimer'),
                  ('matplotlib', 'matplotlib', 'matplotlib')]
            for key, name, mod in py:
                ok = self._pkg_present(mod)
                deps.append({'key': key, 'name': name, 'available': ok,
                             'detail': '已安装' if ok else '未安装',
                             'installable': key in self._DEPS_INSTALLABLE,
                             'note': self._DEPS_INSTALLABLE.get(key, {}).get('note', '')})
            paths = self._tool_paths()
            for key, name, probe in (('multiwfn', 'Multiwfn', self._mw),
                                     ('vmd', 'VMD', self._vmd_)):
                try:
                    pr = probe().probe((paths.get(key) or '').strip() or None)
                except Exception as e:                    # noqa: BLE001
                    pr = {'available': False, 'detail': str(e)}
                deps.append({'key': key, 'name': name,
                             'available': bool(pr.get('available')),
                             'detail': pr.get('detail', ''), 'installable': False,
                             'note': '外部程序,请在波函数页填路径或加入 PATH'})
            return {'ok': True, 'deps': deps, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'deps': [], 'error': str(e)}

    @staticmethod
    def _default_deps_runner(pip_names, log_path):
        """默认后台 pip 安装器:Popen 输出重定向到日志文件。"""
        import subprocess
        logf = open(log_path, 'w', encoding='utf-8')
        return subprocess.Popen([sys.executable, '-m', 'pip', 'install', *pip_names],
                                stdout=logf, stderr=subprocess.STDOUT)

    def deps_install(self, pkgs):
        """后台安装依赖组件(白名单内)→ 启动 pip 子进程,进度经 deps_install_status 轮询。

        返回 {'ok','started','pkgs','pip','rejected','log_path','error'}。非白名单组件计入 rejected。
        """
        try:
            allow = self._DEPS_INSTALLABLE
            req = [str(p).strip() for p in (pkgs or []) if str(p).strip()]
            picks = [p for p in req if p in allow]
            rejected = [p for p in req if p not in allow]
            if not picks:
                return {'ok': False, 'started': False, 'pkgs': [], 'pip': [],
                        'rejected': rejected, 'log_path': None,
                        'error': '未选择可安装的组件(可选:' + '、'.join(sorted(allow)) + ')'}
            if self._deps_job is not None:
                proc = self._deps_job.get('proc')
                if proc is not None and proc.poll() is None:
                    return {'ok': False, 'started': False, 'pkgs': [], 'pip': [],
                            'rejected': rejected, 'log_path': self._deps_job.get('log_path'),
                            'error': '已有安装任务进行中,请等待完成或稍后再试'}
            pip_names = []
            for p in picks:
                pip_names += list(allow[p]['pip'])
            log_path = os.path.join(tempfile.gettempdir(),
                                    f'vcstudio_deps_{int(time.time())}.log')
            runner = self._deps_runner or self._default_deps_runner
            proc = runner(pip_names, log_path)
            self._deps_job = {'proc': proc, 'log_path': log_path, 'pkgs': picks,
                              'pip': pip_names, 'started_at': time.time()}
            return {'ok': True, 'started': True, 'pkgs': picks, 'pip': pip_names,
                    'rejected': rejected, 'log_path': log_path, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'started': False, 'pkgs': [], 'pip': [],
                    'rejected': [], 'log_path': None, 'error': str(e)}

    def deps_install_status(self):
        """轮询后台安装进度 → {'ok','active','running','done','returncode','log_tail','pkgs','error'}。

        无任务 → active=False;done 且 returncode==0 视为成功(前端据此刷新 deps_status)。
        """
        try:
            job = self._deps_job
            if not job:
                return {'ok': True, 'active': False, 'running': False, 'done': False,
                        'returncode': None, 'log_tail': '', 'pkgs': [], 'error': None}
            proc = job.get('proc')
            rc = proc.poll() if proc is not None else None
            running = rc is None
            tail = ''
            try:
                if os.path.isfile(job['log_path']):
                    txt = open(job['log_path'], encoding='utf-8', errors='replace').read()
                    tail = '\n'.join(txt.splitlines()[-40:])
            except Exception:                             # noqa: BLE001
                tail = ''
            return {'ok': True, 'active': True, 'running': running, 'done': not running,
                    'returncode': rc, 'log_tail': tail, 'pkgs': list(job.get('pkgs') or []),
                    'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'active': False, 'running': False, 'error': str(e)}

    def overview_stats(self):
        """概览页核时四卡:近30天作业数 / 近30天核时(估算)/ 剩余核时 / 实时监控状态。

        作业数与 30 天窗口取台账 created_at;核时/剩余取 campaign 预算(cap − 估算已花)。
        返回 {'ok','jobs_30d','jobs_total','core_hours_30d','remaining_core_hours',
        'budget_cap','monitor':{running,queued,active,status},'error'}。全程 try/except 降级。
        """
        try:
            import datetime
            entries = list(self._ledger.load_all())
            now = time.time()
            cutoff = now - 30 * 86400
            jobs_total = len(entries)
            jobs_30d, running, queued = 0, 0, 0
            for _d, m in entries:
                if not m:
                    continue
                st = m.get('state')
                if st == 'RUNNING':
                    running += 1
                elif st in ('QUEUED', 'SUBMITTED', 'UPLOADED'):
                    queued += 1
                ca = m.get('created_at')
                if ca:
                    try:
                        ts = datetime.datetime.fromisoformat(str(ca)).timestamp()
                        if ts >= cutoff:
                            jobs_30d += 1
                    except (ValueError, TypeError):
                        pass
            if jobs_30d == 0 and not any((m or {}).get('created_at') for _d, m in entries):
                jobs_30d = jobs_total          # 无时间戳则回退用总数(不误报 0)
            cap_total, spent = 0.0, 0.0
            try:
                camps = self.campaign_list()
                for c in (camps.get('campaigns') or []):
                    bud = c.get('budget') or {}
                    cap = bud.get('cap')
                    est = float(bud.get('estimated') or 0.0)
                    states = c.get('states') or {}
                    n_tasks = int(c.get('n_tasks') or 0)
                    done = int(states.get('completed') or 0)
                    frac = (done / n_tasks) if n_tasks else 0.0
                    spent += est * frac
                    if cap not in (None, ''):
                        cap_total += float(cap)
            except Exception:                             # noqa: BLE001
                pass
            remaining = round(cap_total - spent, 2) if cap_total else None
            active = running + queued
            status = ('运行中' if running else ('排队中' if queued else '空闲'))
            return {'ok': True, 'jobs_30d': jobs_30d, 'jobs_total': jobs_total,
                    'core_hours_30d': round(spent, 2),
                    'remaining_core_hours': remaining,
                    'budget_cap': round(cap_total, 2) if cap_total else None,
                    'monitor': {'running': running, 'queued': queued, 'active': active,
                                'status': status}, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'jobs_30d': 0, 'jobs_total': 0, 'core_hours_30d': 0.0,
                    'remaining_core_hours': None, 'budget_cap': None,
                    'monitor': {}, 'error': str(e)}

    # ── 波函数页:分析项分组菜单(引擎 ANALYSES + api 层 _EXTRA_ANALYSES 补四种) ──
    @staticmethod
    def _script_elf_lol_section(params=None):
        """ELF/LOL 平面截面图(主功能4→9/10 平面数据 → PNG/PDF)。切面:自动/坐标/三原子。"""
        p = dict(params or {})
        func = '9' if str(p.get('func', 'elf')).lower() == 'elf' else '10'
        plane = str(p.get('plane', 'auto')).lower()
        lines = ['4', func]
        if plane == 'atoms' and p.get('atoms'):
            a = [str(int(x)) for x in p['atoms'][:3]]
            lines += ['1', ' '.join(a)]              # 由三原子定义平面
        elif plane == 'xy':
            lines += ['2', '0']
        else:
            lines += ['2', '0']                      # 自动/默认:XY 面 z=0
        lines += ['0']                               # 后处理:导出图形
        return '\n'.join(lines) + '\n'

    @staticmethod
    def _script_adch_charge(params=None):
        """ADCH 原子电荷(主功能7→11):Hirshfeld-I 基础上的偶极校正原子电荷。"""
        return '7\n11\n1\ny\n0\nq\n'

    @staticmethod
    def _script_property_summary(params=None):
        """性质汇总(主功能100→2 等):单点能 / 偶极矩 / 基本热力学量打印在 stdout。"""
        return '100\n2\n0\nq\n'

    @staticmethod
    def _script_fukui_cdft(params=None):
        """Fukui / CDFT(主功能22):f+/f-/f0 与双描述符 DD 四 cube(需 N-1/N/N+1 波函数)。"""
        return '22\n1\n\n0\nq\n'

    def _extra_analyses(self):
        """api 层补充分析注册表(引擎 multiwfn_driver.ANALYSES 暂缺的四种;同 stdin 脚本模式)。

        引擎侧扩展后此表可移除(菜单流已标注版本差异);键不与引擎重复。
        """
        return {
            'elf_lol_section': {
                'name': 'ELF / LOL 截面图', 'stdin_script': self._script_elf_lol_section,
                'outputs': ('plane.png', 'plane.pdf'),
                'note': ('平面截面(切面:自动/坐标/三原子)导出 PNG/PDF。⚠ api 层补充项——'
                         'multiwfn_driver.ANALYSES 暂无此项,菜单号按 Multiwfn 3.8,版本差异请核对。')},
            'adch_charge': {
                'name': 'ADCH 原子电荷', 'stdin_script': self._script_adch_charge,
                'outputs': (),
                'note': ('主功能7→11:ADCH(原子偶极校正 Hirshfeld)电荷,结果在 stdout。'
                         '⚠ api 层补充项,引擎待扩展;版本差异请核对。')},
            'property_summary': {
                'name': '性质汇总(单点能/偶极矩/热力学)',
                'stdin_script': self._script_property_summary, 'outputs': (),
                'note': ('单点能 / 偶极矩 / 基本热力学量汇总打印。⚠ api 层补充项,引擎待扩展。')},
            'fukui_cdft': {
                'name': 'Fukui / CDFT 四 cube', 'stdin_script': self._script_fukui_cdft,
                'outputs': ('f_plus.cub', 'f_minus.cub', 'f_zero.cub', 'CDD.cub'),
                'note': ('主功能22:CDFT 概念密度泛函,f+/f−/f0 与双描述符;需 N-1/N/N+1 波函数。'
                         '⚠ api 层补充项,引擎待扩展;版本差异请核对。')},
        }

    # 分析项分组(对齐 starpivot:常用 / 实空间与截面 / 弱相互作用 / 其它)
    _WAVEFN_GROUPS = (
        ('常用', ('esp_extrema', 'density_cube', 'esp_cube', 'homo_lumo_cube')),
        ('实空间与截面', ('elf_lol_section', 'alie', 'alie_extrema', 'adch_charge')),
        ('弱相互作用', ('nci_rdg', 'igmh', 'iri')),
        ('其它', ('aim_cp', 'property_summary', 'fukui_cdft')),
    )

    def wavefn_analyses(self):
        """波函数分析项分组菜单(引擎 ANALYSES + api 层补充)→ {'ok','groups':[{group,items:[{key,
        name,note,source,outputs}]}],'error'}。source∈{engine,api};其它组收未分组项。"""
        try:
            eng = dict(self._mw().ANALYSES)
            extra = self._extra_analyses()
            merged = {}
            for k, v in eng.items():
                merged[k] = {'key': k, 'name': v.get('name', k), 'note': v.get('note', ''),
                             'outputs': list(v.get('outputs') or ()), 'source': 'engine'}
            for k, v in extra.items():
                if k in merged:
                    continue                              # 引擎已有则不覆盖
                merged[k] = {'key': k, 'name': v.get('name', k), 'note': v.get('note', ''),
                             'outputs': list(v.get('outputs') or ()), 'source': 'api'}
            placed, groups = set(), []
            for gname, keys in self._WAVEFN_GROUPS:
                items = [merged[k] for k in keys if k in merged]
                for it in items:
                    placed.add(it['key'])
                if items:
                    groups.append({'group': gname, 'items': items})
            leftover = [merged[k] for k in merged if k not in placed]
            if leftover:
                other = next((g for g in groups if g['group'] == '其它'), None)
                if other:
                    other['items'].extend(leftover)
                else:
                    groups.append({'group': '其它', 'items': leftover})
            return {'ok': True, 'groups': groups, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'groups': [], 'error': str(e)}

    def wavefn_run_extra(self, wavefn_file, analyses, params=None, exe=None, workdir=None):
        """跑 api 层补充分析项(elf_lol_section/adch_charge/property_summary/fukui_cdft)。

        用 _EXTRA_ANALYSES 的 stdin 脚本经 multiwfn_driver.run_script(或降级回传脚本)。返回同
        wavefn_run 形状 {'ok','results':[{analysis,ok,outputs,stdout_tail,script,error}],'error'}。
        引擎缺失(无 run_script)→ 各项回传可复制 stdin 脚本 + '引擎待扩展' 说明。
        """
        try:
            wf = (wavefn_file or '').strip()
            keys = [str(a).strip() for a in (analyses or []) if str(a).strip()]
            if not wf:
                return {'ok': False, 'results': [], 'error': '未选择波函数文件'}
            if not keys:
                return {'ok': False, 'results': [], 'error': '未选择分析项'}
            extra = self._extra_analyses()
            mw = self._mw()
            mw_exe = (exe or self._tool_paths().get('multiwfn') or '').strip() or None
            wd = (workdir or '').strip() or None
            p = dict(params or {})
            results = []
            for k in keys:
                spec = extra.get(k)
                if spec is None:
                    results.append({'analysis': k, 'ok': False, 'outputs': [],
                                    'stdout_tail': '', 'script': '',
                                    'error': f'未知补充分析项 {k!r}'})
                    continue
                try:
                    script = spec['stdin_script'](p)
                except Exception as e:                    # noqa: BLE001
                    script = ''
                    results.append({'analysis': k, 'ok': False, 'outputs': [],
                                    'stdout_tail': '', 'script': '', 'error': str(e)})
                    continue
                runner = getattr(mw, 'run_script', None)
                if callable(runner):
                    try:
                        r = runner(wf, script, exe=mw_exe, workdir=wd,
                                   outputs=list(spec.get('outputs') or ()))
                        results.append({'analysis': k, 'ok': bool(r.get('ok')),
                                        'outputs': list(r.get('outputs') or []),
                                        'stdout_tail': r.get('stdout_tail', ''),
                                        'script': r.get('script', script),
                                        'error': r.get('error') or None})
                    except Exception as e:                # noqa: BLE001
                        results.append({'analysis': k, 'ok': False, 'outputs': [],
                                        'stdout_tail': '', 'script': script, 'error': str(e)})
                else:
                    results.append({'analysis': k, 'ok': False, 'outputs': [],
                                    'stdout_tail': '', 'script': script,
                                    'error': '引擎待扩展:multiwfn_driver 暂无 run_script,'
                                             '可复制上方 stdin 脚本手动运行'})
            return {'ok': any(r['ok'] for r in results), 'results': results, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'results': [], 'error': str(e)}

    @staticmethod
    def _parse_cube_values(path):
        """.cub/.cube → (n_atoms, (nx,ny,nz), 扁平体数据 float 列表)。解析失败 → (0,(0,0,0),[])。"""
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                lines = f.read().splitlines()
            if len(lines) < 6:
                return 0, (0, 0, 0), []
            natoms = int(float(lines[2].split()[0]))
            nx = int(float(lines[3].split()[0]))
            ny = int(float(lines[4].split()[0]))
            nz = int(float(lines[5].split()[0]))
            data_start = 6 + abs(natoms)
            vals = []
            for ln in lines[data_start:]:
                for tok in ln.split():
                    try:
                        vals.append(float(tok))
                    except ValueError:
                        pass
            return abs(natoms), (nx, ny, nz), vals
        except Exception:                                 # noqa: BLE001
            return 0, (0, 0, 0), []

    def wavefn_scatter(self, func1_cube, func2_cube, kind='nci', max_points=3000, out_png=None):
        """NCI/IRI 散点(RDG-sign(λ2)ρ):读两 cube 配对采样 → {'ok','kind','points':[{x,y}],
        'n','png','error'}。func1=sign(λ2)ρ(x)、func2=RDG(y);前端可 echarts 渲染,给 out_png 则
        经 native_charts 另出 PNG(缺 matplotlib 则跳过,points 仍返回)。"""
        try:
            f1 = (func1_cube or '').strip()
            f2 = (func2_cube or '').strip()
            if not f1 or not os.path.isfile(f1):
                return {'ok': False, 'kind': kind, 'points': [], 'n': 0, 'png': None,
                        'error': 'sign(λ2)ρ cube(func1)不存在'}
            if not f2 or not os.path.isfile(f2):
                return {'ok': False, 'kind': kind, 'points': [], 'n': 0, 'png': None,
                        'error': 'RDG cube(func2)不存在'}
            _n1, _d1, xs = self._parse_cube_values(f1)
            _n2, _d2, ys = self._parse_cube_values(f2)
            n = min(len(xs), len(ys))
            if n == 0:
                return {'ok': False, 'kind': kind, 'points': [], 'n': 0, 'png': None,
                        'error': '两 cube 无可解析体数据'}
            step = max(1, n // int(max_points or 3000))
            points = []
            for i in range(0, n, step):
                x, y = xs[i], ys[i]
                if abs(x) <= 0.05 and 0.0 <= y <= 2.0:   # NCI 典型窗口
                    points.append({'x': round(x, 5), 'y': round(y, 5)})
            png = None
            dest = (out_png or '').strip()
            if dest and points:
                try:
                    nc = self._nc()
                    xs2 = [p['x'] for p in points]
                    ys2 = [p['y'] for p in points]
                    got = nc.scaling_relation(
                        xs2, ys2, dest, xlabel=r'sign($\lambda_2$)$\rho$ (a.u.)',
                        ylabel='RDG', fit=False)
                    png = got[0] if isinstance(got, (list, tuple)) and got else dest
                except Exception:                         # noqa: BLE001 出图失败不挡数据
                    png = None
            return {'ok': True, 'kind': kind, 'points': points, 'n': len(points),
                    'png': png, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'kind': kind, 'points': [], 'n': 0, 'png': None,
                    'error': str(e)}

    def wavefn_render_remote(self, scene, files, name, password, remote_dir,
                             remote_exe='vmd', trust_new=False, params=None):
        """(实验性)远程集群渲染波函数场景:上传 cube/结构 → 远端 VMD Tachyon → 下载 PNG。

        返回 {'ok','png'(本地),'tcl','experimental':True,'needs_trust','error'}。任一步失败给
        中文说明;大文件/复杂路径后续完善。VMD 缺 tcl 时回传脚本供手动渲染。
        """
        try:
            import shlex
            sc = (scene or '').strip()
            rdir = (remote_dir or '').strip()
            fmap = dict(files or {})
            if not sc:
                return {'ok': False, 'png': None, 'tcl': '', 'experimental': True,
                        'needs_trust': False, 'error': '未选择渲染场景'}
            if not rdir:
                return {'ok': False, 'png': None, 'tcl': '', 'experimental': True,
                        'needs_trust': False, 'error': '未指定远端工作目录'}
            vmd = self._vmd_()
            spec = vmd.SCENES.get(sc)
            if spec is None:
                return {'ok': False, 'png': None, 'tcl': '', 'experimental': True,
                        'needs_trust': False, 'error': f'未知场景 {sc!r}'}
            for role in (spec.get('files') or ()):
                lp = (fmap.get(role) or '').strip()
                if not lp or not os.path.isfile(lp):
                    return {'ok': False, 'png': None, 'tcl': '', 'experimental': True,
                            'needs_trust': False, 'error': f'渲染文件「{role}」不存在'}
            prof, pw, err = self._resolve(name, password)
            if err:
                return err
            conn = self._conn()
            rexe = (remote_exe or 'vmd').strip() or 'vmd'
            remote_files = {}
            for role, lp in fmap.items():
                remote_files[role] = rdir.rstrip('/') + '/' + os.path.basename(lp)
            remote_out = rdir.rstrip('/') + '/' + sc + '.png'
            try:
                tcl = spec['build'](remote_files, remote_out, dict(params or {}))
            except Exception as e:                        # noqa: BLE001
                return {'ok': False, 'png': None, 'tcl': '', 'experimental': True,
                        'needs_trust': False, 'error': f'场景脚本生成失败:{e}'}
            try:
                client, jump = conn.open_client(prof, pw, trust_new=bool(trust_new))
            except conn.ConnectError as e:
                return {'ok': False, 'png': None, 'tcl': tcl, 'experimental': True,
                        'needs_trust': bool(getattr(e, 'needs_trust', False)), 'error': str(e)}
            local_png = None
            try:
                sftp = client.open_sftp()
                for role, lp in fmap.items():
                    sftp.put(lp, remote_files[role])
                tcl_remote = rdir.rstrip('/') + '/_vcs_scene.tcl'
                with sftp.open(tcl_remote, 'w') as f:
                    f.write(tcl)
                cmd = (f'cd {shlex.quote(rdir)} && {rexe} -dispdev text -eofexit '
                       f'-e _vcs_scene.tcl')
                _in, out, _err = client.exec_command(cmd, timeout=1800)
                code = out.channel.recv_exit_status()
                tail = '\n'.join(out.read().decode('utf-8', errors='replace')
                                 .splitlines()[-40:])
                if code == 0:
                    first = next(iter(fmap.values()))
                    local_png = os.path.join(os.path.dirname(first), sc + '_remote.png')
                    try:
                        sftp.get(remote_out, local_png)
                    except Exception:                     # noqa: BLE001 下载失败仅记
                        local_png = None
                sftp.close()
            finally:
                conn.close_quiet(client, jump)
            ok = local_png is not None and os.path.isfile(local_png)
            return {'ok': ok, 'png': local_png, 'tcl': tcl, 'experimental': True,
                    'needs_trust': False,
                    'error': None if ok else ('远端渲染完成但未取回 PNG(退出码/网络问题):' + tail
                                              if code == 0 else f'远端 VMD 退出码 {code};{tail}')}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'png': None, 'tcl': '', 'experimental': True,
                    'needs_trust': False, 'error': str(e)}
