"""pywebview js_api:集群 + 任务处理器门面。

薄处理器:每个 JS 调用由 pywebview 派独立线程执行,可同步阻塞。
铁律:每个公开方法都返回 JSON-safe dict,异常一律 try/except 兜成
{'error': str(e)},绝不让异常穿透到 JS 侧。重模块(batch_ops/ssh_test/
submitter)延迟导入,测试注入假件即可全离线跑,不碰网络/keyring。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import inspect
import json
import math
import ntpath
import os
import posixpath
import re
import sys
import tempfile
import threading
import time
import types
import uuid

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, fields as dc_fields
from datetime import datetime, timezone

# 合法计算类型(决定 KPOINTS 网格);前端下拉与后端都以此为准
_CALC_TYPES = ('slab', 'bulk', 'molecule')

# 主题/视觉密度白名单；二者都只影响 UI，不参与任何科学数据或计算参数。
_THEMES = ('classic', 'paper', 'deep')
_DENSITIES = ('comfortable', 'standard', 'compact')
_STAGES = ('generate', 'submit', 'monitor', 'recover', 'analysis', 'report_done')
# 活跃(在队/在跑)状态与可续算终态:pipeline_tick/status 复用
_ACTIVE_STATES = ('UPLOADED', 'SUBMITTED', 'QUEUED', 'RUNNING')
_TERMINAL_FAIL = ('FAILED', 'UNCONVERGED')
_REPORT_KINDS = frozenset({'final', 'diagnostic', 'draft'})
_REPORT_FORMATS = ('html', 'docx', 'pdf')
_REPORT_QUALIFICATIONS = frozenset({
    'diagnostic',
    'adsorption_result_verified',
    'thermodynamic_path_verified',
    'kinetic_evidence_verified',
    'publication_package_verified',
    'human_scientific_reviewed',
})


class _ReportInputChanged(RuntimeError):
    """Raised when report inputs change between snapshot and marker commit."""

# SAC 矩阵机时粗估系数(核时·原子⁻¹·作业⁻¹,数量级参考,可解释:Σ原子数 × 系数)
_SAC_EST_COEF = 0.8

# MPI/调度器中“进程数/核数”的常见写法。既用于自动轨的 vasp_cmd，也用于
# 模板轨渲染后的最终脚本；覆盖无空格、等号和长参数三种常见形式。
_CORE_COUNT_RE = re.compile(
    r'(?<![\w-])(?:--?np|-n|--ntasks(?:-per-node)?|ppn|ncpus|mpiprocs)'
    r'(?:\s*=\s*|\s+)?(\d+)\b')
_CORE_PLACEHOLDER_RE = re.compile(
    r'(?<![\w-])(?:--?np|-n|--ntasks(?:-per-node)?|ppn|ncpus|mpiprocs)'
    r'(?:\s*=\s*|\s+)?\{(?:cores|ppn)\}')
_NODE_COUNT_RE = re.compile(
    r'(?<![\w-])(?:--nodes|-N|nodes)(?:\s*=\s*|\s+)(\d+)\b')
_WALLTIME_RE = re.compile(
    r'(?<![\w-])(?:--time|-t|walltime)(?:\s*=\s*|\s+)'
    r'((?:\d+-)?\d{1,4}(?::[0-5]\d){0,2})\b'
    r'|#BSUB\s+-W\s+((?:\d+-)?\d{1,4}(?::[0-5]\d){0,2})\b')
_WALLTIME_PLACEHOLDER_RE = re.compile(
    r'(?<![\w-])(?:--time|-t|walltime)(?:\s*=\s*|\s+)\{walltime\}'
    r'|#BSUB\s+-W\s+\{walltime\}')


def _parallel_counts(text) -> list[int]:
    """提取命令/脚本中可确认的 MPI 或调度器核数。"""
    return [int(match.group(1)) for match in _CORE_COUNT_RE.finditer(str(text or ''))]


def _walltimes(text) -> list[str]:
    """提取 Slurm/PBS/LSF 常见墙时指令中的值。"""
    values = []
    for match in _WALLTIME_RE.finditer(str(text or '')):
        values.append(match.group(1) or match.group(2))
    return values


def _sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _formula_composition(formula: str) -> dict[str, int] | None:
    value = str(formula or '').strip()
    parts = re.findall(r'([A-Z][a-z]?)(\d*)', value)
    if not parts or ''.join(element + count for element, count in parts) != value:
        return None
    result = {}
    for element, count in parts:
        result[element] = result.get(element, 0) + int(count or 1)
    return result


def _poscar_composition_cell(path):
    from vcstudio.generate.poscar import (parse_poscar_species, read_cell_vectors,
                                          read_poscar)
    text = read_poscar(path)
    elements, counts = parse_poscar_species(text)
    return dict(zip(elements, counts)), read_cell_vectors(text)


def _method_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    token = str(value).strip().upper()
    if token in {'T', '.T.', 'TRUE', '.TRUE.'}:
        return True
    if token in {'F', '.F.', 'FALSE', '.FALSE.'}:
        return False
    return None


def _method_number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _method_integer(value):
    number = _method_number(value)
    return int(number) if number is not None and number == int(number) else None


def _method_choice(value, default='F'):
    raw = default if value is None else value
    logical = _method_bool(raw)
    if logical is not None:
        return 'T' if logical else 'F'
    return str(raw).strip().upper()


def _method_vector(value, *, integer=False):
    """Return a finite expanded VASP vector or ``None`` when it is ambiguous."""
    if isinstance(value, (list, tuple)):
        tokens = list(value)
    elif value is None or isinstance(value, bool):
        return None
    else:
        tokens = str(value).replace(',', ' ').split()
    result = []
    for token in tokens:
        repeated = re.fullmatch(r'(\d+)\*([^*]+)', str(token).strip())
        count, raw = (int(repeated.group(1)), repeated.group(2)) if repeated else (1, token)
        number = _method_number(raw)
        if number is None or (integer and number != int(number)):
            return None
        result.extend([int(number) if integer else number] * count)
    return result or None


def _titel_element(titel):
    """Extract the element from a normal VASP TITEL without guessing variants."""
    for token in str(titel or '').split()[1:]:
        base = token.split('_', 1)[0]
        if re.fullmatch(r'[A-Z][a-z]?', base):
            return base
    return None


def _method_vector_map(value, element_orders, *, integer=False):
    """Map a vector by POSCAR/POTCAR order only when every viable map agrees."""
    vector = _method_vector(value, integer=integer)
    if vector is None:
        return None
    candidates = []
    for raw_order in element_orders or []:
        order = [str(element) for element in (raw_order or [])]
        if len(order) != len(vector) or len(set(order)) != len(order):
            continue
        mapped = dict(zip(order, vector))
        if mapped not in candidates:
            candidates.append(mapped)
    return candidates[0] if len(candidates) == 1 else None


def _method_u_by_element(plan):
    """Return effective Hubbard settings keyed by element when unambiguous.

    ``LDAU`` is a global switch, but its physical effect is element-local:
    ``LDAUL=-1`` disables the correction for one species.  Comparing only the
    global switch would therefore reject a perfectly valid molecule/slab pair
    when the catalyst alone uses +U.  Unknown vector order remains fail-closed
    for the later energy-comparability gate.
    """
    from vcstudio.project.method_policy import effective_u_by_element
    return effective_u_by_element(plan)

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
                 incar_builder_mod=None, metal_slab_mod=None, usage_mod=None,
                 result_import_mod=None, task_analysis_mod=None,
                 pipeline_supervisor_cls=None, assistant_chat_mod=None,
                 comparison_mod=None, candidate_evaluation_mod=None,
                 paper_report_mod=None, workspace_state_store=None,
                 report_service=None, analysis_preferences_store=None,
                 research_index_service=None, research_view_store=None,
                 project_lifecycle_service=None, lab_policy_store=None,
                 workspace_context_store=None):
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
        self._metal_slab = metal_slab_mod           # generate.metal_slab(金属 slab + 层厚配方)
        self._usage_mod = usage_mod                 # cluster.usage(实际核时统计,纯函数)
        # 本地已算结果的递归预检/导入：延迟加载，便于 API 单测注入假件。
        self._result_import = result_import_mod
        # 23 类任务共用的能力矩阵/证据报告；未接解析器的任务必须显式标记。
        self._task_analysis = task_analysis_mod
        # MatClaw 式对话入口仅复用会话/附件/状态设计；Python 原生实现延迟加载，
        # 不把 Node、Docker 或任意 shell 执行面带进桌面程序。
        self._assistant_chat = assistant_chat_mod
        # 多项目比较、确定性候选评价与 DOCX/PDF 同源报告。三个模块均延迟加载，
        # 使基础提交/监控路径不会因可选文档或绘图库缺失而无法启动。
        self._comparison = comparison_mod
        self._candidate_evaluation = candidate_evaluation_mod
        self._paper_report = paper_report_mod
        # Phase C report workbench orchestration.  The service is lazy so the
        # normal submit/monitor path does not import report dependencies.
        self._report_service_instance = report_service
        # Report publication uses its own purpose-bound destination registry.
        # It is intentionally separate from SI capsule export tokens.
        self._report_workbench_destinations = None
        # Insight export destinations remain server-side behind opaque,
        # single-use tokens; browser callers never submit filesystem paths.
        self._report_insight_destinations = None
        # Phase B 可恢复工作区状态只保存 UI 偏好/草稿引用，与 project.yaml 科学事实分离。
        # 测试可注入内存/临时目录 store；生产首次调用时再创建用户级 JSON store。
        self._workspace_state_store = workspace_state_store
        # Workflow/engine/calculation form one revisioned server-owned
        # context.  The store remains lazy so unrelated API tests and startup
        # paths do not touch user configuration.
        self._workspace_context_store = workspace_context_store
        # Phase D reusable analysis templates/favourites are user-level state,
        # never project.yaml fields.  Keep the store lazy for normal job paths.
        self._analysis_preferences_store = analysis_preferences_store
        # Cross-project research discovery is a rebuildable in-memory read
        # model; saved filters live in their own authority_id + revision CAS
        # store.  Neither is a project/job/report fact source.
        self._research_index_service = research_index_service
        self._research_view_store = research_view_store
        self._research_index_lock = threading.RLock()
        # Confirmed laboratory recommendations are user-level state.  They do
        # not mutate job manifests or grant submission authority.
        self._lab_policy_store = lab_policy_store
        # Phase 3 project location/identity mutations are isolated behind a
        # hash-bound lifecycle service. Browser callers receive opaque server
        # selections and plans, never an authority-bearing filesystem path.
        self._project_lifecycle_service = project_lifecycle_service
        self._project_lifecycle_lock = threading.RLock()
        self._project_lifecycle_selections = {}
        self._project_lifecycle_plans = {}
        # Every browser project-ID request is rebound under one process-local
        # transaction lock before any private path helper may use it.  The
        # thread-local map also makes every nested project reload verify the
        # originally resolved opaque identity before returning data.
        self._project_identity_lock = threading.RLock()
        self._project_identity_local = threading.local()
        # pywebview 可并发调用同一个 js_api；后端锁才是自动托管的正确性边界。
        # 前端的 running 标志只负责交互，不能阻止两条线程同时续算/出报告。
        self._pipeline_lock = threading.Lock()
        # 高风险批量动作的请求键在确认框之前由前端创建，并在密码/主机指纹重试中复用。
        # 这里才是真正的并发边界：相同请求不得再次进入 SSH 提交/续算/取消实现。
        self._job_operation_lock = threading.RLock()
        self._job_operations = {}
        # 报告渲染可持续数分钟；marker 提交必须串行重载当前项目并执行 CAS，
        # 不能把渲染前的旧 project dict 覆盖回 project.yaml。
        self._report_marker_lock = threading.RLock()
        self._pipeline_supervisor_cls = pipeline_supervisor_cls
        self._pipeline_supervisor = None
        self._pipeline_runtime_lock = threading.RLock()
        self._pipeline_runtime = {
            'running': False, 'paused': False, 'tick_running': False,
            'enabled': None, 'interval_seconds': None, 'last_started': None,
            'last_finished': None, 'next_check': None, 'outcome_seq': 0,
            'outcome_history': [],
            'last_outcome': None, 'last_error': None,
        }

    # ── 桥活性探测(前端用来确认 js_api 已就绪) ──
    def ping(self) -> str:
        return 'pong'

    # ── 进程级自动托管调度器 ────────────────────────────────────────────────
    _PIPELINE_LOCATOR_KEYS = frozenset({
        'path', 'paths', 'project_path', 'project_paths', 'project_root',
        'root', 'dir', 'dirs', 'directory', 'directories', 'locator',
        'locators', 'destination', 'report', 'files', 'figures_dir',
        'out_dir', 'output_dir', 'manifest_path', 'recovery_path',
    })
    _PIPELINE_RUNTIME_KEYS = (
        'running', 'paused', 'tick_running', 'enabled', 'interval_seconds',
        'last_started', 'last_finished', 'next_check', 'outcome_seq',
        'outcome_history', 'last_outcome', 'last_error',
    )

    @classmethod
    def _pipeline_public_value(cls, value, *, key=''):
        """Recursively project one pipeline value across the browser boundary."""
        normalized = str(key or '').strip().lower().replace('-', '_')
        if isinstance(value, dict):
            projected = {}
            for raw_key, item in value.items():
                item_key = str(raw_key)
                item_normalized = item_key.strip().lower().replace('-', '_')
                if (item_normalized in cls._PIPELINE_LOCATOR_KEYS
                        or item_normalized.endswith(('_path', '_paths', '_dir', '_dirs'))):
                    continue
                safe_key = cls._pipeline_public_value(item_key, key='field_name')
                projected[str(safe_key)] = cls._pipeline_public_value(
                    item, key=item_normalized)
            return projected
        if isinstance(value, (list, tuple)):
            return [cls._pipeline_public_value(item, key=normalized) for item in value]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if normalized == 'last_error' and value:
            return 'Pipeline scheduler operation failed.'
        try:
            from vcstudio.project.report_insights import redact
            projected = redact(value, key=normalized)
            # ``redact`` JSON-normalises unknown objects with ``str(value)``.
            # Re-check that resulting string so PathLike/custom objects cannot
            # smuggle a locator or credential through their textual form.
            if isinstance(projected, str):
                projected = redact(projected, key=normalized)
                return cls._workspace_public_text(projected)
            return projected
        except Exception:                                 # noqa: BLE001 fail-safe projection
            return '[redacted-sensitive-value]'

    @classmethod
    def _pipeline_public_state(cls, state):
        source = state if isinstance(state, dict) else {}
        return {
            key: cls._pipeline_public_value(source.get(key), key=key)
            for key in cls._PIPELINE_RUNTIME_KEYS
        }

    def _publish_pipeline_runtime(self, state):
        """保存调度线程的只读快照；前端只轮询该快照，不再自己驱动计算。"""
        with self._pipeline_runtime_lock:
            self._pipeline_runtime = copy.deepcopy(
                self._pipeline_public_state(state))

    def start_background_services(self):
        """启动唯一后台调度线程；由桌面入口调用，重复调用安全。"""
        try:
            if self._pipeline_supervisor is None:
                cls = self._pipeline_supervisor_cls
                if cls is None:
                    from vcstudio.gui_web.pipeline_supervisor import PipelineSupervisor
                    cls = PipelineSupervisor
                self._pipeline_supervisor = cls(
                    self._autopilot_cfg, self.pipeline_tick,
                    self._publish_pipeline_runtime)
            started = bool(self._pipeline_supervisor.start())
            return {'ok': True, 'started': started,
                    'state': self._pipeline_public_state(
                        self._pipeline_supervisor.snapshot()), 'error': None}
        except Exception:                                 # noqa: BLE001 public boundary
            return {'ok': False, 'started': False, 'state': None,
                    'error': 'Pipeline scheduler could not be started.'}

    def stop_background_services(self):
        """关闭后台线程；已在执行的一拍不会被暴力终止。"""
        try:
            if self._pipeline_supervisor is None:
                return {'ok': True, 'stopped': True, 'error': None}
            stopped = bool(self._pipeline_supervisor.stop(timeout=2.0))
            return {'ok': stopped, 'stopped': stopped,
                    'error': None if stopped else '后台任务仍在收尾，将在本拍结束后退出'}
        except Exception:                                 # noqa: BLE001 public boundary
            return {'ok': False, 'stopped': False,
                    'error': 'Pipeline scheduler could not be stopped.'}

    def pipeline_runtime_status(self):
        """返回调度器状态与最近一拍结果，供 UI 展示下一次检查和阻塞原因。"""
        try:
            if self._pipeline_supervisor is not None:
                state = self._pipeline_supervisor.snapshot()
            else:
                with self._pipeline_runtime_lock:
                    state = copy.deepcopy(self._pipeline_runtime)
            return {'ok': True, 'state': self._pipeline_public_state(state),
                    'error': None}
        except Exception:                                 # noqa: BLE001 public boundary
            return {'ok': False, 'state': None,
                    'error': 'Pipeline runtime status is unavailable.'}

    def pipeline_wake(self):
        """要求后台线程立即补跑一拍；不会创建第二个并发调度器。"""
        started = self.start_background_services()
        if not started.get('ok'):
            return {'ok': False, 'queued': False, 'error': started.get('error')}
        try:
            queued = bool(self._pipeline_supervisor.wake())
            return {'ok': True, 'queued': queued, 'error': None}
        except Exception:                                 # noqa: BLE001 public boundary
            return {'ok': False, 'queued': False,
                    'error': 'Pipeline scheduler could not be woken.'}

    # ── Phase B:可恢复工作区上下文（UI 偏好与项目科学事实严格分轴） ─────────────
    _WORKSPACE_CONTEXT_SCHEMA = 'vcstudio.workspace-context/v1'
    _WORKSPACE_RUNTIME_FIELDS = (
        'running', 'paused', 'tick_running', 'enabled', 'interval_seconds',
        'last_started', 'last_finished', 'next_check', 'outcome_seq',
    )

    @staticmethod
    def _workspace_path_key(path):
        return os.path.normcase(os.path.normpath(str(path or '')))

    @staticmethod
    def _workspace_project_id(path, project=None):
        """Return a stable opaque id without exposing the registry path."""
        project = project if isinstance(project, dict) else {}
        prepared = project.get('preparation') or {}
        raw_uuid = str(project.get('project_uuid') or
                       (prepared.get('project_uuid') if isinstance(prepared, dict) else '') or '')
        compact = raw_uuid.strip().lower().replace('-', '')
        if re.fullmatch(r'[a-f0-9]{32}', compact):
            return f'project-{compact}'
        canonical = os.path.normcase(os.path.realpath(os.path.abspath(
            os.path.expanduser(str(path or '')))))
        digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:24]
        return f'registry-{digest}'

    _PROJECT_ID_RE = re.compile(
        r'(?:project-[a-f0-9]{32}|registry-[a-f0-9]{24})')

    @staticmethod
    def _canonical_project_path(path):
        """Canonicalise one server-held registry locator for internal use."""
        locator = str(path or '').strip()
        if not locator:
            raise ValueError('registered project locator is empty')
        return os.path.realpath(os.path.abspath(os.path.expanduser(locator)))

    @classmethod
    def _project_identity_fingerprint(cls, path, project):
        """Fingerprint only stable identity fields, not mutable project facts."""
        project = project if isinstance(project, dict) else {}
        preparation = project.get('preparation') or {}
        payload = {
            'path': cls._workspace_path_key(cls._canonical_project_path(path)),
            'project_id': cls._workspace_project_id(path, project),
            'project_uuid': str(project.get('project_uuid') or ''),
            'prepared_project_uuid': str(
                preparation.get('project_uuid')
                if isinstance(preparation, dict) else ''),
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest()

    def _project_registry_snapshot(self):
        """Build the single private project identity authority.

        Browser DTOs are projected from ``public_rows``.  Filesystem locators
        and loaded project dictionaries exist only in the private ``records``
        and ``by_id`` members.  Duplicate identities are deliberately retained
        in ``by_id`` so resolution can fail closed rather than selecting one.
        """
        locators = list(self._adsorption.list_projects())
        records = []
        failures = []
        for index, raw_locator in enumerate(locators, start=1):
            try:
                project = self._adsorption.load_project(raw_locator)
                if not isinstance(project, dict):
                    raise ValueError('registered project is unreadable')
                path = self._canonical_project_path(raw_locator)
                project_id = self._workspace_project_id(path, project)
                member_dirs = self._project_member_dirs(project)
                reference_species = sorted(
                    str(species) for species in
                    (project.get('species_ref_jobs') or {})
                    if str(species).strip())
                n_done = sum(
                    1 for member_dir in member_dirs
                    if ((self._manifest.load_manifest(member_dir) or {}).get('state')
                        == 'DONE'))
                public = {
                    'project_id': project_id,
                    'name': str(project.get('name') or ''),
                    'n_members': len(member_dirs),
                    'n_done': n_done,
                    'reference_mode': (
                        'species' if reference_species else
                        'single' if (project.get('members') or {}).get('gas_ref')
                        else 'none'),
                    'reference_species': reference_species,
                    'n_species_refs': len(reference_species),
                }
                records.append({
                    'project_id': project_id,
                    'request_project_id': project_id,
                    'path': path,
                    'project': project,
                    'identity_fingerprint': self._project_identity_fingerprint(
                        path, project),
                    'public': public,
                })
            except Exception:                             # noqa: BLE001 safe registry projection
                failures.append({
                    'project_ref': f'registered-project-{index}',
                    'code': 'project_unreadable',
                    'message': 'Registered project could not be read.',
                })

        by_id = {}
        for record in records:
            by_id.setdefault(record['project_id'], []).append(record)
        duplicate_ids = {
            project_id for project_id, matches in by_id.items()
            if len(matches) != 1
        }
        public_rows = [
            copy.deepcopy(record['public']) for record in records
            if record['project_id'] not in duplicate_ids
        ]
        return {
            'registered_total': len(locators),
            'records': records,
            'by_id': by_id,
            'duplicate_ids': duplicate_ids,
            'public_rows': public_rows,
            'failures': failures,
        }

    def _resolve_project_id(self, project_id, *, snapshot=None):
        """Resolve exactly one opaque ID; paths, unknown IDs and duplicates fail."""
        identifier = str(project_id or '').strip().lower()
        if not self._PROJECT_ID_RE.fullmatch(identifier):
            raise ValueError('project_id is not a registered opaque identity')
        authority = snapshot or self._project_registry_snapshot()
        matches = authority['by_id'].get(identifier) or []
        if len(matches) != 1:
            raise LookupError('project identity is missing or ambiguous')
        record = dict(matches[0])
        record['request_project_id'] = identifier
        return record

    def _resolve_project_ids(self, project_ids, *, min_count=1, max_count=None):
        """Resolve an ordered, duplicate-free opaque project-ID sequence."""
        if isinstance(project_ids, (str, bytes)) or project_ids is None:
            raise TypeError('project_ids must be a sequence of opaque identities')
        identifiers = [str(item or '').strip().lower() for item in project_ids]
        if len(identifiers) < int(min_count):
            raise ValueError(f'at least {int(min_count)} project identities are required')
        if max_count is not None and len(identifiers) > int(max_count):
            raise ValueError(f'at most {int(max_count)} project identities are allowed')
        if len(identifiers) != len(set(identifiers)):
            raise ValueError('project_ids must not contain duplicate identities')
        snapshot = self._project_registry_snapshot()
        return [self._resolve_project_id(item, snapshot=snapshot)
                for item in identifiers]

    def _project_records_for_request(
            self, anchor, request=None, *, include_registry=False):
        """Collect every opaque project identity one public request may consume."""
        identifiers = [anchor['request_project_id']]
        if isinstance(request, dict):
            comparison = request.get('comparison_project_ids')
            if isinstance(comparison, (list, tuple)):
                identifiers.extend(str(item or '').strip() for item in comparison)
            spec = request.get('spec')
            scope = spec.get('scope') if isinstance(spec, dict) else None
            scoped = scope.get('project_ids') if isinstance(scope, dict) else None
            if isinstance(scoped, (list, tuple)):
                identifiers.extend(str(item or '').strip() for item in scoped)
        identifiers = list(dict.fromkeys(item for item in identifiers if item))
        if include_registry:
            snapshot = self._project_registry_snapshot()
            identifiers.extend(
                record['project_id'] for record in snapshot['records']
                if record['project_id'] not in snapshot['duplicate_ids'])
            identifiers = list(dict.fromkeys(identifiers))
        if identifiers == [anchor['request_project_id']]:
            return [anchor]
        return self._resolve_project_ids(identifiers, min_count=1)

    def _active_project_bindings(self):
        bindings = getattr(self._project_identity_local, 'bindings', None)
        return bindings if isinstance(bindings, dict) else {}

    @staticmethod
    def _identity_mismatch_message():
        return 'identity_mismatch'

    def _assert_project_binding(self, record, loaded):
        """Reject a reload whose identity differs from the resolved request."""
        if not isinstance(loaded, dict):
            raise RuntimeError(self._identity_mismatch_message())
        current_id = self._workspace_project_id(record['path'], loaded)
        current_fingerprint = self._project_identity_fingerprint(
            record['path'], loaded)
        if (current_id != record['request_project_id']
                or current_fingerprint != record['identity_fingerprint']):
            raise RuntimeError(self._identity_mismatch_message())
        return loaded

    def _rebind_project_record(self, record):
        """Reload and validate one record immediately before/after private use."""
        try:
            loaded = self._adsorption.load_project(record['path'])
        except Exception as exc:                          # noqa: BLE001 fail closed
            raise RuntimeError(self._identity_mismatch_message()) from exc
        return self._assert_project_binding(record, loaded)

    def _load_project_for_path(self, path):
        """Load a project and enforce the active browser-request binding, if any."""
        try:
            key = self._workspace_path_key(self._canonical_project_path(path))
        except Exception:                                 # noqa: BLE001 normal load semantics
            return self._adsorption.load_project(path)
        record = self._active_project_bindings().get(key)
        try:
            loaded = self._adsorption.load_project(path)
        except Exception as exc:                          # noqa: BLE001 fail closed when bound
            if record:
                raise RuntimeError(self._identity_mismatch_message()) from exc
            raise
        return self._assert_project_binding(record, loaded) if record else loaded

    def _call_with_project_bindings(self, records, invoke, *, failure=None):
        """Run one private operation under resolve/rebind identity authority."""
        records = list(records or [])
        try:
            with self._project_identity_lock:
                previous = getattr(self._project_identity_local, 'bindings', None)
                bindings = dict(previous) if isinstance(previous, dict) else {}
                for record in records:
                    key = self._workspace_path_key(
                        self._canonical_project_path(record['path']))
                    bindings[key] = record
                self._project_identity_local.bindings = bindings
                try:
                    for record in records:
                        self._rebind_project_record(record)
                    result = invoke()
                    for record in records:
                        self._rebind_project_record(record)
                    return result
                finally:
                    if previous is None:
                        try:
                            del self._project_identity_local.bindings
                        except AttributeError:
                            pass
                    else:
                        self._project_identity_local.bindings = previous
        except RuntimeError as exc:
            if str(exc) != self._identity_mismatch_message() or failure is None:
                raise
            return self._project_identity_mismatch(**failure)

    @classmethod
    def _project_identity_mismatch(cls, **extra):
        return {
            'ok': False,
            **extra,
            'error_code': 'identity_mismatch',
            'error': cls._identity_mismatch_message(),
        }

    @classmethod
    def _project_public_result(cls, value):
        """Redact diagnostic text while preserving legitimate output artifacts."""
        if isinstance(value, dict):
            projected = {}
            for key, item in value.items():
                if key in {'error', 'message', 'reason', 'gate_reason'} and item is not None:
                    projected[key] = cls._workspace_public_text(item) or None
                elif key in {'warnings', 'advisories'} and isinstance(item, (list, tuple)):
                    projected[key] = [cls._workspace_public_text(entry)
                                      for entry in item]
                else:
                    projected[key] = cls._project_public_result(item)
            return projected
        if isinstance(value, list):
            return [cls._project_public_result(item) for item in value]
        if isinstance(value, tuple):
            return [cls._project_public_result(item) for item in value]
        return value

    @classmethod
    def _project_identity_failure(cls, **extra):
        """Return a path-free public failure for any resolver rejection."""
        return {
            'ok': False,
            **extra,
            'error': 'The project identity is not registered or is ambiguous.',
        }

    def _project_locator_projection(self, value, records):
        """Replace registered-project locators in an otherwise public result."""
        by_path = {}
        by_uuid = {}
        for record in records:
            project_id = record['project_id']
            project_path = self._workspace_path_key(record['path'])
            project_root = self._workspace_path_key(
                os.path.dirname(record['path']))
            by_path[project_path] = project_id
            by_path[project_root] = project_id
            raw_uuid = str(record['project'].get('project_uuid') or '').strip()
            if raw_uuid:
                by_uuid[raw_uuid] = project_id

        def project(value):
            if isinstance(value, dict):
                current = dict(value)
                matched = None
                for key in ('path', 'project_path', 'root'):
                    raw = current.get(key)
                    if not isinstance(raw, str) or not raw.strip():
                        continue
                    try:
                        candidate = self._workspace_path_key(
                            self._canonical_project_path(raw))
                    except Exception:                     # noqa: BLE001 non-path public value
                        candidate = ''
                    if candidate in by_path:
                        matched = by_path[candidate]
                        current.pop(key, None)
                raw_uuid = str(current.get('project_uuid') or '').strip()
                if raw_uuid in by_uuid:
                    matched = matched or by_uuid[raw_uuid]
                    current.pop('project_uuid', None)
                if matched:
                    current['project_id'] = matched
                return {key: project(item) for key, item in current.items()}
            if isinstance(value, (list, tuple)):
                return [project(item) for item in value]
            return value

        return self._project_public_result(project(value))

    @staticmethod
    def _workspace_job_id(job_dir, manifest=None):
        manifest = manifest if isinstance(manifest, dict) else {}
        candidate = str(manifest.get('job_uuid') or manifest.get('job_id') or '').strip()
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', candidate):
            return candidate
        canonical = os.path.normcase(os.path.realpath(os.path.abspath(
            os.path.expanduser(str(job_dir or '')))))
        digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:24]
        return f'job-{digest}'

    @staticmethod
    def _workspace_public_text(value, limit=400):
        """Redact local/UNC paths from context text that may reach an assistant."""
        text = str(value or '')
        remote_urls = []

        def preserve_remote_url(match):
            remote_urls.append(match.group(0))
            return f'<workspace-remote-url-{len(remote_urls) - 1}>'

        text = re.sub(r'(?i)\b(?:https?|s3)://[^\s,;，；]+',
                      preserve_remote_url, text)
        text = re.sub(r'(?i)\bfile:(?://+|\\+)[^\s,;，；]+',
                      '<local-path>', text)
        text = re.sub(r'(?i)\b[A-Z]:[\\/][^\s,;，；]+', '<local-path>', text)
        text = re.sub(r'(?<![:A-Za-z0-9])(?:\\\\|//)[^\\/\s,;，；]+'
                      r'[\\/][^\s,;，；]+', '<local-path>', text)
        # Preserve URLs and canonical ``#/`` routes while redacting general
        # POSIX absolute paths, including /mnt, /opt, /srv and custom roots.
        text = re.sub(r'(?<![#/A-Za-z0-9_])/(?!/)[^\s,;，；]+',
                      '<local-path>', text)
        for index, url in enumerate(remote_urls):
            text = text.replace(f'<workspace-remote-url-{index}>', url)
        return text[:max(0, int(limit))]

    @classmethod
    def _workspace_context_failure(cls, error):
        return {
            'ok': False,
            'schema': cls._WORKSPACE_CONTEXT_SCHEMA,
            'authority_id': None,
            'state_revision': None,
            'selection': None,
            'workspace_intent': {'scenario': None, 'engine': None,
                                 'calculation': None},
            'project': None,
            'pipeline': None,
            'runtime': None,
            'sync': {'last_check_at': None, 'last_success_at': None,
                     'status': 'unknown', 'synced_targets': 0},
            'restore': {'panels': {}, 'filters': {}, 'sort': {}, 'scroll': {},
                        'draft_refs': {}},
            'unsaved': {'known': False, 'dirty': None, 'draft_ids': []},
            'activity': {'latest_cursor': 0, 'unread': 0, 'recent': [],
                         'durable': False},
            'projects': [],
            'degraded': [{'kind': 'workspace_state_unavailable',
                          'message': cls._workspace_public_text(error)}],
            'error': cls._workspace_public_text(error),
        }

    def workspace_preferences_update(self, patch, expected_revision,
                                     expected_authority_id=None):
        """Atomically update UI-only workspace preferences with revision CAS."""
        try:
            if not isinstance(expected_authority_id, str) \
                    or not re.fullmatch(r'[a-f0-9]{32}', expected_authority_id):
                raise ValueError(
                    'expected_authority_id is required for workspace-state CAS')
            result = self._ws().update(
                patch, expected_revision, expected_authority_id)
            return {
                'ok': bool(result.get('ok')),
                'conflict': bool(result.get('conflict')),
                'authority_id': result.get('authority_id'),
                'state_revision': result.get('state_revision'),
                'preferences': result.get('preferences'),
                'error': self._workspace_public_text(result.get('error')) or None,
            }
        except Exception as e:                            # noqa: BLE001 JSON-safe bridge envelope
            return {'ok': False, 'conflict': False, 'authority_id': None,
                    'state_revision': None,
                    'preferences': None,
                    'error': self._workspace_public_text(e)}

    def _workspace_intent(self, degraded):
        # Embedders historically override these three narrow accessors on an
        # Api instance.  Preserve that explicit adapter seam; the production
        # class path below always reads one atomic settings-context snapshot.
        legacy_names = ('scenario_get', 'engine_get', 'calculation_get')
        if all(callable(self.__dict__.get(name)) for name in legacy_names):
            try:
                scenario_result = self.scenario_get()
                engine_result = self.engine_get()
                calculation_result = self.calculation_get()
                scenario = scenario_result.get('scenario') or {}
                scenario_view = {
                    'key': scenario.get('key'), 'name': scenario.get('name'),
                    'configured': bool(scenario_result.get('configured')),
                    'source': 'legacy.context.adapter',
                }
                engine_view = {
                    'key': engine_result.get('engine'),
                    'configured': bool(engine_result.get('configured')),
                    'allowed': bool(engine_result.get('engine')),
                    'capability': engine_result.get('capability') or {},
                    'source': 'legacy.context.adapter',
                }
                key = calculation_result.get('active_calculation') or None
                calculation_view = {
                    'key': key,
                    'configured': bool(calculation_result.get('configured')),
                    'allowed': bool(key and key in (calculation_result.get('allowed') or [])),
                    'allowed_keys': list(calculation_result.get('allowed') or []),
                    'engine': calculation_result.get('engine') or None,
                    'source': 'legacy.context.adapter',
                }
                return {'scenario': scenario_view, 'engine': engine_view,
                        'calculation': calculation_view}
            except Exception as e:                       # noqa: BLE001 compatibility adapter
                degraded.append({'kind': 'workspace_intent_unavailable',
                                 'message': self._workspace_public_text(e)})
        try:
            result = self.settings_context_get()
            context = result.get('context') if result.get('ok') else None
            if not isinstance(context, dict):
                raise RuntimeError(result.get('error') or 'workspace context unavailable')
            scenario = context.get('scenario') or {}
            configured = context.get('configured') or {}
            scenario_view = {
                'key': scenario.get('key'),
                'name': scenario.get('name'),
                'configured': bool(configured.get('scenario')),
                'source': 'settings_context.authority',
            }
            engine_view = {
                'key': context.get('engine'),
                'configured': bool(configured.get('engine')),
                'allowed': bool(context.get('engine')),
                'capability': context.get('capability') or {},
                'source': 'settings_context.authority',
            }
            key = context.get('calculation') or None
            calculation_view = {
                'key': key,
                'configured': bool(configured.get('calculation')),
                'allowed': bool(key and key in (context.get('allowed') or [])),
                'allowed_keys': list(context.get('allowed') or []),
                'engine': context.get('engine') or None,
                'source': 'settings_context.authority',
            }
        except Exception as e:                            # noqa: BLE001 partial context remains usable
            message = self._workspace_public_text(e)
            degraded.extend([
                {'kind': 'scenario_unavailable', 'message': message},
                {'kind': 'engine_unavailable', 'message': message},
                {'kind': 'calculation_unavailable', 'message': message},
            ])
            scenario_view = None
            engine_view = None
            calculation_view = None
        return {'scenario': scenario_view, 'engine': engine_view,
                'calculation': calculation_view}

    def _workspace_runtime(self, degraded):
        try:
            result = self.pipeline_runtime_status()
        except Exception as e:                            # noqa: BLE001
            degraded.append({'kind': 'runtime_unavailable',
                             'message': self._workspace_public_text(e)})
            return None, {}, {'last_check_at': None, 'last_success_at': None,
                              'status': 'unknown', 'synced_targets': 0}, {
                                  'latest_cursor': 0, 'unread': 0, 'recent': [],
                                  'durable': False}
        state = result.get('state') if result.get('ok') else None
        if not isinstance(state, dict):
            degraded.append({'kind': 'runtime_unavailable',
                             'message': self._workspace_public_text(result.get('error'))})
            return None, {}, {'last_check_at': None, 'last_success_at': None,
                              'status': 'unknown', 'synced_targets': 0}, {
                                  'latest_cursor': 0, 'unread': 0, 'recent': [],
                                  'durable': False}
        runtime = {key: copy.deepcopy(state.get(key))
                   for key in self._WORKSPACE_RUNTIME_FIELDS}
        runtime['last_error'] = (self._workspace_public_text(state.get('last_error'))
                                 if state.get('last_error') else None)
        outcome = state.get('last_outcome') if isinstance(state.get('last_outcome'), dict) else {}
        try:
            synced = max(0, int(outcome.get('synced') or 0))
        except (TypeError, ValueError):
            synced = 0
        outcome_errors = outcome.get('errors') if isinstance(outcome.get('errors'), list) else []
        has_sync_error = bool(runtime['last_error'] or outcome_errors)
        if synced and has_sync_error:
            sync_status = 'partial'
            degraded.append({
                'kind': 'sync_partial_failure',
                'message': f'{len(outcome_errors) + int(bool(runtime["last_error"]))} '
                           'sync error(s) occurred after at least one target succeeded',
            })
        elif synced:
            sync_status = 'succeeded'
        elif has_sync_error:
            sync_status = 'failed'
        elif outcome:
            sync_status = 'checked'
        else:
            sync_status = 'unknown'
        sync = {
            'last_check_at': state.get('last_finished'),
            'last_success_at': state.get('last_finished') if synced else None,
            'status': sync_status,
            'synced_targets': synced,
        }
        recent = []
        history = state.get('outcome_history') if isinstance(
            state.get('outcome_history'), list) else []
        for item in history[-5:]:
            if not isinstance(item, dict):
                continue
            item_outcome = item.get('outcome') if isinstance(item.get('outcome'), dict) else {}
            errors = item_outcome.get('errors') if isinstance(
                item_outcome.get('errors'), list) else []
            events = item_outcome.get('events') if isinstance(
                item_outcome.get('events'), list) else []
            try:
                item_synced = max(0, int(item_outcome.get('synced') or 0))
            except (TypeError, ValueError):
                item_synced = 0
            item_failed = bool(item.get('error') or errors)
            recent.append({
                'cursor': int(item.get('seq') or 0),
                'ts': item.get('finished'),
                'kind': 'pipeline_tick',
                'status': ('partial' if item_synced and item_failed else
                           'failed' if item_failed else 'completed'),
                'event_count': len(events),
                'error_count': len(errors) + int(bool(item.get('error'))),
                'durability': 'process',
            })
        activity = {
            'latest_cursor': int(state.get('outcome_seq') or 0),
            'unread': 0,
            'recent': recent,
            'durable': False,
        }
        return runtime, state, sync, activity

    def _workspace_project_facts(self, project_id, project, list_row, pipeline_row,
                                 selected_job_id, degraded):
        member_dirs = self._project_member_dirs(project)
        engines, task_types = set(), set()
        manifests = {}
        unreadable = 0
        for directory in member_dirs:
            try:
                manifest = self._manifest.load_manifest(directory)
            except Exception:                             # noqa: BLE001 member remains explicitly partial
                manifest = None
            if not isinstance(manifest, dict):
                unreadable += 1
                continue
            job_id = self._workspace_job_id(directory, manifest)
            manifests[job_id] = manifest
            inputs = manifest.get('inputs') if isinstance(manifest.get('inputs'), dict) else {}
            raw_engine = inputs.get('engine') or 'vasp'
            try:
                engine = self._ta().normalize_engine(raw_engine)
            except Exception:                             # noqa: BLE001 legacy-safe VASP fallback
                engine = str(raw_engine or 'vasp').strip().lower() or 'vasp'
            if engine:
                engines.add(engine)
            task_type = str(manifest.get('task_type') or '').strip()
            if task_type:
                task_types.add(task_type)
        if unreadable:
            degraded.append({
                'kind': 'project_manifest_incomplete', 'project_id': project_id,
                'count': unreadable,
                'message': f'{unreadable} 个项目成员的 job.yaml 不可读',
            })
        preparation = project.get('preparation') or {}
        project_mode = str(project.get('work_mode') or
                           (preparation.get('work_mode')
                            if isinstance(preparation, dict) else '') or '').strip() or None
        engine_list = sorted(engines)
        task_list = sorted(task_types)
        project_view = {
            'id': project_id,
            'name': str(project.get('name') or list_row.get('name') or ''),
            'selection_status': 'valid',
            'project_mode': project_mode,
            'member_count': int(list_row.get('n_members') or len(member_dirs)),
            'done_count': int((pipeline_row or {}).get('done')
                              if (pipeline_row or {}).get('done') is not None
                              else list_row.get('n_done') or 0),
            'actual_engines': engine_list,
            'actual_engine': engine_list[0] if len(engine_list) == 1 and not unreadable else None,
            'actual_task_types': task_list,
            'actual_task_type': task_list[0] if len(task_list) == 1 and not unreadable else None,
            'facts_completeness': 'complete' if not unreadable else 'partial',
        }
        selected_job = None
        if selected_job_id:
            manifest = manifests.get(selected_job_id)
            if manifest is None:
                degraded.append({
                    'kind': 'selected_job_missing', 'job_id': selected_job_id,
                    'message': '保存的作业选择不属于当前项目或其清单不可读',
                })
            else:
                inputs = manifest.get('inputs') if isinstance(manifest.get('inputs'), dict) else {}
                raw_engine = inputs.get('engine') or 'vasp'
                try:
                    job_engine = self._ta().normalize_engine(raw_engine)
                except Exception:                         # noqa: BLE001
                    job_engine = str(raw_engine or 'vasp').strip().lower() or 'vasp'
                history = manifest.get('state_history') or []
                updated = (history[-1].get('at') if history and isinstance(history[-1], dict)
                           else manifest.get('created_at'))
                selected_job = {
                    'id': selected_job_id,
                    'state': manifest.get('state'),
                    'engine': job_engine,
                    'task_type': manifest.get('task_type') or None,
                    'calc_type': manifest.get('calc_type') or None,
                    'updated_at': updated or None,
                }
        return project_view, selected_job

    def _workspace_pipeline_view(self, pipeline_row):
        if not isinstance(pipeline_row, dict):
            return None
        allowed = (
            'stage', 'stage_index', 'stages', 'needs_human', 'recover_round',
            'done', 'total', 'artifact_status', 'scientific_status',
            'scientific_qualification', 'publication_gate_status',
            'desired_report_kind', 'report_kind', 'report_status', 'report_reason',
        )
        out = {key: copy.deepcopy(pipeline_row.get(key)) for key in allowed}
        out['report_reason'] = self._workspace_public_text(out.get('report_reason')) or None
        blockers = []
        if out.get('needs_human'):
            blockers.append({'kind': 'manual_intervention_required',
                             'message': '至少一个项目成员需要人工处理'})
        if out.get('publication_gate_status') == 'blocked':
            blockers.append({'kind': 'publication_gate_blocked',
                             'message': out.get('report_reason') or '报告发布门禁未通过'})
        out['blockers_state'] = 'known'
        out['blockers'] = blockers
        return out

    def _workspace_context_build(self):
        """Return one canonical, path-free workspace context snapshot.

        ``workspace_intent`` contains user preferences.  ``project`` (including
        its selected-job subrecord) and ``pipeline`` contain only facts read
        from the selected registered project and its manifests.
        """
        try:
            state = self._ws().read()
        except Exception as e:                            # noqa: BLE001 state is the restore authority
            return self._workspace_context_failure(e)
        preferences = copy.deepcopy(state.get('preferences') or {})
        degraded = []
        intent = self._workspace_intent(degraded)

        try:
            registry = self._project_registry_snapshot()
        except Exception as e:                            # noqa: BLE001
            registry = {'public_rows': [], 'by_id': {}, 'duplicate_ids': set(),
                        'failures': []}
            degraded.append({'kind': 'project_registry_unavailable',
                             'message': self._workspace_public_text(e)})
        if registry['duplicate_ids']:
            degraded.append({'kind': 'duplicate_project_id',
                             'message': '项目注册表包含重复 opaque project_id'})
        if registry['failures']:
            degraded.append({'kind': 'project_registry_incomplete',
                             'count': len(registry['failures']),
                             'message': '部分注册项目不可读'})
        try:
            pipeline_result = self.pipeline_status()
        except Exception as e:                            # noqa: BLE001
            pipeline_result = {'ok': False, 'projects': [], 'error': str(e)}
        if not pipeline_result.get('ok'):
            degraded.append({'kind': 'pipeline_status_unavailable',
                             'message': self._workspace_public_text(
                                 pipeline_result.get('error'))})
        pipeline_by_id = {
            str(row.get('project_id') or ''): row
            for row in (pipeline_result.get('projects') or []) if isinstance(row, dict)
        }

        projects = []
        candidates = {}
        for list_row in registry['public_rows']:
            if not isinstance(list_row, dict):
                continue
            project_id = str(list_row.get('project_id') or '')
            matches = registry['by_id'].get(project_id) or []
            if len(matches) != 1:
                continue
            record = matches[0]
            project = record['project']
            pipeline_row = pipeline_by_id.get(project_id)
            row = {
                'project_id': project_id,
                'name': str(project.get('name') or list_row.get('name') or ''),
                'n_members': int(list_row.get('n_members') or 0),
                'n_done': int((pipeline_row or {}).get('done')
                              if (pipeline_row or {}).get('done') is not None
                              else list_row.get('n_done') or 0),
                'stage': (pipeline_row or {}).get('stage'),
                'needs_human': ((pipeline_row or {}).get('needs_human')
                                if pipeline_row is not None else None),
            }
            projects.append(row)
            candidates.setdefault(project_id, []).append(
                {'project': project, 'list_row': list_row,
                 'pipeline_row': pipeline_row})

        current_id = preferences.get('current_project_id')
        selection_status = 'none'
        selected = None
        if current_id:
            matches = candidates.get(current_id) or []
            if len(matches) == 1 and isinstance(matches[0].get('project'), dict):
                selection_status = 'valid'
                selected = matches[0]
            elif len(matches) > 1:
                selection_status = 'ambiguous'
                degraded.append({'kind': 'duplicate_project_id', 'project_id': current_id,
                                 'message': '保存的项目 ID 对应多个注册项目'})
            else:
                selection_status = 'missing'
                degraded.append({'kind': 'selected_project_missing',
                                 'project_id': current_id,
                                 'message': '保存的项目选择已不存在或不可读'})

        project_view = None
        selected_job = None
        pipeline_view = None
        if selected is not None:
            project_view, selected_job = self._workspace_project_facts(
                current_id, selected['project'], selected['list_row'],
                selected['pipeline_row'], preferences.get('selected_job_id'), degraded)
            project_view['selected_job'] = selected_job
            pipeline_view = self._workspace_pipeline_view(selected['pipeline_row'])
            if pipeline_view is None:
                degraded.append({'kind': 'selected_pipeline_unavailable',
                                 'project_id': current_id,
                                 'message': '当前项目的管线状态不可用'})

        runtime, _runtime_state, sync, activity = self._workspace_runtime(degraded)
        draft_refs = copy.deepcopy(preferences.get('draft_refs') or {})
        dirty_drafts = sorted(
            draft_id for draft_id, ref in draft_refs.items()
            if isinstance(ref, dict) and ref.get('dirty') is True)
        has_draft_signal = bool(draft_refs)
        selection = {
            'route': copy.deepcopy(preferences.get('route')),
            'route_hash': ((preferences.get('route') or {}).get('hash')
                           if isinstance(preferences.get('route'), dict) else None),
            'project_id': current_id,
            'analysis_id': preferences.get('current_analysis_id'),
            'job_id': preferences.get('selected_job_id'),
            'selection_status': selection_status,
            'source': ('persisted_user_preference'
                       if any(preferences.get(key) is not None
                              for key in ('route', 'current_project_id',
                                          'current_analysis_id', 'selected_job_id'))
                       else 'default'),
        }
        return {
            'ok': True,
            'schema': self._WORKSPACE_CONTEXT_SCHEMA,
            'authority_id': state.get('authority_id'),
            'state_revision': state.get('revision'),
            'selection': selection,
            'workspace_intent': intent,
            'project': project_view,
            'pipeline': pipeline_view,
            'runtime': runtime,
            'sync': sync,
            'restore': {
                'panels': copy.deepcopy(preferences.get('panels') or {}),
                'filters': copy.deepcopy(preferences.get('filters') or {}),
                'sort': copy.deepcopy(preferences.get('sort') or {}),
                'scroll': copy.deepcopy(preferences.get('scroll') or {}),
                'draft_refs': draft_refs,
            },
            'unsaved': {
                'known': has_draft_signal,
                'dirty': bool(dirty_drafts) if has_draft_signal else None,
                'draft_ids': dirty_drafts,
            },
            'activity': activity,
            'projects': projects,
            'degraded': degraded,
            'error': None,
        }

    def workspace_context(self):
        """Public JSON-safe workspace context endpoint."""
        try:
            return self._workspace_context_build()
        except Exception as e:                            # noqa: BLE001 bridge must never leak exceptions
            return self._workspace_context_failure(e)

    def workspace_context_get(self):
        """Compatibility alias for clients that use explicit getter names."""
        return self.workspace_context()

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

    def _comparison_model(self):
        if self._comparison is None:
            from vcstudio.project import comparison
            self._comparison = comparison
        return self._comparison

    def _candidate_eval(self):
        if self._candidate_evaluation is None:
            from vcstudio.project import candidate_evaluation
            self._candidate_evaluation = candidate_evaluation
        return self._candidate_evaluation

    def _paper(self):
        if self._paper_report is None:
            from vcstudio.project import paper_report
            self._paper_report = paper_report
        return self._paper_report

    def _reports(self):
        """Return the single Phase C report-service instance for every adapter."""
        if self._report_service_instance is None:
            from vcstudio.project.report_service import ReportService

            self._report_service_instance = ReportService(self)
        return self._report_service_instance

    def _analysis_preferences(self):
        if self._analysis_preferences_store is None:
            from vcstudio.project.analysis_preferences import AnalysisPreferencesStore

            self._analysis_preferences_store = AnalysisPreferencesStore()
        return self._analysis_preferences_store

    def _ri(self):
        """本地结果文件夹扫描/导入引擎（纯本地 I/O）。"""
        if self._result_import is None:
            from vcstudio.project import result_import
            self._result_import = result_import
        return self._result_import

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

    def _chat(self):
        """Python 原生对话服务；会话/附件落用户配置目录，不进入项目源文件。"""
        service = self._assistant_chat
        if service is not None and hasattr(service, 'list_sessions'):
            return service
        if service is None:
            from vcstudio.project import assistant_chat as service
        root_fn = getattr(self._config, 'user_config_dir', None)
        root = (root_fn() if callable(root_fn)
                else os.path.join(os.path.expanduser('~'), '.config', 'vcstudio'))
        cls = getattr(service, 'AssistantChat')
        self._assistant_chat = cls(
            os.path.join(str(root), 'assistant'),
            config_loader=self._config.load_config,
            key_loader=self._ai().load_api_key,
        )
        return self._assistant_chat

    def _ws(self):
        """用户级工作区状态仓（原子/CAS；不写 project.yaml/config.yaml）。"""
        if self._workspace_state_store is None:
            from vcstudio.gui_web.workspace_state import WorkspaceStateStore
            self._workspace_state_store = WorkspaceStateStore()
        return self._workspace_state_store

    def _workspace_context(self):
        """Atomic/CAS authority for scenario, engine, and calculation."""
        if self._workspace_context_store is None:
            from vcstudio.gui_web.workspace_context import WorkspaceContextStore
            self._workspace_context_store = WorkspaceContextStore(self._config)
        return self._workspace_context_store

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

    def _ta(self):
        """任务分析能力矩阵与可追溯报告（纯本地 I/O）。"""
        if self._task_analysis is None:
            from vcstudio.project import task_analysis
            self._task_analysis = task_analysis
        return self._task_analysis

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

    def _msl(self):
        """金属 slab 建模引擎(numpy 相邻,重)延迟加载。"""
        if self._metal_slab is None:
            from vcstudio.generate import metal_slab
            self._metal_slab = metal_slab
        return self._metal_slab

    def _usage(self):
        """实际核时统计引擎(纯函数)延迟加载。"""
        if self._usage_mod is None:
            from vcstudio.cluster import usage
            self._usage_mod = usage
        return self._usage_mod

    def _profile_cores(self):
        """集群名 → 当前配置核数(nodes×ppn;ppn 未配则不入表)——usage 的回退口径。"""
        out = {}
        try:
            for nm, prof in (self._profiles.load_profiles() or {}).items():
                ppn = int(getattr(prof, 'ppn', 0) or 0)
                nodes = int(getattr(prof, 'nodes', 1) or 1)
                if ppn:
                    out[nm] = nodes * ppn
        except Exception:                                 # noqa: BLE001 配置坏不挡统计
            pass
        return out

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

    def _save_password_verified(self, name, password):
        """写入 keyring 后立刻回读；setter 未报错不代表凭据真的可用。"""
        stored = self._secrets.set_password(name, password)
        if stored is False:
            raise RuntimeError('系统凭据库拒绝保存')
        if self._secrets.get_password(name) != password:
            raise RuntimeError('系统凭据库写入后回读不一致')

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
            scheduler = str(fields.get('scheduler') or 'Slurm').strip()
            if scheduler not in ('Slurm', 'PBS'):
                return {'ok': False,
                        'error': f'当前只支持 Slurm / PBS 自动提交，不能保存 {scheduler} 为可提交调度器'}
            fields['scheduler'] = scheduler
            if 'engine_commands' in fields:
                raw_commands = fields['engine_commands']
                if not isinstance(raw_commands, dict):
                    return {'ok': False,
                            'error': 'engine_commands 必须是「引擎: 执行命令」对照表'}
                fields['engine_commands'] = {
                    str(key).strip().lower(): str(value or '').strip()
                    for key, value in raw_commands.items() if str(key).strip()
                }
            allp = self._profiles.load_profiles()
            # 旧前端还不认识该字段时不会随表单回传；保存其它
            # 集群参数不应悄悄清空已配置的多引擎命令。
            if 'engine_commands' not in fields and name in allp:
                fields['engine_commands'] = dict(
                    getattr(allp[name], 'engine_commands', {}) or {})
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
            res = self._ssh().check_connection(prof, password, trust_new=trust_new)
            # 成功 + 显式给了密码 + 该 profile 走密码认证 → 顺手存 keyring
            if res.ok and password and prof.auth == 'password':
                try:
                    self._save_password_verified(name, password)
                except Exception as e:                    # noqa: BLE001
                    return {'ok': False, 'message': f'{res.message}；密码保存失败：{e}',
                            'scheduler': res.scheduler, 'needs_trust': False}
            return {'ok': bool(res.ok), 'message': res.message,
                    'scheduler': res.scheduler, 'needs_trust': bool(res.needs_trust),
                    **self._host_key_evidence(res)}
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
    @staticmethod
    def _project_member_dirs(project):
        """Return every managed project member once, including molecule refs."""
        members = (project or {}).get('members') or {}
        dirs = [members.get('clean_slab'), members.get('gas_ref')]
        dirs.extend(members.get('configs') or [])
        for refs in (members.get('molecules'),
                     (project or {}).get('species_ref_jobs')):
            if isinstance(refs, dict):
                dirs.extend(refs.values())
            elif isinstance(refs, (list, tuple)):
                dirs.extend(refs)
        result, seen = [], set()
        for directory in dirs:
            if not directory:
                continue
            key = os.path.normcase(os.path.normpath(str(directory)))
            if key not in seen:
                seen.add(key)
                result.append(directory)
        return result

    def _project_role_map(self):
        """Map managed directories to a path-stable project and scientific role.

        供 list_jobs 给作业行注入所属吸附能项目组;任何异常(注册表坏/单个
        project.yaml 畸形)兜成空映射或跳过该项目,绝不拖垮 list_jobs。
        """
        def norm(d):
            return os.path.normcase(os.path.normpath(str(d)))

        mapping = {}
        try:
            snapshot = self._project_registry_snapshot()
            for record in snapshot['records']:
                if record['project_id'] in snapshot['duplicate_ids']:
                    continue
                proj = record['project']
                group = {
                    'project': str(proj.get('name') or ''),
                    'project_id': record['project_id'],
                }
                mem = proj.get('members') or {}
                if mem.get('clean_slab'):
                    mapping[norm(mem['clean_slab'])] = {**group, 'role': 'clean'}
                if mem.get('gas_ref'):
                    mapping[norm(mem['gas_ref'])] = {**group, 'role': 'gas'}
                for d in (mem.get('configs') or []):
                    if d:
                        mapping[norm(d)] = {**group, 'role': 'config'}
                molecule_dirs = []
                for refs in (mem.get('molecules'), proj.get('species_ref_jobs')):
                    if isinstance(refs, dict):
                        molecule_dirs.extend(refs.values())
                    elif isinstance(refs, (list, tuple)):
                        molecule_dirs.extend(refs)
                for d in molecule_dirs:
                    if d:
                        mapping.setdefault(norm(d), {**group, 'role': 'molecule'})
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
                inputs = m.get('inputs') if isinstance(m.get('inputs'), dict) else {}
                jobs.append({
                    'id': self._workspace_job_id(job_dir, m),
                    'job_uuid': m.get('job_uuid') or '',
                    'dir': job_dir,
                    'name': os.path.basename(os.path.normpath(job_dir)),
                    'project': grp['project'] if grp else None,
                    'project_id': grp['project_id'] if grp else None,
                    'role': grp['role'] if grp else None,
                    'state': state,
                    'task': f"{m.get('task_type', '')}/{m.get('calc_type', '')}",
                    'task_type': m.get('task_type') or None,
                    'calc_type': m.get('calc_type') or None,
                    'engine': str(inputs.get('engine') or 'vasp').strip().lower(),
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
            return {'jobs': [], 'stale': [],
                    'error': self._workspace_public_text(e)}

    # ── Selection Tray: server-evidenced, advisory resource forecast ────────
    _RESOURCE_FORECAST_SCHEMA = 'vcstudio.resource-forecast-api/v1'
    _RESOURCE_ANALYSIS_TASKS = frozenset({
        'analysis', 'dos', 'dos_pdos', 'bands', 'band', 'bader', 'chgdiff',
        'charge_difference', 'elf', 'workfunction', 'work_function',
        'surface_energy', 'formation_energy', 'binding_energy',
        'formation_binding', 'vaspsol', 'solvation', 'eos', 'conv_encut',
        'conv_kpoints', 'conv_vacuum', 'conv_layers', 'conv_slab',
    })

    @classmethod
    def _resource_task_kind(cls, manifest):
        raw = str((manifest or {}).get('task_type') or '').strip().lower()
        if raw in {'static'}:
            return 'static'
        if raw in {'relax', 'cellopt', 'cell_opt', 'geometry_optimization'}:
            return 'relax'
        if raw in {'neb', 'dimer'}:
            return 'neb'
        if raw == 'aimd':
            return 'aimd'
        if raw in {'freq', 'frequency'}:
            return 'freq'
        if raw in cls._RESOURCE_ANALYSIS_TASKS:
            return 'analysis'
        return None

    @staticmethod
    def _resource_positive_int(value, *, maximum):
        if isinstance(value, bool):
            return None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if 1 <= number <= maximum else None

    @classmethod
    def _resource_cores(cls, manifest):
        attempts = [item for item in ((manifest or {}).get('attempts') or [])
                    if isinstance(item, dict)]
        if not attempts:
            return None
        # Only the submission-time record is actual evidence.  Current
        # profile defaults may have changed and are deliberately not used.
        return cls._resource_positive_int(
            attempts[-1].get('cores'), maximum=65_536)

    @staticmethod
    def _resource_structure_text(job_dir, manifest):
        for name in ('CONTCAR', 'POSCAR'):
            candidate = os.path.join(job_dir, name)
            if os.path.isfile(candidate):
                with open(candidate, encoding='utf-8', errors='replace') as handle:
                    return handle.read()
        if str((manifest or {}).get('task_type') or '').strip().lower() == 'neb':
            try:
                image_dirs = sorted(
                    name for name in os.listdir(job_dir)
                    if name.isdigit() and os.path.isdir(os.path.join(job_dir, name)))
            except OSError:
                image_dirs = []
            for image in image_dirs:
                candidate = os.path.join(job_dir, image, 'POSCAR')
                if os.path.isfile(candidate):
                    with open(candidate, encoding='utf-8', errors='replace') as handle:
                        return handle.read()
        return ''

    @classmethod
    def _resource_natoms(cls, job_dir, manifest):
        text = cls._resource_structure_text(job_dir, manifest)
        lines = [line.strip() for line in text.splitlines()]
        if len(lines) < 7:
            return None
        # VASP 4 places counts on line 6; VASP 5/6 uses a symbol line then
        # counts on line 7.  Do not scan later coordinate rows, which could
        # otherwise be mistaken for counts in a malformed file.
        for index in (5, 6):
            parts = lines[index].split()
            if not parts or not all(re.fullmatch(r'\d+', part) for part in parts):
                continue
            counts = [int(part) for part in parts]
            total = sum(counts)
            return cls._resource_positive_int(total, maximum=100_000)
        return None

    @classmethod
    def _resource_nkpts(cls, job_dir, manifest):
        candidate = os.path.join(job_dir, 'KPOINTS')
        if os.path.isfile(candidate):
            try:
                with open(candidate, encoding='utf-8', errors='replace') as handle:
                    lines = [line.strip() for line in handle if line.strip()]
            except OSError:
                lines = []
            if len(lines) >= 2:
                count = cls._resource_positive_int(lines[1], maximum=10_000_000)
                mode = lines[2].lower() if len(lines) >= 3 else ''
                if count is not None and 'line' not in mode:
                    return count
                if lines[1] == '0' and len(lines) >= 4:
                    mesh = lines[3].split()
                    if len(mesh) >= 3:
                        axes = [cls._resource_positive_int(item, maximum=10_000_000)
                                for item in mesh[:3]]
                        if all(item is not None for item in axes):
                            product = axes[0] * axes[1] * axes[2]
                            return product if product <= 10_000_000 else None
            # An existing but malformed/unsupported (for example line-mode)
            # KPOINTS file is conflicting evidence.  Never mask it with a
            # possibly stale manifest summary.
            return None
        inputs = ((manifest or {}).get('inputs')
                  if isinstance((manifest or {}).get('inputs'), dict) else {})
        raw_mesh = inputs.get('kpoints')
        if isinstance(raw_mesh, (list, tuple)) and len(raw_mesh) == 3:
            axes = [cls._resource_positive_int(item, maximum=10_000_000)
                    for item in raw_mesh]
            if all(item is not None for item in axes):
                product = axes[0] * axes[1] * axes[2]
                return product if product <= 10_000_000 else None
        return None

    @staticmethod
    def _resource_budget_evidence(manifest):
        candidates = []
        for container, key in (
                (manifest, 'remaining_budget_core_hours'),
                (manifest.get('budget') if isinstance(manifest.get('budget'), dict) else {},
                 'remaining_core_hours'),
                (manifest.get('resources')
                 if isinstance(manifest.get('resources'), dict) else {},
                 'remaining_budget_core_hours')):
            if key in container:
                candidates.append(container.get(key))
        if not candidates:
            return 'missing', None
        if len(candidates) != 1 or isinstance(candidates[0], bool):
            return 'invalid', None
        try:
            number = float(candidates[0])
        except (TypeError, ValueError):
            return 'invalid', None
        if not math.isfinite(number) or number < 0:
            return 'invalid', None
        return 'available', number

    @classmethod
    def _resource_request(cls, job_dir, manifest, job_id):
        values = {
            'task_kind': cls._resource_task_kind(manifest),
            'natoms': cls._resource_natoms(job_dir, manifest),
            'nkpts': cls._resource_nkpts(job_dir, manifest),
            'cores': cls._resource_cores(manifest),
        }
        missing = sorted(key for key, value in values.items() if value is None)
        return ({'job_id': job_id, **values} if not missing else None), missing

    @staticmethod
    def _resource_history_state(manifest):
        state = str((manifest or {}).get('state') or '').strip().upper()
        if state in {'DONE', 'COMPLETED', 'FINISHED', 'SUCCESS'}:
            return 'DONE'
        if state in {'FAILED', 'ERROR', 'CANCELLED', 'TIMEOUT', 'LOST',
                     'UNCONVERGED', 'NEEDS_HUMAN'}:
            return 'FAILED'
        return None

    def _resource_history(self, entries):
        from vcstudio.cluster.usage import runtime_window

        usable, unknown_reasons = [], {}
        for job_dir, manifest in entries:
            if not isinstance(manifest, dict):
                unknown_reasons['manifest'] = unknown_reasons.get('manifest', 0) + 1
                continue
            job_id = self._workspace_job_id(job_dir, manifest)
            request, missing = self._resource_request(job_dir, manifest, job_id)
            state = self._resource_history_state(manifest)
            start, end, running, why = runtime_window(manifest)
            if state is None:
                missing.append('terminal_state')
            if start is None or end is None or running or why:
                missing.append('actual_walltime')
            if missing:
                for reason in sorted(set(missing)):
                    unknown_reasons[reason] = unknown_reasons.get(reason, 0) + 1
                continue
            seconds = (end - start).total_seconds()
            if not math.isfinite(seconds) or seconds < 0:
                unknown_reasons['actual_walltime'] = (
                    unknown_reasons.get('actual_walltime', 0) + 1)
                continue
            usable.append({
                'task_kind': request['task_kind'], 'natoms': request['natoms'],
                'nkpts': request['nkpts'], 'cores': request['cores'],
                'elapsed_seconds': seconds, 'state': state,
            })
        return usable, dict(sorted(unknown_reasons.items()))

    @staticmethod
    def _resource_history_basis_sha256(history):
        normalized = [{
            'task_kind': str(item['task_kind']),
            'natoms': int(item['natoms']),
            'nkpts': int(item['nkpts']),
            'cores': int(item['cores']),
            'elapsed_seconds': round(float(item['elapsed_seconds']), 6),
            'state': str(item['state']),
        } for item in history]
        normalized.sort(key=lambda item: json.dumps(
            item, sort_keys=True, separators=(',', ':'), allow_nan=False))
        payload = json.dumps(
            normalized, sort_keys=True, separators=(',', ':'),
            allow_nan=False).encode('utf-8')
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _resource_forecast_display(forecast):
        if not isinstance(forecast, dict):
            return None
        def fixed(value):
            return f'{float(value):.3f}'

        rows = []
        for item in forecast.get('jobs') or []:
            failure_rate = item.get('failure_rate')
            rows.append({
                'job_id': str(item.get('job_id') or ''),
                'estimate_core_hours': fixed(item.get('estimate_core_hours')),
                'range_core_hours': (
                    f'{fixed((item.get("range_core_hours") or {}).get("low"))}–'
                    f'{fixed((item.get("range_core_hours") or {}).get("high"))}'),
                'matching_successful_samples': str(
                    int(item.get('matching_successful_samples') or 0)),
                'matching_observed_samples': str(
                    int(item.get('matching_observed_samples') or 0)),
                'confidence': str(item.get('confidence') or 'low'),
                'failure_rate': ('unknown' if failure_rate is None
                                 else f'{float(failure_rate):.1%}'),
            })
        remaining = forecast.get('remaining_budget_core_hours')
        return {
            'estimate_core_hours': fixed(forecast.get('estimate_core_hours')),
            'range_core_hours': (
                f'{fixed((forecast.get("range_core_hours") or {}).get("low"))}–'
                f'{fixed((forecast.get("range_core_hours") or {}).get("high"))}'),
            'remaining_budget_core_hours': (
                'unknown' if remaining is None else fixed(remaining)),
            'confidence': str(forecast.get('confidence') or 'low'),
            'budget_status': str(forecast.get('budget_status') or 'unlimited_or_unknown'),
            'jobs': rows,
        }

    def jobs_resource_forecast(self, job_ids):
        """Forecast selected ledger jobs from server evidence only.

        Browser callers provide opaque job IDs only.  Scientific/resource
        values, historical measurements, and remaining budget are reconstructed
        from the current ledger and manifests on the server.
        """
        from vcstudio.project.resource_forecast import forecast_batch

        boundary = {
            'recommendation_only': True,
            'requires_user_confirmation': True,
            'authorizes_submission': False,
        }
        try:
            if (not isinstance(job_ids, list) or not 1 <= len(job_ids) <= 512
                    or any(not isinstance(item, str) or not item.strip()
                           for item in job_ids)):
                raise ValueError('job_ids must contain between 1 and 512 opaque IDs')
            requested = [item.strip() for item in job_ids]
            if len(set(requested)) != len(requested):
                raise ValueError('job_ids must be unique')
            entries = list(self._ledger.load_all())
            index = {}
            for job_dir, manifest in entries:
                if not isinstance(manifest, dict):
                    continue
                opaque_id = self._workspace_job_id(job_dir, manifest)
                if opaque_id in index:
                    raise ValueError('ledger contains duplicate opaque job IDs')
                index[opaque_id] = (job_dir, manifest)
            missing_ids = sorted(set(requested) - set(index))
            if missing_ids:
                raise ValueError('one or more job IDs are not present in the server ledger')

            requests, unknown_jobs, budget_rows = [], [], []
            for job_id in requested:
                job_dir, manifest = index[job_id]
                request, missing = self._resource_request(job_dir, manifest, job_id)
                if request is None:
                    unknown_jobs.append({'job_id': job_id, 'missing': missing})
                else:
                    requests.append(request)
                budget_rows.append(self._resource_budget_evidence(manifest))

            budget = None
            if all(status == 'available' for status, _value in budget_rows):
                values = {round(float(value), 9) for _status, value in budget_rows}
                if len(values) == 1:
                    budget_status = 'available'
                    budget = values.pop()
                else:
                    budget_status = 'conflicting'
            elif any(status == 'invalid' for status, _value in budget_rows):
                budget_status = 'invalid'
            else:
                budget_status = 'unknown'

            history, history_missing = self._resource_history(entries)
            history_basis_sha256 = self._resource_history_basis_sha256(history)
            forecast = (forecast_batch(
                requests, history, remaining_budget_core_hours=budget)
                if requests else None)
            status = 'unavailable'
            if forecast is not None:
                critical_risks = {'budget_at_risk', 'high_recent_failure_rate'}
                status = ('at_risk' if critical_risks.intersection(
                    forecast.get('risk_flags') or []) else 'ready')
            semantic = {
                'schema': self._RESOURCE_FORECAST_SCHEMA,
                'selected_job_ids': sorted(requested),
                'forecast_semantic_sha256': (
                    forecast.get('semantic_sha256') if forecast else None),
                'forecast_jobs': [{
                    'job_id': item.get('job_id'),
                    'request': item.get('request'),
                    'semantic_sha256': item.get('semantic_sha256'),
                } for item in ((forecast or {}).get('jobs') or [])],
                'unknown_jobs': sorted(unknown_jobs, key=lambda item: item['job_id']),
                'history_basis_sha256': history_basis_sha256,
                'budget_evidence_status': budget_status,
            }
            semantic_sha256 = hashlib.sha256(json.dumps(
                semantic, sort_keys=True, separators=(',', ':'),
                allow_nan=False).encode('utf-8')).hexdigest()
            result = {
                'schema': self._RESOURCE_FORECAST_SCHEMA,
                'ok': True,
                'status': status,
                'forecast': forecast,
                'display': self._resource_forecast_display(forecast),
                'unknown_jobs': sorted(unknown_jobs, key=lambda item: item['job_id']),
                'denominators': {
                    'selected_jobs': len(requested),
                    'forecastable_jobs': len(requests),
                    'unknown_jobs': len(unknown_jobs),
                    'ledger_entries': len(entries),
                    'usable_history_entries': len(history),
                    'unknown_history_entries': len(entries) - len(history),
                },
                'history_missing_reasons': history_missing,
                'history_basis_sha256': history_basis_sha256,
                'budget_evidence_status': budget_status,
                'semantic_sha256': semantic_sha256,
                **boundary,
                'error': None,
            }
            return self._analysis_workbench_public_value(result)
        except Exception as exc:                         # noqa: BLE001 public seam
            return {
                'schema': self._RESOURCE_FORECAST_SCHEMA,
                'ok': False, 'status': 'unavailable', 'forecast': None,
                'display': None,
                'unknown_jobs': [], 'denominators': {},
                'history_missing_reasons': {},
                'history_basis_sha256': None, 'semantic_sha256': None,
                'budget_evidence_status': 'unknown', **boundary,
                'error': self._workspace_public_text(exc),
            }

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

    @staticmethod
    def _job_operation_fingerprint(action, profile, dirs):
        payload = {
            'action': str(action or ''),
            'profile': str(profile or ''),
            'dirs': sorted({str(item) for item in (dirs or [])}),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _job_batch_with_idempotency(function, *args, idempotency_key=None, **kwargs):
        """Pass an operation id exactly once when an injected adapter supports it."""
        try:
            parameters = inspect.signature(function).parameters.values()
        except (TypeError, ValueError):
            parameters = ()
        supports_key = any(
            parameter.name == 'idempotency_key'
            or parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        if supports_key:
            call_kwargs = dict(kwargs)
            call_kwargs['idempotency_key'] = idempotency_key
            return function(*args, **call_kwargs)
        return function(*args, **kwargs)

    @staticmethod
    def _submit_batch_with_idempotency(submit_batch, profile, password, dirs,
                                       trust_new, idempotency_key):
        """Pass the durable operation id while retaining old injected adapters.

        Production ``batch_ops.submit_batch`` accepts the keyword.  Some
        extensions and lightweight tests still expose the historical four-
        argument callable, so signature inspection avoids a retry-on-TypeError
        pattern that could itself duplicate a remote mutation.
        """
        return Api._job_batch_with_idempotency(
            submit_batch, profile, password, list(dirs), trust_new,
            idempotency_key=idempotency_key)

    def _run_job_operation_once(self, idempotency_key, *, action, profile, dirs, invoke):
        """Run one mutating remote batch exactly once per client operation key.

        Password discovery and host-key confirmation deliberately happen before this seam.  Their
        provisional replies must remain retryable with the same key; only an invocation that reaches
        the mutating batch implementation is reserved and cached.  The cache is process-local because
        the job manifests remain the durable source of state across restarts.
        """
        key = str(idempotency_key or '').strip()
        if not key:
            return invoke()
        if len(key) > 128 or not re.fullmatch(r'[A-Za-z0-9_.:-]{12,128}', key):
            return {'ok': False, 'busy': False, 'duplicate': False,
                    'error': '无效的作业操作请求标识'}
        fingerprint = self._job_operation_fingerprint(action, profile, dirs)
        now = time.monotonic()
        with self._job_operation_lock:
            # 有界缓存避免长期开机时无限增长；正在执行的记录绝不逐出。
            expired = [item for item, record in self._job_operations.items()
                       if record.get('state') == 'complete'
                       and now - float(record.get('finished_at') or now) > 3600]
            for item in expired:
                self._job_operations.pop(item, None)
            if len(self._job_operations) > 256:
                complete = sorted(
                    ((item, record) for item, record in self._job_operations.items()
                     if record.get('state') == 'complete'),
                    key=lambda pair: float(pair[1].get('finished_at') or 0))
                for item, _record in complete[:len(self._job_operations) - 256]:
                    self._job_operations.pop(item, None)

            previous = self._job_operations.get(key)
            if previous:
                if previous.get('fingerprint') != fingerprint:
                    return {'ok': False, 'busy': False, 'duplicate': True,
                            'idempotency_key': key,
                            'error': '同一作业操作请求标识不能用于不同的目标'}
                if previous.get('state') == 'running':
                    return {'ok': False, 'busy': True, 'duplicate': True,
                            'idempotency_key': key,
                            'error': '相同作业操作正在执行，请等待当前请求完成'}
                replay = copy.deepcopy(previous.get('result') or {})
                replay['idempotency_key'] = key
                replay['duplicate'] = True
                replay['replayed'] = True
                return replay
            self._job_operations[key] = {
                'state': 'running', 'fingerprint': fingerprint, 'started_at': now,
            }

        try:
            result = invoke()
            if not isinstance(result, dict):
                result = {'ok': False, 'error': '作业操作返回格式无效'}
        except Exception as exc:                          # noqa: BLE001 public bridge must not raise
            result = {'ok': False, 'error': str(exc)}

        provisional = (bool(result.get('needs_trust'))
                       or result.get('error') == 'NEED_PASSWORD'
                       or bool(result.get('busy')))
        with self._job_operation_lock:
            if provisional:
                self._job_operations.pop(key, None)
            else:
                stored = copy.deepcopy(result)
                stored['idempotency_key'] = key
                stored['duplicate'] = False
                stored['replayed'] = False
                self._job_operations[key] = {
                    'state': 'complete', 'fingerprint': fingerprint,
                    'finished_at': time.monotonic(), 'result': stored,
                }
                result = stored
        return result

    @staticmethod
    def _host_key_evidence(value):
        """从连接异常/结果对象或 dict 透传可核对的 SSH 主机证据。"""
        def _get(key):
            if isinstance(value, dict):
                return value.get(key, '')
            return getattr(value, key, '')
        return {key: str(_get(key) or '')
                for key in ('fingerprint', 'algorithm', 'host')}

    def submit_jobs(self, dirs, name, password, trust_new=False, idempotency_key=None):
        return self._delegate(name, password,
                              lambda prof, pw: self._run_job_operation_once(
                                  idempotency_key, action='submit', profile=prof.name,
                                  dirs=dirs, invoke=lambda: self._submit_batch_with_idempotency(
                                      self._bo().submit_batch, prof, pw, dirs, trust_new,
                                      idempotency_key)))

    def fetch_jobs(self, dirs, name, password, trust_new=False, files=None):
        def _fetch(prof, pw):
            if files is None:
                return self._bo().fetch_batch(prof, pw, list(dirs), trust_new)
            return self._bo().fetch_batch(prof, pw, list(dirs), trust_new, files)
        return self._delegate(name, password, _fetch)

    def continue_jobs(self, dirs, name, password, trust_new=False, idempotency_key=None):
        return self._delegate(name, password,
                              lambda prof, pw: self._run_job_operation_once(
                                  idempotency_key, action='continue', profile=prof.name,
                                  dirs=dirs, invoke=lambda: self._job_batch_with_idempotency(
                                      self._bo().continue_batch,
                                      prof, pw, list(dirs), trust_new,
                                      idempotency_key=idempotency_key)))

    def refresh_status(self, name, password, trust_new=False):
        """Refresh one profile without racing the background supervisor."""
        if not self._pipeline_lock.acquire(blocking=False):
            return {
                'needs_trust': False, 'results': [], 'busy': True,
                'error': '后台自动托管正在同步，请等待本轮完成后再手动查询',
            }
        try:
            return self._refresh_status_once(name, password, trust_new)
        finally:
            self._pipeline_lock.release()

    def _refresh_status_once(self, name, password, trust_new=False):
        def _refresh(prof, pw):
            # 目标 dirs 逻辑照抄 jobs_tab._on_refresh_status:
            # 台账里该集群 + 有作业号 + 状态 SUBMITTED/QUEUED/RUNNING
            targets = [d for d, m in self._ledger.load_all()
                       if m and m.get('scheduler_job_id') and m.get('cluster') == prof.name
                       and m.get('state') in ('SUBMITTED', 'QUEUED', 'RUNNING')]
            if not targets:
                return {'needs_trust': False, 'results': []}
            return self._bo().refresh_batch(prof, pw, targets, trust_new)
        return self._delegate(name, password, _refresh)

    def refresh_all_status(self):
        """并行刷新所有服务器上的活跃作业；单台失败、缺密码或待确认指纹不拖累其它服务器。"""
        if not self._pipeline_lock.acquire(blocking=False):
            return {
                'ok': True, 'profiles': [], 'results': [], 'errors': [],
                'needs_trust': False, 'busy': True,
                'error': '后台自动托管正在同步，本次独立刷新已跳过',
            }
        try:
            return self._refresh_all_status_once()
        finally:
            self._pipeline_lock.release()

    def _refresh_all_status_once(self):
        try:
            profiles = self._profiles.load_profiles()
            active = set()
            for _d, manifest in self._ledger.load_all():
                if (manifest and manifest.get('scheduler_job_id')
                        and manifest.get('state') in ('SUBMITTED', 'QUEUED', 'RUNNING')
                        and manifest.get('cluster')):
                    active.add(str(manifest.get('cluster')))
            names = [name for name in profiles if name in active]
            missing_names = sorted(active.difference(profiles))
            if not names and not missing_names:
                return {'ok': True, 'profiles': [], 'results': [], 'errors': [],
                        'needs_trust': False, 'error': None}

            rows, errors, flattened = [], [], []
            if names:
                workers = min(8, len(names))
                with ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix='vcs-refresh') as pool:
                    futures = {pool.submit(
                        self._refresh_status_once, name, None, False): name
                               for name in names}
                    for future in as_completed(futures):
                        name = futures[future]
                        try:
                            result = future.result() or {}
                        except Exception as exc:          # noqa: BLE001 单台隔离
                            result = {'error': str(exc)}
                        needs_trust = bool(result.get('needs_trust'))
                        # 缺主机信任时 batch 层没有普通 error；若仍标 ok，前端会把它
                        # 当成“该服务器无活跃作业”而静默吞掉安全提示。
                        error = result.get('error')
                        if needs_trust and not error:
                            error = 'HOST_KEY_CONFIRMATION_REQUIRED'
                        entry = {'name': name,
                                 'ok': not bool(error),
                                 'results': list(result.get('results') or []),
                                 'error': error,
                                 'needs_trust': needs_trust,
                                 **self._host_key_evidence(result)}
                        rows.append(entry)
                        for item in entry['results']:
                            flattened.append({'cluster': name, 'result': item})
                        if entry['error']:
                            errors.append(f'[{name}] {entry["error"]}')
            # 台账仍引用已删除的 profile 时必须显式暴露；否则这些活跃作业会永远
            # 消失在“全部服务器监控”之外，用户误以为没有任务。
            for name in missing_names:
                msg = '集群配置已删除或改名；请恢复同名配置后再监控该服务器上的作业'
                rows.append({'name': name, 'ok': False, 'results': [],
                             'error': msg, 'needs_trust': False,
                             'fingerprint': '', 'algorithm': '', 'host': ''})
                errors.append(f'[{name}] {msg}')
            order = names + missing_names
            rows.sort(key=lambda row: order.index(row['name']))
            return {'ok': not errors, 'profiles': rows, 'results': flattened,
                    'errors': errors,
                    'needs_trust': any(row['needs_trust'] for row in rows),
                    'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'profiles': [], 'results': [], 'errors': [],
                    'needs_trust': False, 'error': str(e)}

    def queue_detail(self, name, password, trust_new=False):
        return self._delegate(name, password,
                              lambda prof, pw: self._bo().queue_detail(
                                  prof, pw, trust_new))

    def job_live_energy(self, job_dir, name=None, password=None, trust_new=False,
                        max_points=400):
        """运行中作业实时能量曲线:本地 OSZICAR 优先,无则经 SSH 读远端(manifest.remote_dir)。

        返回 {'ok','source'('local'|'remote'),'steps':[{'n','e0','de','scf'}],'natoms',
        'state','needs_trust','error'}。远端尚无 OSZICAR(排队中)/未提交 → 中文说明;
        需要密码 → error='NEED_PASSWORD'(前端弹框口径同其余集群操作)。绝不抛。
        """
        empty = {'ok': False, 'source': None, 'steps': [], 'natoms': None,
                 'state': None, 'needs_trust': False}
        try:
            d = (job_dir or '').strip()
            if not d or not os.path.isdir(d):
                return {**empty, 'error': '作业目录不存在'}
            m = self._manifest.load_manifest(d) or {}
            state = m.get('state')
            text, source = None, None
            local = os.path.join(d, 'OSZICAR')
            if os.path.isfile(local):
                with open(local, encoding='utf-8', errors='replace') as f:
                    text = f.read()
                source = 'local'
            else:
                rdir = (m.get('remote_dir') or '').strip()
                requested = str(name or '').strip()
                bound = str(m.get('cluster') or '').strip()
                if rdir and not bound:
                    return {**empty, 'state': state,
                            'error': ('该旧作业虽有远端目录，但未记录所属服务器。为防止从'
                                      '错误服务器读取同名路径，请先通过“认领外部作业”'
                                      '明确绑定服务器后再查看实时能量。')}
                if requested and bound and requested != bound:
                    return {**empty, 'state': state,
                            'error': (f'实时能量拒绝跨服务器读取：作业属于「{bound}」，'
                                      f'当前选择「{requested}」')}
                cl = bound
                if not rdir or not cl:
                    return {**empty, 'state': state,
                            'error': ('本地无 OSZICAR,且该作业没有远端目录/集群记录'
                                      '(尚未提交?);提交后才有实时能量可看。')}
                prof, pw, err = self._resolve(cl, password)
                if err:
                    return {**empty, 'state': state, 'error': err.get('error') or str(err)}
                try:
                    self._sub().assert_profile_binding(
                        prof, d, '读取实时能量', manifest=m)
                except Exception as e:                    # noqa: BLE001 连网前失败即停
                    return {**empty, 'state': state, 'error': str(e)}
                conn = self._conn()
                try:
                    client, jump = conn.open_client(prof, pw, trust_new=trust_new)
                except conn.ConnectError as e:
                    return {**empty, 'state': state,
                            'needs_trust': bool(getattr(e, 'needs_trust', False)),
                            **self._host_key_evidence(e),
                            'error': str(e)}
                try:
                    sftp = client.open_sftp()
                    try:
                        with sftp.file(rdir.rstrip('/') + '/OSZICAR', 'r') as f:
                            raw = f.read()
                    finally:
                        sftp.close()
                    text = (raw.decode('utf-8', errors='replace')
                            if isinstance(raw, bytes) else str(raw))
                    source = 'remote'
                except FileNotFoundError:
                    return {**empty, 'state': state,
                            'error': '远端尚无 OSZICAR(排队中或刚启动),稍后再试。'}
                except Exception as e:                    # noqa: BLE001
                    return {**empty, 'state': state, 'error': f'读取远端 OSZICAR 失败:{e}'}
                finally:
                    conn.close_quiet(client, jump)
            steps = self._conv.parse_oszicar(text or '')
            pts = [{'n': i + 1, 'e0': s.get('E0'), 'de': s.get('dE'),
                    'scf': s.get('scf_iters')}
                   for i, s in enumerate(steps)][-int(max_points or 400):]
            natoms = None
            ptxt = self._read_named_text(d, 'POSCAR')
            if ptxt:
                natoms = self._poscar_natoms(ptxt) or None
            return {'ok': True, 'source': source, 'steps': pts, 'natoms': natoms,
                    'state': state, 'needs_trust': False, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {**empty, 'error': str(e)}

    def query_workdir(self, job_id, name, password, trust_new=False):
        """认领辅助:按作业号查远程工作目录(PBS qstat -f;查不到 workdir='')。"""
        return self._delegate(name, password,
                              lambda prof, pw: self._bo().workdir_lookup(
                                  prof, pw, str(job_id), trust_new))

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
                         if (m and m.get('scheduler_job_id')
                             and str(m.get('cluster') or '') == str(prof.name))}
            root = self.adopt_root_get().get('root')
            return self._bo().adopt_scan(prof, pw, trust_new, known_ids, root)
        return self._delegate(name, password, _scan)

    def adopt_job(self, local_dir, name, job_id, remote_dir, job_name='', task_type=None):
        try:
            prof = self._profiles.load_profiles().get(name)
            if prof is None:
                return {'error': f'集群「{name}」不存在,请先在集群页保存'}
            kwargs = {'name': job_name}
            if task_type is not None and str(task_type).strip():
                # 严格规范化在 submitter 单一入口完成；这里只透传显式选择。
                kwargs['task_type'] = task_type
            self._sub().adopt_external_job(local_dir, prof, job_id, remote_dir, **kwargs)
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
    def proj_scan_structures(self, root, clean_slab=None, reference_species=None):
        """递归发现可用于 slab/adsorption 的普通结构文件（严格只读）。"""
        try:
            source = str(root or '').strip()
            if not source:
                raise ValueError('请先选择结构根目录')
            items = self._adsorption.scan_structure_files(source)
            for item in items:
                resolution = self._resolve_lis_member_incar(item.get('path'))
                item.update({
                    'incar_path': resolution['path'],
                    'incar_sha256': resolution['sha256'],
                    'incar_status': resolution['status'],
                    'incar_issues': resolution['issues'],
                    'incar_source': resolution['source'],
                })
            grouped = {'items': items, 'species_groups': [], 'warnings': []}
            clean = str(clean_slab or '').strip()
            if clean:
                if reference_species:
                    grouped = self._adsorption.identify_config_species(
                        clean, items, reference_species=reference_species)
                else:
                    grouped = self._adsorption.identify_config_species(clean, items)
            return {'ok': True, 'root': os.path.abspath(source),
                    'items': grouped.get('items') or [],
                    'species_groups': grouped.get('species_groups') or [],
                    'warnings': grouped.get('warnings') or [], 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'root': str(root or ''), 'items': [],
                    'species_groups': [], 'warnings': [], 'error': str(e)}

    def proj_scan_lis_inputs(self, root, reference_species=None):
        """一次只读识别逐目录 INCAR、clean slab 与 adsorption 结构族。"""
        empty = {
            'ok': False, 'root': str(root or ''), 'incar': '',
            'incar_candidates': [], 'clean_slab': '', 'clean_candidates': [],
            'clean_incar': '', 'clean_incar_sha256': '',
            'clean_incar_status': 'missing', 'clean_incar_issues': [],
            'clean_incar_source': '',
            'configs': [], 'structures': [], 'warnings': [],
            'species_groups': [], 'unresolved_species': 0,
            'source_read_only': True,
        }
        try:
            source = str(root or '').strip()
            if not source:
                raise ValueError('请先选择本次计算文件夹')
            if reference_species:
                scanned = self._adsorption.scan_lis_input_bundle(
                    source, reference_species=reference_species)
            else:
                scanned = self._adsorption.scan_lis_input_bundle(source)
            result = dict(scanned or {})
            return {**empty, **result, 'ok': True, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {**empty, 'error': str(e)}

    def proj_resolve_member_incar(self, structure_path, fallback_incar=''):
        """只读解析一个结构实际会使用的 INCAR，供逐项选择后的界面即时复核。"""
        try:
            result = self._resolve_lis_member_incar(
                structure_path, fallback_incar=fallback_incar)
            return {'ok': result.get('status') == 'ready', **result,
                    'error': None if result.get('status') == 'ready'
                    else '；'.join(result.get('issues') or ['没有可用的 INCAR'])}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'path': '', 'sha256': '', 'status': 'invalid',
                    'source': '', 'issues': [str(e)], 'error': str(e)}

    def _resolve_lis_member_incar(self, structure_path, *, fallback_incar='',
                                  expected_path='', expected_sha256=''):
        """后端重新绑定结构同目录 INCAR；本地文件优先，显式备用只补缺失。"""
        structure = os.path.abspath(os.path.expanduser(str(structure_path or '').strip()))
        if not os.path.isfile(structure):
            raise ValueError(f'结构文件不存在：{structure}')
        resolver = getattr(self._adsorption, 'resolve_structure_incar', None)
        if callable(resolver):
            resolved = dict(resolver(structure, fallback=str(fallback_incar or '').strip()) or {})
        else:
            parent = os.path.dirname(structure)
            candidates = []
            try:
                names = os.listdir(parent)
            except OSError as exc:
                raise ValueError(f'无法读取结构目录：{parent}：{exc}') from exc
            for name in names:
                path = os.path.join(parent, name)
                if name.casefold() == 'incar' and os.path.isfile(path) and not os.path.islink(path):
                    candidates.append(os.path.abspath(path))
            if len(candidates) > 1:
                resolved = {'path': '', 'status': 'ambiguous', 'source': 'same_directory',
                            'issues': ['同目录存在多个大小写不同的 INCAR，无法安全选择']}
            else:
                fallback = os.path.abspath(os.path.expanduser(str(fallback_incar).strip())) \
                    if str(fallback_incar or '').strip() else ''
                selected = candidates[0] if candidates else fallback
                source = 'same_directory' if candidates else ('explicit_fallback' if selected else '')
                resolved = {'path': selected, 'status': 'ready' if selected else 'missing',
                            'source': source, 'issues': [] if selected else ['同目录缺少 INCAR']}
        path = os.path.abspath(os.path.expanduser(str(
            resolved.get('path') or resolved.get('incar_path') or '').strip())) \
            if str(resolved.get('path') or resolved.get('incar_path') or '').strip() else ''
        issues = [str(item) for item in (resolved.get('issues') or []) if str(item).strip()]
        status = str(resolved.get('status') or ('ready' if path else 'missing'))
        if path:
            if not os.path.isfile(path):
                status = 'missing'
                issues.append(f'INCAR 不存在：{path}')
            elif os.path.islink(path):
                status = 'invalid'
                issues.append('INCAR 不能是符号链接')
            else:
                from vcstudio.generate.incar_builder import parse_incar
                try:
                    with open(path, 'r', encoding='utf-8', errors='replace') as handle:
                        parsed = parse_incar(handle.read())
                except (OSError, ValueError) as exc:
                    parsed = {}
                    issues.append(f'INCAR 无法读取/解析：{exc}')
                if not parsed:
                    status = 'invalid'
                    issues.append('INCAR 未解析到任何 KEY=VALUE 参数')
                ispin = _method_integer(parsed.get('ISPIN')) if parsed else None
                if parsed and 'ISPIN' in parsed and ispin not in {1, 2}:
                    status = 'invalid'
                    issues.append(f'ISPIN={parsed.get("ISPIN")!r} 无效（仅允许 1 或 2）')
                if parsed and 'ENCUT' in parsed:
                    encut = _method_number(parsed.get('ENCUT'))
                    if encut is None or encut <= 0:
                        status = 'invalid'
                        issues.append(f'ENCUT={parsed.get("ENCUT")!r} 不是正数')
                for key in ('AEXX', 'HFSCREEN'):
                    if parsed and key in parsed and _method_number(parsed.get(key)) is None:
                        status = 'invalid'
                        issues.append(f'{key}={parsed.get(key)!r} 必须是有限数值')
                if (parsed and 'LDAUTYPE' in parsed
                        and _method_integer(parsed.get('LDAUTYPE')) is None):
                    status = 'invalid'
                    issues.append(f'LDAUTYPE={parsed.get("LDAUTYPE")!r} 必须是整数')
                for key in ('NSW', 'IBRION', 'NELM', 'ISIF'):
                    if parsed and key in parsed and _method_integer(parsed.get(key)) is None:
                        status = 'invalid'
                        issues.append(f'{key}={parsed.get(key)!r} 必须是整数')
        digest = _sha256_file(path) if path and os.path.isfile(path) and not os.path.islink(path) else ''
        expected = os.path.abspath(os.path.expanduser(str(expected_path or '').strip())) \
            if str(expected_path or '').strip() else ''
        if expected and path and os.path.normcase(expected) != os.path.normcase(path):
            status = 'changed'
            issues.append(f'扫描时绑定的 INCAR 已变化：原 {expected}，现 {path}；请重新扫描')
        if expected_sha256 and digest and str(expected_sha256).lower() != digest.lower():
            status = 'changed'
            issues.append('INCAR 在扫描后内容已变化；请重新扫描并确认后再生成')
        if issues and status == 'ready':
            status = 'invalid'
        return {
            'path': path, 'incar_path': path, 'sha256': digest,
            'incar_sha256': digest, 'status': status,
            'incar_status': status, 'source': str(resolved.get('source') or ''),
            'issues': issues, 'incar_issues': issues,
        }

    @staticmethod
    def _reasonable_reference_energy(value) -> bool:
        """Li-S 参考态能量硬门：有限、负值且避免明显解析污染。"""
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(float(value)) and -10000.0 <= float(value) < 0.0)

    def _validated_reference_project(self, reference_project_path, required_species=None):
        """加载并验证本批实际使用的参考物种，返回受审计的能量与目录。

        参考项目可以包含尚未完成的其它 Li-S 物种；那些作业与本批没有直接
        能量关系，不能阻止当前构型提交。调用方传入 ``required_species`` 后只
        对这些标签执行 DONE/能量/方法签名硬门，同时仍明确拒绝缺失标签。
        """
        raw_path = str(reference_project_path or '').strip()
        if not raw_path:
            raise ValueError('请选择已导入并收敛的 Li-S 参考结果项目')
        project = self._load_project_for_path(raw_path)
        if project is None:
            raise ValueError('Li-S 参考项目不存在或 project.yaml 无法读取')
        jobs = dict(project.get('species_ref_jobs') or {})
        if not jobs:
            raise ValueError('参考项目没有 species_ref_jobs；请先把 Li-S 结果导入为分子参考')
        filter_requested = required_species is not None
        requested = {
            str(species or '').strip() for species in (required_species or [])
            if str(species or '').strip()
        }
        missing = sorted(requested - set(jobs))
        if missing:
            raise ValueError('参考项目缺少本批物种：' + '、'.join(missing))
        selected_jobs = ({species: jobs[species] for species in sorted(requested)}
                         if filter_requested else jobs)
        energies, resolved_jobs, signatures = {}, {}, {}
        composition_labels = {}
        reference_root = str(project.get('root') or os.path.dirname(raw_path))
        for raw_species, raw_job_dir in sorted(selected_jobs.items()):
            species = str(raw_species or '').strip()
            if not species:
                raise ValueError('参考项目包含空物种名')
            composition = _formula_composition(species)
            if composition:
                composition_key = tuple(sorted(composition.items()))
                previous = composition_labels.get(composition_key)
                if previous and previous != species:
                    raise ValueError(
                        f'参考标签 {previous} 与 {species} 具有相同原子组成；'
                        'POSCAR 无法区分别名/异构体/电荷或自旋态，请先保留唯一明确参考')
                composition_labels[composition_key] = species
            job_dir = os.path.expanduser(str(raw_job_dir or '').strip())
            if not os.path.isabs(job_dir):
                job_dir = os.path.join(reference_root, job_dir)
            job_dir = os.path.abspath(os.path.normpath(job_dir))
            manifest = self._manifest.load_manifest(job_dir)
            if manifest is None:
                raise ValueError(f'参考物种 {species} 缺少可读 job.yaml')
            if manifest.get('state') != 'DONE':
                raise ValueError(
                    f'参考物种 {species} 尚未通过 DONE 门（当前 {manifest.get("state") or "未知"}）')
            from vcstudio.project import energy_gate
            energy, manifest, _completion = energy_gate.validate_done_energy(
                job_dir, f'参考物种 {species}', self._manifest, require_oszicar=True)
            if not self._reasonable_reference_energy(energy):
                raise ValueError(f'参考物种 {species} 的 DONE 能量缺失或不合理')
            energies[species] = float(energy)
            resolved_jobs[species] = job_dir
            signatures[species] = dict(
                (manifest.get('results') or {}).get('reference_method_signature')
                or (manifest.get('inputs') or {}).get('reference_method_signature') or {})
        return project, energies, resolved_jobs, signatures

    @staticmethod
    def _reference_method_check(signatures, incar_path, planned_potcar=None,
                                effective_encut=None, encut_source=None,
                                planned_element_orders=None, effective_ispin=None,
                                planned_kpoints=None):
        """Compare hard method invariants that are knowable before generation."""
        from vcstudio.generate.incar_builder import parse_incar
        from vcstudio.campaign import fingerprint as fingerprint_mod

        with open(incar_path, 'r', encoding='utf-8', errors='replace') as handle:
            incar = {str(key).upper(): value for key, value in parse_incar(handle.read()).items()}
        gga = str(incar.get('GGA') or '').strip().upper()
        functional = {'RP': 'RPBE', 'PE': 'PBE', 'PS': 'PBEsol', '91': 'PW91'}.get(
            gga, gga or None)
        if functional is None and planned_potcar:
            flavors = {str(titel).split()[0].upper()
                       for titel in planned_potcar if str(titel).split()}
            # In normal VASP workflows a PAW_PBE POTCAR with no explicit GGA
            # uses the PBE semilocal base.  Treating this as "unknown" would let
            # an explicit RPBE reference slip through a human override.
            if flavors == {'PAW_PBE'}:
                functional = 'PBE'
        has_explicit_encut = incar.get('ENCUT') is not None
        explicit_encut = _method_number(incar.get('ENCUT'))
        planned_encut = explicit_encut if has_explicit_encut else _method_number(effective_encut)
        planned_ldau = _method_bool(incar.get('LDAU', False))
        explicit_ispin = _method_integer(incar.get('ISPIN')) \
            if 'ISPIN' in incar else None
        local_method_issues = []
        planned_hybrid_value = (_method_bool(incar.get('LHFCALC'))
                                if 'LHFCALC' in incar else False)
        if 'LHFCALC' in incar and planned_hybrid_value is None:
            local_method_issues.append(
                f'新任务 LHFCALC={incar.get("LHFCALC")!r} 不是合法布尔值')
        planned_hybrid = planned_hybrid_value is True
        planned_aexx = (0.25 if planned_hybrid and 'AEXX' not in incar
                        else _method_number(incar.get('AEXX')))
        planned_hfscreen = (0.0 if planned_hybrid and 'HFSCREEN' not in incar
                            else _method_number(incar.get('HFSCREEN')))
        for key, value in (('AEXX', planned_aexx), ('HFSCREEN', planned_hfscreen)):
            if key in incar and value is None:
                local_method_issues.append(
                    f'新任务 {key}={incar.get(key)!r} 不是有限数值')
        effective_functional = (
            None if local_method_issues else fingerprint_mod.canonical_functional(
                base=functional, metagga=incar.get('METAGGA'),
                lhfcalc=planned_hybrid, aexx=planned_aexx,
                hfscreen=planned_hfscreen))
        planned = {
            'functional': effective_functional,
            'base_functional': functional,
            'ivdw': _method_integer(incar.get('IVDW', 0)),
            'ispin': (explicit_ispin if 'ISPIN' in incar
                      else (_method_integer(effective_ispin) or 1)),
            'ispin_source': ('member_incar' if 'ISPIN' in incar
                             else ('generated_completion' if effective_ispin is not None
                                   else 'vasp_default')),
            'ldau': planned_ldau,
            'ldautype': _method_integer(incar.get('LDAUTYPE')),
            'ldaul': _method_vector(incar.get('LDAUL'), integer=True),
            'ldauu': _method_vector(incar.get('LDAUU')),
            'ldauj': _method_vector(incar.get('LDAUJ')),
            'encut': planned_encut,
            'encut_source': encut_source or (
                'shared_incar' if explicit_encut is not None else 'unavailable'),
            'metagga': _method_choice(incar.get('METAGGA')),
            'lhfcalc': planned_hybrid,
            'aexx': planned_aexx,
            'hfscreen': planned_hfscreen,
            'potcar_titel': list(planned_potcar or []),
            'element_orders': [list(order) for order in (planned_element_orders or [])],
            'kpoints_scheme': planned_kpoints,
        }
        issues, warnings, advisories = list(local_method_issues), [], []
        checked = 0
        for species, signature in sorted((signatures or {}).items()):
            if not signature:
                warnings.append(f'{species}: 导入结果缺方法签名')
                continue
            reference_base = (signature.get('base_functional')
                              or signature.get('gga') or signature.get('functional'))
            legacy_hybrid_label = str(reference_base or '').strip().upper() in {
                'HSE03', 'HSE06', 'PBE0'}
            reference_hybrid_value = (
                _method_bool(signature.get('lhfcalc'))
                if 'lhfcalc' in signature else
                (None if legacy_hybrid_label else False))
            reference_hybrid = reference_hybrid_value is True
            reference_aexx = (
                0.25 if reference_hybrid and 'aexx' not in signature
                else _method_number(signature.get('aexx')))
            reference_hfscreen = (
                0.0 if reference_hybrid and 'hfscreen' not in signature
                else _method_number(signature.get('hfscreen')))
            reference_method_invalid = (
                ('lhfcalc' in signature and reference_hybrid_value is None)
                or ('aexx' in signature and reference_aexx is None)
                or ('hfscreen' in signature and reference_hfscreen is None))
            reference = {
                'functional': (
                    None if reference_method_invalid
                    else fingerprint_mod.canonical_functional(
                        base=reference_base, metagga=signature.get('metagga'),
                        lhfcalc=(reference_hybrid
                                 if 'lhfcalc' in signature else None),
                        aexx=reference_aexx,
                        hfscreen=reference_hfscreen)),
                'base_functional': reference_base,
                'ivdw': _method_integer(signature.get('ivdw')),
                'ispin': _method_integer(signature.get('ispin')),
                'ldau': _method_bool(signature.get('ldau')),
                'ldautype': _method_integer(signature.get('ldautype')),
                'ldaul': signature.get('ldaul'),
                'ldauu': signature.get('ldauu'),
                'ldauj': signature.get('ldauj'),
                'encut': _method_number(signature.get('encut')),
                'metagga': _method_choice(signature.get('metagga')),
                'lhfcalc': (None if reference_hybrid_value is None
                            else reference_hybrid),
                'aexx': reference_aexx,
                'hfscreen': reference_hfscreen,
            }
            reference_elements = list(signature.get('potcar_elements') or [])
            if not reference_elements:
                reference_elements = [
                    _titel_element(titel) for titel in signature.get('potcar_titel') or []]
            reference['element_orders'] = (
                [reference_elements]
                if reference_elements and all(reference_elements) else [])
            for key in ('functional', 'ivdw', 'metagga', 'lhfcalc'):
                if reference.get(key) is None or planned.get(key) is None:
                    warnings.append(f'{species}: 无法核对 {key}')
                elif reference[key] != planned[key]:
                    issues.append(
                        f'{species}: {key} 参考={reference[key]!r}，新任务={planned[key]!r}')
                else:
                    checked += 1
            reference_spin = reference.get('ispin')
            planned_spin = planned.get('ispin')
            if reference_spin is None or planned_spin is None:
                warnings.append(f'{species}: 无法核对 ISPIN')
            elif reference_spin not in {1, 2}:
                issues.append(f'{species}: 参考分子 ISPIN={reference_spin!r} 无效（仅允许 1 或 2）')
            elif planned_spin not in {1, 2}:
                issues.append(f'{species}: 新任务 ISPIN={planned_spin!r} 无效（仅允许 1 或 2）')
            elif reference_spin == planned_spin:
                checked += 1
            elif reference_spin == 1 and planned_spin == 2:
                advisories.append(
                    f'{species}: 参考分子 ISPIN=1，新建 clean slab/adsorption 作业 '
                    'ISPIN=2；不同体系可按各自基态磁性设置，这不是自动不兼容。'
                    '参考分子的自旋设置不参与周期体系 INCAR 绑定；建议保留其基态验证记录')
            else:
                advisories.append(
                    f'{species}: 参考分子 ISPIN={reference_spin}，新建 clean slab/adsorption '
                    f'作业 ISPIN={planned_spin}；不同体系可按各自基态磁性设置，这不是自动'
                    '不兼容。新任务将使用自己的局部自旋设置')
            reference_u = _method_u_by_element(reference)
            planned_u = _method_u_by_element(planned)
            if reference_u is None or planned_u is None:
                warnings.append(f'{species}: 无法按 POTCAR/POSCAR 元素顺序可靠核对 DFT+U')
            else:
                missing_elements = sorted(set(reference_u) - set(planned_u))
                if missing_elements:
                    warnings.append(
                        f'{species}: 新任务缺参考元素 '
                        f'{", ".join(missing_elements)} 的可靠 DFT+U 映射')
                for element in sorted(set(reference_u) & set(planned_u)):
                    if reference_u[element] != planned_u[element]:
                        issues.append(
                            f'{species}: {element} DFT+U 参考={reference_u[element]!r}，'
                            f'新任务={planned_u[element]!r}')
                    else:
                        checked += 1
            if reference.get('encut') is None or planned.get('encut') is None:
                warnings.append(f'{species}: ENCUT 证据不完整')
            elif abs(float(reference['encut']) - planned['encut']) > 1e-8:
                issues.append(
                    f'{species}: ENCUT 参考={reference["encut"]} eV，'
                    f'新任务={planned["encut"]} eV')
            else:
                checked += 1
            if not signature.get('potcar_titel'):
                warnings.append(f'{species}: 参考结果缺 POTCAR TITEL')
            elif not planned['potcar_titel']:
                warnings.append(f'{species}: 无法在提交前核对计划 POTCAR TITEL')
            else:
                planned_set = set(planned['potcar_titel'])
                missing = [titel for titel in signature['potcar_titel']
                           if titel not in planned_set]
                if missing:
                    issues.append(f'{species}: POTCAR TITEL 不一致：{missing}')
                else:
                    checked += len(signature['potcar_titel'])
            if reference['lhfcalc'] and planned['lhfcalc']:
                if reference['aexx'] is None or planned['aexx'] is None:
                    warnings.append(f'{species}: 杂化泛函 AEXX 证据不完整')
                elif abs(reference['aexx'] - planned['aexx']) > 1e-12:
                    issues.append(
                        f'{species}: AEXX 参考={reference["aexx"]}，'
                        f'新任务={planned["aexx"]}')
                else:
                    checked += 1
                if reference['hfscreen'] is None or planned['hfscreen'] is None:
                    warnings.append(f'{species}: 杂化泛函 HFSCREEN 证据不完整')
                elif abs(float(reference['hfscreen']) - planned['hfscreen']) > 1e-12:
                    issues.append(
                        f'{species}: HFSCREEN 参考={reference["hfscreen"]}，'
                        f'新任务={planned["hfscreen"]}')
                else:
                    checked += 1
        status = 'incompatible' if issues else ('verified' if not warnings else 'unverified')
        return {'status': status, 'issues': issues, 'warnings': warnings,
                'advisories': advisories,
                'checked_fields': checked, 'planned': planned,
                'reference_signatures': signatures}

    @staticmethod
    def _periodic_method_check(clean_plan, member_plans):
        """核对 clean slab 与各 adsorption 的能量可比方法；运行控制键允许不同。"""
        from vcstudio.project.method_policy import compare_plans

        issues, warnings, advisories, checked = [], [], [], 0
        for member_name, plan in member_plans.items():
            result = compare_plans(
                clean_plan, plan, left_label='clean slab',
                right_label=member_name, relation='periodic_delta')
            issues.extend(result['issues'])
            warnings.extend(result['warnings'])
            advisories.extend(result['notes'])
            checked += int(result.get('checked_fields') or 0)
        return {'issues': issues, 'warnings': warnings, 'advisories': advisories,
                'checked_fields': checked}

    @staticmethod
    def _lis_repair_plan(structure_paths, plans, parsed_incars,
                         structure_compositions, incar_records,
                         member_source_evidence=None):
        """Build a read-only, hash-bound repair preview for managed copies.

        Only mechanically safe fixes are proposed here: aligning periodic
        ``ENCUT`` upward to the largest effective value, and supplying a local
        MAGMOM candidate when a collinear ``ISPIN=2`` member has none (or the
        vector length cannot match its own POSCAR).  Functional, dispersion,
        POTCAR, DFT+U and the magnetic state itself are scientific choices and
        are deliberately never auto-selected.
        """
        actions, suggestions = [], []

        def _label(path):
            name = os.path.basename(path)
            if name.casefold() in {'poscar', 'contcar'}:
                name = os.path.basename(os.path.dirname(path))
            return name or path

        effective = {
            path: _method_number((plans.get(path) or {}).get('encut'))
            for path in structure_paths
        }
        finite = [value for value in effective.values() if value is not None and value > 0]
        if finite and len({round(value, 10) for value in finite}) > 1:
            target = max(finite)
            target = int(target) if target == int(target) else target
            for path in structure_paths:
                parsed = parsed_incars.get(path) or {}
                old = _method_number(parsed.get('ENCUT')) if 'ENCUT' in parsed else None
                if old is not None and abs(old - float(target)) <= 1e-10:
                    continue
                record = incar_records.get(path) or {}
                actions.append({
                    'member': _label(path), 'structure_path': path,
                    'incar_path': record.get('path') or '',
                    'before_sha256': record.get('sha256') or '',
                    'key': 'ENCUT', 'old': old, 'new': target,
                    'reason': ('将周期成员 ENCUT 统一提高到当前组的安全最大值；'
                               '只修改受管项目副本，不改源目录'),
                    'risk': 'low',
                })

        try:
            from vcstudio.generate.incar_builder import build_magmom, has_magnetic
        except Exception:                                # noqa: BLE001
            build_magmom = has_magnetic = None
        if build_magmom and has_magnetic:
            for path in structure_paths:
                parsed = parsed_incars.get(path) or {}
                plan = plans.get(path) or {}
                composition = structure_compositions.get(path) or {}
                elements, counts = list(composition), list(composition.values())
                if plan.get('ispin_source') == 'generated_completion':
                    # build_job_dir will add its audited MAGMOM/ISPIN
                    # completion to this managed member; no manual suggestion.
                    continue
                if plan.get('ispin') != 2 or not elements or not has_magnetic(elements):
                    continue
                noncollinear = _method_bool(parsed.get('LNONCOLLINEAR')) is True
                if noncollinear:
                    continue
                raw = parsed.get('MAGMOM')
                vector = _method_vector(raw)
                if vector is not None and len(vector) == sum(counts):
                    continue
                candidate = build_magmom(elements, counts)
                if not candidate:
                    continue
                record = incar_records.get(path) or {}
                suggestions.append({
                    'member': _label(path), 'structure_path': path,
                    'incar_path': record.get('path') or '',
                    'before_sha256': record.get('sha256') or '',
                    'key': 'MAGMOM', 'old': raw, 'new': candidate,
                    'reason': ('ISPIN=2 的 MAGMOM 应按本目录 POSCAR 的 NIONS 顺序给出；'
                               '磁矩初态属于科学选择，只给候选，不自动写入'),
                    'risk': 'scientific_choice',
                })

        if not actions and not suggestions:
            return None
        evidence = {}
        for path in structure_paths:
            supplied = dict((member_source_evidence or {}).get(path) or {})
            files = {}
            for raw_name, record in supplied.items():
                if not isinstance(record, dict):
                    continue
                name = str(raw_name or '').upper()
                file_path = str(record.get('path') or '').strip()
                digest = str(record.get('sha256') or '').strip().lower()
                if file_path or digest:
                    files[name] = {'path': file_path, 'sha256': digest}
            if 'INCAR' not in files:
                record = incar_records.get(path) or {}
                files['INCAR'] = {
                    'path': record.get('path') or '',
                    'sha256': record.get('sha256') or '',
                }
            evidence[path] = {'files': files}
        payload = {
            'schema': 1, 'actions': actions, 'suggestions': suggestions,
            'evidence': evidence,
        }
        plan_id = hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode('utf-8')).hexdigest()
        return {**payload, 'plan_id': plan_id,
                'source_unchanged': True, 'target': 'managed_project_copy'}

    def proj_prepare_lis(self, name, slab_path, config_items, incar_path,
                         out_root, reference_project_id, method_confirmation=None,
                         member_incars=None, repair_request=None):
        """Prepare Li-S inputs using one registry-owned opaque reference ID."""
        try:
            reference = self._resolve_project_id(reference_project_id)
        except Exception:                                 # noqa: BLE001 public identity seam
            return self._project_identity_failure(
                project_id=None, name=None, reference_species=[],
                advisories=[], warnings=[], method_check=None,
                needs_method_confirmation=False, repair_plan=None,
                needs_repair_decision=False)
        result = self._call_with_project_bindings(
            [reference],
            lambda: self._proj_prepare_lis_for_reference_path(
                name, slab_path, config_items, incar_path, out_root,
                reference['path'], method_confirmation=method_confirmation,
                member_incars=member_incars, repair_request=repair_request),
            failure={
                'project_id': None, 'name': None, 'reference_species': [],
                'advisories': [], 'warnings': [], 'method_check': None,
                'needs_method_confirmation': False, 'repair_plan': None,
                'needs_repair_decision': False,
            })
        if result.get('error_code') == 'identity_mismatch':
            return result
        locator = str(result.pop('project_path', '') or '')
        result.pop('job_dirs', None)
        result.pop('preparation', None)
        result['project_id'] = None
        result['name'] = None
        records = []
        if locator:
            try:
                project = self._load_project_for_path(locator)
                if isinstance(project, dict):
                    result['project_id'] = self._workspace_project_id(locator, project)
                    result['name'] = str(project.get('name') or name or '')
                    records.append({
                        'project_id': result['project_id'],
                        'path': self._canonical_project_path(locator),
                        'project': project,
                    })
            except Exception:                             # noqa: BLE001 result remains fail-closed
                pass
        return self._project_locator_projection(result, records)

    def _proj_prepare_lis_for_reference_path(
            self, name, slab_path, config_items, incar_path,
            out_root, reference_project_path, method_confirmation=None,
            member_incars=None, repair_request=None):
        """用既有参考能与每个结构同目录的 INCAR 建立可直接提交的 Li-S 项目。"""
        empty = {'ok': False, 'project_path': None, 'job_dirs': [],
                 'reference_species': [], 'advisories': [], 'warnings': [],
                 'method_check': None, 'needs_method_confirmation': False,
                 'repair_plan': None, 'needs_repair_decision': False}
        try:
            project_name = str(name or '').strip()
            slab = os.path.abspath(os.path.expanduser(str(slab_path or '').strip()))
            fallback_incar = os.path.abspath(os.path.expanduser(
                str(incar_path or '').strip())) if str(incar_path or '').strip() else ''
            output = os.path.abspath(os.path.expanduser(str(out_root or '').strip()))
            if (not project_name or project_name in ('.', '..')
                    or os.path.basename(project_name) != project_name):
                raise ValueError('项目名不能为空，且不能包含路径分隔符')
            if not os.path.isfile(slab):
                raise ValueError('清洁表面结构文件不存在')
            if fallback_incar and not os.path.isfile(fallback_incar):
                raise ValueError('显式备用 INCAR 不存在')
            if not str(out_root or '').strip():
                raise ValueError('请选择项目输出根目录')
            target = os.path.join(output, project_name)

            requested_species = {
                str(item.get('species') or '').strip()
                for item in (config_items or []) if isinstance(item, dict)
                and str(item.get('species') or '').strip()
            }
            reference, refs, ref_jobs, ref_signatures = self._validated_reference_project(
                reference_project_path, required_species=requested_species)
            configs, source_species, source_species_evidence = [], {}, {}
            source_incar_evidence = {}
            seen_paths, seen_members = set(), set()
            for raw_item in (config_items or []):
                if not isinstance(raw_item, dict):
                    raise ValueError('吸附构型列表格式无效')
                path = os.path.abspath(os.path.expanduser(
                    str(raw_item.get('path') or '').strip()))
                species = str(raw_item.get('species') or '').strip()
                if not os.path.isfile(path):
                    raise ValueError(f'吸附构型结构文件不存在：{path}')
                if not species:
                    raise ValueError(f'请为构型 {os.path.basename(path)} 选择 Li-S 物种')
                if species not in refs:
                    raise ValueError(f'构型物种 {species} 在已验证参考结果中不存在')
                key = os.path.normcase(path)
                if key in seen_paths:
                    raise ValueError(f'吸附构型重复：{path}')
                member_stem = os.path.splitext(os.path.basename(path))[0]
                if member_stem.casefold() in ('poscar', 'contcar'):
                    member_stem = os.path.basename(os.path.dirname(path))
                member_stem = re.sub(r'[^A-Za-z0-9_.-]', '_',
                                     member_stem) or 'config'
                member_key = member_stem.casefold()
                if member_key in seen_members:
                    raise ValueError('多个构型会生成同名作业目录；请先给结构文件使用不同文件名')
                seen_paths.add(key)
                seen_members.add(member_key)
                configs.append(path)
                source_species[path] = species
                source_incar_evidence[path] = {
                    'path': str(raw_item.get('incar_path') or raw_item.get('incar') or '').strip(),
                    'sha256': str(raw_item.get('incar_sha256') or '').strip(),
                }
            if not configs:
                raise ValueError('至少选择一个 adsorption 构型结构')

            # Resolve the actual same-directory quartet at prepare time.  A
            # complete valid quartet is an atomic member input and will later
            # be copied byte-for-byte; a partial folder may still use the
            # established generator/fallback path.  Re-resolution in
            # create_project binds these hashes across the staging copy.
            member_input_bundles = {}
            quartet_resolver = getattr(
                self._adsorption, 'resolve_structure_quartet', None)
            if callable(quartet_resolver):
                for structure_path in [slab, *configs]:
                    bundle = dict(quartet_resolver(structure_path) or {})
                    if (bundle.get('status') == 'invalid'
                            or bundle.get('mode') == 'blocked'):
                        label = (os.path.basename(os.path.dirname(structure_path))
                                 if os.path.basename(structure_path).casefold()
                                 in {'poscar', 'contcar'}
                                 else os.path.basename(structure_path))
                        raise ValueError(
                            f'{label} 的同目录四件套不可用：'
                            + '；'.join(bundle.get('issues') or ['输入校验失败']))
                    member_input_bundles[structure_path] = bundle

            supplied_incars = dict(member_incars or {}) \
                if isinstance(member_incars, dict) else {}
            clean_expected = supplied_incars.get('clean_slab') or {}
            if isinstance(clean_expected, str):
                clean_expected = {'path': clean_expected}

            config_expected_map = supplied_incars.get('configs') or {}
            if isinstance(config_expected_map, list):
                config_expected_map = {
                    os.path.normcase(os.path.abspath(str(item.get('path') or ''))): item
                    for item in config_expected_map
                    if isinstance(item, dict) and item.get('path')
                }
            elif isinstance(config_expected_map, dict):
                config_expected_map = {
                    os.path.normcase(os.path.abspath(str(key))): value
                    for key, value in config_expected_map.items()
                }
            else:
                config_expected_map = {}

            def _assert_scanned_quartet_unchanged(structure_path, expected):
                """Bind prepare-time inputs to the quartet the user reviewed."""
                if not isinstance(expected, dict):
                    return
                scanned = expected.get('quartet')
                if not isinstance(scanned, dict) or not scanned:
                    return
                current = member_input_bundles.get(structure_path) or {}
                scanned_mode = str(
                    scanned.get('mode') or expected.get('input_mode') or '').lower()
                current_mode = str(current.get('mode') or '').lower()
                if scanned_mode and scanned_mode != current_mode:
                    raise ValueError(
                        f'{structure_path} 的四件套模式在扫描后已变化'
                        f'（{scanned_mode} → {current_mode or "unknown"}）；请重新扫描')
                scanned_files = scanned.get('files') or {}
                current_files = current.get('files') or {}
                if not isinstance(scanned_files, dict):
                    raise ValueError(f'{structure_path} 的扫描四件套证据格式无效')
                scanned_names = {str(name).upper() for name in scanned_files}
                current_names = {str(name).upper() for name in current_files}
                if scanned_names != current_names:
                    raise ValueError(
                        f'{structure_path} 的四件套文件集在扫描后已变化；请重新扫描')
                for raw_name, record in scanned_files.items():
                    name = str(raw_name).upper()
                    if not isinstance(record, dict):
                        raise ValueError(
                            f'{structure_path} 的 {name} 扫描证据格式无效')
                    actual = current_files.get(name) or current_files.get(raw_name) or {}
                    expected_path = str(record.get('path') or '').strip()
                    actual_path = str(actual.get('path') or '').strip()
                    if (expected_path and actual_path
                            and os.path.normcase(os.path.realpath(expected_path))
                            != os.path.normcase(os.path.realpath(actual_path))):
                        raise ValueError(
                            f'{structure_path} 的 {name} 路径在扫描后已变化；请重新扫描')
                    expected_hash = str(record.get('sha256') or '').strip().lower()
                    actual_hash = str(actual.get('sha256') or '').strip().lower()
                    if (not re.fullmatch(r'[0-9a-f]{64}', expected_hash)
                            or expected_hash != actual_hash):
                        raise ValueError(
                            f'{structure_path} 的 {name} 内容在扫描后已变化；请重新扫描')

            _assert_scanned_quartet_unchanged(slab, clean_expected)
            for config_path in configs:
                _assert_scanned_quartet_unchanged(
                    config_path,
                    config_expected_map.get(os.path.normcase(config_path)) or {})

            clean_incar_result = self._resolve_lis_member_incar(
                slab, fallback_incar=fallback_incar,
                expected_path=str(clean_expected.get('path') or clean_expected.get('incar_path') or ''),
                expected_sha256=str(clean_expected.get('sha256') or
                                    clean_expected.get('incar_sha256') or ''))
            if clean_incar_result['status'] != 'ready':
                raise ValueError(
                    'clean slab 的 INCAR 不可用：'
                    + '；'.join(clean_incar_result['issues'] or ['同目录缺少 INCAR']))
            member_incar_paths = {slab: clean_incar_result['path']}
            member_incar_records = {slab: clean_incar_result}
            for config_path in configs:
                expected = source_incar_evidence.get(config_path) or {}
                supplied = config_expected_map.get(os.path.normcase(config_path)) or {}
                if isinstance(supplied, str):
                    supplied = {'path': supplied}
                expected_path = str(supplied.get('incar_path') or supplied.get('path')
                                    or expected.get('path') or '')
                expected_sha = str(supplied.get('incar_sha256') or supplied.get('sha256')
                                   or expected.get('sha256') or '')
                resolved = self._resolve_lis_member_incar(
                    config_path, fallback_incar=fallback_incar,
                    expected_path=expected_path, expected_sha256=expected_sha)
                if resolved['status'] != 'ready':
                    label = os.path.basename(os.path.dirname(config_path)) or os.path.basename(config_path)
                    raise ValueError(
                        f'构型 {label} 的 INCAR 不可用：'
                        + '；'.join(resolved['issues'] or ['同目录缺少 INCAR']))
                member_incar_paths[config_path] = resolved['path']
                member_incar_records[config_path] = resolved

            for structure_path, bundle in member_input_bundles.items():
                if bundle.get('mode') != 'copy':
                    continue
                quartet_incar = str(
                    ((bundle.get('files') or {}).get('INCAR') or {}).get('path') or '')
                if (not quartet_incar
                        or os.path.normcase(os.path.realpath(quartet_incar))
                        != os.path.normcase(os.path.realpath(
                            member_incar_paths[structure_path]))):
                    raise ValueError(
                        f'{structure_path} 的 INCAR 与同目录四件套绑定不一致；请重新扫描')

            # Capture the source structures before parsing/method planning.  These
            # digests, together with the INCAR digests captured by the resolver,
            # are passed into create_project and checked around every copy.
            member_poscar_hashes = {
                path: _sha256_file(path) for path in [slab, *configs]
            }
            member_source_evidence = {
                path: {
                    'poscar': {'path': path, 'sha256': member_poscar_hashes[path]},
                    'incar': {'path': member_incar_paths[path],
                              'sha256': member_incar_records[path]['sha256']},
                }
                for path in [slab, *configs]
            }
            for path, bundle in member_input_bundles.items():
                if bundle.get('mode') != 'copy':
                    continue
                for filename, record in (bundle.get('files') or {}).items():
                    member_source_evidence[path][filename.lower()] = {
                        'path': str(record.get('path') or ''),
                        'sha256': str(record.get('sha256') or ''),
                    }

            slab_composition, slab_cell = _poscar_composition_cell(slab)
            structure_compositions = {slab: slab_composition}
            for config_path in configs:
                config_composition, config_cell = _poscar_composition_cell(config_path)
                structure_compositions[config_path] = config_composition
                if any(abs(float(config_cell[i][j]) - float(slab_cell[i][j])) > 1e-6
                       for i in range(3) for j in range(3)):
                    raise ValueError(
                        f'构型 {os.path.basename(config_path)} 与 clean slab 晶格不一致；'
                        '不能将不同周期模型的总能直接相减')
                delta_composition = {}
                for element in set(slab_composition) | set(config_composition):
                    difference = config_composition.get(element, 0) - slab_composition.get(element, 0)
                    if difference < 0:
                        raise ValueError(
                            f'构型 {os.path.basename(config_path)} 比 clean slab 少 {element} 原子，'
                            '无法识别为“slab + 吸附物”')
                    if difference:
                        delta_composition[element] = difference
                declared = _formula_composition(source_species[config_path])
                if declared != delta_composition:
                    actual = ''.join(element + (str(count) if count != 1 else '')
                                     for element, count in sorted(delta_composition.items())) or '无'
                    raise ValueError(
                        f'构型 {os.path.basename(config_path)} 选择了 '
                        f'{source_species[config_path]}，但 config−slab 实际组成为 {actual}；'
                        '请更正物种或结构')
                source_species_evidence[config_path] = {
                    'status': 'exact', 'source': 'poscar_minus_clean_slab',
                    'species': source_species[config_path],
                    'adsorbate_composition': delta_composition,
                    'clean_source': slab,
                }

            lib_root = ''
            try:
                lib_root = self._config.load_config().get('potcar_lib_root', '') or ''
            except Exception:                             # noqa: BLE001
                pass
            all_elements = sorted({
                element for composition in structure_compositions.values()
                for element in composition
            })
            from vcstudio.generate.incar_builder import parse_incar
            parsed_incars = {}
            explicit_encuts = {}
            for structure_path, member_incar in member_incar_paths.items():
                with open(member_incar, 'r', encoding='utf-8', errors='replace') as handle:
                    parsed = {str(key).upper(): value
                              for key, value in parse_incar(handle.read()).items()}
                parsed_incars[structure_path] = parsed
                if 'ENCUT' in parsed:
                    value = _method_number(parsed.get('ENCUT'))
                    if value is None or value <= 0:
                        raise ValueError(
                            f'{member_incar} 的 ENCUT={parsed.get("ENCUT")!r} 无效')
                    explicit_encuts[structure_path] = value

            distinct_encuts = sorted({round(value, 10) for value in explicit_encuts.values()})
            encut_issues = []
            if len(distinct_encuts) > 1:
                encut_issues.append(
                    '周期成员显式 ENCUT 不一致：' + '；'.join(
                        f'{os.path.basename(os.path.dirname(path)) or os.path.basename(path)}='
                        f'{value:g} eV' for path, value in explicit_encuts.items()))
            group_encut = distinct_encuts[0] if len(distinct_encuts) == 1 else None
            auto_encut = None
            if not explicit_encuts and lib_root:
                try:
                    from vcstudio.generate import potcar as _potcar
                    max_enmax = _potcar.max_enmax(all_elements, lib_root)
                    auto_encut = int(math.ceil(1.3 * max_enmax / 50.0) * 50)
                except Exception:                         # noqa: BLE001 精确错误由生成阶段返回
                    auto_encut = None

            plans = {}
            # 跨目录差异属于“最终能量可比性”证据，不属于“作业能否运行”硬门。
            # 本目录 INCAR/POSCAR/KPOINTS/POTCAR 的合法性在前面的成员输入门处理。
            reference_issues, reference_warnings, method_advisories = [], [], []
            reference_warnings.extend(encut_issues)
            checked_fields = 0
            for structure_path in [slab, *configs]:
                elements = list(structure_compositions[structure_path])
                planned_titels = []
                bundle = member_input_bundles.get(structure_path) or {}
                local_potcar = ((bundle.get('files') or {}).get('POTCAR') or {}).get('path')
                local_kpoints = ((bundle.get('files') or {}).get('KPOINTS') or {}).get('path')
                local_default_encut = None
                try:
                    from vcstudio.generate.kpoints import recommend_kpoints
                    planned_kpoints = {
                        'scheme': 'Gamma',
                        'grid': recommend_kpoints(slab_cell, 'slab'),
                        'shift': [0.0, 0.0, 0.0],
                    }
                except Exception:                         # noqa: BLE001 仅降级方法证据
                    planned_kpoints = None
                if bundle.get('mode') == 'copy' and local_potcar:
                    try:
                        from vcstudio.generate import methods_text as _methods_text
                        with open(local_potcar, 'r', encoding='utf-8',
                                  errors='replace') as handle:
                            potcar_text = handle.read()
                            planned_titels = [
                                row.get('titel') or ''
                                for row in _methods_text.parse_potcar_titels(potcar_text)]
                        enmax_values = [float(value) for value in re.findall(
                            r'\bENMAX\s*=\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)',
                            potcar_text, re.I)]
                        if enmax_values and all(math.isfinite(value)
                                                for value in enmax_values):
                            # VASP's effective default when ENCUT is omitted.
                            local_default_encut = max(enmax_values)
                        if local_kpoints:
                            with open(local_kpoints, 'r', encoding='utf-8',
                                      errors='replace') as handle:
                                planned_kpoints = _methods_text.parse_kpoints_scheme(
                                    handle.read())
                    except Exception:                     # noqa: BLE001 四件套硬门已给精确错误
                        planned_titels, local_default_encut, planned_kpoints = [], None, None
                elif lib_root:
                    try:
                        from vcstudio.generate import potcar as _potcar
                        planned_titels = [row.get('titel') or '' for row in
                                          _potcar.potcar_provenance(elements, lib_root)]
                    except Exception:                     # noqa: BLE001 方法检查降级为证据缺项
                        planned_titels = []
                if structure_path in explicit_encuts:
                    effective_encut = explicit_encuts[structure_path]
                    encut_source = 'member_incar'
                elif bundle.get('mode') == 'copy':
                    effective_encut = local_default_encut
                    encut_source = 'source_quartet_vasp_default_max_enmax'
                elif group_encut is not None:
                    effective_encut = group_encut
                    encut_source = 'project_member_incar'
                elif auto_encut is not None:
                    effective_encut = auto_encut
                    encut_source = 'potcar_enmax_1.3_round_up_50'
                else:
                    effective_encut = None
                    encut_source = ('unavailable:no_potcar_lib_root' if not lib_root
                                    else 'unavailable:potcar_enmax')
                signatures = ({source_species[structure_path]:
                               ref_signatures[source_species[structure_path]]}
                              if structure_path in configs else {})
                effective_ispin = None
                if (bundle.get('mode') != 'copy'
                        and 'ISPIN' not in parsed_incars[structure_path]
                        and 'MAGMOM' not in parsed_incars[structure_path]):
                    try:
                        from vcstudio.generate.incar_builder import has_magnetic
                        if has_magnetic(elements):
                            effective_ispin = 2
                    except Exception:                     # noqa: BLE001 仅影响预览证据
                        effective_ispin = None
                check = self._reference_method_check(
                    signatures, member_incar_paths[structure_path],
                    planned_potcar=planned_titels,
                    effective_encut=effective_encut, encut_source=encut_source,
                    planned_element_orders=[elements],
                    effective_ispin=effective_ispin,
                    planned_kpoints=planned_kpoints)
                check['planned']['element_orders'] = [elements]
                plans[structure_path] = check['planned']
                if structure_path in configs:
                    label = os.path.basename(os.path.dirname(structure_path)) \
                        if os.path.basename(structure_path).casefold() in ('poscar', 'contcar') \
                        else os.path.basename(structure_path)
                    reference_issues.extend(f'{label}: {item}' for item in check['issues'])
                    reference_warnings.extend(f'{label}: {item}' for item in check['warnings'])
                    method_advisories.extend(
                        f'{label}: {item}' for item in check.get('advisories') or [])
                    checked_fields += int(check.get('checked_fields') or 0)

            periodic = self._periodic_method_check(
                plans[slab], {
                    (os.path.basename(os.path.dirname(path))
                     if os.path.basename(path).casefold() in ('poscar', 'contcar')
                     else os.path.basename(path)): plans[path]
                    for path in configs
                })
            reference_issues.extend(periodic['issues'])
            reference_warnings.extend(periodic['warnings'])
            method_advisories.extend(periodic.get('advisories') or [])
            checked_fields += periodic['checked_fields']
            planned_members = {
                ('clean_slab' if path == slab else
                 (os.path.basename(os.path.dirname(path))
                  if os.path.basename(path).casefold() in ('poscar', 'contcar')
                  else os.path.basename(path))): plan
                for path, plan in plans.items()
            }
            method_check = {
                # analysis_blocked 只表示这些能量当前不能直接进入最终 ΔE/报告；
                # 作业本身仍可按各目录四件套生成并提交。
                'status': ('analysis_blocked' if reference_issues else
                           ('review' if reference_warnings else
                            ('advisory' if method_advisories else 'verified'))),
                'execution_status': 'ready',
                'comparability_status': (
                    'incompatible' if reference_issues else
                    ('unverified' if reference_warnings else 'verified')),
                'issues': reference_issues, 'warnings': reference_warnings,
                'advisories': list(dict.fromkeys(method_advisories)),
                'notes': list(dict.fromkeys(method_advisories)),
                'submission_allowed': True,
                'analysis_ready': not reference_issues and not reference_warnings,
                'checked_fields': checked_fields,
                # 兼容旧消费者：代表性 planned 取首个 adsorption；完整矩阵另存。
                'planned': plans[configs[0]], 'planned_members': planned_members,
                'reference_signatures': ref_signatures,
            }
            repair_plan = self._lis_repair_plan(
                [slab, *configs], plans, parsed_incars,
                structure_compositions, member_incar_records,
                member_source_evidence)
            if repair_plan:
                repair_plan['manual_review'] = list(dict.fromkeys([
                    *reference_issues, *reference_warnings,
                ]))
                method_check['repair_plan'] = repair_plan
                repair_notes = [
                    f'{action["member"]}: {action["key"]} 可在受管副本中智能修复；'
                    '源目录不会修改'
                    for action in repair_plan['actions']
                ]
                repair_notes.extend(
                    f'{item["member"]}: {item["key"]} 仅提供候选建议；不会自动修改'
                    for item in repair_plan.get('suggestions') or [])
                method_check['advisories'] = list(dict.fromkeys([
                    *method_check.get('advisories', []), *repair_notes,
                ]))
                method_check['notes'] = list(method_check['advisories'])
                if method_check['status'] == 'verified':
                    method_check['status'] = 'advisory'
            decision = dict(repair_request or {}) \
                if isinstance(repair_request, dict) else {}
            member_incar_patches = {}
            if repair_plan and repair_plan.get('actions'):
                mode = str(decision.get('mode') or '').strip().lower()
                supplied_plan = str(decision.get('plan_id') or '').strip().lower()
                if mode and supplied_plan != repair_plan['plan_id']:
                    return {**empty, 'reference_species': sorted(refs),
                            'method_check': method_check, 'repair_plan': repair_plan,
                            'error': '智能修复预览已过期；输入文件可能变化，请重新预览'}
                if mode not in {'keep', 'apply'}:
                    return {**empty, 'reference_species': sorted(refs),
                            'method_check': method_check, 'repair_plan': repair_plan,
                            'needs_repair_decision': True, 'error': None}
                if mode == 'apply':
                    for action in repair_plan['actions']:
                        member_incar_patches.setdefault(
                            action['structure_path'], {})[action['key']] = action['new']
                        if action['key'] == 'ENCUT':
                            plans[action['structure_path']]['encut'] = float(action['new'])
                            plans[action['structure_path']]['encut_source'] = \
                                'managed_copy_low_risk_repair'

                    # ENCUT is the only auto-repairable field.  Re-evaluate its
                    # comparability after the previewed patches so the stored
                    # plan and UI describe the bytes that will actually run.
                    reference_issues[:] = [
                        item for item in reference_issues if 'ENCUT' not in item]
                    reference_warnings[:] = [
                        item for item in reference_warnings if 'ENCUT' not in item]
                    for path in configs:
                        species = source_species[path]
                        label = (os.path.basename(os.path.dirname(path))
                                 if os.path.basename(path).casefold()
                                 in ('poscar', 'contcar') else os.path.basename(path))
                        reference_encut = _method_number(
                            (ref_signatures.get(species) or {}).get('encut'))
                        planned_encut = _method_number(plans[path].get('encut'))
                        if reference_encut is None or planned_encut is None:
                            reference_warnings.append(
                                f'{label}: {species}: ENCUT 证据不完整')
                        elif abs(reference_encut - planned_encut) > 1e-8:
                            reference_issues.append(
                                f'{label}: {species}: ENCUT 参考={reference_encut} eV，'
                                f'新任务={planned_encut} eV')
                    clean_encut = _method_number(plans[slab].get('encut'))
                    for path in configs:
                        member_encut = _method_number(plans[path].get('encut'))
                        label = (os.path.basename(os.path.dirname(path))
                                 if os.path.basename(path).casefold()
                                 in ('poscar', 'contcar') else os.path.basename(path))
                        if clean_encut is None or member_encut is None:
                            reference_warnings.append(
                                f'clean slab ↔ {label}: ENCUT 证据不完整')
                        elif abs(clean_encut - member_encut) > 1e-8:
                            reference_issues.append(
                                f'clean slab ↔ {label}: ENCUT 不一致'
                                f'（{clean_encut!r} vs {member_encut!r}）')
                    method_check['issues'] = list(dict.fromkeys(reference_issues))
                    method_check['warnings'] = list(dict.fromkeys(reference_warnings))
                    method_check['comparability_status'] = (
                        'incompatible' if method_check['issues'] else
                        ('unverified' if method_check['warnings'] else 'verified'))
                    method_check['analysis_ready'] = (
                        not method_check['issues'] and not method_check['warnings'])
                    method_check['status'] = (
                        'analysis_blocked' if method_check['issues'] else
                        ('review' if method_check['warnings'] else 'advisory'))
                    method_check['repairs'] = [
                        {**action, 'applied': True, 'source_unchanged': True}
                        for action in repair_plan['actions']]
                method_check['repair_decision'] = {
                    'mode': mode, 'plan_id': repair_plan['plan_id'],
                    'source_unchanged': True,
                }
            confirmation = dict(method_confirmation or {}) \
                if isinstance(method_confirmation, dict) else {}
            if confirmation.get('confirmed') and str(confirmation.get('reason') or '').strip():
                method_check['confirmation'] = {
                    'confirmed': True,
                    'reason': str(confirmation.get('reason') or '').strip(),
                    'confirmed_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
                }

            molecules_dir = str(reference.get('molecules_dir') or '').strip()
            if molecules_dir and not os.path.isabs(molecules_dir):
                molecules_dir = os.path.join(
                    str(reference.get('root') or os.path.dirname(reference_project_path)),
                    molecules_dir)
            if not molecules_dir:
                parents = [os.path.dirname(path) for path in ref_jobs.values()]
                molecules_dir = os.path.commonpath(parents) if parents else reference.get('root')
            reference_path = os.path.abspath(str(reference_project_path))
            if os.path.isdir(reference_path):
                reference_path = os.path.join(reference_path, 'project.yaml')
            member_inputs = [{
                'role': 'clean_slab',
                'poscar': {'path': slab, 'sha256': member_poscar_hashes[slab]},
                'incar': {'path': member_incar_paths[slab],
                          'sha256': member_incar_records[slab]['sha256'],
                          'source': member_incar_records[slab]['source']},
                'input_bundle': member_input_bundles.get(slab),
            }]
            member_inputs.extend({
                'role': 'config', 'species': source_species[path],
                'poscar': {'path': path, 'sha256': member_poscar_hashes[path]},
                'incar': {'path': member_incar_paths[path],
                          'sha256': member_incar_records[path]['sha256'],
                          'source': member_incar_records[path]['source']},
                'input_bundle': member_input_bundles.get(path),
                'species_evidence': source_species_evidence[path],
            } for path in configs)
            request_inputs = {
                'name': project_name,
                'slab': {'path': slab, 'sha256': member_poscar_hashes[slab]},
                # 代表性旧字段保留；真实逐成员绑定以 members 为准。
                'incar': {'path': member_incar_paths[slab],
                          'sha256': member_incar_records[slab]['sha256']},
                'configs': [
                    {'path': path, 'sha256': member_poscar_hashes[path],
                     'incar_path': member_incar_paths[path],
                     'incar_sha256': member_incar_records[path]['sha256'],
                     'input_bundle': member_input_bundles.get(path),
                     'species': source_species[path],
                     'species_evidence': source_species_evidence[path]}
                    for path in configs
                ],
                'members': member_inputs,
                'reference_project': reference_path,
                'species_refs': refs,
                'species_ref_jobs': ref_jobs,
                'planned_method': method_check['planned'],
                'planned_methods': method_check['planned_members'],
                'repair_decision': method_check.get('repair_decision'),
            }
            request_sha256 = hashlib.sha256(json.dumps(
                request_inputs, ensure_ascii=False, sort_keys=True,
                separators=(',', ':')).encode('utf-8')).hexdigest()
            preparation = {
                'schema': 2, 'request_sha256': request_sha256,
                'inputs': request_inputs,
                'prepared_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
                'method_check': method_check,
            }
            if os.path.exists(target):
                existing = self._load_project_for_path(target)
                old_hash = ((existing or {}).get('preparation') or {}).get('request_sha256')
                if existing is not None and old_hash == request_sha256:
                    members = existing.get('members') or {}
                    job_dirs = [path for path in (
                        [members.get('clean_slab'), members.get('gas_ref')]
                        + list(members.get('configs') or [])) if path]
                    return {'ok': True, 'project_path': os.path.join(target, 'project.yaml'),
                            'job_dirs': job_dirs, 'reference_species': sorted(refs),
                            'advisories': [], 'warnings': ['输入指纹一致，已复用现有项目'],
                            'reused': True, 'preparation': existing.get('preparation'),
                            'method_check': method_check,
                            'needs_method_confirmation': False,
                            'error': None}
                raise FileExistsError(
                    f'目标项目已存在且输入指纹不同，不会覆盖：{target}；'
                    '请更换项目名')
            result = self._adsorption.create_project(
                target, project_name, clean_poscar=slab,
                config_poscars=configs,
                incar_path=(fallback_incar or member_incar_paths[slab]),
                member_incars=member_incar_paths,
                member_source_evidence=member_source_evidence,
                member_input_bundles=member_input_bundles,
                lib_root=(lib_root or None),
                config_species=source_species, species_refs=refs,
                config_species_evidence=source_species_evidence,
                species_ref_jobs=ref_jobs, molecules_dir=molecules_dir,
                reference_project=reference_path, preparation=preparation,
                fail_if_exists=True,
                member_incar_patches=member_incar_patches)
            warnings = []
            for member, _job_dir, item_warnings in (result.get('generated') or []):
                warnings.extend(f'{member}:{warning}' for warning in (item_warnings or []))
            warnings.extend(f'{member}:{message}'
                            for member, message in (result.get('errors') or []))
            advisories = [f'[{priority}·{advisor}] {message}'
                          for priority, advisor, message in (result.get('advisories') or [])]
            job_dirs = [job_dir for _member, job_dir, _warnings
                        in (result.get('generated') or [])]
            build_errors = list(result.get('errors') or [])
            complete = (bool(result.get('ok')) and not build_errors
                        and len(job_dirs) == len(configs) + 1)
            error = None if complete else '整组未完整生成；不会自动提交，请按 warnings 修正后重建项目'
            return {'ok': complete, 'project_path': result.get('project_path'),
                    'job_dirs': job_dirs, 'reference_species': sorted(refs),
                    'reused': False, 'preparation': preparation,
                    'method_check': method_check,
                    'repair_plan': repair_plan,
                    'needs_method_confirmation': False,
                    'advisories': advisories, 'warnings': warnings, 'error': error}
        except Exception as e:                            # noqa: BLE001
            return {**empty, 'error': str(e)}

    def submit_project_with_resources(self, project_id, profile_name, cores,
                                      walltime, password=None, trust_new=False):
        """Submit one registered project selected by opaque identity."""
        try:
            record = self._resolve_project_id(project_id)
        except Exception:                                 # noqa: BLE001 public identity seam
            return self._project_identity_failure(
                results=[], submitted=[], skipped=[], resources={},
                needs_trust=False)
        result = self._call_with_project_bindings(
            [record],
            lambda: self._submit_project_with_resources_for_path(
                record['path'], profile_name, cores, walltime,
                password=password, trust_new=trust_new),
            failure={
                'results': [], 'submitted': [], 'skipped': [],
                'resources': {}, 'needs_trust': False,
            })
        return self._project_public_result(result)

    def _submit_project_with_resources_for_path(
            self, project_path, profile_name, cores,
            walltime, password=None, trust_new=False):
        """按本次选择的核数/墙时提交项目，不改集群 profile 的持久配置。"""
        base = {'ok': False, 'results': [], 'submitted': [], 'skipped': [],
                'resources': {}, 'needs_trust': False}
        try:
            project = self._load_project_for_path(str(project_path or '').strip())
            if project is None:
                raise ValueError('项目不存在或 project.yaml 无法读取')
            try:
                if isinstance(cores, bool):
                    raise ValueError
                ncores = int(cores)
            except (TypeError, ValueError) as e:
                raise ValueError('核数必须是正整数') from e
            if ncores <= 0:
                raise ValueError('核数必须是正整数')
            wall = str(walltime or '').strip()
            if not re.fullmatch(r'\d{1,4}:[0-5]\d:[0-5]\d', wall):
                raise ValueError('墙时格式应为 HH:MM:SS，例如 48:00:00')
            hours, minutes, seconds = (int(value) for value in wall.split(':'))
            if hours * 3600 + minutes * 60 + seconds <= 0:
                raise ValueError('墙时必须大于 00:00:00')
            prof, pw, resolve_error = self._resolve(str(profile_name or '').strip(), password)
            if resolve_error:
                return {**base, 'error': resolve_error.get('error')}
            launch_profile = copy.copy(prof)
            launch_profile.nodes = 1
            launch_profile.ppn = ncores
            launch_profile.walltime = wall
            resources = {'profile': prof.name, 'nodes': 1, 'cores': ncores,
                         'ppn': ncores, 'walltime': wall}
            if hasattr(self._sub(), 'profile_binding'):
                resources['profile_binding'] = self._sub().profile_binding(prof)
            mode = str(getattr(prof, 'script_mode', 'auto') or 'auto')
            raw_commands = getattr(prof, 'engine_commands', {}) or {}
            commands = ({str(key).strip().lower(): str(value or '').strip()
                         for key, value in raw_commands.items()}
                        if isinstance(raw_commands, dict) else {})
            mapped_vasp = str(commands.get('vasp') or '').strip()
            command = mapped_vasp or str(getattr(prof, 'vasp_cmd', '') or '')
            if mode == 'auto':
                rendered_command = command.replace('{cores}', str(ncores)).replace(
                    '{ppn}', str(ncores))
                literal_counts = _parallel_counts(rendered_command)
                if literal_counts and any(value != ncores for value in literal_counts):
                    raise ValueError(
                        f'服务器 vasp_cmd 中的 MPI/任务核数 {literal_counts} 与本次选择 '
                        f'{ncores} 核不一致；请改为 {{cores}} 占位符或删除冲突硬编码')
                if mapped_vasp:
                    # copy.copy 不会复制 dict；新建一份，避免“本次核数”
                    # 反向修改已保存的 profile。
                    launch_profile.engine_commands = {
                        **commands, 'vasp': rendered_command}
                else:
                    launch_profile.vasp_cmd = rendered_command
            elif mode == 'template':
                template_path = str(getattr(prof, 'template_path', '') or '')
                if not template_path or not os.path.isfile(template_path):
                    raise ValueError('模板模式但模板文件不存在；请重新选择提交脚本')
                with open(template_path, 'r', encoding='utf-8', errors='replace') as handle:
                    template_text = handle.read()
                if not _CORE_PLACEHOLDER_RE.search(template_text):
                    raise ValueError(
                        '提交模板必须在有效的 MPI/调度器核数参数中使用 '
                        '{cores} 或 {ppn} 占位符')
                if not _WALLTIME_PLACEHOLDER_RE.search(template_text):
                    raise ValueError(
                        '提交模板必须在有效的调度器墙时参数中使用 {walltime} 占位符')
                rendered_template = (template_text
                                     .replace('{cores}', str(ncores))
                                     .replace('{ppn}', str(ncores))
                                     .replace('{nodes}', '1')
                                     .replace('{walltime}', wall))
                template_counts = _parallel_counts(rendered_template)
                if not template_counts or any(value != ncores for value in template_counts):
                    raise ValueError(
                        f'提交模板渲染后的 MPI/任务核数 {template_counts} 与本次选择 '
                        f'{ncores} 核冲突；请删除模板中的硬编码资源')
                node_counts = [int(match.group(1))
                               for match in _NODE_COUNT_RE.finditer(rendered_template)]
                if node_counts and any(value != 1 for value in node_counts):
                    raise ValueError(
                        f'提交模板硬编码节点数 {node_counts}，与一站式提交的 1 节点冲突')
                template_walltimes = _walltimes(rendered_template)
                if (not template_walltimes
                        or any(value != wall for value in template_walltimes)):
                    raise ValueError(
                        f'提交模板渲染后的墙时 {template_walltimes} 与本次选择 {wall} 冲突；'
                        '请删除模板中的硬编码墙时')
            else:
                raise ValueError(f'未知提交脚本模式：{mode!r}')

            # 与状态监控/报告共用同一成员枚举：除 clean/gas/config 外，导入项目的
            # species_ref_jobs/molecules 中若仍是 CREATED，也必须能在这一站提交。
            # 已完成的分子参考会在下面按 DONE 安全跳过。
            job_dirs = list(dict.fromkeys(
                str(d) for d in self._project_member_dirs(project) if d))
            previous_launch = dict(project.get('launch') or {})
            previous_resources = dict(previous_launch.get('resources') or {})
            previous_profile = str(previous_resources.get('profile') or '').strip()
            previous_binding = previous_resources.get('profile_binding') or {}
            current_binding = resources.get('profile_binding') or {}
            previous_submitted = list(previous_launch.get('submitted_job_dirs') or [])
            if previous_submitted and previous_profile and previous_profile != prof.name:
                raise ValueError(
                    f'该项目已有 {len(previous_submitted)} 个作业由服务器'
                    f'「{previous_profile}」自动托管，不能改用「{prof.name}」覆盖项目'
                    '级服务器绑定。请继续使用原服务器，或新建项目。')
            if (previous_submitted and previous_binding
                    and previous_binding.get('fingerprint')
                    != current_binding.get('fingerprint')):
                raise ValueError(
                    f'服务器「{prof.name}」的连接端点已与项目首次提交时不同；'
                    '为防止同名配置接管旧任务，已阻止本次提交。请恢复原配置或新建项目。')

            eligible, skipped, reconciled = [], [], []
            for job_dir in job_dirs:
                manifest = self._manifest.load_manifest(job_dir)
                state = (manifest or {}).get('state')
                job_id = (manifest or {}).get('scheduler_job_id')
                if manifest is None:
                    skipped.append({'dir': job_dir, 'reason': '缺少可读 job.yaml'})
                elif state == 'DONE':
                    # 已收敛分子参考是只读依赖，可跨服务器复用；它既不应进入本项目
                    # 的提交白名单，也不应因保留了原服务器 job_id 而被误判为串服。
                    skipped.append({'dir': job_dir, 'reason': '当前状态 DONE 不重复提交'})
                elif job_id:
                    bound_profile = str(manifest.get('cluster') or '').strip()
                    if bound_profile and bound_profile != prof.name:
                        raise ValueError(
                            f'项目成员 {self._base(job_dir)} 已绑定服务器'
                            f'「{bound_profile}」(作业号 {job_id})，不能改投'
                            f'「{prof.name}」。同一项目当前只允许一台服务器'
                            '自动托管。')
                    if bound_profile == prof.name:
                        if hasattr(self._sub(), 'assert_profile_binding'):
                            self._sub().assert_profile_binding(
                                prof, job_dir, '项目继续托管', manifest=manifest)
                        reconciled.append(job_dir)
                    skipped.append({'dir': job_dir, 'reason': f'已有作业号 {job_id}'})
                elif state in _ACTIVE_STATES:
                    skipped.append({'dir': job_dir, 'reason': f'当前状态 {state} 不重复提交'})
                else:
                    eligible.append(job_dir)

            managed_before = list(previous_submitted)
            managed_before.extend(d for d in reconciled if d not in managed_before)
            repair_needed = bool(
                managed_before
                and (not project.get('autopilot_managed')
                     or previous_profile != prof.name
                     or any(d not in previous_submitted for d in reconciled)))

            def _persist_management(new_submitted):
                """Persist or repair the explicit project automation allow-list."""
                persistence_errors = []
                all_submitted = list(managed_before)
                all_submitted.extend(d for d in new_submitted if d not in all_submitted)
                project['launch'] = {
                    **previous_launch,
                    'resources': resources,
                    'submitted_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
                    'submitted_job_dirs': all_submitted,
                }
                project['autopilot_managed'] = True
                try:
                    root = project.get('root') or os.path.dirname(str(project_path))
                    self._adsorption.save_project(root, project)
                except Exception as e:                    # noqa: BLE001 远程提交不能回滚
                    persistence_errors.append(f'提交成功但 launch 资源写回失败：{e}')
                try:
                    self._config.set_ui_state(
                        autopilot=True, autopilot_continue=True,
                        autopilot_fetch=True, autopilot_report=True)
                    if self._pipeline_supervisor is not None:
                        self._pipeline_supervisor.reconfigure()
                        self._pipeline_supervisor.wake()
                except Exception as e:                    # noqa: BLE001 自动化开关可重试
                    persistence_errors.append(f'任务已提交，但自动托管设置保存失败：{e}')
                if getattr(prof, 'auth', 'key') == 'password' and pw:
                    try:
                        self._save_password_verified(prof.name, pw)
                    except Exception as e:                # noqa: BLE001 无人值守凭据失败
                        persistence_errors.append(
                            f'提交成功但密码未能保存到系统凭据库：{e}')
                return persistence_errors

            if not eligible:
                unsafe = [row for row in skipped
                          if not ('已有作业号' in row['reason']
                                  or '当前状态 DONE' in row['reason']
                                  or any(f'当前状态 {state}' in row['reason']
                                         for state in _ACTIVE_STATES))]
                persistence_errors = (_persist_management([])
                                      if repair_needed and not unsafe else [])
                return {**base, 'ok': not unsafe and not persistence_errors,
                        'skipped': skipped, 'resources': resources,
                        'error': ('；'.join(persistence_errors) if persistence_errors else
                                  ('没有可提交作业；' + '；'.join(
                                      f'{self._base(row["dir"])}:{row["reason"]}'
                                      for row in unsafe) if unsafe else None))}

            response = self._bo().submit_batch(
                launch_profile, pw, eligible, trust_new)
            if response.get('needs_trust'):
                return {**base, 'skipped': skipped, 'resources': resources,
                        'needs_trust': True,
                        **self._host_key_evidence(response),
                        'error': response.get('message') or '需要确认服务器指纹'}
            results, submitted = [], []
            for row in (response.get('results') or []):
                job_dir, success, message = row
                item = {'dir': job_dir, 'ok': bool(success), 'message': str(message)}
                results.append(item)
                if success:
                    submitted.append(job_dir)
            persistence_errors = []
            if submitted or repair_needed:
                persistence_errors = _persist_management(submitted)
            failed = [row for row in results if not row['ok']]
            if persistence_errors:
                error = '；'.join(persistence_errors)
            elif failed:
                error = f'{len(failed)} 个作业提交失败；已成功的不会重复提交，可安全重试'
            else:
                error = None
            return {**base, 'ok': not failed and not persistence_errors,
                    'results': results, 'submitted': submitted, 'skipped': skipped,
                    'resources': resources, 'needs_trust': False, 'error': error}
        except Exception as e:                            # noqa: BLE001
            return {**base, 'error': str(e)}

    def proj_import_scan(self, source_root):
        """递归预检本地已算 VASP 结果（纯只读，不落盘）。

        返回每个计算目录的能量来源、多证据收敛判定、建议角色和可执行下一步。
        不要在扫描阶段写 job.yaml，用户可先修正角色/任务类型再提交。
        """
        try:
            root = str(source_root or '').strip()
            if not root:
                return {'ok': False, 'source_root': '', 'candidates': [],
                        'summary': {}, 'suggested_name': '', 'error': '请先选择结果根目录'}
            result = dict(self._ri().scan_folder(root) or {})
            result.setdefault('ok', True)
            result.setdefault('source_root', root)
            result.setdefault('candidates', [])
            result.setdefault('summary', {})
            result.setdefault('suggested_name', os.path.basename(os.path.normpath(root)))
            result['error'] = None
            return result
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'source_root': str(source_root or ''),
                    'candidates': [], 'summary': {}, 'suggested_name': '', 'error': str(e)}

    def proj_import_commit(self, source_root, out_root, project_name, selections):
        """把预检后选中的结果复制到受管项目，生成 manifest 并登记台账。

        后端会重新扫描并校验 ``path/role/task_type/manual_confirm``，不信任
        前端缓存的 DONE 结论；源目录始终只读。
        """
        try:
            source = str(source_root or '').strip()
            target = str(out_root or '').strip()
            name = str(project_name or '').strip()
            if not source:
                raise ValueError('未选择结果根目录')
            if not target:
                raise ValueError('未选择导入后的项目保存位置')
            if not name:
                raise ValueError('请填写项目名')
            items = [dict(x) for x in (selections or []) if isinstance(x, dict)]
            if not any(x.get('selected', True) and x.get('role') != 'ignore' for x in items):
                raise ValueError('至少选择一个可导入的结果')
            result = dict(self._ri().commit_import(
                source, target, name, items,
                adsorption_mod=self._adsorption, ledger_mod=self._ledger,
                manifest_mod=self._manifest) or {})
            if result.get('ok') and isinstance(result.get('project'), dict):
                result['project']['work_mode'] = 'lis'
                project_root = result['project'].get('root') or os.path.join(target, name)
                self._adsorption.save_project(project_root, result['project'])
                # 已算结果导入后不再要求用户额外点一次“生成报告”。只有全部成员
                # 已完成且本地证据可读时才立即出报告；最终门禁未过则出诊断版。
                try:
                    states = self._member_states(result['project'])
                    if states and self._project_all_done(states):
                        project_path = (result.get('project_path')
                                        or os.path.join(project_root, 'project.yaml'))
                        summary = self._adsorption.delta_e_rows(result['project'])
                        eligible, reason = self._final_report_gate(
                            result['project'], summary)
                        report_dir = os.path.join(project_root, 'report')
                        report_formats = self._available_report_formats()
                        generated = self._proj_report_bundle_for_path(
                            project_path, report_dir,
                            report_formats, final=True,
                            stem=f'{name}_吸附能评估报告')
                        result['auto_report'] = generated
                        actual_kind = str(
                            generated.get('scientific_status')
                            or generated.get('kind') or '').strip().lower()
                        actual_reason = str(
                            generated.get('gate_reason') or reason or '')
                        if actual_kind != 'final':
                            result['auto_report_reason'] = actual_reason
                            if generated.get('ok'):
                                # Compatibility response key; the canonical
                                # diagnostic record now lives at autopilot_report.
                                result['auto_report_blocked'] = generated.get('marker')
                except Exception as exc:                 # noqa: BLE001 导入成功不因报告降级而回滚
                    result['auto_report'] = self._report_failure_envelope(exc)
            result.setdefault('ok', True)
            result.setdefault('imported', [])
            result.setdefault('summary', {})
            project = result.get('project') if isinstance(
                result.get('project'), dict) else None
            locator = str(result.get('project_path') or '')
            project_id = (self._workspace_project_id(locator, project)
                          if project is not None and locator else None)
            public = {
                'ok': bool(result.get('ok')),
                'project_id': project_id,
                'name': str((project or {}).get('name') or name or '') or None,
                'imported_count': len(result.get('imported') or []),
                'summary': result.get('summary') or {},
                'warnings': list(result.get('warnings') or []),
                'blocking_reasons': list(result.get('blocking_reasons') or []),
                'auto_report': result.get('auto_report'),
                'auto_report_reason': result.get('auto_report_reason'),
                'auto_report_blocked': result.get('auto_report_blocked'),
                'error': None,
            }
            if project_id and project is not None:
                return self._project_locator_projection(public, [{
                    'project_id': project_id,
                    'path': self._canonical_project_path(locator),
                    'project': project,
                }])
            return self._project_public_result(public)
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'project_id': None, 'name': None,
                    'imported_count': 0, 'summary': {},
                    'error': self._workspace_public_text(e)}

    # -- Phase 3 project clone / move / copied-folder adoption -------------
    def _project_lifecycle_backend(self):
        if self._project_lifecycle_service is None:
            from vcstudio.project.project_lifecycle import ProjectLifecycleService
            registry_path = getattr(self._adsorption, 'default_registry_path', None)
            if not callable(registry_path):
                raise RuntimeError('Project registry location is unavailable')
            ledger_path = getattr(self._ledger, 'default_ledger_path', None)
            self._project_lifecycle_service = ProjectLifecycleService(
                registry_path(),
                ledger_path=(ledger_path() if callable(ledger_path) else None))
        return self._project_lifecycle_service

    def _project_lifecycle_prune(self):
        now = time.monotonic()
        for store in (self._project_lifecycle_selections,
                      self._project_lifecycle_plans):
            expired = [key for key, item in store.items()
                       if now - float(item.get('created_at') or 0) > 900]
            for key in expired:
                store.pop(key, None)
            overflow = max(0, len(store) - 64)
            if overflow:
                oldest = sorted(
                    store,
                    key=lambda key: float(store[key].get('created_at') or 0),
                )[:overflow]
                for key in oldest:
                    store.pop(key, None)

    @staticmethod
    def _project_lifecycle_leaf(value):
        name = str(value or '').strip()
        if (not name or name in {'.', '..'} or len(name) > 96
                or name.endswith((' ', '.'))
                or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)):
            raise ValueError('Destination folder name is invalid')
        reserved = {'CON', 'PRN', 'AUX', 'NUL',
                    *(f'COM{i}' for i in range(1, 10)),
                    *(f'LPT{i}' for i in range(1, 10))}
        if name.split('.', 1)[0].upper() in reserved:
            raise ValueError('Destination folder name is reserved by Windows')
        return name

    def _project_lifecycle_registered_source(self, project_id):
        record = self._resolve_project_id(project_id)
        return self._call_with_project_bindings(
            [record], lambda: record['path'])

    def proj_lifecycle_select(self, kind):
        """Create an opaque, short-lived server selection for lifecycle use.

        The browser never receives the selected absolute path.  In particular,
        copied-folder adoption can only inspect a folder returned by the native
        server-side picker; a browser-supplied identity or locator is ignored.
        """
        selection_kind = str(kind or '').strip().lower()
        allowed = {'adopt_source', 'clone_destination', 'move_destination'}
        if selection_kind not in allowed:
            return {'ok': False, 'selection_token': None, 'selection': None,
                    'error': 'Unsupported project lifecycle selection'}
        try:
            picked = self.pick_dir()
            path = str((picked or {}).get('path') or '').strip()
            if not path:
                return {'ok': False, 'cancelled': True, 'selection_token': None,
                        'selection': None, 'error': None}
            canonical = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
            if not os.path.isdir(canonical):
                raise ValueError('The selected folder is unavailable')
            contains_project = os.path.isfile(os.path.join(canonical, 'project.yaml'))
            if selection_kind == 'adopt_source' and not contains_project:
                raise ValueError('The selected folder does not contain project.yaml')
            token = uuid.uuid4().hex
            label = os.path.basename(os.path.normpath(canonical)) or 'Selected folder'
            label = re.sub(r'[\x00-\x1f]+', '', label)[:80] or 'Selected folder'
            with self._project_lifecycle_lock:
                self._project_lifecycle_prune()
                self._project_lifecycle_selections[token] = {
                    'kind': selection_kind, 'path': canonical, 'label': label,
                    'contains_project': contains_project,
                    'created_at': time.monotonic(),
                }
            return {
                'ok': True,
                'cancelled': False,
                'selection_token': token,
                'selection': {'label': label,
                              'contains_project': contains_project},
                'error': None,
            }
        except Exception as e:                            # noqa: BLE001 JSON-safe bridge
            return {'ok': False, 'cancelled': False, 'selection_token': None,
                    'selection': None,
                    'error': self._workspace_public_text(e) or
                             'Project folder selection failed'}

    def proj_lifecycle_preflight(self, action, project_id=None,
                                 selection_token=None, target_name=None,
                                 identity_mode='remint'):
        """Return a path-free dry-run and an opaque one-use operation token."""
        lifecycle_action = str(action or '').strip().lower()
        token = str(selection_token or '').strip().lower()
        if lifecycle_action not in {'clone', 'move', 'adopt'}:
            return {'ok': False, 'ready': False, 'operation_token': None,
                    'conflicts': [], 'warnings': [],
                    'error': 'Unsupported project lifecycle action'}
        if not re.fullmatch(r'[a-f0-9]{32}', token):
            return {'ok': False, 'ready': False, 'operation_token': None,
                    'conflicts': [], 'warnings': [],
                    'error': 'The server folder selection is missing or expired'}
        try:
            with self._project_lifecycle_lock:
                self._project_lifecycle_prune()
                selection = copy.deepcopy(
                    self._project_lifecycle_selections.get(token))
            expected_kind = ('adopt_source' if lifecycle_action == 'adopt'
                             else f'{lifecycle_action}_destination')
            if not selection or selection.get('kind') != expected_kind:
                raise ValueError('The server folder selection is missing or expired')
            backend = self._project_lifecycle_backend()
            if lifecycle_action == 'adopt':
                mode = str(identity_mode or 'remint').strip().lower()
                if mode not in {'remint', 'preserve'}:
                    raise ValueError('Identity mode must be remint or preserve')
                plan = backend.preflight_adopt(selection['path'], mode)
            else:
                record = self._resolve_project_id(project_id)
                leaf = self._project_lifecycle_leaf(target_name)
                destination = os.path.join(selection['path'], leaf)
                plan = self._call_with_project_bindings(
                    [record],
                    lambda: (
                        backend.preflight_clone(record['path'], destination)
                        if lifecycle_action == 'clone'
                        else backend.preflight_move(record['path'], destination)))
            public = plan.public_summary()
            public['selection'] = {'label': selection.get('label') or 'Selected folder'}
            public['operation_token'] = None
            if plan.ready:
                operation_token = uuid.uuid4().hex
                with self._project_lifecycle_lock:
                    self._project_lifecycle_prune()
                    self._project_lifecycle_plans[operation_token] = {
                        'plan': plan, 'created_at': time.monotonic(),
                    }
                public['operation_token'] = operation_token
            return public
        except Exception as e:                            # noqa: BLE001 dry-run never raises to JS
            return {
                'ok': False, 'ready': False, 'action': lifecycle_action,
                'operation_token': None, 'project': None, 'target': None,
                'identity_mode': (str(identity_mode or '').strip().lower() or None),
                'impact': None, 'conflicts': [], 'warnings': [],
                'error': self._workspace_public_text(e) or
                         'Project lifecycle preflight failed',
            }

    def proj_lifecycle_apply(self, operation_token):
        """Consume one server-held plan; no browser path or identity is accepted."""
        token = str(operation_token or '').strip().lower()
        if not re.fullmatch(r'[a-f0-9]{32}', token):
            return {'ok': False, 'action': None, 'project': None,
                    'error_code': 'operation_token_invalid',
                    'requires_manual_recovery': False,
                    'error': 'The lifecycle preflight is missing or expired'}
        with self._project_lifecycle_lock:
            self._project_lifecycle_prune()
            stored = self._project_lifecycle_plans.pop(token, None)
        if not stored:
            return {'ok': False, 'action': None, 'project': None,
                    'error_code': 'operation_token_expired',
                    'requires_manual_recovery': False,
                    'error': 'The lifecycle preflight is missing, expired, or already used'}
        plan = stored['plan']
        try:
            result = self._project_lifecycle_backend().apply(plan)
            project = result.get('project') if isinstance(result, dict) else None
            locator = str((result or {}).get('project_path') or '')
            if not isinstance(project, dict) or not locator:
                raise RuntimeError('Lifecycle service returned an invalid result')
            project_id = self._workspace_project_id(locator, project)
            return {
                'ok': True,
                'action': plan.action,
                'project': {
                    'id': project_id,
                    'project_id': project_id,
                    'name': str(project.get('name') or ''),
                },
                'identity_mode': plan.identity_mode,
                'registry_updated': True,
                'jobs_ledger_updated': True,
                'jobs_updated': int(result.get('jobs_updated') or 0),
                'error_code': None,
                'requires_manual_recovery': False,
                'error': None,
            }
        except Exception as e:                            # noqa: BLE001 one-use failure envelope
            code = str(getattr(e, 'code', '') or 'lifecycle_apply_failed')
            return {
                'ok': False, 'action': getattr(plan, 'action', None),
                'project': None, 'identity_mode': getattr(plan, 'identity_mode', None),
                'registry_updated': False, 'jobs_ledger_updated': False,
                'jobs_updated': 0, 'error_code': code,
                'requires_manual_recovery': code == 'partial_rollback',
                'error': self._workspace_public_text(e) or
                         'Project lifecycle operation failed',
            }

    def proj_list(self):
        """Return the path-free public projection of the private registry."""
        try:
            snapshot = self._project_registry_snapshot()
            ambiguous = len(snapshot['duplicate_ids'])
            unreadable = len(snapshot['failures'])
            error = None
            if ambiguous or unreadable:
                error = (
                    f'{ambiguous} ambiguous and {unreadable} unreadable registered '
                    'project identities were excluded.')
            return {'projects': snapshot['public_rows'], 'error': error}
        except Exception:                                 # noqa: BLE001 public registry seam
            return {'projects': [],
                    'error': 'The registered project list is unavailable.'}

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
                return {'ok': False, 'project_id': None, 'name': None,
                        'advisories': [], 'warnings': [],
                        'error': self._workspace_public_text(';'.join(errs))}
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
            # 报告口径绑定到项目，之后切换界面工作模式不会改变旧项目的科学输出。
            if res.get('ok') and isinstance(res.get('project'), dict):
                res['project']['work_mode'] = 'lis'
                self._adsorption.save_project(os.path.join(root, name), res['project'])
            advisories = [f'[{pri}·{aname}] {msg}'
                          for pri, aname, msg in (res.get('advisories') or [])]
            warnings = []
            for member, _d, wlist in (res.get('generated') or []):
                for w in (wlist or []):
                    warnings.append(f'{member}:{w}')
            for member, msg in (res.get('errors') or []):
                warnings.append(f'{member}:{msg}')
            project = res.get('project') if isinstance(res.get('project'), dict) else None
            locator = str(res.get('project_path') or '')
            project_id = (self._workspace_project_id(locator, project)
                          if project is not None and locator else None)
            return self._project_public_result({
                'ok': bool(res.get('ok')),
                'project_id': project_id,
                'name': str((project or {}).get('name') or name or '') or None,
                'advisories': advisories, 'warnings': warnings, 'error': None,
            })
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'project_id': None, 'name': None,
                    'advisories': [], 'warnings': [],
                    'error': self._workspace_public_text(e)}

    def proj_delta(self, project_id):
        try:
            record = self._resolve_project_id(project_id)
        except Exception:                                 # noqa: BLE001 public identity seam
            return self._project_identity_failure(rows=[], note='')
        result = self._call_with_project_bindings(
            [record], lambda: self._proj_delta_for_path(record['path']),
            failure={'rows': [], 'note': ''})
        return self._project_public_result(result)

    def _proj_delta_for_path(self, path):
        """项目 ΔE 汇总(镜像 project_tab._on_delta)。

        rows 忠实透传 delta_e_rows 的字段(name/state/e_config/delta_e/note);ΔE 门控语义
        原样——任一成员未 DONE 时对应行 delta_e=None 且 note 明说缺谁。note 顶层汇总清洁
        表面/气相参考状态(镜像 _on_delta 头部两行)。
        """
        try:
            proj = self._load_project_for_path((path or '').strip())
            if proj is None:
                return {'ok': False, 'rows': [], 'note': '',
                        'error': '项目不存在或 project.yaml 已被移动'}
            s = self._adsorption.delta_e_rows(proj)
            slab_state, e_slab = s['slab']
            ref_state, e_ref = s['ref']
            rows = [{'name': r['name'], 'state': r['state'],
                     'e_config': r['e_config'], 'delta_e': r['delta_e'],
                     'note': r['note'], 'species': r.get('species'),
                     'reference_species': r.get('reference_species'),
                     'e_ref': r.get('e_ref'),
                     'reference_job': r.get('reference_job'),
                     'reference_source': r.get('reference_source'),
                     'reference_state': r.get('reference_state'),
                     'reference_valid': r.get('reference_valid'),
                     'reference_note': r.get('reference_note'),
                     'method_check': r.get('method_check'),
                     'method_warnings': r.get('method_warnings') or [],
                     'dd_e': r.get('dd_e'),
                     'is_most_stable': bool(r.get('is_most_stable'))}
                    for r in (s.get('rows') or [])]
            parts = [f'清洁表面:{slab_state}']
            if s.get('reference_mode') == 'species':
                parts.append(f'逐物种参考:{len(s.get("species_refs") or {})} 个')
            else:
                parts.append(f'气相参考:{ref_state}' if s.get('has_ref')
                             else '未设气相参考')
            final_eligible, final_reason = self._final_report_gate(proj, s)
            return {'ok': True, 'rows': rows, 'note': ';'.join(parts),
                    'reference_mode': s.get('reference_mode', 'none'),
                    'species_refs': s.get('species_refs') or {},
                    'dataset_groups': s.get('dataset_groups') or [],
                    'method_consistency': s.get('method_consistency') or {},
                    'slab': {'state': slab_state, 'energy': e_slab},
                    'ref': {'state': ref_state, 'energy': e_ref,
                            'has_ref': bool(s.get('has_ref'))},
                    'final_report_eligible': final_eligible,
                    'final_report_reason': final_reason,
                    'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'rows': [], 'note': '', 'error': str(e)}

    def proj_export_csv(self, project_id, save_to):
        try:
            record = self._resolve_project_id(project_id)
        except Exception:                                 # noqa: BLE001 public identity seam
            return self._project_identity_failure(file=None)
        result = self._call_with_project_bindings(
            [record],
            lambda: self._proj_export_csv_for_path(record['path'], save_to),
            failure={'file': None})
        return self._project_public_result(result)

    def _proj_export_csv_for_path(self, path, save_to):
        """ΔE 表导出 CSV(镜像 project_tab._on_export;utf-8-sig 语义在 adsorption 层)。"""
        try:
            proj = self._load_project_for_path((path or '').strip())
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

    def proj_report(self, project_id, save_to, final=False):
        try:
            record = self._resolve_project_id(project_id)
        except Exception:                                 # noqa: BLE001 public identity seam
            return self._project_identity_failure(
                file=None, files=[], kind=None, scientific_status=None,
                marker=None)
        result = self._call_with_project_bindings(
            [record],
            lambda: self._proj_report_for_path(
                record['path'], save_to, final=final),
            failure={
                'file': None, 'files': [], 'kind': None,
                'scientific_status': None, 'marker': None,
            })
        return self._project_public_result(result)

    def _proj_report_for_path(self, path, save_to, final=False):
        """Compatibility adapter for the canonical one-snapshot report bundle.

        The historical API accepts one HTML filename and returns ``files`` as a
        list.  Generation, validation, and marker persistence must nevertheless
        use :meth:`proj_report_bundle`; keeping a second renderer here would let
        old callers bypass the canonical contract chain.
        """
        out = str(save_to or '').strip()
        if not out:
            return {'ok': False, 'file': None, 'files': [],
                    'kind': None, 'scientific_status': None, 'marker': None,
                    'error': '未指定报告路径'}
        try:
            requested_path = os.path.abspath(os.path.normpath(out))
            out_dir = os.path.dirname(requested_path) or os.getcwd()
            stem = os.path.splitext(os.path.basename(requested_path))[0]
            bundle = self._proj_report_bundle_for_path(
                path, out_dir, formats=('html',), final=bool(final), stem=stem)
            bundle_files = (bundle.get('files')
                            if isinstance(bundle.get('files'), dict) else {})
            primary = str(bundle_files.get('html') or '') or None
            ok = bool(bundle.get('ok')) and bool(primary)
            error = bundle.get('error')
            if bundle.get('ok') and not primary:
                ok = False
                error = error or '报告生成器未产出 HTML 主文件'
            return {
                'ok': ok,
                'file': primary if ok else None,
                'files': [primary] if primary else [],
                'kind': bundle.get('kind'),
                'scientific_status': bundle.get('scientific_status')
                or bundle.get('kind'),
                'scientific_qualification': bundle.get(
                    'scientific_qualification'),
                'artifact_status': bundle.get('artifact_status'),
                'gate_reason': bundle.get('gate_reason') or '',
                'marker': bundle.get('marker'),
                'error': None if ok else (error or '报告生成失败'),
            }
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'file': None, 'files': [],
                    'kind': None, 'scientific_status': None, 'marker': None,
                    'error': str(e)}

    @staticmethod
    def _safe_report_stem(value) -> str:
        """Filesystem-safe, readable report stem (also valid on Windows)."""
        stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '_', str(value or 'report')).strip(' ._')
        return (stem or 'report')[:96]

    @staticmethod
    def _normalize_report_formats(formats) -> tuple[str, ...]:
        """Normalize an explicit report-format request without treating [] as default."""
        values = _REPORT_FORMATS if formats is None else formats
        if isinstance(values, str):
            values = (values,)
        try:
            raw = tuple(values)
        except TypeError as exc:
            raise TypeError('报告 formats 必须为字符串序列') from exc
        if not raw:
            raise ValueError('至少选择一种报告格式')
        normalized = []
        for value in raw:
            fmt = str(value or '').strip().lower()
            if fmt not in _REPORT_FORMATS:
                raise ValueError(
                    f'不支持的报告格式 {value!r}；可选值为 html、docx、pdf')
            if fmt not in normalized:
                normalized.append(fmt)
        if not normalized:
            raise ValueError('至少选择一种报告格式')
        return tuple(normalized)

    @staticmethod
    def _report_failure_envelope(error, *, kind=None, qualification=None,
                                 gate_reason='', artifact_status='failed',
                                 preserve_artifact=False, files=None, **extra):
        """Stable artifact/science axes for every canonical bundle failure."""
        actual_kind = str(kind or '').strip().lower() or None
        if actual_kind not in _REPORT_KINDS:
            actual_kind = None
        has_unrecorded_artifact = bool(preserve_artifact and actual_kind)
        actual_qualification = str(qualification or '').strip().lower() or None
        if actual_qualification not in _REPORT_QUALIFICATIONS:
            actual_qualification = None
        result = {
            'ok': False,
            'kind': actual_kind if has_unrecorded_artifact else None,
            'artifact_status': str(artifact_status or 'failed'),
            'scientific_status': actual_kind if has_unrecorded_artifact else None,
            'scientific_qualification': (
                actual_qualification if has_unrecorded_artifact else None),
            'publication_gate_status': 'unknown',
            'desired_report_kind': None,
            'gate_reason': str(gate_reason or ''),
            'marker': None,
            'files': (dict(files or {}) if has_unrecorded_artifact else {}),
            'error': str(error or '报告生成失败'),
        }
        result.update(extra)
        return result

    @staticmethod
    def _json_safe_report_result(value):
        """Convert renderer ``Path`` objects to pywebview/JSON-safe strings."""
        if isinstance(value, os.PathLike):
            return os.fspath(value)
        if isinstance(value, dict):
            return {
                str(key): Api._json_safe_report_result(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [Api._json_safe_report_result(item) for item in value]
        return value

    @staticmethod
    def _comparison_u_by_element(fingerprint):
        """Map DFT+U vectors onto POTCAR elements without comparing spin.

        Catalyst projects can legitimately contain different metals, so a raw
        LDAUU vector is not a cross-project protocol identifier.  Mapping the
        vector to element names lets :mod:`project.comparison` check only
        elements shared by a pair of projects.
        """
        fp = fingerprint if isinstance(fingerprint, dict) else {}
        potcars = fp.get('potcar_ids') or {}
        elements = [str(element) for element in potcars]
        if not elements:
            return None
        values = fp.get('u_values')
        if not isinstance(values, dict):
            return {element: {'enabled': False} for element in elements}
        enabled = _method_bool(values.get('LDAU'))
        if enabled is False or values.get('LDAU') is False:
            return {element: {'enabled': False} for element in elements}
        if enabled is not True and values.get('LDAU') is not True:
            return None

        def _tokens(key, *, integer=False):
            raw = values.get(key)
            if isinstance(raw, (list, tuple)):
                items = list(raw)
            else:
                items = str(raw or '').replace(',', ' ').split()
            parsed = []
            for item in items:
                number = _method_number(item)
                if number is None or (integer and number != int(number)):
                    return None
                parsed.append(int(number) if integer else round(number, 10))
            return parsed

        orbitals = _tokens('LDAUL', integer=True)
        strengths = _tokens('LDAUU')
        exchanges = _tokens('LDAUJ')
        if (orbitals is None or strengths is None or exchanges is None
                or not (len(orbitals) == len(strengths) == len(exchanges)
                        == len(elements))):
            return None
        ldau_type = _method_integer(values.get('LDAUTYPE'))
        result = {}
        for index, element in enumerate(elements):
            if orbitals[index] < 0:
                result[element] = {'enabled': False}
            else:
                result[element] = {
                    'enabled': True,
                    'type': ldau_type,
                    'l': orbitals[index],
                    'u_eV': strengths[index],
                    'j_eV': exchanges[index],
                }
        return result

    def _comparison_method_evidence(self, project, summary):
        """Build a spin-neutral cross-project protocol from actual job evidence."""
        method = (summary or {}).get('method_consistency') or {}
        if str(method.get('status') or '').lower() != 'verified':
            return {
                'status': 'unverified',
                'fingerprint': '',
                'missing': ['项目内部方法证据尚未 verified'],
            }
        members = (project or {}).get('members') or {}
        configs = [str(path) for path in (members.get('configs') or []) if path]
        selected_names = {
            str(row.get('name') or '')
            for row in self._comparison_model().stable_species_rows(summary)
        }
        representative = next(
            (path for path in configs
             if os.path.basename(os.path.normpath(path)) in selected_names),
            configs[0] if configs else members.get('clean_slab'),
        )
        if not representative:
            return {
                'status': 'unverified',
                'fingerprint': '',
                'missing': ['没有可读取的周期能量操作数'],
            }
        try:
            from vcstudio.project import energy_gate
            manifest = self._manifest.load_manifest(representative)
            record = energy_gate.method_record(
                representative, manifest, '跨项目代表构型')
        except Exception as exc:                         # noqa: BLE001
            return {
                'status': 'unverified',
                'fingerprint': '',
                'missing': [f'实际方法证据读取失败：{exc}'],
            }
        fp = record.get('fingerprint') or {}
        known = record.get('known') or {}
        required = ('functional', 'dispersion', 'encut',
                    'kpoints_scheme', 'potcar_ids')
        missing = [field for field in required if not known.get(field)]
        potcars = dict(fp.get('potcar_ids') or {})
        if not potcars and 'potcar_ids' not in missing:
            missing.append('potcar_ids')
        u_by_element = self._comparison_u_by_element(fp)
        if u_by_element is None:
            missing.append('u_values_by_element')
        protocol = {
            'functional': fp.get('functional'),
            'dispersion': fp.get('dispersion'),
            'encut_eV': _method_number(fp.get('encut')),
            'kpoints_scheme': fp.get('kpoints_scheme'),
            'reference_mode': str((summary or {}).get('reference_mode') or ''),
            'energy_quantity': 'E0',
        }
        if missing:
            return {
                'status': 'unverified',
                'fingerprint': '',
                'protocol': protocol,
                'potcar_ids': potcars,
                'u_by_element': u_by_element or {},
                'source_job': str(representative),
                'missing': list(dict.fromkeys(missing)),
            }
        encoded = json.dumps(
            protocol, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode('utf-8')
        return {
            'status': 'verified',
            'fingerprint': hashlib.sha256(encoded).hexdigest(),
            'protocol': protocol,
            'potcar_ids': potcars,
            'u_by_element': u_by_element,
            'source_job': str(representative),
            'missing': [],
        }

    @staticmethod
    def _candidate_evaluation_table(
            evaluations, *, title=None, locale='zh-CN', precision=3):
        """Render candidate decisions in the report's selected locale.

        The evaluator deliberately stores auditable machine codes plus Chinese
        operator guidance.  An English report must not merely translate the
        section heading while leaking that operator guidance into its body, so
        this projection is rebuilt from the locale-neutral decision fields.
        """
        english = str(locale or '').strip() == 'en-US'
        precision = max(2, min(8, int(precision)))
        rows = []
        for evaluation in evaluations or []:
            candidate = evaluation.get('candidate') or {}
            decision = evaluation.get('decision') or {}
            evidence = evaluation.get('evidence') or {}
            profile = evaluation.get('profile') or {}
            thermo = evaluation.get('thermodynamics') or {}
            if english:
                summary = (
                    f"Priority: {decision.get('priority') or 'hold_for_evidence'}; "
                    f"profile verdict: {decision.get('profile_verdict') or 'unknown'}; "
                    f"data quality: {decision.get('data_quality') or 'unknown'}."
                )
            else:
                summary = decision.get('summary_zh') or '证据不足'
            rows.append([
                candidate.get('name') or ('Project' if english else '项目'),
                decision.get('priority') or 'hold_for_evidence',
                summary,
                (profile.get('short_chain_risk') or {}).get('status') or 'unknown',
                evidence.get('claim_ceiling') or 'electronic_adsorption_screen',
                ('—' if thermo.get('u_l_V') is None
                 else f'{thermo.get("u_l_V"):.{precision}f}'),
                ('—' if thermo.get('eta_V') is None
                 else f'{thermo.get("eta_V"):.{precision}f}'),
            ])
        if english:
            resolved_title = title or 'Candidate follow-up priority'
            columns = [
                'Catalyst', 'Recommendation', 'Assessment', 'Short-chain risk',
                'Claim ceiling', 'U_L / V', 'eta / V',
            ]
            caption = (
                'advance prioritizes free-energy, solvation, and key-barrier '
                'calculations; hold_for_evidence requires the stated evidence '
                'gaps to be closed first. Adsorption energy alone is not activity.'
            )
        else:
            resolved_title = title or '候选材料后续计算优先级'
            columns = [
                '催化剂', '建议', '评价结论', '短链风险',
                '结论上限', 'U_L / V', 'η / V',
            ]
            caption = (
                'advance 表示建议优先进入自由能、溶剂化与关键 NEB；'
                'hold_for_evidence 表示先补齐方法或物种证据。电子吸附能不直接等同于活性。')
        return {
            'title': resolved_title,
            'columns': columns,
            'rows': rows,
            'caption': caption,
        } if rows else None

    @staticmethod
    def _recommendation_blocks(evaluations, *, locale='zh-CN'):
        english = str(locale or '').strip() == 'en-US'
        english_guidance = {
            'RECONCILE_METHOD_CONFLICTS': (
                'Reconcile method conflicts',
                'Known method conflicts invalidate direct subtraction of total energies.'),
            'AUDIT_METHOD_PROVENANCE': (
                'Audit method provenance',
                'Complete method fingerprints and comparability evidence before ranking.'),
            'CALC_VALID_ADSORBATE_REFERENCE': (
                'Calculate a valid adsorbate reference',
                'A verified reference energy is required for the declared adsorption-energy formula.'),
            'RESOLVE_ADSORPTION_ENERGIES': (
                'Resolve adsorption-energy inputs',
                'At least one numeric and method-compatible adsorption energy is required.'),
            'CALC_MISSING_LIS_SPECIES': (
                'Calculate missing Li-S species',
                'Sequence coverage is required to assess anchoring and terminal-product risk.'),
            'SAMPLE_MORE_CONFIGURATIONS': (
                'Sample more configurations',
                'One geometry cannot establish the global minimum adsorption state.'),
            'VERIFY_ADSORPTION_GEOMETRY': (
                'Verify adsorption geometries',
                'Check adsorbate integrity and surface reconstruction before comparing minima.'),
            'CALC_ZPE_ENTROPY': (
                'Calculate ZPE and entropy corrections',
                'Electronic adsorption energy does not replace adsorption or reaction free energy.'),
            'CALC_SOLVATION_LONG_CHAIN': (
                'Calculate consistent solvation corrections',
                'Vacuum adsorption energies may misrepresent long-chain anchoring in electrolyte.'),
            'BUILD_BALANCED_FREE_ENERGY_PATH': (
                'Build a balanced free-energy pathway',
                'Adsorption energies of different Li2Sx species are not adjacent reaction steps.'),
            'AUDIT_FREE_ENERGY_METRICS': (
                'Audit free-energy metrics',
                'Inconsistent potential identities cannot support activity ranking.'),
            'NEB_LI2S_CHARGE_DECOMPOSITION': (
                'Calculate the Li2S charge-decomposition barrier',
                'Strong short-chain binding may create a terminal-product trap.'),
            'NEB_DISCHARGE_KEY_STEPS': (
                'Calculate key discharge barriers',
                'Adsorption energy is thermodynamic evidence and cannot replace kinetics.'),
            'ADD_DAC_BASELINES': (
                'Add DAC baseline calculations',
                'Bare-slab and corresponding SAC baselines are required to test dual-site synergy.'),
        }
        blocks, seen = [], set()
        for evaluation in evaluations or []:
            candidate = ((evaluation.get('candidate') or {}).get('name')
                         or ('Project' if english else '项目'))
            for item in evaluation.get('recommendations') or []:
                code = str(item.get('code') or '')
                key = (candidate, code)
                if key in seen:
                    continue
                seen.add(key)
                separator = ', ' if english else '、'
                targets = separator.join(
                    str(value) for value in item.get('targets') or [])
                if english:
                    action, reason = english_guidance.get(code, (
                        code.replace('_', ' ').title() or 'Complete evidence action',
                        'Complete this evidence action before making stronger claims.',
                    ))
                    suffix = f' Targets: {targets}.' if targets else ''
                    title = f'{item.get("priority") or "P2"} · {candidate} · {action}'
                    text = f'{reason}{suffix}'
                else:
                    suffix = f'；目标：{targets}' if targets else ''
                    title = (
                        f'{item.get("priority") or "P2"} · {candidate} · '
                        f'{item.get("action_zh") or code}'
                    )
                    text = f'{item.get("reason") or ""}{suffix}'
                blocks.append({
                    'title': title,
                    'text': text,
                })
        return blocks

    @staticmethod
    def _portable_locator_parts(value):
        """Parse a locator lexically, independent from this process's host OS.

        Report contracts may be replayed or validated on a host other than the
        one which first wrote their private locators.  ``os.path`` therefore
        cannot decide whether a value is Windows, UNC, or POSIX: on Linux, for
        example, ``D:\\jobs\\config`` is one ordinary filename.  This helper
        intentionally does no expansion, realpath lookup, or filesystem access.
        It merely normalizes explicit separators and returns an anchor plus the
        path components below it.
        """
        raw = str(value or '').strip()
        if not raw:
            return '', '', ()

        # A drive prefix, a backslash-rooted locator, and //host/share all use
        # Windows rules even if the current process is running on POSIX.
        windows_style = bool(re.match(r'^[A-Za-z]:', raw)) or raw.startswith(
            ('\\\\', '\\', '//'))
        if windows_style:
            normalized = ntpath.normpath(raw.replace('/', '\\'))
            drive, tail = ntpath.splitdrive(normalized)
            if drive.startswith(('\\\\', '//')):
                unc_drive = drive.replace('/', '\\').casefold()
                anchor = f'unc:{unc_drive}'
            elif drive:
                anchor = f'drive:{drive.casefold()}'
            elif ntpath.isabs(normalized):
                # A locator rooted on the current Windows drive has no public
                # drive letter, but remains a distinct lexical namespace.
                anchor = 'windows-root'
            else:
                anchor = ''
            parts = tuple(
                part for part in tail.replace('\\', '/').split('/')
                if part and part != '.')
            return 'windows', anchor, parts

        normalized = posixpath.normpath(raw.replace('\\', '/'))
        anchor = 'posix-root' if normalized.startswith('/') else ''
        parts = tuple(
            part for part in normalized.split('/') if part and part != '.')
        return 'posix', anchor, parts

    @classmethod
    def _portable_relative_locator(cls, job_dir, project_root):
        """Return a safe lexical member-relative locator, or ``None``.

        Relative output is allowed only where both locators have the same
        lexical namespace and the member is demonstrably below the project
        root.  Different drives, UNC shares, absolute-vs-relative locators,
        and sibling paths deliberately fall back to a basename-plus-index ID.
        """
        job_style, job_anchor, job_parts = cls._portable_locator_parts(job_dir)
        root_style, root_anchor, root_parts = cls._portable_locator_parts(
            project_root)
        if (not job_parts or job_style != root_style
                or job_anchor != root_anchor or len(job_parts) <= len(root_parts)):
            return None

        # Windows paths are case-insensitive regardless of the CI host.  POSIX
        # paths remain case-sensitive so two scientific member names are not
        # silently collapsed merely because tests execute on Windows.
        if job_style == 'windows':
            same_root = tuple(part.casefold() for part in job_parts[:len(root_parts)]) == (
                tuple(part.casefold() for part in root_parts))
        else:
            same_root = job_parts[:len(root_parts)] == root_parts
        if not same_root:
            return None

        relative_parts = job_parts[len(root_parts):]
        if not relative_parts or any(part == '..' for part in relative_parts):
            return None
        if job_style == 'windows':
            relative_parts = tuple(part.casefold() for part in relative_parts)
        return '/'.join(relative_parts)

    @classmethod
    def _portable_member_id(cls, job_dir, project_root, index, manifest=None):
        """Return a path-free logical member ID for report contracts.

        Stable opaque manifest identities take precedence.  Without one, use a
        lexical relative locator only when it is safe across Windows, POSIX, and
        UNC syntax; otherwise use a basename plus the caller's deterministic
        member index.  Neither branch resolves a browser-facing locator.
        """
        manifest = manifest if isinstance(manifest, dict) else {}
        job_uuid = str(manifest.get('job_uuid') or '').strip()
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', job_uuid):
            return job_uuid

        relative = cls._portable_relative_locator(job_dir, project_root)
        if relative:
            return relative

        style, _anchor, parts = cls._portable_locator_parts(job_dir)
        base = parts[-1] if parts else 'member'
        if style == 'windows':
            base = base.casefold()
        # A bare drive name is an absolute-locator fragment, not a useful
        # logical basename.  Avoid ever returning it as a public member ID.
        if re.fullmatch(r'[A-Za-z]:', base):
            base = 'member'
        return f'{base}:{index}'

    @staticmethod
    def _semantic_without_locators(value):
        """Remove navigation-only paths from a scientific fingerprint payload."""
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                lowered = str(key).lower()
                if (lowered in {'path', 'root', 'dir', 'directory', 'locator',
                                'source_job', 'display_name'}
                        or lowered.endswith(('_path', '_dir', '_locator'))):
                    continue
                result[str(key)] = Api._semantic_without_locators(item)
            return result
        if isinstance(value, (list, tuple)):
            return [Api._semantic_without_locators(item) for item in value]
        return value

    def _project_report_contracts(self, proj, project_path, summary, fed, *,
                                  requested_kind, report_kind, formats,
                                  eligible_final, gate_reason,
                                  report_model_sha256, report_spec=None):
        """Freeze the report request, scientific snapshot and gate decision."""
        from vcstudio.project.report_contracts import (
            ClaimRecord,
            ReportSnapshot,
            ReportSpec,
            ValidationCheck,
            ValidationResult,
            validate_bindings,
        )

        generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        input_fingerprint = self._report_scientific_fingerprint(proj, summary)
        member_dirs = self._project_member_dirs(proj)
        project_root = os.path.abspath(os.path.normpath(str(
            proj.get('root') or os.path.dirname(str(project_path)))))
        resolved_jobs = []
        for index, path in enumerate(member_dirs, 1):
            manifest = self._manifest.load_manifest(path) or {}
            resolved_jobs.append(
                self._workspace_job_id(path, manifest)
                if report_spec is not None else
                self._portable_member_id(path, project_root, index, manifest))
        if report_spec is None:
            project_id = str(
                proj.get('project_uuid') or proj.get('name') or 'project')
            spec = ReportSpec(
                requested_kind=requested_kind,
                audience='researcher',
                locale='zh-CN',
                formats=tuple(formats),
                scope={
                    'kind': 'project',
                    'project_ids': [project_id],
                    'job_ids': resolved_jobs,
                },
                policy_refs=({
                    'id': 'project-final-report-gate',
                    'version': '1',
                },),
                created_at_utc=generated_at,
            )
        else:
            source_spec = (report_spec if isinstance(report_spec, ReportSpec)
                           else ReportSpec.from_mapping(report_spec))
            if source_spec.requested_kind != requested_kind:
                raise ValueError('工作台 ReportSpec.requested_kind 与报告请求不一致')
            if tuple(source_spec.formats) != tuple(formats):
                raise ValueError('工作台 ReportSpec.formats 与渲染格式不一致')
            project_ids = list(source_spec.scope.get('project_ids') or [])
            if len(project_ids) != 1:
                raise ValueError('单项目报告必须绑定恰好一个 opaque project_id')
            project_id = str(project_ids[0])
            spec = ReportSpec(
                preset_id=source_spec.preset_id,
                requested_kind=source_spec.requested_kind,
                audience=source_spec.audience,
                locale=source_spec.locale,
                formats=source_spec.formats,
                scope=source_spec.scope,
                outline=source_spec.outline,
                theme_id=source_spec.theme_id,
                template_ref={
                    'id': f'vcstudio-{source_spec.theme_id}',
                    'version': '1',
                },
                policy_refs=({
                    'id': 'project-final-report-gate',
                    'version': '1',
                },),
                options=source_spec.options,
                created_at_utc=generated_at,
            )
        project_locator = os.path.abspath(os.path.normpath(str(project_path)))
        if os.path.isdir(project_locator):
            project_locator = os.path.join(project_locator, 'project.yaml')
        source = {
            'source_id': f'project:{project_id}',
            'kind': 'project_scientific_projection',
            'schema': 'vcstudio.report-project-projection/v1',
            'locator': project_locator,
            # Bind the path-independent scientific projection, not raw
            # project.yaml, which also contains report/runtime state.
            'sha256': input_fingerprint,
        }
        scope_state = ((summary or {}).get('_report_scope')
                       if isinstance(summary, dict) else {}) or {}
        if report_spec is not None:
            selected_configuration_ids = list(
                scope_state.get('selected_configuration_ids') or [])
            dependency_job_ids = list(
                scope_state.get('dependency_job_ids') or [])
            excluded_configuration_ids = list(
                scope_state.get('excluded_configuration_ids') or [])
            resolved_jobs = list(dict.fromkeys(
                [*dependency_job_ids, *selected_configuration_ids]))
        else:
            selected_configuration_ids = list(resolved_jobs)
            dependency_job_ids = []
            excluded_configuration_ids = []
        resolved_scope = {
            'project_ids': [project_id],
            'scope_sha256': self._report_scope_sha256(spec),
            # ``job_ids`` is retained for v1 readers, but now means exactly the
            # members whose bytes participate in this frozen snapshot.
            'job_ids': resolved_jobs,
            'requested_job_ids': list(spec.scope.get('job_ids') or []),
            'requested_configuration_ids': list(
                spec.scope.get('configuration_ids') or []),
            'selected_configuration_ids': selected_configuration_ids,
            'dependency_job_ids': dependency_job_ids,
            'excluded_configuration_ids': excluded_configuration_ids,
            'species': list(spec.scope.get('species') or []),
            'configuration_ids': list(
                spec.scope.get('configuration_ids') or []),
            'stable_only': bool(spec.scope.get('stable_only')),
            'include_failed': bool(spec.scope.get('include_failed')),
            'selected_rows': len((summary or {}).get('rows') or []),
        }
        scientific_payload = self._report_scientific_payload(proj, summary)
        if report_spec is not None:
            payload_member_ids = {
                str(item.get('member_id') or '')
                for item in scientific_payload.get('members') or []
                if isinstance(item, dict) and item.get('member_id')
            }
            payload_configuration_ids = {
                str(item.get('configuration_id') or item.get('job_id') or '')
                for item in scientific_payload.get('rows') or []
                if isinstance(item, dict)
                and (item.get('configuration_id') or item.get('job_id'))
            }
            if payload_member_ids != set(resolved_jobs):
                raise RuntimeError(
                    'ReportSnapshot payload members 与 resolved_scope.job_ids 不一致')
            if payload_configuration_ids != set(selected_configuration_ids):
                raise RuntimeError(
                    'ReportSnapshot payload rows 与 selected_configuration_ids 不一致')
        snapshot = ReportSnapshot(
            spec_sha256=spec.semantic_sha256,
            input_fingerprint=input_fingerprint,
            created_at_utc=generated_at,
            resolved_scope=resolved_scope,
            sources=(source,),
            payload={
                'project': {
                    'project_id': project_id,
                    'name': str(proj.get('name') or ''),
                },
                'adsorption_summary': scientific_payload,
                'free_energy_path': self._json_safe_report_result(fed or {}),
            },
            evidence={
                'final_report_gate': {
                    'eligible': bool(eligible_final),
                    'reason': str(gate_reason or ''),
                },
                'method_consistency': self._json_safe_report_result(
                    (summary or {}).get('method_consistency') or {}),
                'method_confirmation': self._json_safe_report_result(
                    self._method_confirmation(proj)),
            },
        )
        english = spec.locale == 'en-US'
        gate_check = ValidationCheck(
            id='adsorption-result-delivery-gate',
            status='pass' if eligible_final else 'fail',
            severity='blocking',
            required=True,
            message=(
                ('Reference-state, adsorption-energy, and method evidence satisfy '
                 'the adsorption-result delivery gate.'
                 if english else '参考态、吸附能与方法证据满足吸附结果交付门槛。')
                if eligible_final else
                ('The final-report delivery gate is blocked; inspect the bound '
                 'snapshot evidence.' if english else
                 str(gate_reason or '最终报告门禁未通过'))
            ),
            evidence_refs=('snapshot:evidence/final_report_gate',),
            remediation=(None if eligible_final else
                         ('Complete reference-state, numeric Delta E, and method-'
                          'comparability evidence, then validate again.'
                          if english else
                          '补齐参考态、有效 ΔE 与方法一致性证据后重新验证。')),
        )
        has_path = bool(fed and fed.get('steps'))
        path_check = ValidationCheck(
            id='thermodynamic-path-coverage',
            status='pass' if has_path else 'warn',
            severity='warning',
            required=False,
            message=(
                ('A balanced reaction free-energy pathway is included.'
                 if english else '已包含配平反应路径的自由能台阶。')
                if has_path else
                ('A complete free-energy pathway is absent; no kinetic or '
                 'complete thermodynamic claim is supported.' if english else
                 '未包含完整自由能台阶；不得据此声称动力学或完整热力学结论。')),
            evidence_refs=('snapshot:payload/free_energy_path',),
        )
        qualification = ('adsorption_result_verified'
                         if report_kind == 'final' else 'diagnostic')
        validation_status = ('passed_with_warnings'
                             if eligible_final and not has_path
                             else 'passed' if eligible_final else 'blocked')
        claim = ClaimRecord(
            id='claim.adsorption.delivery',
            text=(
                ('The adsorption-energy results in this report pass the declared '
                 'delivery gate.' if english else
                 '本报告中的吸附能结果通过声明的交付门禁。')
                if report_kind == 'final' else
                ('This report records available data and blocking evidence only; '
                 'it is not a final scientific conclusion.' if english else
                 '当前报告仅记录可用数据与阻断原因，不构成最终科学结论。')),
            qualification=qualification,
            status='supported' if report_kind == 'final' else 'limited',
            evidence_refs=('check:adsorption-result-delivery-gate',),
        )
        validation = ValidationResult(
            spec_sha256=spec.semantic_sha256,
            snapshot_sha256=snapshot.semantic_sha256,
            validated_at_utc=generated_at,
            validator={
                'id': 'project-final-report-gate',
                'version': '1',
            },
            status=validation_status,
            effective_kind=report_kind,
            final_allowed=bool(report_kind == 'final' and eligible_final),
            scientific_qualification=qualification,
            claim_ceiling='electronic_adsorption_screen',
            report_model_sha256=report_model_sha256,
            checks=(gate_check, path_check),
            claims=(claim,),
        )
        validate_bindings(spec, snapshot, validation)
        return {
            'report_id': f'{project_id}-{spec.preset_id}-{report_kind}',
            'input_fingerprint': input_fingerprint,
            'scientific_qualification': qualification,
            'claim_ceiling': validation.claim_ceiling,
            'preset_id': spec.preset_id,
            'report_spec': spec.to_dict(),
            'report_snapshot': snapshot.to_dict(),
            'validation': validation.to_dict(),
            'claims': [item.to_dict() for item in validation.claims],
            'contract_refs': {
                'spec': {'schema': spec.schema, 'sha256': spec.semantic_sha256},
                'snapshot': {
                    'schema': snapshot.schema,
                    'sha256': snapshot.semantic_sha256,
                    'input_fingerprint': input_fingerprint,
                },
                'validation': {
                    'schema': validation.schema,
                    'sha256': validation.semantic_sha256,
                    'status': validation.status,
                    'final_allowed': validation.final_allowed,
                    'report_model_sha256': validation.report_model_sha256,
                },
            },
        }

    def _project_report_model(self, proj, summary, fed, *, report_kind='final',
                              figures=None, comparison_context=None,
                              report_contracts=None, report_spec=None):
        """Build the single source of truth consumed by HTML/DOCX/PDF renderers."""
        from vcstudio.project.report_contracts import ReportSpec

        spec = None
        if report_spec is not None:
            spec = (report_spec if isinstance(report_spec, ReportSpec)
                    else ReportSpec.from_mapping(report_spec))
        locale = spec.locale if spec is not None else 'zh-CN'
        english = locale == 'en-US'
        try:
            precision = int((spec.options if spec is not None else {}).get(
                'precision', 3))
        except (TypeError, ValueError):
            precision = 3
        precision = max(2, min(8, precision))
        # These service-owned references are part of visible-content identity.
        # Put them on the base model before report_content_sha256 is computed;
        # adding them only after ValidationResult would make preview/render
        # bindings disagree and would let theme changes escape the content hash.
        template_ref = None
        policy_refs = []
        if spec is not None:
            template_ref = self._json_safe_report_result(
                spec.template_ref or {
                    'id': f'vcstudio-{spec.theme_id}',
                    'version': '1',
                })
            policy_refs = self._json_safe_report_result(
                list(spec.policy_refs) or [{
                    'id': 'project-final-report-gate',
                    'version': '1',
                }])
        evaluation = self._candidate_eval().evaluate_candidate(
            summary, project=proj, fed=fed)
        stable_only = True if spec is None else bool(
            spec.scope.get('stable_only'))
        if stable_only:
            selected = self._comparison_model().stable_species_rows(summary)
        else:
            selected = []
            for raw in (summary or {}).get('rows') or []:
                row = dict(raw or {})
                selected.append({
                    'species': row.get('species') or 'unknown',
                    'name': row.get('name') or row.get('job_id') or '—',
                    'delta_e': row.get('delta_e'),
                    'co_minima': [],
                    'method_status': (
                        (row.get('method_check') or {}).get('status')
                        if isinstance(row.get('method_check'), dict) else
                        row.get('method_status')) or 'unverified',
                    'state': row.get('state') or 'UNKNOWN',
                })
        method = summary.get('method_consistency') or {}
        basis = evaluation.get('basis') or {}
        decision = evaluation.get('decision') or {}
        profile = evaluation.get('profile') or {}
        evidence = evaluation.get('evidence') or {}
        trend = profile.get('trend') or {}
        short_chain = profile.get('short_chain_risk') or {}
        rows = []
        for row in selected:
            co_minima = row.get('co_minima') or []
            delta_e = _method_number(row.get('delta_e'))
            rows.append([
                row.get('species'),
                row.get('name'),
                (f'{delta_e:.{precision}f}' if delta_e is not None else '—'),
                (
                    f'{len(co_minima) + 1} near-degenerate configurations'
                    if english and co_minima else
                    'Unique minimum configuration'
                    if english and stable_only else
                    f'{len(co_minima) + 1} 个近简并构型'
                    if co_minima else
                    '唯一最低构型' if stable_only else
                    str(row.get('state') or 'UNKNOWN')
                ),
                row.get('method_status') or 'unverified',
            ])
        if english:
            findings = [
                (
                    f"Decision priority: {decision.get('priority') or 'hold_for_evidence'}; "
                    f"profile verdict: {decision.get('profile_verdict') or 'unknown'}; "
                    f"data quality: {decision.get('data_quality') or 'unknown'}."
                ),
                f'Adsorption-sequence trend: {trend.get("status") or "insufficient"}; '
                f'short-chain risk: {short_chain.get("status") or "unknown"}.',
            ]
        else:
            findings = [
                decision.get('summary_zh') or '当前数据仅支持吸附能层面的初筛判断。',
                f'吸附序列趋势：{trend.get("status") or "insufficient"}；'
                f'短链风险：{short_chain.get("status") or "unknown"}。',
            ]
        if fed:
            u_l = _method_number(fed.get('u_l'))
            pds = fed.get('pds_index')
            if english:
                findings.append(
                    'Free-energy pathway: '
                    f'U_L={f"{u_l:.{precision}f}" if u_l is not None else "—"} V; '
                    f'PDS={pds if pds is not None else "—"}.')
            else:
                findings.append(
                    '自由能路径：'
                    f'U_L={f"{u_l:.{precision}f}" if u_l is not None else "—"} V，'
                    f'PDS={pds if pds is not None else "—"}。')
        limitations = []
        for missing in (evidence.get('coverage') or {}).get('missing_species') or []:
            limitations.append(
                f'Missing species evidence: {missing}.' if english
                else f'缺少证据：{missing}')
        method_warnings = list(method.get('warnings') or [])
        if english and method_warnings:
            limitations.append(
                f'Method comparability has {len(method_warnings)} unresolved '
                'warning(s); inspect the bound validation evidence before reuse.')
        elif not english:
            limitations.extend(str(value) for value in method_warnings)
        comparability = evaluation.get('comparability') or {}
        if english:
            for category, prefix in (
                    ('blocking', 'Comparability blocker'),
                    ('warnings', 'Comparability warning')):
                for index, item in enumerate(comparability.get(category) or [], 1):
                    code = (str(item.get('code') or '').strip()
                            if isinstance(item, dict) else '')
                    limitations.append(
                        f'{prefix}: {code or f"UNSPECIFIED_{index}"}.')
        else:
            limitations.extend(
                str(item.get('message') or item)
                for item in comparability.get('blocking') or [])
            limitations.extend(
                str(item.get('message') or item)
                for item in comparability.get('warnings') or [])
        if not fed:
            limitations.append(
                'No balanced reaction free-energy pathway is available; U_L '
                'cannot be inferred from adsorption-energy differences.'
                if english else
                '未获得配平反应路径的自由能台阶，不能由吸附能差值推导 U_L。')
        else:
            fed_warnings = list(fed.get('warnings') or [])
            if english and fed_warnings:
                limitations.append(
                    f'The free-energy pathway has {len(fed_warnings)} validation '
                    'warning(s); inspect the bound pathway evidence.')
            elif not english:
                limitations.extend(str(value) for value in fed_warnings)
        if english:
            methods = [
                'Adsorption energy is defined as E_ads = E(slab+ads) - E(slab) '
                '- E(adsorbate); negative values indicate exothermic adsorption.',
                'This report uses the lis_eads_sabatier_screen_v1 screening policy. '
                'Its intervals are heuristics, not a universal optimum across '
                'materials, coverages, or computational settings.',
                f'Method-comparability status: {method.get("status") or "unverified"}.',
                'Configurations within approximately 0.15 eV are retained as '
                'near-degenerate candidates rather than forced into one minimum.',
            ]
        else:
            methods = [
                '吸附能定义：E_ads = E(slab+ads) - E(slab) - E(adsorbate)，负值表示放热吸附。',
                ('本报告使用 lis_eads_sabatier_screen_v1 初筛策略；区间是筛选启发式，'
                 '不是跨材料、覆盖度和计算设置通用的最佳吸附能标准。'),
                f'方法可比性状态：{method.get("status") or "unverified"}。',
                ('吸附能小于约 0.15 eV 的构型按近简并处理，不强行声明唯一最稳构型。'),
            ]
        if fed:
            if english:
                methods.append(
                    'Reaction steps include ZPE-TS corrections.'
                    if fed.get('thermo_corrected') else
                    'Reaction steps currently use electronic energies without '
                    'ZPE-TS corrections.')
            else:
                methods.append(
                    '反应台阶已叠加 ZPE−TS 热校正。'
                    if fed.get('thermo_corrected')
                    else '反应台阶当前为未叠加 ZPE−TS 的电子能口径。')
        project_name = str(proj.get('name') or ('Catalyst' if english else '催化剂'))
        claim_ceiling = (decision.get('claim_ceiling') or
                         evidence.get('level') or 'electronic_adsorption_screen')
        if english:
            subtitle = (
                'First-principles screening report for Li-S cathode catalysts'
                if report_kind == 'final' else
                'Research draft: content and evidence remain editable'
                if report_kind == 'draft' else
                'Diagnostic report: evidence does not yet satisfy the final gate')
            metadata = {
                'Project': project_name,
                'Project ID': proj.get('project_uuid') or '—',
                'Data quantity': basis.get('quantity') or 'delta_E_ads',
                'Method gate': method.get('status') or 'unverified',
                'Claim ceiling': claim_ceiling,
                'Evaluation policy': (evaluation.get('audit') or {}).get('policy_id')
                                     or 'lis_eads_sabatier_screen_v1',
                'Numeric precision': f'{precision} decimal places',
            }
            executive_summary = (
                f"The selected evidence supports an adsorption-energy screening "
                f"decision of {decision.get('priority') or 'hold_for_evidence'}. "
                'Free-energy and kinetic evidence are required before broader '
                'catalytic-performance claims.'
            )
            table_title = (
                'Minimum-energy configuration and electronic adsorption energy by species'
                if stable_only else
                'Electronic adsorption energy and state for selected configurations')
            table_columns = [
                'Species', 'Configuration', 'E_ads / eV',
                'Near-degeneracy protection' if stable_only else 'Job state',
                'Method status',
            ]
            table_caption = (
                'The minimum E_ads is selected within each species; configurations '
                'within 0.15 eV remain near-degenerate candidates. Adjacent rows '
                'must not be subtracted directly as reaction steps.'
                if stable_only else
                'All configurations in the frozen workbench scope are retained; '
                'failed or unfinished entries display an explicit missing value.')
        else:
            subtitle = (
                '锂硫电池正极催化材料第一性原理筛选报告'
                if report_kind == 'final' else
                '研究草稿：内容与证据仍可继续编辑' if report_kind == 'draft' else
                '诊断报告：证据尚未满足最终结论门槛')
            metadata = {
                '项目': project_name,
                '项目标识': proj.get('project_uuid') or '—',
                '数据口径': basis.get('quantity') or 'delta_E_ads',
                '方法门禁': method.get('status') or 'unverified',
                '结论上限': claim_ceiling,
                '评价策略': (evaluation.get('audit') or {}).get('policy_id')
                            or 'lis_eads_sabatier_screen_v1',
                '数值精度': f'小数点后 {precision} 位',
            }
            executive_summary = decision.get('summary_zh') or (
                '当前结果仅支持吸附能初筛，建议结合自由能和势垒继续验证。')
            table_title = ('各物种最稳吸附构型与电子吸附能' if stable_only
                           else '所选构型的电子吸附能与状态')
            table_columns = ['物种', '构型', 'E_ads / eV',
                             '近简并保护' if stable_only else '作业状态', '方法状态']
            table_caption = (
                '同一物种按 E_ads 最低值选取；差值不超过 0.15 eV 的构型'
                '保留为近简并候选。该表不能直接相邻相减作为反应台阶。'
                if stable_only else
                '按工作台冻结的数据范围保留全部所选构型；失败或未完成项以缺失数值明确显示。')
        model = {
            'schema': 'vcstudio.research-report/v1',
            'locale': locale,
            'outline': (list(spec.outline) if spec is not None else None),
            'preset_id': (spec.preset_id if spec is not None else 'scientific-review'),
            'template_ref': template_ref,
            'policy_refs': policy_refs,
            'title': (f'{project_name} Adsorption-Energy and Reaction-Path Assessment'
                      if english else f'{project_name} 吸附能与反应路径评估'),
            'subtitle': subtitle,
            'kicker': 'VASP CATALYST STUDIO · RESEARCH REPORT',
            'report_kind': report_kind,
            'metadata': metadata,
            'executive_summary': executive_summary,
            'key_findings': findings,
            'candidate_evaluations': self._candidate_evaluation_table(
                [evaluation], locale=locale, precision=precision),
            'adsorption_table': {
                'title': table_title,
                'columns': table_columns,
                'rows': rows,
                'caption': table_caption,
            },
            'figures': list(figures or []),
            'methods': methods,
            'limitations': list(dict.fromkeys(limitations)),
            'recommendations': self._recommendation_blocks(
                [evaluation], locale=locale),
            'comparison_context': comparison_context or {},
        }
        if report_contracts:
            model.update(self._json_safe_report_result(report_contracts))
        else:
            # Legacy direct callers remain renderable, but the explicit
            # qualification prevents the renderer from inferring science state
            # from a subtitle or filename.
            model.update({
                'scientific_qualification': (
                    'adsorption_result_verified' if report_kind == 'final'
                    else 'diagnostic'),
                'claim_ceiling': claim_ceiling,
            })
        return model

    def _project_report_figures(self, proj, summary, fed, out_dir, report_spec=None):
        """Generate report figures from the same selected rows/fed snapshot."""
        from vcstudio.project.report_contracts import ReportSpec

        spec = None
        if report_spec is not None:
            spec = (report_spec if isinstance(report_spec, ReportSpec)
                    else ReportSpec.from_mapping(report_spec))
        english = bool(spec is not None and spec.locale == 'en-US')
        nc = self._nc()
        os.makedirs(out_dir, exist_ok=True)
        selected = self._comparison_model().stable_species_rows(summary)
        name = str(proj.get('name') or 'project')
        figures, files = [], []
        if selected:
            data = {
                'adsorbates': [row['species'] for row in selected],
                'substrates': {name: [row['delta_e'] for row in selected]},
            }
            bar_files = nc.adsorption_bar(
                data, os.path.join(out_dir, 'adsorption_profile.png'),
                negative_up=False, value_labels=True,
                title='Adsorption-energy profile', formats=('png', 'pdf'))
            files.extend(bar_files)
            figures.append({
                'path': bar_files[0],
                'title': ('Electronic adsorption energies of minimum-energy '
                          'Li-S configurations' if english else
                          'Li-S 物种最稳构型的电子吸附能'),
                'caption': (
                    'More negative values indicate stronger adsorption; stronger '
                    'adsorption does not automatically imply better catalytic performance.'
                    if english else
                    '数值越负表示吸附越强；过强吸附不自动等同于更优催化性能。'),
            })
            table_files = nc.energy_matrix_table(
                data, os.path.join(out_dir, 'adsorption_table.png'),
                title='Adsorption-energy screening', formats=('png', 'pdf'))
            files.extend(table_files)
        if fed and fed.get('steps'):
            ladder_files = nc.free_energy_ladder(
                [{
                    'name': name,
                    'G': [step['G'] for step in fed['steps']],
                    'pds_index': fed.get('pds_index'),
                    'u_l': fed.get('u_l'),
                }],
                os.path.join(out_dir, 'free_energy_ladder.png'),
                step_labels=[step.get('label') or step.get('species')
                             for step in fed['steps']],
                show_ul=True, title='Li-S reaction free-energy path',
                formats=('png', 'pdf'))
            files.extend(ladder_files)
            figures.append({
                'path': ladder_files[0],
                'title': ('Free-energy steps of the balanced reaction pathway'
                          if english else '配平反应路径的自由能台阶'),
                'caption': (
                    'PDS and U_L are taken from the electron-resolved free-energy '
                    'definition and are not re-derived by the plotting layer.'
                    if english else
                    'PDS 与 U_L 直接取自自由能模块的逐电子定义，不由绘图层重新推导。'),
            })
        return figures, files

    # ── Phase C:统一报告工作台服务接线 ─────────────────────────────────────
    def _report_workbench_project_context(self, path):
        """Resolve one project without exposing its registry path to the browser."""
        project_path = str(path or '').strip()
        if not project_path:
            raise ValueError('未指定项目路径')
        project = self._load_project_for_path(project_path)
        if project is None:
            raise FileNotFoundError('项目不存在或 project.yaml 已被移动')
        root = str(project.get('root') or '').strip()
        if not root:
            absolute = os.path.abspath(os.path.normpath(project_path))
            root = absolute if os.path.isdir(absolute) else os.path.dirname(absolute)
        return {
            'project': project,
            'project_path': project_path,
            'project_root': os.path.abspath(os.path.normpath(root)),
            'project_id': self._workspace_project_id(project_path, project),
            'project_name': str(project.get('name') or ''),
        }

    @staticmethod
    def _report_scope_sha256(report_spec):
        """Hash only the selector semantics shared by request and replay.

        Service-owned template/policy references change the full ReportSpec
        digest, but they must not make an otherwise identical data selector look
        different while a marker is replayed.  This projection is therefore
        intentionally limited to ``scope``.
        """
        from vcstudio.project.report_contracts import ReportSpec

        spec = (report_spec if isinstance(report_spec, ReportSpec)
                else ReportSpec.from_mapping(report_spec))
        scope = Api._json_safe_report_result(dict(spec.scope))
        encoded = json.dumps(
            scope, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
            allow_nan=False).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest()

    def _report_workbench_scope_summary(self, context, summary, report_spec):
        """Apply a validated opaque-ID scope and reject unresolved selections."""
        from vcstudio.project.report_contracts import ReportSpec

        spec = (report_spec if isinstance(report_spec, ReportSpec)
                else ReportSpec.from_mapping(report_spec))
        scope = spec.scope
        project_ids = list(scope.get('project_ids') or [])
        if project_ids != [context['project_id']]:
            raise ValueError('ReportSpec 项目范围与当前项目身份不一致')
        project = context['project']
        member_ids = {}
        config_ids = set()
        config_name_ids = {}
        members = project.get('members') or {}
        config_paths = {
            self._workspace_path_key(path)
            for path in (members.get('configs') or [])
        }
        for member_dir in self._project_member_dirs(project):
            manifest = self._manifest.load_manifest(member_dir) or {}
            job_id = self._workspace_job_id(member_dir, manifest)
            member_key = self._workspace_path_key(member_dir)
            member_ids[member_key] = job_id
            if member_key in config_paths:
                config_ids.add(job_id)
                base_name = os.path.basename(os.path.normpath(str(member_dir)))
                config_name_ids.setdefault(os.path.normcase(base_name), []).append(job_id)

        requested_jobs = set(scope.get('job_ids') or [])
        requested_configs = set(scope.get('configuration_ids') or [])
        known_jobs = set(member_ids.values())
        unknown_jobs = sorted(requested_jobs - known_jobs)
        unknown_configs = sorted(requested_configs - config_ids)
        if unknown_jobs:
            raise ValueError('ReportSpec 包含不属于当前项目的 job_id：'
                             + '、'.join(unknown_jobs))
        if unknown_configs:
            raise ValueError('ReportSpec 包含不属于当前项目的 configuration_id：'
                             + '、'.join(unknown_configs))

        species = set(scope.get('species') or [])
        selected_ids = requested_configs | {
            item for item in requested_jobs if item in config_ids
        }
        has_member_filter = bool(requested_jobs or requested_configs)
        include_failed = bool(scope.get('include_failed'))
        rows = []
        for raw in (summary or {}).get('rows') or []:
            row = copy.deepcopy(dict(raw or {}))
            raw_job = (row.get('job') or row.get('dir') or row.get('path')
                       or row.get('source_job') or row.get('job_id')
                       or row.get('configuration_id'))
            raw_job_text = str(raw_job or '').strip()
            job_id = (raw_job_text if raw_job_text in config_ids else
                      member_ids.get(self._workspace_path_key(raw_job))
                      if raw_job else None)
            if not job_id:
                row_name = os.path.basename(os.path.normpath(
                    str(row.get('name') or '')))
                candidates = config_name_ids.get(os.path.normcase(row_name)) or []
                if len(candidates) == 1:
                    job_id = candidates[0]
            if job_id not in config_ids:
                job_id = None
            if job_id:
                row['job_id'] = job_id
                row['configuration_id'] = job_id
            if has_member_filter and job_id not in selected_ids:
                continue
            if species and str(row.get('species') or '') not in species:
                continue
            if not include_failed:
                if (str(row.get('state') or '').upper() != 'DONE'
                        or _method_number(row.get('delta_e')) is None):
                    continue
            rows.append(row)
        has_explicit_row_filter = bool(has_member_filter or species)
        if has_explicit_row_filter and not rows:
            raise ValueError('ReportSpec 数据范围没有解析到任何项目构型')
        selected_configuration_ids = sorted({
            row.get('job_id') for row in rows if row.get('job_id')
        })
        unresolved_rows = [
            str(row.get('name') or '?') for row in rows if not row.get('job_id')
        ]
        if unresolved_rows:
            raise ValueError(
                'ReportSpec 无法把构型行绑定到服务器 opaque configuration_id：'
                + '、'.join(unresolved_rows))

        dependency_paths = []
        clean_slab = members.get('clean_slab')
        if clean_slab:
            dependency_paths.append(clean_slab)
        reference_mode = str((summary or {}).get('reference_mode') or '')
        if reference_mode == 'single' and members.get('gas_ref'):
            dependency_paths.append(members.get('gas_ref'))
        elif reference_mode == 'species':
            selected_species = {
                str(row.get('species') or '') for row in rows if row.get('species')
            }
            species_ref_jobs = project.get('species_ref_jobs') or {}
            if isinstance(species_ref_jobs, dict):
                dependency_paths.extend(
                    species_ref_jobs.get(species) for species in selected_species
                    if species_ref_jobs.get(species))
            dependency_paths.extend(
                row.get('reference_job') for row in rows if row.get('reference_job'))
        dependency_job_ids = sorted({
            member_ids.get(self._workspace_path_key(path))
            for path in dependency_paths if path
        } - {None})
        included_member_ids = sorted({
            *selected_configuration_ids, *dependency_job_ids,
        })
        excluded_configuration_ids = sorted(config_ids - set(
            selected_configuration_ids))

        scoped = copy.deepcopy(dict(summary or {}))
        scoped['rows'] = rows
        # Method evidence must describe the selected rows, not configurations
        # excluded by the ReportSpec.  Every production delta_e row carries its
        # pairwise method check; legacy rows without it retain the aggregate.
        selected_checks = [
            row.get('method_check') for row in rows
            if isinstance(row.get('method_check'), dict)
        ]
        if selected_checks:
            issues = list(dict.fromkeys(
                item for check in selected_checks
                for item in (check.get('issues') or [])))
            warnings = list(dict.fromkeys(
                item for check in selected_checks
                for item in (check.get('warnings') or [])))
            advisories = list(dict.fromkeys(
                item for check in selected_checks
                for item in (check.get('advisories') or [])))
            scoped['method_consistency'] = {
                'status': ('incompatible' if issues else
                           'verified' if not warnings else 'unverified'),
                'issues': issues,
                'warnings': warnings,
                'advisories': advisories,
            }
        scoped['_report_scope'] = {
            'scope_sha256': self._report_scope_sha256(spec),
            'available_member_ids': sorted(known_jobs),
            'available_configuration_ids': sorted(config_ids),
            'requested_job_ids': sorted(requested_jobs),
            'requested_configuration_ids': sorted(requested_configs),
            'selected_configuration_ids': selected_configuration_ids,
            'dependency_job_ids': dependency_job_ids,
            'excluded_configuration_ids': excluded_configuration_ids,
            'included_member_ids': included_member_ids,
            'selected_species': sorted(
                {str(row.get('species')) for row in rows if row.get('species')}),
            'input_rows': len((summary or {}).get('rows') or []),
            'selected_rows': len(rows),
        }
        return scoped

    def _report_workbench_build(self, path, report_spec, work_dir):
        """Build one frozen model/contract chain without publishing artifacts."""
        from vcstudio.project.report_contracts import ReportSpec

        spec = (report_spec if isinstance(report_spec, ReportSpec)
                else ReportSpec.from_mapping(report_spec))
        context = self._report_workbench_project_context(path)
        project = context['project']
        raw_summary = self._adsorption.delta_e_rows(project)
        summary = self._report_workbench_scope_summary(context, raw_summary, spec)
        eligible, gate_reason = self._final_report_gate(project, summary)
        requested = spec.requested_kind
        report_kind = ('final' if requested == 'final' and eligible else
                       'draft' if requested == 'draft' else 'diagnostic')
        include_thermo = bool(spec.options.get('include_thermochemistry'))
        if include_thermo:
            fed, fed_reason = self._proj_fed(project, summary)
        else:
            fed, fed_reason = None, '当前 ReportSpec 未请求热化学校正或反应路径'
        input_fingerprint = self._report_input_fingerprint(project, summary)
        scientific_fingerprint = self._report_scientific_fingerprint(project, summary)
        figure_dir = os.path.join(str(work_dir), 'figures')
        figures, figure_files = self._project_report_figures(
            project, summary, fed, figure_dir, report_spec=spec)
        model = self._project_report_model(
            project, summary, fed, report_kind=report_kind, figures=figures,
            report_spec=spec)
        qualification = ('adsorption_result_verified'
                         if report_kind == 'final' else 'diagnostic')
        model.update({
            'scientific_qualification': qualification,
            'claim_ceiling': 'electronic_adsorption_screen',
        })
        english = spec.locale == 'en-US'
        if gate_reason and report_kind == 'diagnostic':
            model['limitations'] = [
                ('The final-report gate is blocked; inspect ValidationResult '
                 'and its bound evidence before publication.'
                 if english else f'最终报告门禁未通过：{gate_reason}'),
                *model.get('limitations', []),
            ]
        if fed_reason:
            model['limitations'] = [
                ('The free-energy pathway was not generated for the selected '
                 'scope; no pathway-derived claim is made.'
                 if english else f'自由能台阶未生成：{fed_reason}'),
                *model.get('limitations', []),
            ]
        content_hasher = getattr(self._paper(), 'report_content_sha256', None)
        if not callable(content_hasher):
            from vcstudio.project.paper_report import report_content_sha256

            content_hasher = report_content_sha256
        try:
            report_model_sha256 = str(
                content_hasher(model, outline=spec.outline)).lower()
        except TypeError:
            report_model_sha256 = str(content_hasher(model)).lower()
        contracts = self._project_report_contracts(
            project, path, summary, fed,
            requested_kind=requested,
            report_kind=report_kind,
            formats=spec.formats,
            eligible_final=eligible,
            gate_reason=gate_reason,
            report_model_sha256=report_model_sha256,
            report_spec=spec,
        )
        model.update(self._json_safe_report_result(contracts))
        canonical_spec = contracts['report_spec']
        model.update({
            'locale': canonical_spec['locale'],
            'outline': list(canonical_spec['outline']),
            'preset_id': canonical_spec['preset_id'],
            'template_ref': canonical_spec.get('template_ref'),
            'policy_refs': canonical_spec.get('policy_refs') or [],
        })
        return {
            'project': project,
            'project_path': str(path),
            'project_root': context['project_root'],
            'project_id': context['project_id'],
            'summary': summary,
            'report_spec': spec.to_dict(),
            'requested_kind': requested,
            'report_kind': report_kind,
            'eligible_final': bool(eligible),
            'gate_reason': str(gate_reason or ''),
            'fed_reason': str(fed_reason or ''),
            'input_fingerprint': input_fingerprint,
            'scientific_fingerprint': scientific_fingerprint,
            'report_model_sha256': report_model_sha256,
            'contracts': contracts,
            'model': model,
            'figure_dir': figure_dir,
            'figure_files': figure_files,
            'default_stem': self._safe_report_stem(
                f'{project.get("name") or "project"}_{spec.preset_id}'),
        }

    def _report_workbench_render_preview(self, model):
        renderer = getattr(self._paper(), 'render_report_html_preview', None)
        if not callable(renderer):
            from vcstudio.project.paper_report import render_report_html_preview

            renderer = render_report_html_preview
        return self._json_safe_report_result(renderer(model))

    def _report_workbench_current_state(self, path, report_spec=None):
        if report_spec is not None:
            from vcstudio.project.report_contracts import ReportSpec

            candidate = (report_spec if isinstance(report_spec, ReportSpec)
                         else ReportSpec.from_mapping(report_spec))
            if str(candidate.scope.get('kind') or '') == 'comparison':
                return self._comparison_report_current_state(path, candidate)
        context = self._report_workbench_project_context(path)
        project = context['project']
        summary = self._adsorption.delta_e_rows(project)
        if report_spec is not None:
            summary = self._report_workbench_scope_summary(
                context, summary, report_spec)
        eligible, reason = self._final_report_gate(project, summary)
        return {
            'project_id': context['project_id'],
            'input_fingerprint': self._report_input_fingerprint(project, summary),
            'scientific_fingerprint': self._report_scientific_fingerprint(
                project, summary),
            'eligible_final': bool(eligible),
            'gate_reason': str(reason or ''),
        }

    def _comparison_report_current_state(self, path, report_spec):
        """Rebuild one comparison selector for preview CAS and marker freshness."""
        from vcstudio.project.report_contracts import ReportSpec, sha256_json

        spec = (report_spec if isinstance(report_spec, ReportSpec)
                else ReportSpec.from_mapping(report_spec))
        if str(spec.scope.get('kind') or '') != 'comparison':
            raise ValueError('比较报告 current-state 需要 comparison scope')
        project_ids = [str(item) for item in spec.scope.get('project_ids') or []]
        if (len(project_ids) < 2 or len(project_ids) != len(set(project_ids))):
            raise ValueError('比较报告 scope 至少需要两个不同项目')
        context = self._report_workbench_project_context(path)
        if context['project_id'] != project_ids[0]:
            raise ValueError('比较报告锚点项目与 scope 首项目不一致')
        index, _projects = self._analysis_workbench_project_index()
        unresolved = [project_id for project_id in project_ids if project_id not in index]
        if unresolved:
            raise ValueError('比较报告项目已无法从注册表解析：' + '、'.join(unresolved))
        anchor = index[project_ids[0]]
        if (self._analysis_workbench_path_key(anchor)
                != self._analysis_workbench_path_key(context['project_path'])):
            raise ValueError('比较报告锚点项目注册表绑定已变化')
        preset_key = str(spec.options.get('preset_key') or '') or None
        _items, snapshot, resolved_ids = self._comparison_snapshot_for_paths(
            [index[project_id] for project_id in project_ids], preset_key)
        if resolved_ids != project_ids:
            raise ValueError('比较报告项目顺序或身份已变化')
        fingerprint = sha256_json(
            self._comparison_report_scientific_payload(snapshot, preset_key))
        gate = snapshot.get('comparison_gate') or {}
        eligible = bool(snapshot.get('can_final_report'))
        reason = '；'.join(gate.get('blocking') or gate.get('warnings') or [])
        return {
            'project_id': context['project_id'],
            'input_fingerprint': fingerprint,
            'scientific_fingerprint': fingerprint,
            'eligible_final': eligible,
            'gate_reason': reason,
            'comparison_project_ids': project_ids,
        }

    def _report_workbench_validate_history_entry(self, entry):
        """Re-audit one immutable revision from bytes, never history labels.

        History is an index, not scientific authority.  This seam verifies the
        manifest hash, all rendered files, the frozen model, the three contract
        sidecars, their semantic bindings, and the exact revision projection.
        Only then are scientific status and qualification derived from the
        validated ValidationResult.
        """
        result = {
            'ok': False,
            'current': False,
            'scientific_status': None,
            'scientific_qualification': None,
            'validation_status': None,
            'publication_gate_status': 'unknown',
            'error': None,
        }
        try:
            if not isinstance(entry, dict):
                raise TypeError('历史报告 revision 必须为对象')
            manifest_path = str(entry.get('manifest') or '')
            expected_manifest_sha256 = str(
                entry.get('manifest_sha256') or '').strip().lower()
            if (not manifest_path or not os.path.isabs(manifest_path)
                    or not os.path.isfile(manifest_path)
                    or not re.fullmatch(r'[0-9a-f]{64}',
                                        expected_manifest_sha256)
                    or _sha256_file(manifest_path)
                    != expected_manifest_sha256):
                raise RuntimeError('历史报告 manifest 不存在或哈希已变化')
            try:
                with open(manifest_path, 'r', encoding='utf-8') as handle:
                    manifest = json.load(handle)
            except Exception as exc:                     # noqa: BLE001 fail closed
                raise RuntimeError(f'历史报告 manifest 无法解析：{exc}') from exc
            if not isinstance(manifest, dict):
                raise RuntimeError('历史报告 manifest 必须为对象')

            raw_kind = str(manifest.get('report_kind') or '').strip().lower()
            raw_qualification = str(
                manifest.get('scientific_qualification') or '').strip().lower()
            scientific_fingerprint = str(
                manifest.get('input_fingerprint') or '').strip().lower()
            report_model_sha256 = str(
                manifest.get('report_model_sha256') or '').strip().lower()
            model_sha256 = str(
                manifest.get('model_sha256') or '').strip().lower()
            normalized_contracts = self._validated_report_contract_refs(
                manifest.get('contracts'), raw_kind, require_sidecars=True)
            for key in ('spec', 'snapshot', 'validation'):
                if (str(entry.get(f'{key}_sha256') or '').strip().lower()
                        != normalized_contracts[key]['sha256']):
                    raise RuntimeError(
                        f'历史报告 {key} 语义哈希与 revision 不一致')
            if (str(entry.get('report_model_sha256') or '').strip().lower()
                    != report_model_sha256):
                raise RuntimeError('历史报告正文指纹与 revision 不一致')

            report_files = entry.get('files')
            contract_files = entry.get('contract_files')
            if not isinstance(report_files, dict) or not report_files:
                raise RuntimeError('历史报告格式文件清单无效')
            if (not isinstance(contract_files, dict)
                    or set(contract_files) != {'spec', 'snapshot', 'validation'}):
                raise RuntimeError('历史报告 contract sidecar 清单不完整')
            files = {
                str(fmt): str(path) for fmt, path in report_files.items()
                if fmt in _REPORT_FORMATS and path
            }
            if set(files) != set(report_files):
                raise RuntimeError('历史报告包含未知格式文件')
            files['model'] = str(entry.get('model_file') or '')
            files['manifest'] = manifest_path
            for key, path in contract_files.items():
                files[f'contract_{key}'] = str(path or '')

            payloads = self._validate_report_contract_sidecars(
                files,
                normalized_contracts,
                kind=raw_kind,
                scientific_fingerprint=scientific_fingerprint,
                scientific_qualification=raw_qualification,
                report_model_sha256=report_model_sha256,
            )
            from vcstudio.project.report_contracts import (
                ReportSnapshot,
                ReportSpec,
                ValidationResult,
                validate_bindings,
            )

            spec = ReportSpec.from_mapping(payloads['spec'])
            snapshot = ReportSnapshot.from_mapping(
                payloads['snapshot'], spec=spec)
            validation = ValidationResult.from_mapping(
                payloads['validation'], spec=spec, snapshot=snapshot)
            validate_bindings(spec, snapshot, validation)
            derived_kind = validation.effective_kind
            derived_qualification = validation.scientific_qualification
            if raw_kind != derived_kind:
                raise RuntimeError('历史报告 manifest 与 ValidationResult 科学状态不一致')
            if raw_qualification != derived_qualification:
                raise RuntimeError('历史报告 manifest 与 ValidationResult 科学资格不一致')
            if str(entry.get('scientific_status') or '') != derived_kind:
                raise RuntimeError('历史报告 revision 科学状态不一致')
            if (str(entry.get('scientific_qualification') or '')
                    != derived_qualification):
                raise RuntimeError('历史报告 revision 科学资格不一致')
            if snapshot.input_fingerprint != scientific_fingerprint:
                raise RuntimeError('历史报告 snapshot 与 manifest 输入指纹不一致')

            revision_fields = (
                'schema', 'report_id', 'revision_id', 'sequence',
                'parent_manifest_sha256', 'spec_sha256', 'snapshot_sha256',
                'validation_sha256', 'report_model_sha256', 'created_at_utc',
            )
            revision = {key: entry.get(key) for key in revision_fields}
            self._validate_report_manifest(
                manifest_path,
                kind=derived_kind,
                model_sha256=model_sha256,
                scientific_fingerprint=scientific_fingerprint,
                contracts=normalized_contracts,
                scientific_qualification=derived_qualification,
                files=files,
                report_model_sha256=validation.report_model_sha256,
                revision=revision,
            )
            result.update({
                'ok': True,
                'current': True,
                'scientific_status': derived_kind,
                'scientific_qualification': derived_qualification,
                'validation_status': validation.status,
                'publication_gate_status': (
                    'eligible' if validation.final_allowed else 'blocked'),
                'error': None,
            })
        except Exception as exc:                         # noqa: BLE001 public seam
            result['error'] = str(exc)
        return result

    def _report_workbench_render_build(self, build, out_dir, *, stem, revision):
        """Render one already frozen preview build; marker/history remain separate."""
        target = os.path.abspath(os.path.normpath(str(out_dir or '').strip()))
        if not str(out_dir or '').strip():
            raise ValueError('未指定报告目录')
        os.makedirs(target, exist_ok=True)
        model = copy.deepcopy(build['model'])
        model['revision'] = copy.deepcopy(dict(revision or {}))
        if revision and revision.get('report_id'):
            model['report_id'] = revision['report_id']
        wanted = tuple((build['contracts']['report_spec'] or {}).get('formats') or [])
        safe_stem = self._safe_report_stem(stem)
        rendered = self._json_safe_report_result(
            self._paper().render_report_bundle(
                model, target, stem=safe_stem, formats=wanted))
        rendered.setdefault('ok', True)
        rendered_files = (dict(rendered.get('files') or {})
                          if isinstance(rendered.get('files'), dict) else {})
        sidecars = (dict(rendered.get('sidecar_files') or {})
                    if isinstance(rendered.get('sidecar_files'), dict) else {})
        model_file = str(rendered.get('model_file') or sidecars.get('model')
                         or rendered_files.get('model') or '')
        missing = [
            fmt for fmt in wanted
            if not rendered_files.get(fmt)
            or not os.path.isfile(str(rendered_files.get(fmt)))
        ]
        error = str(rendered.get('error') or '').strip()
        if rendered.get('ok') is False or error:
            rendered.update({
                'ok': False, 'artifact_status': 'failed',
                'error': error or '报告渲染器返回失败状态',
            })
        elif missing:
            rendered.update({
                'ok': False, 'artifact_status': 'failed',
                'error': '报告渲染器未产出所请求格式：' + '、'.join(sorted(missing)),
            })
        elif not model_file or not os.path.isfile(model_file):
            rendered.update({
                'ok': False, 'artifact_status': 'failed',
                'error': '报告渲染器未产出冻结 model sidecar',
            })
        elif (str(rendered.get('report_model_sha256') or '').lower()
              != build['report_model_sha256']):
            rendered.update({
                'ok': False, 'artifact_status': 'failed',
                'error': '报告渲染器正文指纹与冻结预览不一致',
            })
        elif (str(rendered.get('input_fingerprint') or '')
              != build['scientific_fingerprint']):
            rendered.update({
                'ok': False, 'artifact_status': 'failed',
                'error': '报告渲染器输入指纹与冻结快照不一致',
            })
        rendered.update({
            'kind': build['report_kind'],
            'scientific_status': build['report_kind'],
            'scientific_qualification': build['contracts'][
                'scientific_qualification'],
            'requested_kind': build['requested_kind'],
            'eligible_final': build['eligible_final'],
            'publication_gate_status': (
                'eligible' if build['eligible_final'] else 'blocked'),
            'desired_report_kind': (
                'final' if build['eligible_final'] else 'diagnostic'),
            'gate_reason': build['gate_reason'],
            'figures': build['figure_files'],
            'model_file': model_file,
            'error': rendered.get('error'),
        })
        rendered.setdefault('artifact_status', 'complete')
        return rendered

    def _persist_comparison_report_marker(self, build, rendered, *, revision):
        """Persist a comparison marker after replaying every selected project."""
        kind = str(build.get('report_kind') or '').strip().lower()
        qualification = str(
            build.get('contracts', {}).get('scientific_qualification') or '')
        if kind not in _REPORT_KINDS or qualification not in _REPORT_QUALIFICATIONS:
            raise ValueError('比较报告科学状态或资格无效')
        normalized_contracts = self._validated_report_contract_refs(
            rendered.get('contracts') or
            build.get('contracts', {}).get('contract_refs') or {},
            kind, require_sidecars=True)
        with self._report_marker_lock:
            current = self._comparison_report_current_state(
                build['project_path'], build['contracts']['report_spec'])
            if (current.get('scientific_fingerprint')
                    != build.get('scientific_fingerprint')):
                raise _ReportInputChanged(
                    '比较报告生成期间项目结果已变化，产物未登记；请重新生成')
            if kind == 'final' and current.get('eligible_final') is not True:
                raise _ReportInputChanged(
                    '比较报告生成期间最终门禁已变化，产物未登记：'
                    + str(current.get('gate_reason') or '比较门禁未通过'))
            context = self._report_workbench_project_context(build['project_path'])
            if context['project_id'] != build['project_id']:
                raise _ReportInputChanged('比较报告锚点项目身份已变化，产物未登记')

            rendered_files = dict(rendered.get('files') or {})
            marker_files = dict(rendered_files)
            marker_files['model'] = rendered.get('model_file')
            contract_files = rendered.get('contract_files') or {}
            if not isinstance(contract_files, dict):
                raise TypeError('比较报告 contract_files 必须为对象')
            for key, value in contract_files.items():
                marker_files[f'contract_{key}'] = value
            if rendered.get('manifest'):
                marker_files.setdefault('manifest', rendered.get('manifest'))
            by_format = {}
            for declared_format, raw_path in marker_files.items():
                if not raw_path or not os.path.isfile(str(raw_path)):
                    continue
                by_format[str(declared_format)] = os.path.abspath(str(raw_path))
            if not {'html', 'docx', 'pdf'}.intersection(by_format):
                raise RuntimeError('比较报告 marker 没有可读的正式产物')
            manifest_path = os.path.abspath(str(rendered.get('manifest') or ''))
            if (not manifest_path or by_format.get('manifest') != manifest_path):
                raise RuntimeError('比较报告 manifest 未纳入 marker 文件绑定')
            report_model_sha256 = str(
                rendered.get('report_model_sha256') or '').strip().lower()
            scientific_fingerprint = str(
                current.get('scientific_fingerprint') or '').strip().lower()
            self._validate_report_contract_sidecars(
                by_format, normalized_contracts, kind=kind,
                scientific_fingerprint=scientific_fingerprint,
                scientific_qualification=qualification,
                report_model_sha256=report_model_sha256)
            self._validate_report_manifest(
                manifest_path, kind=kind,
                model_sha256=rendered.get('model_sha256'),
                scientific_fingerprint=scientific_fingerprint,
                contracts=normalized_contracts,
                scientific_qualification=qualification,
                files=by_format,
                report_model_sha256=report_model_sha256,
                revision=revision)
            hashes = {fmt: _sha256_file(path) for fmt, path in by_format.items()}
            primary = (by_format.get('pdf') or by_format.get('html')
                       or next(iter(by_format.values())))
            generated_at = time.strftime('%Y-%m-%dT%H:%M:%S')
            marker = {
                'schema': 'vcstudio.report-marker/v2',
                'generated_at': generated_at,
                'input_fingerprint': scientific_fingerprint,
                'scientific_fingerprint': scientific_fingerprint,
                'artifact_status': 'ready',
                'kind': kind,
                'scientific_status': kind,
                'scientific_qualification': qualification,
                'gate_reason': str(build.get('gate_reason') or ''),
                'file': primary,
                'report_sha256': _sha256_file(primary),
                'files': by_format,
                'sha256': hashes,
                'report_hashes': hashes,
                'figures_dir': os.path.abspath(str(build.get('figure_dir') or
                                                   os.path.dirname(primary))),
                'model_sha256': str(rendered.get('model_sha256') or ''),
                'report_model_sha256': report_model_sha256,
                'contracts': normalized_contracts,
                'manifest': manifest_path,
                'workspace_project_id': build['project_id'],
                'scope_kind': 'comparison',
                'comparison_project_ids': list(build['comparison_project_ids']),
                'comparison_preset_key': str(build.get('preset_key') or ''),
                'revision': self._json_safe_report_result(revision or {}),
            }
            project = context['project']
            persisted = dict(project)
            persisted['autopilot_report'] = marker
            if kind == 'draft':
                persisted.pop('autopilot_report_done', None)
            else:
                persisted['autopilot_report_done'] = generated_at
            persisted.pop('autopilot_report_blocked', None)
            try:
                self._adsorption.save_project(context['project_root'], persisted)
            except Exception as exc:                     # noqa: BLE001 durable boundary
                raise RuntimeError(f'比较报告 marker 落盘失败：{exc}') from exc
            project['autopilot_report'] = marker
            if kind == 'draft':
                project.pop('autopilot_report_done', None)
            else:
                project['autopilot_report_done'] = generated_at
            project.pop('autopilot_report_blocked', None)
            build['project'].update(project)
            return marker

    def _report_workbench_persist_build(self, build, rendered, *, revision):
        if rendered.get('ok') is False:
            raise RuntimeError(rendered.get('error') or '报告渲染失败，不能登记 marker')
        if build.get('build_kind') == 'comparison':
            return self._persist_comparison_report_marker(
                build, rendered, revision=revision)
        rendered_files = dict(rendered.get('files') or {})
        marker_files = dict(rendered_files)
        marker_files['model'] = rendered.get('model_file')
        contract_files = rendered.get('contract_files') or {}
        if not isinstance(contract_files, dict):
            raise TypeError('报告渲染器 contract_files 必须为对象')
        for key, value in contract_files.items():
            marker_files[f'contract_{key}'] = value
        if rendered.get('manifest'):
            marker_files.setdefault('manifest', rendered.get('manifest'))
        return self._persist_report_marker(
            build['project'], build['summary'], marker_files,
            kind=build['report_kind'],
            reason=build['gate_reason'],
            figures_dir=build['figure_dir'],
            model_sha256=rendered.get('model_sha256'),
            report_model_sha256=rendered.get('report_model_sha256'),
            contracts=(rendered.get('contracts')
                       or build['contracts'].get('contract_refs') or {}),
            scientific_qualification=build['contracts'][
                'scientific_qualification'],
            manifest=rendered.get('manifest'),
            project_path=build['project_path'],
            expected_input_fingerprint=build['input_fingerprint'],
            expected_scientific_fingerprint=build['scientific_fingerprint'],
            expected_project_id=(build['project'].get('project_uuid')
                                 or build['project'].get('name') or ''),
            workspace_project_id=build['project_id'],
            report_spec=build['contracts']['report_spec'],
            revision=revision,
        )

    @staticmethod
    def _report_workbench_error(error):
        from vcstudio.project.report_insights import redact

        return str(redact(str(error)))

    def _report_workbench_project_path(self, project_id):
        """Resolve only a unique registry-owned opaque project identity."""
        return self._report_workbench_project_record(project_id)['path']

    def _report_workbench_project_record(self, project_id):
        record = self._resolve_project_id(project_id)
        canonical = record['path']
        if not os.path.isfile(canonical):
            raise ValueError('registered project is unavailable')
        return record

    def _workbench_destination_registry(self):
        if self._report_workbench_destinations is None:
            from vcstudio.project.report_insights import OpaqueDestinationRegistry

            self._report_workbench_destinations = OpaqueDestinationRegistry(
                schema='vcstudio.report-workbench-destination/v1',
                purpose='report-workbench',
                token_prefix='report-workbench.',
            )
        return self._report_workbench_destinations

    def report_workbench_bootstrap(self, project_id, preset_id=None):
        try:
            record = self._report_workbench_project_record(project_id)
            return self._call_with_project_bindings(
                [record], lambda: self._reports().bootstrap(
                    record['path'], preset_id=preset_id))
        except Exception as exc:                         # noqa: BLE001 public bridge
            return {
                'schema': 'vcstudio.report-workbench-bootstrap/v1',
                'ok': False, 'project_id': None, 'project': None,
                'catalog': None, 'report_spec': None, 'status': None,
                'history': None, 'error': self._report_workbench_error(exc),
            }

    def report_workbench_preview(self, project_id, request=None):
        try:
            record = self._report_workbench_project_record(project_id)
            records = self._project_records_for_request(record, request)
            return self._call_with_project_bindings(
                records, lambda: self._reports().preview(
                    record['path'], request))
        except Exception as exc:                         # noqa: BLE001 public bridge
            return {
                'schema': 'vcstudio.report-preview/v1', 'ok': False,
                'project_id': None, 'preview_id': None, 'preview_token': None,
                'artifact_status': 'failed', 'error': self._report_workbench_error(exc),
            }

    def report_workbench_pick_destination(self, project_id):
        """Select a directory natively and expose only a project-bound token."""

        schema = 'vcstudio.report-workbench-destination/v1'
        try:
            identifier = str(project_id or '').strip()
            record = self._report_workbench_project_record(identifier)

            def select_destination():
                if self._dialog_fn is not None:
                    path = self._dialog_fn('dir')
                else:
                    import webview

                    win = webview.windows[0] if webview.windows else None
                    selected = (win.create_file_dialog(webview.FOLDER_DIALOG)
                                if win else None)
                    path = selected[0] if selected else None
                if not path:
                    return {
                        'schema': schema, 'ok': True, 'cancelled': True,
                        'destination_token': None, 'display_name': None,
                        'error': None,
                    }
                selected = self._workbench_destination_registry().register(
                    path, binding=identifier)
                return {'ok': True, 'cancelled': False, 'error': None, **selected}

            return self._call_with_project_bindings(
                [record], select_destination)
        except Exception as exc:                         # noqa: BLE001 public bridge
            return {
                'schema': schema, 'ok': False, 'cancelled': False,
                'destination_token': None, 'display_name': None,
                'error': self._report_workbench_error(exc),
            }

    def report_workbench_publish(self, project_id, destination_token,
                                 preview_id, expected=None):
        try:
            identifier = str(project_id or '').strip()
            record = self._report_workbench_project_record(identifier)

            def publish():
                if self._report_workbench_destinations is None:
                    raise ValueError('report destination has not been selected')
                out_dir = self._report_workbench_destinations.consume(
                    destination_token, expected_binding=identifier)
                return self._reports().publish(
                    record['path'], out_dir, preview_id, expected,
                    public=True, record_artifact=True)

            return self._call_with_project_bindings([record], publish)
        except Exception as exc:                         # noqa: BLE001 public bridge
            return {
                'schema': 'vcstudio.report-publish/v1', 'ok': False,
                'project_id': None, 'preview_id': str(preview_id or '') or None,
                'kind': None, 'artifact_status': 'failed', 'files': {},
                'error': self._report_workbench_error(exc),
            }

    def report_workbench_history(self, project_id):
        try:
            record = self._report_workbench_project_record(project_id)
            return self._call_with_project_bindings(
                [record], lambda: self._reports().history(record['path']))
        except Exception as exc:                         # noqa: BLE001 public bridge
            return {
                'schema': 'vcstudio.report-history/v1', 'ok': False,
                'project_id': None, 'generation': 0, 'revisions': [],
                'error': self._report_workbench_error(exc),
            }

    def _report_insight_path(self, project_id):
        """Resolve one workspace opaque identity entirely on the server."""
        return self._report_insight_record(project_id)['path']

    def _report_insight_record(self, project_id):
        return self._resolve_project_id(project_id)

    @staticmethod
    def _report_insight_failure(schema, exc, **extra):
        from vcstudio.project.report_insights import StaleRevisionError, redact

        status = ('stale' if isinstance(exc, StaleRevisionError) else
                  'blocked' if isinstance(exc, FileExistsError) else
                  'unavailable')
        result = {
            'schema': schema, 'ok': False, 'status': status,
            'project_id': None, 'error': redact(str(exc)),
        }
        result.update(extra)
        return result

    def report_revision_scientific_diff(self, project_id, left_revision_id,
                                        right_revision_id):
        """Compare two revalidated frozen revisions, never timestamps alone."""
        from vcstudio.project.report_insights import DIFF_SCHEMA, scientific_diff

        try:
            record = self._report_insight_record(project_id)
            return self._call_with_project_bindings(
                [record], lambda: scientific_diff(
                    self._reports(), record['path'], left_revision_id,
                    right_revision_id))
        except Exception as exc:                         # noqa: BLE001 public seam
            return self._report_insight_failure(
                DIFF_SCHEMA, exc, left=None, right=None, changed=None)

    def report_evidence_graph(self, project_id, revision_id):
        """Project a path-free graph from one revalidated frozen bundle."""
        from vcstudio.project.report_insights import GRAPH_SCHEMA, evidence_graph

        try:
            record = self._report_insight_record(project_id)
            return self._call_with_project_bindings(
                [record], lambda: evidence_graph(
                    self._reports(), record['path'], revision_id))
        except Exception as exc:                         # noqa: BLE001 public seam
            return self._report_insight_failure(
                GRAPH_SCHEMA, exc, revision=None, nodes=[], edges=[],
                missing_links=[])

    def report_capsule_pick_destination(self):
        """Open a server-side directory picker and return only an opaque token."""
        from vcstudio.project.report_insights import (
            DESTINATION_SCHEMA,
            CapsuleDestinations,
        )

        try:
            if self._dialog_fn is not None:
                path = self._dialog_fn('dir')
            else:
                import webview                         # delayed optional dependency

                result = webview.windows[0].create_file_dialog(
                    webview.FOLDER_DIALOG)
                path = result[0] if result else None
            if not path:
                return {
                    'schema': DESTINATION_SCHEMA, 'ok': True,
                    'cancelled': True, 'destination_token': None,
                    'display_name': None, 'error': None,
                }
            if self._report_insight_destinations is None:
                self._report_insight_destinations = CapsuleDestinations()
            selected = self._report_insight_destinations.register(path)
            return {'ok': True, 'cancelled': False, 'error': None, **selected}
        except Exception as exc:                         # noqa: BLE001 public seam
            return self._report_insight_failure(
                DESTINATION_SCHEMA, exc, cancelled=False,
                destination_token=None, display_name=None)

    def report_capsule_export(self, project_id, revision_id,
                              destination_token):
        """Export a deterministic capsule; destination_token is single-use."""
        from vcstudio.project.report_insights import CAPSULE_SCHEMA, export_capsule

        try:
            record = self._report_insight_record(project_id)

            def export():
                if self._report_insight_destinations is None:
                    raise ValueError('capsule destination has not been selected')
                destination = self._report_insight_destinations.consume(
                    destination_token)
                return export_capsule(
                    self._reports(), record['path'], revision_id, destination)

            return self._call_with_project_bindings([record], export)
        except Exception as exc:                         # noqa: BLE001 public seam
            return self._report_insight_failure(
                CAPSULE_SCHEMA, exc, revision=None, file=None)

    # ── Phase D:分析配置工作台 API ───────────────────────────────────────
    _ANALYSIS_BOOTSTRAP_SCHEMA = 'vcstudio.analysis-workbench-bootstrap/v1'
    _ANALYSIS_PREVIEW_SCHEMA = 'vcstudio.analysis-workbench-preview/v1'

    @classmethod
    def _analysis_workbench_public_value(cls, value):
        """Return a detached public projection without paths or credentials."""
        private_keys = {
            'path', 'project_path', 'root', 'dir', 'directory', 'locator',
            'source_job', 'secret', 'password', 'passwd', 'credential',
            'authorization', 'cookie', 'token', 'api_key', 'private_key',
        }
        if isinstance(value, os.PathLike):
            return '<local-path>'
        if isinstance(value, dict):
            result = {}
            for raw_key, item in value.items():
                key = str(raw_key)
                normalized = re.sub(r'[^a-z0-9]+', '_', key.lower()).strip('_')
                if (normalized in private_keys
                        or normalized.endswith(('_path', '_dir', '_directory',
                                                '_locator', '_secret', '_password',
                                                '_token', '_credential', '_cookie'))):
                    continue
                result[key] = cls._analysis_workbench_public_value(item)
            return result
        if isinstance(value, (list, tuple)):
            return [cls._analysis_workbench_public_value(item) for item in value]
        if isinstance(value, str):
            if re.search(
                    r'(?i)(?:\b(?:github_pat_|gh[opusr]_|sk-)[A-Za-z0-9_-]{12,}'
                    r'|\bBearer\s+\S+|-----BEGIN[^\r\n]{0,40}PRIVATE KEY-----'
                    r'|\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+)',
                    value):
                return '<redacted>'
            return cls._workspace_public_text(value, limit=4000)
        return cls._json_safe_report_result(value)

    @staticmethod
    def _analysis_workbench_path_key(path):
        return os.path.normcase(os.path.realpath(os.path.abspath(
            os.path.expanduser(str(path or '')))))

    def _analysis_workbench_project_index(self):
        """Resolve registry IDs to paths internally and return a path-free list."""
        snapshot = self._project_registry_snapshot()
        if snapshot['duplicate_ids']:
            raise RuntimeError('项目注册表存在重复 opaque project_id')
        return ({record['project_id']: record['path']
                 for record in snapshot['records']},
                copy.deepcopy(snapshot['public_rows']))

    @classmethod
    def _analysis_preferences_failure(cls, error, **extra):
        result = {
            'ok': False,
            'conflict': False,
            'revision': None,
            'preferences': None,
            'error': cls._analysis_workbench_public_value(str(error)),
        }
        result.update(extra)
        return result

    def analysis_preferences_read(self):
        try:
            return self._analysis_workbench_public_value(
                self._analysis_preferences().read())
        except Exception as exc:                         # noqa: BLE001 public bridge
            return self._analysis_preferences_failure(exc)

    def analysis_preferences_update(self, template, project_id,
                                    expected_revision, set_default=False):
        try:
            opaque_id = str(project_id or '').strip()
            index, _projects = self._analysis_workbench_project_index()
            if not opaque_id or opaque_id not in index:
                raise ValueError('project_id 不是服务端已知的 workspace opaque 身份')
            result = self._analysis_preferences().update(
                template, project_id=opaque_id,
                expected_revision=expected_revision,
                set_default=set_default)
            return self._analysis_workbench_public_value(result)
        except Exception as exc:                         # noqa: BLE001 public bridge
            return self._analysis_preferences_failure(exc, template=None)

    def analysis_preferences_delete(self, template_id, expected_revision):
        try:
            result = self._analysis_preferences().delete(
                template_id, expected_revision=expected_revision)
            return self._analysis_workbench_public_value(result)
        except Exception as exc:                         # noqa: BLE001 public bridge
            return self._analysis_preferences_failure(
                exc, deleted_template_id=None)

    def analysis_preferences_favorite(self, analysis_id, favorite,
                                      expected_revision):
        try:
            result = self._analysis_preferences().favorite(
                analysis_id, favorite=favorite,
                expected_revision=expected_revision)
            return self._analysis_workbench_public_value(result)
        except Exception as exc:                         # noqa: BLE001 public bridge
            return self._analysis_preferences_failure(exc)

    def analysis_preferences_set_default(self, analysis_id, template_id,
                                         expected_revision):
        try:
            result = self._analysis_preferences().set_default(
                analysis_id, template_id,
                expected_revision=expected_revision)
            return self._analysis_workbench_public_value(result)
        except Exception as exc:                         # noqa: BLE001 public bridge
            return self._analysis_preferences_failure(exc)

    def _research_index(self):
        if self._research_index_service is None:
            from vcstudio.project.research_explorer import ResearchIndexService

            self._research_index_service = ResearchIndexService()
        return self._research_index_service

    def _research_views(self):
        if self._research_view_store is None:
            from vcstudio.project.research_views import ResearchViewStore

            self._research_view_store = ResearchViewStore()
        return self._research_view_store

    def _research_source_version(self, records):
        """Fingerprint source-file versions without returning their locators."""
        rows = []
        for record in records:
            project_id = str(record.get('project_id') or '')
            project = record.get('project') or {}
            candidates = [('project.yaml', record.get('path'))]
            for member in self._project_member_dirs(project):
                candidates.extend((name, os.path.join(member, name)) for name in (
                    'job.yaml', 'INCAR', 'POSCAR', 'KPOINTS', 'POTCAR',
                    'OSZICAR', 'OUTCAR', 'vasprun.xml', 'validation.json',
                    'validation.yaml'))
            reference_jobs = project.get('species_ref_jobs') or {}
            if isinstance(reference_jobs, dict):
                for member in reference_jobs.values():
                    candidates.extend((name, os.path.join(str(member), name)) for name in (
                        'job.yaml', 'INCAR', 'POSCAR', 'KPOINTS', 'POTCAR',
                        'OSZICAR', 'OUTCAR', 'vasprun.xml', 'validation.json',
                        'validation.yaml'))
            seen = set()
            member_index = 0
            for kind, raw_path in candidates:
                if not raw_path:
                    continue
                try:
                    path = self._canonical_project_path(raw_path)
                except Exception:                         # noqa: BLE001 unreadable source
                    continue
                key = self._workspace_path_key(path)
                if key in seen:
                    continue
                seen.add(key)
                member_index += 1
                try:
                    stat = os.stat(path)
                    rows.append({
                        'project_id': project_id, 'source_index': member_index,
                        'kind': kind, 'size': int(stat.st_size),
                        'mtime_ns': int(stat.st_mtime_ns), 'available': True,
                    })
                except OSError:
                    rows.append({
                        'project_id': project_id, 'source_index': member_index,
                        'kind': kind, 'size': None, 'mtime_ns': None,
                        'available': False,
                    })
        return rows

    def _research_authority(self):
        snapshot = self._project_registry_snapshot()
        failures = copy.deepcopy(snapshot['failures'])
        failures.extend({
            'project_ref': f'ambiguous-project-{index}',
            'code': 'duplicate_project_id',
            'message': 'Registered project identity is ambiguous.',
        } for index, _project_id in enumerate(
            sorted(snapshot['duplicate_ids']), start=1))
        records = [
            record for record in snapshot['records']
            if record['project_id'] not in snapshot['duplicate_ids']
        ]
        return snapshot, records, failures, self._research_source_version(records)

    @staticmethod
    def _research_report_revision_id(project):
        marker = project.get('autopilot_report') if isinstance(project, dict) else None
        if not isinstance(marker, dict):
            return ''
        revision = marker.get('revision')
        revision = revision if isinstance(revision, dict) else {}
        return str(marker.get('revision_id') or revision.get('revision_id') or '').strip()

    def _research_frozen_context(self, target, cache):
        """Revalidate one current frozen revision and its separate evidence graph."""
        project = target.get('project') or {}
        record = target.get('record') or {}
        revision_id = self._research_report_revision_id(project)
        key = (str(record.get('path') or ''), revision_id)
        if key in cache:
            return cache[key]
        cache[key] = None
        if (not revision_id
                or not self._report_marker_current(project, target.get('summary'))):
            return None
        try:
            from vcstudio.project.report_contracts import (
                ReportSnapshot,
                ReportSpec,
                ValidationResult,
                validate_bindings,
            )
            from vcstudio.project.report_insights import (
                evidence_graph,
                load_frozen_revision,
            )

            reports = self._reports()
            bundle = load_frozen_revision(reports, record['path'], revision_id)
            spec = ReportSpec.from_mapping(bundle.spec)
            snapshot = ReportSnapshot.from_mapping(bundle.snapshot, spec=spec)
            validation = ValidationResult.from_mapping(
                bundle.validation, spec=spec, snapshot=snapshot)
            validate_bindings(spec, snapshot, validation)
            if (validation.status not in {'passed', 'passed_with_warnings'}
                    or validation.scientific_qualification == 'diagnostic'):
                return None
            graph = evidence_graph(reports, record['path'], revision_id)
            if not isinstance(graph, dict) or graph.get('ok') is not True:
                return None
            cache[key] = {
                'bundle': bundle,
                'validation': validation,
                'graph': graph,
            }
            return cache[key]
        except Exception:                               # noqa: BLE001 fail closed
            return None

    @staticmethod
    def _research_frozen_members(context):
        summary = ((context['bundle'].snapshot.get('payload') or {})
                   .get('adsorption_summary') or {})
        members = {}
        for item in summary.get('members') or []:
            if isinstance(item, dict) and item.get('member_id'):
                members[str(item['member_id'])] = item
        return summary, members

    @staticmethod
    def _research_graph_job_ids(context):
        return {
            str((node.get('record') or {}).get('job_id') or '')
            for node in context['graph'].get('nodes') or []
            if isinstance(node, dict) and node.get('type') == 'job'
        }

    def _research_current_member_hashes(self, target):
        current = {}
        for path in self._project_member_dirs(target.get('project') or {}):
            manifest = self._manifest.load_manifest(path) or {}
            job_id = self._workspace_job_id(path, manifest)
            results = manifest.get('results') or {}
            hashes = results.get('fetched_sha256') or {}
            if not isinstance(hashes, dict) or not hashes:
                current[job_id] = {}
                continue
            root = os.path.realpath(os.path.abspath(str(path)))
            verified = {}
            for name, expected in hashes.items():
                filename = str(name or '').replace('\\', '/')
                parts = filename.split('/')
                digest = str(expected or '').lower()
                if (not filename or any(part in {'', '.', '..'} for part in parts)
                        or not re.fullmatch(r'[0-9a-f]{64}', digest)):
                    verified = {}
                    break
                output = os.path.realpath(os.path.join(root, *parts))
                try:
                    if (os.path.commonpath((root, output)) != root
                            or not os.path.isfile(output)
                            or _sha256_file(output) != digest):
                        verified = {}
                        break
                except (OSError, ValueError):
                    verified = {}
                    break
                verified[filename] = digest
            current[job_id] = verified
        return current

    def _research_validation_evidence(self, target, cache):
        context = self._research_frozen_context(target, cache)
        if context is None:
            return {}
        job_id = str(target.get('job_id') or '')
        summary, members = self._research_frozen_members(context)
        member = members.get(job_id) or {}
        current_members = self._research_current_member_hashes(target)
        output_hash_bound = bool(members) and all(
            bool(current_members.get(member_id))
            and current_members[member_id] == (frozen.get('fetched_sha256') or {})
            for member_id, frozen in members.items()
        )
        if (job_id not in self._research_graph_job_ids(context)
                or not output_hash_bound):
            return {}
        current_row = target.get('summary_row') or {}
        verified_quantities = {}
        if current_row.get('delta_e') is not None:
            frozen_row = next((
                row for row in summary.get('rows') or []
                if isinstance(row, dict)
                and str(row.get('configuration_id') or row.get('job_id') or '')
                == job_id
            ), None)
            if (not frozen_row or frozen_row.get('reference_valid') is not True
                    or frozen_row.get('delta_e') != current_row.get('delta_e')):
                return {}
            verified_quantities['energy_eV'] = (
                (target.get('quantity_sha256') or {}).get('energy_eV'))
        else:
            current_energy = (target.get('energy') or {}).get('energy_eV')
            if (current_energy is not None
                    and member.get('energy_e0_eV') == current_energy):
                verified_quantities['energy_eV'] = (
                    (target.get('quantity_sha256') or {}).get('energy_eV'))
        if (target.get('barrier_eV') is not None
                and 'barrier_eV' in member
                and member.get('barrier_eV') == target.get('barrier_eV')):
            verified_quantities['barrier_eV'] = (
                (target.get('quantity_sha256') or {}).get('barrier_eV'))
        verified_quantities = {
            key: value for key, value in verified_quantities.items() if value
        }
        return {
            'authority': 'validation_result',
            'status': 'verified',
            'hash_bound': True,
            'current': True,
            'output_hash_bound': True,
            'job_id': job_id,
            'source_id': str(target.get('source_id') or ''),
            'reference_mode': summary.get('reference_mode'),
            'verified_quantities': verified_quantities,
        }

    def _research_report_binding(self, target, cache):
        context = self._research_frozen_context(target, cache)
        if context is None:
            return {}
        summary, members = self._research_frozen_members(context)
        rows = {
            str(item.get('configuration_id') or item.get('job_id') or ''): item
            for item in summary.get('rows') or [] if isinstance(item, dict)
        }
        graph_jobs = self._research_graph_job_ids(context)
        current_members = self._research_current_member_hashes(target)
        # The frozen graph must expose the exact frozen adsorption evidence,
        # not only a revision label or a raw project marker.
        graph_has_summary = any(
            isinstance(node, dict)
            and node.get('type') == 'snapshot_record'
            and (node.get('record') or {}).get('pointer')
            == 'snapshot:payload/adsorption_summary'
            for node in context['graph'].get('nodes') or []
        )
        if not graph_has_summary:
            return {}
        bound = []
        for entry in target.get('entries') or []:
            if (entry.get('role') != 'configuration'
                    or entry.get('energy_origin') != 'analysis'):
                continue
            row = rows.get(str(entry.get('job_id') or ''))
            if (not row or row.get('reference_valid') is not True
                    or row.get('delta_e') != entry.get('energy_eV')):
                continue
            operand_jobs = {
                str(operand.get('job_id') or '')
                for operand in entry.get('_operands') or []
                if isinstance(operand, dict) and operand.get('job_id')
            }
            if (len(operand_jobs) != 3 or not operand_jobs.issubset(graph_jobs)
                    or not operand_jobs.issubset(members)):
                continue
            output_bound = True
            for operand_id in operand_jobs:
                frozen_hashes = (members.get(operand_id) or {}).get('fetched_sha256') or {}
                if (not current_members.get(operand_id)
                        or current_members[operand_id] != frozen_hashes):
                    output_bound = False
                    break
            if not output_bound:
                continue
            bound.append({
                'job_id': entry['job_id'],
                'source_id': entry['source_id'],
                'quantity': 'energy_eV',
                'energy_contract_id': entry['energy_contract_id'],
                'quantity_sha256': (entry.get('_quantity_sha256') or {})['energy_eV'],
            })
        if not bound:
            return {}
        return {
            'revision_id': context['bundle'].revision_id,
            'current': True,
            'frozen_graph_revalidated': True,
            'bound_job_ids': sorted(graph_jobs),
            'bound_analyses': bound,
        }

    def _research_prepare(self, *, force=False):
        with self._research_index_lock:
            snapshot, records, failures, source_version = self._research_authority()
            service = self._research_index()
            fingerprint = service.source_fingerprint(
                records, registry_total=snapshot['registered_total'],
                registry_failures=failures, source_version=source_version)
            current = service.index_status()
            if (force or current.get('status') in {'unavailable', 'stale'}
                    or current.get('source_fingerprint') != fingerprint):
                frozen_cache = {}
                service.rebuild(
                    records,
                    manifest_loader=self._manifest.load_manifest,
                    summary_loader=self._adsorption.delta_e_rows,
                    job_id_resolver=self._workspace_job_id,
                    method_resolver=self._analysis_workbench_method_evidence,
                    validation_resolver=lambda target: (
                        self._research_validation_evidence(target, frozen_cache)),
                    report_binding_resolver=lambda target: (
                        self._research_report_binding(target, frozen_cache)),
                    registry_total=snapshot['registered_total'],
                    registry_failures=failures,
                    source_version=source_version,
                )
            return service

    @classmethod
    def _research_failure(cls, exc, *, schema='vcstudio.research-query/v1'):
        return cls._analysis_workbench_public_value({
            'ok': False, 'schema': schema, 'status': 'unavailable',
            'error': str(exc),
        })

    def research_explorer_query(self, request=None):
        try:
            result = self._research_prepare().query(request)
            return self._analysis_workbench_public_value(result)
        except Exception as exc:                         # noqa: BLE001 public bridge
            return self._research_failure(exc)

    def research_explorer_rebuild(self, request=None):
        """Explicitly rebuild the derived index; never mutate scientific sources."""
        try:
            result = self._research_prepare(force=True).query(request)
            result['rebuild'] = {
                'performed': True, 'fact_source_changed': False,
                'remote_side_effects': False,
            }
            return self._analysis_workbench_public_value(result)
        except Exception as exc:                         # noqa: BLE001 public bridge
            return self._research_failure(exc)

    def research_explorer_bootstrap(self):
        try:
            result = self._research_prepare().query()
            result['saved_views'] = self._research_views().read()
            result['contracts'] = {
                'index_is_authority': False,
                'scientific_values_server_finalized': True,
                'default_method_compatible': True,
                'live_graph_kind': 'live_derived',
                'frozen_report_graph_separate': True,
            }
            return self._analysis_workbench_public_value(result)
        except Exception as exc:                         # noqa: BLE001 public bridge
            return self._research_failure(exc)

    def research_explorer_provenance(self, project_id, job_id=None, source_id=None):
        try:
            record = self._resolve_project_id(project_id)

            def build():
                return self._research_index().live_provenance(
                    record['request_project_id'], job_id=job_id, source_id=source_id)

            self._research_prepare()
            result = self._call_with_project_bindings([record], build)
            return self._analysis_workbench_public_value(result)
        except Exception as exc:                         # noqa: BLE001 public bridge
            return self._research_failure(
                exc, schema='vcstudio.live-provenance/v1')

    def research_views_read(self):
        try:
            return self._analysis_workbench_public_value(self._research_views().read())
        except Exception as exc:                         # noqa: BLE001 public bridge
            return self._research_failure(
                exc, schema='vcstudio.research-views/v1')

    def research_view_save(self, view, authority_id, expected_revision):
        try:
            result = self._research_views().save(
                view, authority_id=authority_id,
                expected_revision=expected_revision)
            return self._analysis_workbench_public_value(result)
        except Exception as exc:                         # noqa: BLE001 public bridge
            return self._research_failure(
                exc, schema='vcstudio.research-views/v1')

    def research_view_delete(self, view_id, authority_id, expected_revision):
        try:
            result = self._research_views().delete(
                view_id, authority_id=authority_id,
                expected_revision=expected_revision)
            return self._analysis_workbench_public_value(result)
        except Exception as exc:                         # noqa: BLE001 public bridge
            return self._research_failure(
                exc, schema='vcstudio.research-views/v1')

    def _analysis_workbench_current_project(self, context):
        project = context['project']
        members = self._project_member_dirs(project)
        return {
            'project_id': context['project_id'],
            'name': context['project_name'],
            'n_members': len(members),
            'n_done': sum(
                1 for member in members
                if ((self._manifest.load_manifest(member) or {}).get('state')
                    == 'DONE')),
        }

    @staticmethod
    def _analysis_workbench_reject_duplicate_ids(request):
        if not isinstance(request, dict):
            return
        raw = request.get('comparison_project_ids')
        if not isinstance(raw, (list, tuple)):
            return
        normalized = [str(item) for item in raw]
        if len(normalized) != len(set(normalized)):
            raise ValueError('comparison_project_ids 不得包含重复项目身份')

    @staticmethod
    def _analysis_workbench_unavailable_view(
            spec, reason, *, capability_status='unavailable', next_action=None):
        """Describe a registered capability without inventing parsed values."""
        message = str(reason or '该分析尚未接入实时解析器')
        payload = {
            'schema': 'vcstudio.analysis-view/v1',
            'analysis_id': spec.analysis_id,
            'spec': spec.to_dict(),
            'spec_sha256': spec.semantic_sha256,
            'scientific_status': 'unavailable',
            'capability_status': capability_status,
            'available': False,
            'rows': [],
            'missing': ([message] if capability_status == 'missing_prerequisite' else []),
            'blocking': [message],
            'warnings': [],
            'reason': message,
            'next_action': str(next_action or 'Complete the listed prerequisite and refresh.'),
            'denominator': {
                'available_results': 0,
                'visible_rows': 0,
            },
        }
        encoded = json.dumps(
            {'spec_sha256': spec.semantic_sha256,
             'scientific_status': 'unavailable', 'reason': message},
            ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode('utf-8')
        payload['data_fingerprint'] = hashlib.sha256(encoded).hexdigest()
        return payload

    def _analysis_workbench_targets(self, context):
        """Resolve only server-ledger members and manifest-linked descendants."""
        from vcstudio.project.analysis_sources import resolve_project_targets

        try:
            ledger_entries = list(self._ledger.load_all())
        except Exception:                                 # noqa: BLE001 fail closed
            ledger_entries = []
        return resolve_project_targets(
            context['project'], ledger_entries,
            manifest_loader=self._manifest.load_manifest,
            opaque_id=lambda path, manifest: self._workspace_job_id(path, manifest),
        )

    @staticmethod
    def _analysis_workbench_parser_identities():
        from vcstudio import __version__
        from vcstudio.project import task_analysis

        routes = {
            'dos_pdos': ('vcstudio.project.task_analysis', 'analyze_dos'),
            'bands': ('vcstudio.project.bands', 'parse_bands'),
            'workfunction': ('vcstudio.project.workfunction', 'work_function'),
            'bader': ('vcstudio.project.task_analysis', 'analyze_bader'),
            'chgdiff': ('vcstudio.project.task_analysis', 'analyze_chgdiff'),
            'elf': ('vcstudio.project.elf', 'summarize_elfcar'),
        }
        route_callables = {
            'generic': 'analyze_vasp_outputs', 'freq': 'analyze_frequency',
            'aimd': 'analyze_aimd', 'neb': 'analyze_neb',
            'artifact': 'inspect_artifacts', 'dos': 'analyze_dos',
            'bader': 'analyze_bader', 'chgdiff': 'analyze_chgdiff',
            'elf': 'analyze_elf',
        }
        for task_key in task_analysis.capability_matrix():
            if task_key in routes:
                continue
            cap = task_analysis.capability(task_key)
            callable_name = route_callables.get(cap.get('route'))
            routes[task_key] = (
                'vcstudio.project.task_analysis' if callable_name else '',
                callable_name or '',
            )
        return {
            key: {'module': module, 'callable': callable_name,
                  'version': str(__version__) if module and callable_name else '',
                  'version_source': ('vcstudio.__version__'
                                     if module and callable_name else '')}
            for key, (module, callable_name) in routes.items()
        }

    @staticmethod
    def _analysis_workbench_method_evidence(target):
        """Require a complete, non-drifted method identity before parsing."""
        from vcstudio.project import energy_gate

        manifest = target.get('manifest') or {}
        inputs = manifest.get('inputs') or {}
        engine = str(inputs.get('engine') or 'vasp').strip().lower()
        if engine != 'vasp':
            method = inputs.get('method') or inputs.get('method_fingerprint')
            if method:
                return {'status': 'verified', 'engine': engine,
                        'identity': copy.deepcopy(method), 'missing': []}
            return {'status': 'unverified', 'engine': engine,
                    'missing': ['non-VASP manifest lacks method identity']}
        record = energy_gate.method_record(
            target['path'], manifest, str(target.get('source_id') or 'job'))
        required = ('functional', 'dispersion', 'encut', 'spin',
                    'kpoints_scheme', 'potcar_ids')
        missing = [key for key in required if not record['known'].get(key)]
        warnings = list(record.get('evidence_warnings') or [])
        return {
            'status': 'verified' if not missing and not warnings else 'unverified',
            'fingerprint': copy.deepcopy(record.get('fingerprint') or {}),
            'missing': missing, 'warnings': warnings,
        }

    def _analysis_workbench_property_results(self, kind, targets):
        """Run calculators from manifest-bound server operands only."""
        by_key = {target['path_key']: target for target in targets}
        by_id = {str(target['source_id']): target for target in targets}

        def resolve(value):
            if not value:
                return None
            return by_id.get(str(value)) or by_key.get(
                self._analysis_workbench_path_key(value))

        if kind == 'vaspsol':
            from vcstudio.project import vaspsol

            pairs = {}
            for target in targets:
                manifest = target.get('manifest') or {}
                meta = manifest.get('vaspsol') or (manifest.get('inputs') or {}).get('vaspsol') or {}
                pair_id = str(meta.get('pair_id') or '')
                role = str(meta.get('role') or '')
                if target.get('task_type') == 'vaspsol' and pair_id and role in {'vacuum', 'solvent'}:
                    pairs.setdefault(pair_id, {})[role] = target
            results = []
            for pair in pairs.values():
                if set(pair) != {'vacuum', 'solvent'}:
                    continue
                source_ids = [pair['vacuum']['source_id'], pair['solvent']['source_id']]
                try:
                    result = vaspsol.analyze_pair(
                        pair['vacuum']['path'], pair['solvent']['path'])
                    results.append({
                        'ok': True, 'source_ids': source_ids,
                        'solvation_energy_eV': result['solvation_energy_eV'],
                        'e_vacuum_eV': result['e_vacuum_eV'],
                        'e_solvent_eV': result['e_solvent_eV'],
                        'eb_k': result.get('eb_k'),
                        'patch_evidence': result.get('patch_evidence'),
                        'method_signature': result.get('method_signature'),
                        'hashes': result.get('hashes'),
                        'parser_module': 'vcstudio.project.vaspsol',
                        'parser_callable': 'analyze_pair',
                        'summary': 'VASPsol solvent minus vacuum energy.',
                    })
                except Exception as exc:                  # noqa: BLE001 fail closed
                    results.append({'ok': False, 'source_ids': source_ids,
                                    'error': str(exc)})
            return results

        results = []
        for owner in targets:
            if owner.get('task_type') != kind:
                continue
            manifest = owner.get('manifest') or {}
            inputs = manifest.get('inputs') or {}
            operands = inputs.get('analysis_operands') or inputs.get('operands') or {}
            if not isinstance(operands, dict):
                operands = {}
            if kind == 'surface_energy':
                slab = resolve(operands.get('slab_job'))
                bulk = resolve(operands.get('bulk_job'))
                if not slab or not bulk:
                    results.append({'ok': False, 'source_ids': [owner['source_id']],
                                    'error': 'surface-energy manifest lacks resolved slab_job/bulk_job operands'})
                    continue
                result = self._surface_energy_calc(
                    slab['path'], bulk['path'], write_report=False)
                results.append({
                    **dict(result or {}),
                    'source_ids': [owner['source_id'], slab['source_id'], bulk['source_id']],
                    'parser_module': 'vcstudio.project.surface_energy',
                    'parser_callable': 'surface_energy',
                    'summary': (result or {}).get('note') or (result or {}).get('error'),
                })
                continue

            sac = resolve(operands.get('sac_job'))
            substrate = resolve(operands.get('substrate_job'))
            atom_jobs = operands.get('atom_energy_jobs') or {}
            chemical_jobs = operands.get('chemical_potential_jobs') or {}
            if not sac or not substrate or not isinstance(atom_jobs, dict) or not isinstance(chemical_jobs, dict):
                results.append({'ok': False, 'source_ids': [owner['source_id']],
                                'error': 'formation/binding manifest lacks resolved SAC/reference operands'})
                continue
            atom_targets = {key: resolve(value) for key, value in atom_jobs.items()}
            chemical_targets = {key: resolve(value) for key, value in chemical_jobs.items()}
            referenced = [sac, substrate, *atom_targets.values(), *chemical_targets.values()]
            if any(target is None for target in referenced):
                results.append({'ok': False, 'source_ids': [owner['source_id']],
                                'error': 'formation/binding manifest references a job outside the project ledger chain'})
                continue
            try:
                from vcstudio.project import energy_gate

                records, energies = [], {}
                for target in referenced:
                    energy, source_manifest, _notes = energy_gate.validate_done_energy(
                        target['path'], target['source_id'], self._manifest)
                    records.append(energy_gate.method_record(
                        target['path'], source_manifest, target['source_id']))
                    energies[target['source_id']] = energy
                method = energy_gate.compare_methods(records, require_same_kpoints=False)
                if method['status'] != 'verified':
                    raise ValueError('formation/binding method evidence is not verified: ' +
                                     '; '.join(method['issues'] + method['warnings']))
                atom_energies = {
                    key: energies[target['source_id']]
                    for key, target in atom_targets.items()
                }
                chem_pots = {
                    key: energies[target['source_id']]
                    for key, target in chemical_targets.items()
                }
                result = self._formation_binding_calc(
                    sac['path'], substrate['path'], atom_energies, chem_pots,
                    write_report=False)
                results.append({
                    **dict(result or {}), 'method_check_all_operands': method,
                    'binding_energy_eV': (result or {}).get('binding_energy'),
                    'formation_energy_eV': (result or {}).get('formation_energy'),
                    'source_ids': [owner['source_id'], *energies],
                    'parser_module': 'vcstudio.project.references',
                    'parser_callable': 'binding_energy/formation_energy',
                    'summary': (result or {}).get('stability_note') or (result or {}).get('error'),
                })
            except Exception as exc:                      # noqa: BLE001 fail closed
                results.append({'ok': False, 'source_ids': [owner['source_id']],
                                'error': str(exc)})
        return results

    def _analysis_workbench_validation_result(self, context):
        """Load one current server-authored ValidationResult sidecar."""
        from vcstudio.project.report_contracts import ValidationResult

        project = context['project']
        marker = project.get('autopilot_report')
        if not isinstance(marker, dict):
            return None, 'No registered report ValidationResult is available.'
        try:
            summary = self._adsorption.delta_e_rows(project)
            if not self._report_marker_current(project, summary):
                return None, 'The registered report validation is stale for current project evidence.'
            files = marker.get('files') or {}
            if not isinstance(files, dict):
                return None, 'The registered report marker has no validated contract files.'
            path = str(files.get('contract_validation') or '')
            if not path or not os.path.isfile(path):
                return None, 'The registered ValidationResult sidecar is unavailable.'
            with open(path, 'r', encoding='utf-8') as handle:
                validation = ValidationResult.from_mapping(json.load(handle))
            refs = marker.get('contracts') or {}
            expected = str((refs.get('validation') or {}).get('sha256') or '')
            if not expected or expected != validation.semantic_sha256:
                return None, 'ValidationResult does not match the registered contract hash.'
            return validation, None
        except Exception as exc:                          # noqa: BLE001 fail closed
            return None, f'ValidationResult could not be revalidated: {exc}'

    def _analysis_workbench_next_calculation(self, context, view):
        from vcstudio.project.next_calculation import build_recommendations

        validation, reason = self._analysis_workbench_validation_result(context)
        return build_recommendations(
            view, validation, validation_error=reason)

    def _analysis_workbench_capability_cards(self, context, projects, selected_view):
        """Project-specific activation states for every registry card."""
        targets = self._analysis_workbench_targets(context)
        task_types = {str(target.get('task_type') or '') for target in targets}
        project = context['project']
        preparation = project.get('preparation')
        mode = str(project.get('work_mode') or
                   (preparation.get('work_mode') if isinstance(preparation, dict) else '')
                   or '').strip().lower()
        charge_status = 'missing_prerequisite'
        if task_types & {'bader', 'chgdiff'}:
            charge_status = 'available'
        elif 'elf' in task_types:
            elf_targets = [target for target in targets if target.get('task_type') == 'elf']
            elf_ready = [target for target in elf_targets
                         if target.get('state') == 'DONE'
                         and os.path.isfile(os.path.join(target['path'], 'ELFCAR'))]
            if elf_ready:
                charge_status = (
                    'available' if any(
                        self._analysis_workbench_method_evidence(target).get('status') ==
                        'verified' for target in elf_ready)
                    else 'unavailable')
        states = {
            'adsorption-energy': (
                'available' if self._project_member_dirs(project) else 'missing_prerequisite'),
            'free-energy-path': (
                'available' if mode == 'lis' else 'mode_mismatch'),
            'task-results': ('available' if targets else 'missing_prerequisite'),
            'electronic-structure': (
                'available' if task_types & {'dos_pdos', 'bands', 'workfunction'}
                else 'missing_prerequisite'),
            'charge-wavefunction': charge_status,
            'multi-project-comparison': (
                'available' if len(projects) >= 2 else 'missing_prerequisite'),
            'property-calculators': (
                'available' if task_types & {'surface_energy', 'formation_binding', 'vaspsol'}
                else 'missing_prerequisite'),
        }
        selected_id = str((selected_view or {}).get('analysis_id') or '')
        selected_status = str((selected_view or {}).get('capability_status') or '')
        if selected_id and selected_status:
            states[selected_id] = selected_status
        actions = {
            'available': 'Open this analysis and inspect its server-finalized evidence.',
            'missing_prerequisite': 'Create or complete the required registered job, then refresh.',
            'mode_mismatch': 'Switch to the required explicit project mode, then refresh.',
            'not_implemented': 'Use a registered quantitative parser; artifact presence alone is not a result.',
            'unavailable': 'Restore the parser/version dependency, then refresh.',
        }
        return {
            analysis_id: {'status': status, 'activatable': status == 'available',
                          'next_action': actions[status]}
            for analysis_id, status in states.items()
        }

    def _analysis_workbench_build(self, path, request=None, *,
                                  default_analysis_id='adsorption-energy'):
        from vcstudio.project.analysis_registry import normalize_analysis_request
        from vcstudio.project.analysis_views import (
            build_adsorption_view,
            build_comparison_view,
            build_free_energy_view,
        )
        from vcstudio.project.analysis_sources import (
            build_property_view,
            build_task_analysis_view,
        )

        context = self._report_workbench_project_context(path)
        prepared = {} if request is None else copy.deepcopy(request)
        if not isinstance(prepared, dict):
            raise TypeError('analysis request 必须为对象')
        self._analysis_workbench_reject_duplicate_ids(prepared)
        spec = normalize_analysis_request(
            prepared,
            project_id=context['project_id'],
            default_analysis_id=default_analysis_id,
        )
        if spec.analysis_id == 'adsorption-energy':
            from vcstudio.project.report_contracts import ReportSpec

            identity_spec = ReportSpec(
                requested_kind='diagnostic', formats=('html',),
                scope={
                    'kind': 'project',
                    'project_ids': [context['project_id']],
                    'job_ids': [], 'species': [], 'configuration_ids': [],
                    'stable_only': False, 'include_failed': True,
                })
            summary = self._report_workbench_scope_summary(
                context, self._adsorption.delta_e_rows(context['project']),
                identity_spec)
            view = build_adsorption_view(summary, spec)
        elif spec.analysis_id == 'multi-project-comparison':
            comparison_ids = list(spec.comparison_project_ids)
            if len(comparison_ids) < 2:
                raise ValueError('多项目比较至少需要两个不同的项目身份')
            if len(comparison_ids) != len(set(comparison_ids)):
                raise ValueError('多项目比较不得包含重复项目身份')
            index, _safe_projects = self._analysis_workbench_project_index()
            unresolved = [item for item in comparison_ids if item not in index]
            if unresolved:
                raise ValueError(
                    '无法从项目注册表解析 comparison_project_ids：'
                    + '、'.join(unresolved))
            current_registry_path = index.get(context['project_id'])
            if (not current_registry_path
                    or self._analysis_workbench_path_key(current_registry_path)
                    != self._analysis_workbench_path_key(context['project_path'])):
                raise ValueError('当前项目与项目注册表 opaque 身份绑定不一致')
            paths = [index[item] for item in comparison_ids]
            items = self._comparison_items(paths)
            if len(items) != len(comparison_ids):
                raise RuntimeError('比较项目解析数量与冻结 spec 不一致')
            resolved_items = []
            for project_id, item in zip(comparison_ids, items):
                if not isinstance(item, dict) or item.get('project') is None:
                    raise ValueError(f'比较项目不可解析：{project_id}')
                resolved = copy.deepcopy(item)
                resolved['project_id'] = project_id
                resolved_items.append(resolved)
            view = build_comparison_view(resolved_items, spec)
        elif spec.analysis_id == 'free-energy-path':
            project = context['project']
            preparation = project.get('preparation')
            declared_mode = str(
                project.get('work_mode') or
                (preparation.get('work_mode')
                 if isinstance(preparation, dict) else '') or '').strip().lower()
            if declared_mode != 'lis':
                view = self._analysis_workbench_unavailable_view(
                    spec, '该自由能分析只适用于显式 Li-S 工作模式',
                    capability_status='mode_mismatch',
                    next_action='将项目工作模式显式设为 Li-S，并完成反应路径证据。')
            else:
                summary = self._adsorption.delta_e_rows(project)
                fed, fed_reason = self._proj_fed(project, summary)
                frozen = {
                    'result': copy.deepcopy(fed),
                    'missing': ([str(fed_reason)] if fed_reason else []),
                    'method_consistency': copy.deepcopy(
                        (summary or {}).get('method_consistency') or {}),
                }
                view = build_free_energy_view(frozen, spec)
        elif spec.analysis_id in {
                'electronic-structure', 'charge-wavefunction', 'task-results'}:
            targets = self._analysis_workbench_targets(context)
            view = build_task_analysis_view(
                spec, targets,
                runner=lambda target_path, kind: self.analyze_task(
                    target_path, kind=kind),
                parser_identities=self._analysis_workbench_parser_identities(),
                method_evidence=self._analysis_workbench_method_evidence,
            )
        elif spec.analysis_id == 'property-calculators':
            from vcstudio import __version__

            targets = self._analysis_workbench_targets(context)
            view = build_property_view(
                spec, targets,
                runner=self._analysis_workbench_property_results,
                parser_version=str(__version__),
            )
        else:
            view = self._analysis_workbench_unavailable_view(
                spec, f'分析 {spec.analysis_id} 已注册，但实时解析器尚未接入')
        view = dict(view)
        view['next_calculation'] = self._analysis_workbench_next_calculation(
            context, view)
        return context, spec, view

    def analysis_workbench_catalog(self):
        try:
            from vcstudio.project.analysis_registry import analysis_catalog

            catalog = self._analysis_workbench_public_value(analysis_catalog())
            return {'ok': True, **catalog, 'error': None}
        except Exception as exc:                         # noqa: BLE001 public bridge
            return {
                'ok': False, 'schema': 'vcstudio.analysis-catalog/v1',
                'analyses': [], 'categories': [], 'task_capabilities': [],
                'view_templates': [],
                'error': self._analysis_workbench_public_value(str(exc)),
            }

    def _analysis_workbench_project_path(self, project_id):
        return self._analysis_workbench_project_record(project_id)['path']

    def _analysis_workbench_project_record(self, project_id):
        return self._resolve_project_id(project_id)

    @classmethod
    def _analysis_workbench_identity_failure(cls, *, field):
        """Return a stable, locator-free invalid-identity response."""
        return {
            'ok': False, 'schema': cls._ANALYSIS_PREVIEW_SCHEMA,
            'project_id': None, 'spec': None, 'view': None,
            'error_code': 'invalid_project_identity', 'error_field': field,
            'error': f'{field} must contain registered opaque project identities.',
        }

    def analysis_workbench_preview(self, project_id, request=None):
        try:
            record = self._analysis_workbench_project_record(project_id)
        except (LookupError, ValueError):
            return self._analysis_workbench_identity_failure(field='project_id')
        try:
            records = self._project_records_for_request(record, request)
        except (LookupError, ValueError):
            field = ('comparison_project_ids' if isinstance(request, dict)
                     and isinstance(request.get('comparison_project_ids'),
                                    (list, tuple)) else 'project_ids')
            return self._analysis_workbench_identity_failure(field=field)

        try:

            def build():
                context, spec, view = self._analysis_workbench_build(
                    record['path'], request)
                return self._analysis_workbench_public_value({
                    'ok': True, 'schema': self._ANALYSIS_PREVIEW_SCHEMA,
                    'project_id': context['project_id'], 'spec': spec.to_dict(),
                    'view': view, 'error': None,
                })

            return self._call_with_project_bindings(records, build)
        except Exception as exc:                         # noqa: BLE001 public bridge
            return {
                'ok': False, 'schema': self._ANALYSIS_PREVIEW_SCHEMA,
                'project_id': None, 'spec': None, 'view': None,
                'error': self._analysis_workbench_public_value(str(exc)),
            }

    def analysis_workbench_next_intent(
            self, project_id, request, recommendation_id, confirmed=False):
        """Create a non-executable draft; this seam never calls submission APIs."""
        try:
            from vcstudio.project.next_calculation import draft_intent

            record = self._analysis_workbench_project_record(project_id)
            records = self._project_records_for_request(record, request)

            def build():
                context, _spec, view = self._analysis_workbench_build(
                    record['path'], request)
                recommendations = view.get('next_calculation') or {}
                draft = draft_intent(
                    recommendations, str(recommendation_id or ''),
                    confirmed=confirmed is True)
                return self._analysis_workbench_public_value({
                    'ok': True, 'project_id': context['project_id'],
                    'draft': draft, 'error': None,
                })

            return self._call_with_project_bindings(records, build)
        except Exception as exc:                          # noqa: BLE001 public bridge
            return {
                'ok': False, 'project_id': None, 'draft': None,
                'error': self._analysis_workbench_public_value(str(exc)),
            }

    def analysis_workbench_bootstrap(self, project_id, analysis_id=None):
        try:
            from vcstudio.project.analysis_registry import get_analysis

            selected_analysis = str(analysis_id or 'adsorption-energy').strip()
            get_analysis(selected_analysis)
            anchor = self._analysis_workbench_project_record(project_id)
            records = self._project_records_for_request(
                anchor, include_registry=(
                    selected_analysis == 'multi-project-comparison'))

            def build():
                path = anchor['path']
                context = self._report_workbench_project_context(path)
                index, projects = self._analysis_workbench_project_index()
                request = {'analysis_id': selected_analysis}
                if selected_analysis == 'multi-project-comparison':
                    if context['project_id'] not in index:
                        raise ValueError(
                            '当前项目不在项目注册表中，不能建立多项目比较')
                    other_ids = [
                        item['project_id'] for item in projects
                        if item['project_id'] != context['project_id']]
                    if not other_ids:
                        raise ValueError('多项目比较至少需要两个可解析项目')
                    request['comparison_project_ids'] = [
                        context['project_id'], other_ids[0]]
                built_context, spec, view = self._analysis_workbench_build(
                    path, request, default_analysis_id=selected_analysis)
                catalog_result = self.analysis_workbench_catalog()
                if catalog_result.get('ok') is not True:
                    raise RuntimeError(
                        catalog_result.get('error') or '分析目录不可用')
                catalog = {
                    key: value for key, value in catalog_result.items()
                    if key not in {'ok', 'error'}
                }
                cards = self._analysis_workbench_capability_cards(
                    context, projects, view)
                for item in catalog.get('analyses') or []:
                    card = cards.get(str(item.get('id') or ''), {})
                    item['capability_status'] = card.get(
                        'status', 'unavailable')
                    item['activatable'] = card.get('activatable') is True
                    item['next_action'] = str(
                        card.get('next_action') or item.get('next_action') or '')
                preferences = self.analysis_preferences_read()
                return self._analysis_workbench_public_value({
                    'ok': True, 'schema': self._ANALYSIS_BOOTSTRAP_SCHEMA,
                    'project_id': built_context['project_id'],
                    'project': self._analysis_workbench_current_project(context),
                    'catalog': catalog, 'default_spec': spec.to_dict(),
                    'projects': projects, 'view': view, 'error': None,
                    'preferences': preferences,
                    'preference_revision': preferences.get('revision'),
                })

            return self._call_with_project_bindings(records, build)
        except Exception as exc:                         # noqa: BLE001 public bridge
            return {
                'ok': False, 'schema': self._ANALYSIS_BOOTSTRAP_SCHEMA,
                'project_id': None, 'project': None, 'catalog': None,
                'default_spec': None, 'projects': [], 'view': None,
                'preferences': None, 'preference_revision': None,
                'error': self._analysis_workbench_public_value(str(exc)),
            }

    def proj_report_capabilities(self):
        """Expose report-format dependencies before the user starts a long render."""
        def _fallback(reason):
            detail = str(reason or '报告格式能力探测不可用')
            return {
                'ok': True,
                'probe_ok': False,
                'degraded': True,
                'schema': 'vcstudio.paper-report.capabilities/v1',
                'formats': {
                    'html': {'available': True, 'reason': ''},
                    'docx': {'available': False, 'reason': detail},
                    'pdf': {'available': False, 'reason': detail},
                },
                'error': detail,
            }

        try:
            probe = getattr(self._paper(), 'report_capabilities', None)
            if not callable(probe):
                return _fallback('报告格式能力探测器缺失；仅启用核心 HTML')
            raw = self._json_safe_report_result(probe())
            if not isinstance(raw, dict):
                return _fallback('报告格式能力探测返回无效结果；仅启用核心 HTML')
            declared = raw.get('formats')
            if not isinstance(declared, dict):
                return _fallback('报告格式能力探测缺少 formats；仅启用核心 HTML')
            normalized = {}
            for fmt in _REPORT_FORMATS:
                record = declared.get(fmt)
                if not isinstance(record, dict):
                    normalized[fmt] = {
                        'available': fmt == 'html',
                        'reason': ('' if fmt == 'html' else
                                   f'能力探测未返回 {fmt} 状态'),
                    }
                    continue
                available = record.get('available') is True
                reason = str(record.get('reason') or '')
                if not available and not reason:
                    reason = f'{fmt} 报告依赖不可用或状态未知'
                normalized[fmt] = {'available': available, 'reason': reason}
            result = dict(raw)
            result.update({
                'ok': True,
                'probe_ok': True,
                'degraded': not all(
                    normalized[fmt]['available'] for fmt in _REPORT_FORMATS),
                'formats': normalized,
                'error': None,
            })
            return result
        except Exception as e:                            # noqa: BLE001
            return _fallback(f'报告格式能力探测失败：{e}；仅启用核心 HTML')

    def _available_report_formats(self) -> tuple[str, ...]:
        """Select every proven-available auto format; HTML is the hard baseline."""
        capabilities = self.proj_report_capabilities()
        declared = capabilities.get('formats') or {}
        html = declared.get('html') if isinstance(declared, dict) else None
        if not isinstance(html, dict) or html.get('available') is not True:
            reason = (str((html or {}).get('reason') or '')
                      if isinstance(html, dict) else '')
            raise RuntimeError(reason or 'HTML 报告能力不可用，无法生成自动报告')
        return tuple(
            fmt for fmt in _REPORT_FORMATS
            if isinstance(declared.get(fmt), dict)
            and declared[fmt].get('available') is True
        )

    def proj_report_status(self, project_id):
        try:
            record = self._resolve_project_id(project_id)
        except Exception:                                 # noqa: BLE001 public identity seam
            return self._project_identity_failure(
                schema='vcstudio.report-status/v1', project_id=None,
                artifact_status='missing', artifact_current=False,
                has_marker=False, scientific_status=None,
                scientific_qualification=None, scientific_stale=False,
                eligible_final=False, publication_gate_status='unknown',
                desired_report_kind=None, report_reason='', files={})
        result = self._call_with_project_bindings(
            [record],
            lambda: self._reports().status(record['path']),
            failure={
                'schema': 'vcstudio.report-status/v1', 'project_id': None,
                'artifact_status': 'missing', 'artifact_current': False,
                'has_marker': False, 'scientific_status': None,
                'scientific_qualification': None, 'scientific_stale': False,
                'eligible_final': False,
                'publication_gate_status': 'unknown',
                'desired_report_kind': None, 'report_reason': '', 'files': {},
            })
        result.pop('path', None)
        if result.get('error_code') != 'identity_mismatch':
            result['project_id'] = record['project_id']
        return self._project_public_result(result)

    def _proj_report_status_for_path(self, path):
        """Return canonical marker freshness; report-file mtime is never evidence."""
        project_path = str(path or '').strip()
        if not project_path:
            return {
                'schema': 'vcstudio.report-status/v1',
                'ok': False,
                'path': '',
                'artifact_status': 'missing',
                'artifact_current': False,
                'has_marker': False,
                'scientific_status': None,
                'scientific_qualification': None,
                'scientific_stale': False,
                'eligible_final': False,
                'publication_gate_status': 'unknown',
                'desired_report_kind': None,
                'report_reason': '',
                'files': {},
                'error': '未指定项目路径',
            }
        try:
            project = self._load_project_for_path(project_path)
            if project is None:
                raise FileNotFoundError('项目不存在或 project.yaml 已被移动')
            summary = self._adsorption.delta_e_rows(project)
            status = self._canonical_report_status(project, summary)
            status.update({'ok': True, 'path': project_path, 'error': None})
            return status
        except Exception as exc:                          # noqa: BLE001
            return {
                'schema': 'vcstudio.report-status/v1',
                'ok': False,
                'path': project_path,
                'artifact_status': 'missing',
                'artifact_current': False,
                'has_marker': False,
                'scientific_status': None,
                'scientific_qualification': None,
                'scientific_stale': False,
                'eligible_final': False,
                'publication_gate_status': 'unknown',
                'desired_report_kind': None,
                'report_reason': '',
                'files': {},
                'error': str(exc),
            }

    def proj_report_bundle(self, project_id, out_dir, formats=None, final=True, stem=None,
                           record_artifact=True, requested_kind=None):
        try:
            record = self._resolve_project_id(project_id)
        except Exception:                                 # noqa: BLE001 public identity seam
            return self._project_identity_failure(
                files={}, marker=None, kind=None, scientific_status=None,
                artifact_status='failed')
        result = self._call_with_project_bindings(
            [record],
            lambda: self._proj_report_bundle_for_path(
                record['path'], out_dir, formats=formats, final=final, stem=stem,
                record_artifact=record_artifact,
                requested_kind=requested_kind),
            failure={
                'files': {}, 'marker': None, 'kind': None,
                'scientific_status': None, 'artifact_status': 'failed',
            })
        return self._project_public_result(result)

    def _proj_report_bundle_for_path(
            self, path, out_dir, formats=None, final=True, stem=None,
            record_artifact=True, requested_kind=None):
        """Compatibility adapter into the unified Phase C report service."""
        if record_artifact is not True:
            return self._report_failure_envelope(
                '公开报告入口不允许绕过 revision、manifest 与 marker 登记',
                requested_kind=(str(requested_kind or '').strip().lower() or None),
                record_artifact_required=True,
            )
        return self._reports().legacy_publish(
            path,
            out_dir,
            formats=formats,
            final=final,
            stem=stem,
            record_artifact=record_artifact,
            requested_kind=requested_kind,
        )

    def _report_bundle_unrecorded(self, path, out_dir, formats=None, final=True,
                                  stem=None, requested_kind=None):
        """Private seam for a parent artifact that owns the final audit record.

        This method is intentionally not a public pywebview API contract.  Batch
        comparison may render child documents into its own bundle, while the
        browser-facing ``proj_report_bundle`` can never request an unrecorded
        formal artifact.
        """
        return self._reports().legacy_publish(
            path,
            out_dir,
            formats=formats,
            final=final,
            stem=stem,
            record_artifact=False,
            requested_kind=requested_kind,
        )

    def _proj_report_bundle_legacy_impl(
            self, path, out_dir, formats=None, final=True, stem=None,
            record_artifact=True, requested_kind=None):
        """Generate a thesis-style HTML + DOCX + PDF bundle from one data snapshot.

        A failed final-result gate produces an explicit diagnostic report instead
        of silently skipping the request or pretending the result is final.
        """
        report_kind = None
        qualification = None
        reason = ''
        eligible = None
        try:
            wanted = self._normalize_report_formats(formats)
            requested = (('final' if bool(final) else 'diagnostic')
                         if requested_kind is None else
                         str(requested_kind or '').strip().lower())
            if requested not in _REPORT_KINDS:
                raise ValueError(
                    'requested_kind 必须为 final、diagnostic 或 draft')
            proj = self._load_project_for_path((path or '').strip())
            if proj is None:
                return self._report_failure_envelope(
                    '项目不存在或 project.yaml 已被移动')
            target = os.path.abspath(os.path.normpath(str(out_dir or '').strip()))
            if not str(out_dir or '').strip():
                return self._report_failure_envelope('未指定报告目录')
            os.makedirs(target, exist_ok=True)
            summary = self._adsorption.delta_e_rows(proj)
            eligible, reason = self._final_report_gate(proj, summary)
            report_kind = ('final' if requested == 'final' and eligible else
                           'draft' if requested == 'draft' else 'diagnostic')
            qualification = ('adsorption_result_verified'
                             if report_kind == 'final' else 'diagnostic')
            fed, fed_reason = self._proj_fed(proj, summary)
            frozen_input_fingerprint = self._report_input_fingerprint(proj, summary)
            frozen_project_id = str(
                proj.get('project_uuid') or proj.get('name') or '')
            figure_dir = os.path.join(target, 'figures')
            figures, figure_files = self._project_report_figures(
                proj, summary, fed, figure_dir)
            model = self._project_report_model(
                proj, summary, fed, report_kind=report_kind, figures=figures)
            model.update({
                'scientific_qualification': (
                    'adsorption_result_verified' if report_kind == 'final'
                    else 'diagnostic'),
                'claim_ceiling': 'electronic_adsorption_screen',
            })
            if reason and report_kind == 'diagnostic':
                model['limitations'] = [f'最终报告门禁未通过：{reason}',
                                        *model.get('limitations', [])]
            if fed_reason:
                model['limitations'] = [f'自由能台阶未生成：{fed_reason}',
                                        *model.get('limitations', [])]
            content_hasher = getattr(self._paper(), 'report_content_sha256', None)
            if not callable(content_hasher):
                from vcstudio.project.paper_report import report_content_sha256

                content_hasher = report_content_sha256
            report_model_sha256 = str(content_hasher(model)).lower()
            contracts = self._project_report_contracts(
                proj, path, summary, fed,
                requested_kind=requested,
                report_kind=report_kind,
                formats=wanted,
                eligible_final=eligible,
                gate_reason=reason,
                report_model_sha256=report_model_sha256,
            )
            model.update(self._json_safe_report_result(contracts))
            frozen_scientific_fingerprint = contracts['input_fingerprint']
            safe_stem = self._safe_report_stem(
                stem or f'{proj.get("name") or "project"}_吸附能评估报告')
            rendered = self._json_safe_report_result(
                self._paper().render_report_bundle(
                    model, target, stem=safe_stem, formats=wanted))
            rendered.setdefault('ok', True)
            rendered_files = (dict(rendered.get('files') or {})
                              if isinstance(rendered.get('files'), dict) else {})
            sidecar_files = (rendered.get('sidecar_files')
                             if isinstance(rendered.get('sidecar_files'), dict)
                             else {})
            model_file = str(
                rendered.get('model_file') or sidecar_files.get('model')
                or rendered_files.get('model') or '')
            missing_formats = [
                fmt for fmt in wanted
                if not rendered_files.get(fmt)
                or not os.path.isfile(str(rendered_files.get(fmt)))
            ]
            renderer_kinds = {
                str(rendered.get(field) or '').strip().lower()
                for field in ('scientific_status', 'report_kind')
                if str(rendered.get(field) or '').strip()
            }
            renderer_fingerprint = str(
                rendered.get('input_fingerprint') or '').strip()
            renderer_report_model_sha256 = str(
                rendered.get('report_model_sha256') or '').strip().lower()
            renderer_qualification = str(
                rendered.get('scientific_qualification') or '').strip().lower()
            render_error = str(rendered.get('error') or '').strip()
            if rendered.get('ok') is False or render_error:
                rendered['ok'] = False
                rendered['artifact_status'] = 'failed'
                rendered['error'] = render_error or '报告渲染器返回失败状态'
            elif missing_formats:
                rendered['ok'] = False
                rendered['artifact_status'] = 'failed'
                rendered['error'] = ('报告渲染器未产出所请求格式：'
                                     + '、'.join(sorted(missing_formats)))
            elif not model_file or not os.path.isfile(model_file):
                rendered['ok'] = False
                rendered['artifact_status'] = 'failed'
                rendered['error'] = '报告渲染器未产出冻结 model sidecar'
            elif renderer_kinds and renderer_kinds != {report_kind}:
                rendered['ok'] = False
                rendered['artifact_status'] = 'failed'
                rendered['error'] = (
                    f'报告渲染器科学状态不一致：预期 {report_kind}，'
                    f'实际 {"、".join(sorted(renderer_kinds))}')
            elif (renderer_qualification
                  and renderer_qualification
                  != str(contracts['scientific_qualification']).lower()):
                rendered['ok'] = False
                rendered['artifact_status'] = 'failed'
                rendered['error'] = '报告渲染器科学资格与冻结验证不一致'
            elif (renderer_fingerprint
                  and renderer_fingerprint != frozen_scientific_fingerprint):
                rendered['ok'] = False
                rendered['artifact_status'] = 'failed'
                rendered['error'] = '报告渲染器输入指纹与冻结快照不一致'
            elif (renderer_report_model_sha256
                  and renderer_report_model_sha256 != report_model_sha256):
                rendered['ok'] = False
                rendered['artifact_status'] = 'failed'
                rendered['error'] = '报告渲染器正文指纹与冻结验证不一致'
            marker = None
            if record_artifact and rendered.get('ok') is not False:
                marker_files = dict(rendered_files)
                marker_files['model'] = model_file
                contract_files = rendered.get('contract_files') or {}
                if not isinstance(contract_files, dict):
                    raise TypeError('报告渲染器 contract_files 必须为对象')
                for key, value in contract_files.items():
                    marker_files[f'contract_{key}'] = value
                if rendered.get('manifest'):
                    marker_files.setdefault('manifest', rendered.get('manifest'))
                try:
                    marker = self._persist_report_marker(
                        proj, summary, marker_files,
                        kind=report_kind,
                        reason=reason,
                        figures_dir=figure_dir,
                        model_sha256=rendered.get('model_sha256'),
                        report_model_sha256=rendered.get('report_model_sha256'),
                        contracts=(rendered.get('contracts')
                                   or contracts.get('contract_refs') or {}),
                        scientific_qualification=(
                            contracts.get('scientific_qualification') or 'diagnostic'),
                        manifest=rendered.get('manifest'),
                        project_path=path,
                        expected_input_fingerprint=frozen_input_fingerprint,
                        expected_scientific_fingerprint=frozen_scientific_fingerprint,
                        expected_project_id=frozen_project_id,
                    )
                except _ReportInputChanged as marker_exc:
                    rendered['ok'] = False
                    rendered['marker_error'] = str(marker_exc)
                    rendered['artifact_status'] = 'generated_unrecorded'
                    rendered['stale_input'] = True
                    rendered['error'] = str(marker_exc)
                except Exception as marker_exc:          # noqa: BLE001
                    rendered['ok'] = False
                    rendered['marker_error'] = str(marker_exc)
                    rendered['artifact_status'] = 'generated_unrecorded'
                    rendered['error'] = str(marker_exc)
            rendered.update({
                'kind': report_kind,
                'scientific_status': report_kind,
                'scientific_qualification': contracts['scientific_qualification'],
                'requested_kind': requested,
                'eligible_final': eligible,
                'publication_gate_status': (
                    'eligible' if eligible else 'blocked'),
                'desired_report_kind': ('final' if eligible else 'diagnostic'),
                'gate_reason': reason,
                'figures': figure_files,
                'marker': marker,
                'error': rendered.get('error'),
            })
            if rendered.get('ok') is False:
                generated_unrecorded = (
                    rendered.get('artifact_status') == 'generated_unrecorded')
                return self._report_failure_envelope(
                    rendered.get('error'), kind=report_kind,
                    qualification=contracts['scientific_qualification'],
                    gate_reason=reason,
                    artifact_status=rendered.get('artifact_status') or 'failed',
                    preserve_artifact=generated_unrecorded,
                    files=rendered_files,
                    requested_kind=requested,
                    eligible_final=eligible,
                    publication_gate_status=(
                        'eligible' if eligible else 'blocked'),
                    desired_report_kind=('final' if eligible else 'diagnostic'),
                    stale_input=bool(rendered.get('stale_input')),
                    marker_error=rendered.get('marker_error'),
                    manifest=(rendered.get('manifest')
                              if generated_unrecorded else None),
                    model_file=(model_file if generated_unrecorded else None),
                    contract_files=(rendered.get('contract_files')
                                    if generated_unrecorded else {}),
                )
            rendered.setdefault('artifact_status', 'complete')
            return rendered
        except Exception as e:                            # noqa: BLE001
            gate_fields = ({
                'publication_gate_status': (
                    'eligible' if eligible else 'blocked'),
                'desired_report_kind': ('final' if eligible else 'diagnostic'),
            } if eligible is not None else {})
            return self._report_failure_envelope(
                e, kind=report_kind, qualification=qualification,
                gate_reason=reason,
                requested_kind=(str(requested_kind or '').strip().lower()
                                or None),
                **gate_fields)

    def _comparison_items(self, paths, preset_key=None):
        items = []
        for raw_path in paths or []:
            path = str(raw_path or '').strip()
            proj = self._load_project_for_path(path)
            if proj is None:
                items.append({'path': path, 'project': None})
                continue
            summary = dict(self._adsorption.delta_e_rows(proj) or {})
            explicit_fingerprint = (
                summary.get('comparison_method_fingerprint')
                or proj.get('comparison_method_fingerprint')
            )
            if not explicit_fingerprint:
                method_evidence = self._comparison_method_evidence(proj, summary)
                summary['comparison_method_evidence'] = method_evidence
                if method_evidence.get('fingerprint'):
                    summary['comparison_method_fingerprint'] = (
                        method_evidence['fingerprint'])
            if preset_key:
                fed, reason, _title = self._proj_fed_preset(proj, summary, preset_key)
            else:
                fed, reason = self._proj_fed(proj, summary)
            items.append({
                'path': path, 'project': proj, 'summary': summary,
                'fed': fed, 'fed_reason': reason,
            })
        return items

    def _comparison_snapshot_for_paths(self, paths, preset_key=None):
        """Resolve one ordered comparison set and bind every row to opaque IDs."""
        normalized_paths = [str(path or '').strip() for path in (paths or [])]
        if (len(normalized_paths) < 2 or any(not path for path in normalized_paths)
                or len({self._analysis_workbench_path_key(path)
                        for path in normalized_paths}) != len(normalized_paths)):
            raise ValueError('批次报告至少需要选择 2 个不同项目')
        items = self._comparison_items(normalized_paths, preset_key)
        if len(items) != len(normalized_paths):
            raise RuntimeError('比较项目解析数量与请求不一致')
        id_by_path = {}
        id_by_uuid = {}
        project_ids = []
        for path, item in zip(normalized_paths, items):
            project = item.get('project') if isinstance(item, dict) else None
            if not isinstance(project, dict):
                raise ValueError('比较项目不存在或 project.yaml 已移动')
            project_id = self._workspace_project_id(path, project)
            if project_id in project_ids:
                raise ValueError(f'比较项目 opaque 身份重复：{project_id}')
            project_ids.append(project_id)
            item['project_id'] = project_id
            id_by_path[self._analysis_workbench_path_key(path)] = project_id
            raw_uuid = str(project.get('project_uuid') or '').strip()
            if raw_uuid:
                id_by_uuid[raw_uuid] = project_id
        snapshot = self._comparison_model().build_comparison_snapshot(
            items, preset_key=preset_key)
        projects = snapshot.get('projects') or []
        if len(projects) != len(items):
            raise RuntimeError('比较快照项目数量与冻结请求不一致')
        resolved_ids = []
        for project in projects:
            if not isinstance(project, dict):
                raise RuntimeError('比较快照包含无效项目记录')
            raw_path = str(project.get('path') or '')
            project_id = (id_by_path.get(self._analysis_workbench_path_key(raw_path))
                          if raw_path else None)
            if not project_id:
                project_id = id_by_uuid.get(str(project.get('project_uuid') or ''))
            if not project_id or project_id in resolved_ids:
                raise RuntimeError('比较快照无法确定绑定 opaque project_id')
            project['project_id'] = project_id
            resolved_ids.append(project_id)
        if set(resolved_ids) != set(project_ids):
            raise RuntimeError('比较快照项目身份与冻结请求不一致')
        return items, snapshot, project_ids

    def proj_compare_preview(self, project_ids, preset_key=None):
        try:
            records = self._resolve_project_ids(project_ids, min_count=2)
        except Exception:                                 # noqa: BLE001 public identity seam
            return self._project_identity_failure(
                projects=[], comparison_gate={}, can_plot=False,
                can_final_report=False)
        result = self._call_with_project_bindings(
            records,
            lambda: self._proj_compare_preview_for_paths(
                [record['path'] for record in records], preset_key=preset_key),
            failure={
                'projects': [], 'comparison_gate': {}, 'can_plot': False,
                'can_final_report': False,
            })
        return self._project_locator_projection(result, records)

    def _proj_compare_preview_for_paths(self, paths, preset_key=None):
        """Preview every selected project, including moved/blocked selections."""
        try:
            snapshot = self._comparison_model().build_comparison_snapshot(
                self._comparison_items(paths, preset_key), preset_key=preset_key)
            return {'ok': True, **snapshot, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'projects': [], 'comparison_gate': {},
                    'can_plot': False, 'can_final_report': False, 'error': str(e)}

    def proj_evaluate_candidate(self, project_id, options=None):
        try:
            record = self._resolve_project_id(project_id)
        except Exception:                                 # noqa: BLE001 public identity seam
            return self._project_identity_failure(evaluation=None)
        result = self._call_with_project_bindings(
            [record],
            lambda: self._proj_evaluate_candidate_for_path(
                record['path'], options),
            failure={'evaluation': None})
        return self._project_public_result(result)

    def _proj_evaluate_candidate_for_path(self, path, options=None):
        """Return deterministic, auditable follow-up priority for one project."""
        try:
            proj = self._load_project_for_path((path or '').strip())
            if proj is None:
                return {'ok': False, 'evaluation': None,
                        'error': '项目不存在或 project.yaml 已被移动'}
            summary = self._adsorption.delta_e_rows(proj)
            fed, _reason = self._proj_fed(proj, summary)
            evaluation = self._candidate_eval().evaluate_candidate(
                summary, project=proj, fed=fed, options=options)
            return {'ok': True, 'evaluation': evaluation, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'evaluation': None, 'error': str(e)}

    def _comparison_figures(self, snapshot, out_dir):
        nc = self._nc()
        os.makedirs(out_dir, exist_ok=True)
        figures, files, skipped = [], [], []
        matrix = snapshot.get('adsorption_matrix') or {}
        if matrix.get('rows') and matrix.get('cols'):
            heat_files = nc.heatmap_matrix(
                matrix, os.path.join(out_dir, 'adsorption_comparison.png'),
                cbar_label=r'$E_\mathrm{ads}$ (eV)',
                title='Most-stable adsorption configurations',
                cmap='cividis_r',
                formats=('png', 'pdf'))
            files.extend(heat_files)
            figures.append({
                'path': heat_files[0],
                'title': '多催化剂最稳构型吸附能矩阵',
                'caption': ('仅比较同一规范物种，'
                            '空白表示该项目没有可审计数值。'),
            })
        ladder = snapshot.get('ladder') or {}
        if snapshot.get('can_plot') and ladder.get('paths'):
            ladder_files = nc.free_energy_ladder(
                ladder['paths'], os.path.join(out_dir, 'free_energy_comparison.png'),
                step_labels=ladder.get('step_labels'), show_ul=True, mark_pds=False,
                title='Multi-catalyst free-energy pathways',
                formats=('png', 'pdf'))
            files.extend(ladder_files)
            figures.append({
                'path': ladder_files[0],
                'title': '多催化剂自由能台阶叠加比较',
                'caption': ('图中项目使用相同步骤顺序'
                            '与能量修正口径；U_L 采用各项目自由能模块的权威值。'),
            })
        else:
            skipped.append({
                'kind': 'ladder',
                'reason': '；'.join((snapshot.get('comparison_gate') or {}).get('blocking') or
                                    (snapshot.get('comparison_gate') or {}).get('warnings') or
                                    ['没有至少两组可比自由能路径']),
            })
        return figures, files, skipped

    def _comparison_report_scientific_payload(self, snapshot, preset_key=None):
        """Return the path-free projection shared by build, CAS and marker replay."""
        logical_projects = []
        for index, project in enumerate(snapshot.get('projects') or [], 1):
            stable_name = project.get('name') or f'project-{index}'
            logical_projects.append({
                'project_id': (project.get('project_id')
                               or project.get('project_uuid') or stable_name
                               or f'project-{index}'),
                'name': stable_name,
                'status': project.get('status'),
                'block_reasons': project.get('block_reasons') or [],
                'warnings': project.get('warnings') or [],
                'method_status': project.get('method_status'),
                'method_signature': project.get('method_signature'),
                'method_evidence': self._semantic_without_locators(
                    project.get('method_evidence') or {}),
                'species': project.get('species') or [],
                'ladder': project.get('ladder') or {},
            })
        return self._json_safe_report_result({
            'schema': snapshot.get('schema'),
            'preset_key': preset_key or '',
            'ranking_deadband_eV': snapshot.get('ranking_deadband_eV'),
            'projects': logical_projects,
            'comparison_gate': snapshot.get('comparison_gate') or {},
            'adsorption_matrix': snapshot.get('adsorption_matrix') or {},
            'ladder': snapshot.get('ladder') or {},
        })

    def _comparison_report_contracts(self, snapshot, *, formats, requested_kind,
                                     report_kind, preset_key=None,
                                     report_model_sha256):
        """Build the same versioned contract chain for a comparison report."""
        from vcstudio.project.report_contracts import (
            ClaimRecord,
            ReportSnapshot,
            ReportSpec,
            ValidationCheck,
            ValidationResult,
            sha256_json,
            validate_bindings,
        )

        generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        scientific_payload = self._comparison_report_scientific_payload(
            snapshot, preset_key)
        project_ids = [
            str(item.get('project_id') or '')
            for item in scientific_payload.get('projects') or []]
        if (len(project_ids) < 2 or any(not item for item in project_ids)
                or len(project_ids) != len(set(project_ids))):
            raise ValueError('比较报告必须绑定至少两个不同的 opaque project_id')
        scientific_fingerprint = sha256_json(scientific_payload)
        spec = ReportSpec(
            preset_id='multi-catalyst-comparison',
            requested_kind=requested_kind,
            audience='researcher',
            locale='zh-CN',
            formats=tuple(formats),
            scope={
                'kind': 'comparison',
                'project_ids': project_ids,
                'job_ids': [],
            },
            policy_refs=({
                'id': 'multi-project-comparison-gate',
                'version': '1',
            },),
            options={'preset_key': preset_key or ''},
            created_at_utc=generated_at,
        )
        report_snapshot = ReportSnapshot(
            spec_sha256=spec.semantic_sha256,
            input_fingerprint=scientific_fingerprint,
            created_at_utc=generated_at,
            resolved_scope={'project_ids': project_ids, 'job_ids': []},
            payload=scientific_payload,
            evidence={'comparison_gate': snapshot.get('comparison_gate') or {}},
        )
        can_final = bool(snapshot.get('can_final_report'))
        gate = snapshot.get('comparison_gate') or {}
        gate_reason = '；'.join(gate.get('blocking') or gate.get('warnings') or [])
        gate_check = ValidationCheck(
            id='multi-project-comparison-gate',
            status='pass' if can_final else 'fail',
            severity='blocking',
            required=True,
            message=('项目数据、方法与自由能路径满足定量比较门槛。'
                     if can_final else gate_reason or '跨项目比较门禁未通过'),
            evidence_refs=('snapshot:evidence/comparison_gate',),
            remediation=(None if can_final else
                         '补齐项目内参考态、方法证据和同口径自由能路径后重试。'),
        )
        qualification = ('thermodynamic_path_verified'
                         if report_kind == 'final' else 'diagnostic')
        claim = ClaimRecord(
            id='claim.comparison.delivery',
            text=('所选项目满足声明的跨项目定量比较门槛。'
                  if report_kind == 'final' else
                  '当前比较仅用于诊断缺项，不支持定量排序结论。'),
            qualification=qualification,
            status='supported' if report_kind == 'final' else 'limited',
            evidence_refs=('check:multi-project-comparison-gate',),
        )
        validation = ValidationResult(
            spec_sha256=spec.semantic_sha256,
            snapshot_sha256=report_snapshot.semantic_sha256,
            validated_at_utc=generated_at,
            validator={'id': 'multi-project-comparison-gate', 'version': '1'},
            status='passed' if can_final else 'blocked',
            effective_kind=report_kind,
            final_allowed=bool(report_kind == 'final' and can_final),
            scientific_qualification=qualification,
            claim_ceiling='cross_project_thermodynamic_screen',
            report_model_sha256=report_model_sha256,
            checks=(gate_check,),
            claims=(claim,),
        )
        validate_bindings(spec, report_snapshot, validation)
        return {
            'report_id': f'comparison-{scientific_fingerprint[:16]}-{report_kind}',
            'input_fingerprint': scientific_fingerprint,
            'scientific_qualification': qualification,
            'claim_ceiling': validation.claim_ceiling,
            'preset_id': spec.preset_id,
            'report_spec': spec.to_dict(),
            'report_snapshot': report_snapshot.to_dict(),
            'validation': validation.to_dict(),
            'claims': [item.to_dict() for item in validation.claims],
            'contract_refs': {
                'spec': {'schema': spec.schema, 'sha256': spec.semantic_sha256},
                'snapshot': {
                    'schema': report_snapshot.schema,
                    'sha256': report_snapshot.semantic_sha256,
                    'input_fingerprint': scientific_fingerprint,
                },
                'validation': {
                    'schema': validation.schema,
                    'sha256': validation.semantic_sha256,
                    'status': validation.status,
                    'final_allowed': validation.final_allowed,
                    'report_model_sha256': validation.report_model_sha256,
                },
            },
        }

    def _comparison_report_build(self, paths, preset_key, wanted, requested,
                                 work_dir):
        """Freeze a comparison model for the shared ReportService lifecycle."""
        items, snapshot, project_ids = self._comparison_snapshot_for_paths(
            paths, preset_key)
        figures, figure_files, skipped = self._comparison_figures(
            snapshot, os.path.join(str(work_dir), 'comparison_figures'))
        evaluations = []
        item_by_path = {
            self._analysis_workbench_path_key(item.get('path')): item
            for item in items if isinstance(item, dict) and item.get('path')}
        for project in snapshot.get('projects') or []:
            raw_path = str(project.get('path') or '')
            source = item_by_path.get(self._analysis_workbench_path_key(raw_path))
            if not source or not source.get('project'):
                continue
            evaluations.append(self._candidate_eval().evaluate_candidate(
                source.get('summary') or {}, project=source['project'],
                fed=source.get('fed')))
        matrix = snapshot.get('adsorption_matrix') or {}
        comparison_rows = [
            [name, *[('—' if value is None else f'{value:.3f}')
                     for value in values]]
            for name, values in zip(
                matrix.get('rows') or [], matrix.get('values') or [])
        ]
        gate = snapshot.get('comparison_gate') or {}
        eligible = bool(snapshot.get('can_final_report'))
        gate_reason = '；'.join(
            gate.get('blocking') or gate.get('warnings') or [])
        report_kind = ('final' if requested == 'final' and eligible else
                       'draft' if requested == 'draft' else 'diagnostic')
        qualification = ('thermodynamic_path_verified'
                         if report_kind == 'final' else 'diagnostic')
        model = {
            'schema': 'vcstudio.research-report/v1',
            'locale': 'zh-CN',
            'title': '多催化剂吸附能与自由能路径比较',
            'subtitle': (
                '批次比较研究报告' if report_kind == 'final' else
                '批次比较草稿：内容与证据仍可继续编辑'
                if report_kind == 'draft' else
                '诊断型批次报告：方法或路径证据尚未完全可比'),
            'kicker': 'VASP CATALYST STUDIO · COMPARATIVE STUDY',
            'report_kind': report_kind,
            'scientific_qualification': qualification,
            'claim_ceiling': 'cross_project_thermodynamic_screen',
            'metadata': {
                '已选项目': snapshot.get('selected_count'),
                '可评价项目': snapshot.get('ready_count'),
                '同图台阶项目': snapshot.get('ladder_ready_count'),
                '比较门禁': gate.get('status'),
                '反应预设': preset_key or 'Li-S discharge',
                '数据指纹': snapshot.get('data_fingerprint'),
            },
            'executive_summary': (
                f'本批次选择 {snapshot.get("selected_count")} 个催化剂项目，'
                f'{snapshot.get("ready_count")} 个具有可审计吸附能；'
                f'比较门禁状态为 {gate.get("status")}。'
                + ('当前可生成同轴自由能台阶比较。' if snapshot.get('can_plot')
                   else '当前不强行叠加不可比的自由能路径。')),
            'key_findings': [
                (evaluation.get('decision') or {}).get('summary_zh') or
                f'{(evaluation.get("candidate") or {}).get("name") or "项目"}：证据不足'
                for evaluation in evaluations
            ],
            'candidate_evaluations': self._candidate_evaluation_table(
                evaluations, title='候选材料后续计算优先级'),
            'comparison_table': {
                'title': '多催化剂最稳构型吸附能比较',
                'columns': ['催化剂', *(matrix.get('cols') or [])],
                'rows': comparison_rows,
                'caption': ('每个物种采用项目内最低 E_ads 构型；0.15 eV 内的构型'
                            '作为近简并候选保留。不同 Li2Sx 不能依次相减代替反应自由能。'),
            },
            'figures': figures,
            'methods': [
                '各项目先按规范物种分组并选取最稳构型，再进行横向比较。',
                '台阶图仅叠加步骤标签、顺序和能量修正口径一致的自由能路径。',
                '项目间自旋初态可不同；是否可比较由实际能量操作数和方法证据决定。',
            ],
            'limitations': [
                *gate.get('blocking', []), *gate.get('warnings', []),
                *[f'{item.get("display_name")}: {reason}'
                  for item in snapshot.get('projects') or []
                  for reason in item.get('block_reasons') or []],
                *[f'{item.get("display_name")}: {warning}'
                  for item in snapshot.get('projects') or []
                  for warning in item.get('warnings') or []],
                *[f'图表未生成（{item.get("kind") or "unknown"}）：'
                  f'{item.get("reason") or "原因未记录"}' for item in skipped],
            ],
            'recommendations': self._recommendation_blocks(evaluations),
        }
        content_hasher = getattr(self._paper(), 'report_content_sha256', None)
        if not callable(content_hasher):
            from vcstudio.project.paper_report import report_content_sha256

            content_hasher = report_content_sha256
        report_model_sha256 = str(content_hasher(model)).lower()
        contracts = self._comparison_report_contracts(
            snapshot, formats=wanted, requested_kind=requested,
            report_kind=report_kind, preset_key=preset_key,
            report_model_sha256=report_model_sha256)
        scope_ids = list((contracts.get('report_spec') or {}).get(
            'scope', {}).get('project_ids') or [])
        if scope_ids != project_ids:
            raise RuntimeError('比较报告 contract scope 与冻结项目身份不一致')
        model.update(self._json_safe_report_result(contracts))
        anchor = self._report_workbench_project_context(str(paths[0]))
        return {
            'build_kind': 'comparison',
            'project': anchor['project'],
            'project_path': anchor['project_path'],
            'project_root': anchor['project_root'],
            'project_id': anchor['project_id'],
            'report_spec': contracts['report_spec'],
            'requested_kind': requested,
            'report_kind': report_kind,
            'eligible_final': eligible,
            'gate_reason': gate_reason,
            'fed_reason': '',
            'input_fingerprint': contracts['input_fingerprint'],
            'scientific_fingerprint': contracts['input_fingerprint'],
            'report_model_sha256': report_model_sha256,
            'contracts': contracts,
            'model': model,
            'figure_dir': os.path.join(str(work_dir), 'comparison_figures'),
            'figure_files': figure_files,
            'default_stem': '多催化剂_批次比较报告',
            'comparison_snapshot': snapshot,
            'comparison_project_ids': project_ids,
            'comparison_paths': [str(item) for item in paths],
            'comparison_items': items,
            'comparison_skipped': skipped,
            'qualification': qualification,
            'preset_key': preset_key,
        }

    def _comparison_report_publish_via_service(
            self, paths, out_dir, *, preset_key, wanted, requested):
        """Register a server-built comparison preview, then use ReportService."""
        import shutil
        import uuid
        from vcstudio.project.report_service import (
            PREVIEW_TOKEN_SCHEMA,
            _PreviewRecord,
            _lineage_id,
        )

        service = self._reports()
        service._cleanup_expired()
        temp_root = tempfile.mkdtemp(
            prefix='vcstudio-comparison-preview-', dir=service._temp_root)
        try:
            build = self._comparison_report_build(
                paths, preset_key, wanted, requested, temp_root)
            report_id = _lineage_id(
                build['project_id'], build['contracts']['report_spec'])
            base_revision, base_manifest = service._history_base(
                build['project_root'], build['project_id'], report_id)
            refs = build['contracts'].get('contract_refs') or {}
            preview_id = uuid.uuid4().hex + uuid.uuid4().hex
            token = {
                'schema': PREVIEW_TOKEN_SCHEMA,
                'preview_id': preview_id,
                'project_id': build['project_id'],
                'spec_sha256': str((refs.get('spec') or {}).get('sha256') or ''),
                'snapshot_sha256': str(
                    (refs.get('snapshot') or {}).get('sha256') or ''),
                'validation_sha256': str(
                    (refs.get('validation') or {}).get('sha256') or ''),
                'report_model_sha256': build['report_model_sha256'],
                'base_revision': base_revision,
                'base_manifest_sha256': base_manifest,
            }
            for field in ('spec_sha256', 'snapshot_sha256',
                          'validation_sha256', 'report_model_sha256'):
                if not re.fullmatch(r'[0-9a-f]{64}', str(token.get(field) or '')):
                    raise RuntimeError(f'比较报告冻结预览缺少有效 {field}')
            now = float(service._clock())
            record = _PreviewRecord(
                preview_id=preview_id,
                operation_id='comparison-' + uuid.uuid4().hex,
                project_id=build['project_id'],
                project_path=build['project_path'],
                project_root=build['project_root'],
                report_id=report_id,
                created_at_utc=datetime.now(timezone.utc).replace(
                    microsecond=0).isoformat(),
                created_at=now,
                expires_at=now + service._ttl,
                base_revision=base_revision,
                base_manifest_sha256=base_manifest,
                build=build,
                token=token,
                temp_root=temp_root,
            )
            service._register_preview(record)
        except Exception:
            shutil.rmtree(temp_root, ignore_errors=True)
            raise
        result = service.publish(
            build['project_path'], out_dir, preview_id, token,
            stem=build['default_stem'], public=False, record_artifact=True)
        return result, build

    def _proj_batch_report_revisioned_impl(
            self, paths, out_dir, preset_key=None, formats=None,
            include_individual=True, final=True, requested_kind=None):
        """Publish comparison and child reports through revisioned services."""
        report_kind = None
        qualification = None
        gate_reason = ''
        eligible = None
        requested = None
        target = None
        individual = []
        try:
            wanted = self._normalize_report_formats(formats)
            requested = (('final' if bool(final) else 'diagnostic')
                         if requested_kind is None else
                         str(requested_kind or '').strip().lower())
            if requested not in _REPORT_KINDS:
                raise ValueError('requested_kind 必须为 final、diagnostic 或 draft')
            if not str(out_dir or '').strip():
                return self._report_failure_envelope('未指定批次报告目录')
            target = os.path.abspath(os.path.normpath(str(out_dir).strip()))
            if include_individual:
                used_stems = set()
                for index, source in enumerate(
                        self._comparison_items(list(paths or []), preset_key)):
                    project = source.get('project')
                    if project is None:
                        continue
                    base = self._safe_report_stem(
                        f'{project.get("name") or "project"}_吸附能评估报告')
                    stem = base
                    serial = 2
                    while stem in used_stems:
                        stem = f'{base}_{serial}'
                        serial += 1
                    used_stems.add(stem)
                    child = self._proj_report_bundle_for_path(
                        source['path'], target, wanted, final=final, stem=stem,
                        requested_kind=requested)
                    individual.append({
                        'path': source['path'],
                        'name': project.get('name') or f'项目{index + 1}',
                        'kind': child.get('kind'),
                        'files': child.get('files') or {},
                        'revision': child.get('revision') or {},
                        'marker': child.get('marker'),
                        'ok': bool(child.get('ok')),
                        'error': child.get('error'),
                    })
            comparison_result, build = self._comparison_report_publish_via_service(
                list(paths or []), target, preset_key=preset_key,
                wanted=wanted, requested=requested)
            report_kind = build['report_kind']
            qualification = build['qualification']
            gate_reason = build['gate_reason']
            eligible = build['eligible_final']
            snapshot = build['comparison_snapshot']
            gate = snapshot.get('comparison_gate') or {}
            if comparison_result.get('ok') is not True:
                failed = copy.deepcopy(comparison_result)
                failed.update({
                    'ok': False,
                    'artifact_status': (
                        'partial_success' if any(
                            item.get('ok') is True for item in individual)
                        else 'failed'),
                    'requested_kind': requested,
                    'gate_reason': gate_reason,
                    'files': {
                        'comparison': {},
                        'individual': individual,
                    },
                    'figures': comparison_result.get('assets') or [],
                    'skipped': build['comparison_skipped'],
                    'blocked': gate.get('blocking') or [],
                    'warnings': gate.get('warnings') or [],
                    'snapshot': snapshot,
                    'out_dir': target,
                })
                return failed

            return {
                'ok': True,
                'kind': report_kind,
                'artifact_status': str(
                    comparison_result.get('artifact_status') or 'complete'),
                'scientific_status': report_kind,
                'scientific_qualification': qualification,
                'gate_reason': gate_reason,
                'publication_gate_status': (
                    'eligible' if eligible else 'blocked'),
                'desired_report_kind': ('final' if eligible else 'diagnostic'),
                'marker': comparison_result.get('marker'),
                'revision': comparison_result.get('revision') or {},
                'manifest': comparison_result.get('manifest'),
                'requested_kind': requested,
                'files': {
                    'comparison': comparison_result.get('files') or {},
                    'individual': individual,
                },
                'figures': (comparison_result.get('assets')
                            or build['figure_files']),
                'skipped': build['comparison_skipped'],
                'blocked': gate.get('blocking') or [],
                'warnings': gate.get('warnings') or [],
                'snapshot': snapshot,
                'history': self._reports().history(build['project_path']),
                'out_dir': target,
                'error': comparison_result.get('error'),
            }
        except Exception as exc:                         # noqa: BLE001 stable adapter
            gate_fields = ({
                'publication_gate_status': ('eligible' if eligible else 'blocked'),
                'desired_report_kind': ('final' if eligible else 'diagnostic'),
            } if eligible is not None else {})
            failed = self._report_failure_envelope(
                exc, kind=report_kind, qualification=qualification,
                gate_reason=gate_reason,
                requested_kind=(requested or
                                str(requested_kind or '').strip().lower() or None),
                **gate_fields)
            has_success = any(item.get('ok') is True for item in individual)
            failed['artifact_status'] = (
                'partial_success' if has_success else 'failed')
            # Preserve the batch breakdown only after at least one child was
            # actually attempted.  Validation failures that happen before any
            # publication retain the stable generic failure envelope (`files={}`).
            if individual:
                failed['files'] = {
                    'comparison': {}, 'individual': individual}
            if target is not None:
                failed['out_dir'] = target
            return failed

    def proj_batch_report(self, project_ids, out_dir, preset_key=None, formats=None,
                          include_individual=True, final=True, requested_kind=None):
        try:
            records = self._resolve_project_ids(project_ids, min_count=2)
        except Exception:                                 # noqa: BLE001 public identity seam
            return self._project_identity_failure(
                files={}, marker=None, kind=None, scientific_status=None,
                artifact_status='failed')
        result = self._call_with_project_bindings(
            records,
            lambda: self._proj_batch_report_for_paths(
                [record['path'] for record in records], out_dir,
                preset_key=preset_key, formats=formats,
                include_individual=include_individual, final=final,
                requested_kind=requested_kind),
            failure={
                'files': {}, 'marker': None, 'kind': None,
                'scientific_status': None, 'artifact_status': 'failed',
            })
        return self._project_locator_projection(result, records)

    def _proj_batch_report_for_paths(
            self, paths, out_dir, preset_key=None, formats=None,
            include_individual=True, final=True, requested_kind=None):
        """Generate revisioned individual and multi-catalyst reports."""
        return self._proj_batch_report_revisioned_impl(
            paths, out_dir, preset_key=preset_key, formats=formats,
            include_individual=include_individual, final=final,
            requested_kind=requested_kind)

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

    def _proj_stable_delta_map(self, proj, summary):
        """多项目比较只取每个物种最低能的最稳构型；旧数据无 species 时兼容短名。"""
        rows = list((summary or {}).get('rows') or [])
        if not any(
                str(row.get('species') or row.get('reference_species') or '').strip()
                for row in rows):
            name = str(proj.get('name') or '')
            return {
                self._ads_short(row.get('name'), name): row.get('delta_e')
                for row in rows
            }
        grouped = {}
        for row in rows:
            species = str(row.get('species') or row.get('reference_species') or '').strip()
            if not species:
                continue
            grouped.setdefault(species, []).append(row)
        result = {}
        for species, candidates in grouped.items():
            marked = [row for row in candidates if row.get('is_most_stable')]
            marked_complete = [
                row for row in marked
                if isinstance(row.get('delta_e'), (int, float))
            ]
            complete = [
                row for row in candidates
                if isinstance(row.get('delta_e'), (int, float))
            ]
            pool = marked_complete or complete or marked or candidates
            chosen = (min(pool, key=lambda row: row['delta_e'])
                      if isinstance(pool[0].get('delta_e'), (int, float))
                      else pool[0])
            result[species] = chosen.get('delta_e')
        return result

    def _project_molecules_dir(self, proj) -> str:
        """项目自带分子库优先，失效时回退全局配置。

        整文件夹导入会把锂硫参考态复制到项目的 ``molecules_dir``。这里与
        report_full 使用同一优先级，确保“项目页出图”和“完整报告”读到同一批
        参考能量；老项目仍继续使用 config.lis_molecules_dir。
        """
        local = str((proj or {}).get('molecules_dir') or '').strip()
        if local and os.path.isdir(local):
            return local
        try:
            cfg = self._config.load_config()
        except Exception:                                 # noqa: BLE001
            cfg = {}
        configured = str((cfg or {}).get('lis_molecules_dir') or '').strip()
        return configured if configured and os.path.isdir(configured) else ''

    def _report_thermo_corrections(self):
        """Load the same filtered ZPE−TS corrections used by legacy reports."""
        try:
            cfg = self._config.load_config()
        except Exception:                                 # noqa: BLE001
            cfg = {}
        raw_freq_dirs = (cfg or {}).get('freq_dirs') or {}
        if not isinstance(raw_freq_dirs, dict):
            return (
                None,
                {},
                ['freq_dirs 配置不是“物种 → 频率目录”映射，本次报告未加入热校正'],
                None,
                298.15,
            )
        freq_dirs = dict(raw_freq_dirs)
        if not freq_dirs:
            return None, {}, [], None, 298.15
        from vcstudio.project import thermo
        temperature = _method_number(getattr(thermo, 'DEFAULT_T', 298.15)) or 298.15
        try:
            corrections = thermo.load_corrections(freq_dirs)
        except Exception as exc:                          # noqa: BLE001
            return (
                None,
                {},
                [f'频率热校正读取失败，本次报告保留电子能口径：{exc}'],
                None,
                temperature,
            )
        warnings = []
        missing = [str(species) for species in freq_dirs if species not in corrections]
        if missing:
            warnings.append(
                '以下频率目录没有可用振动证据，未加入热校正：' + '、'.join(missing))
        accepted = {}
        for species, value in corrections.items():
            if not isinstance(value, dict) or value.get('excluded'):
                detail = (value or {}).get('exclude_reason') if isinstance(value, dict) else ''
                warnings.append(
                    f'{species} 热校正未通过虚频/完整性门，已排除'
                    + (f'：{detail}' if detail else ''))
                continue
            number = _method_number(value.get('g_corr'))
            if number is None:
                warnings.append(f'{species} 的 g_corr 不是有限数，已排除')
                continue
            accepted[str(species)] = {**value, 'g_corr': number}
        if not accepted:
            return None, {}, warnings, None, temperature
        g_corr = {
            species: value['g_corr'] for species, value in accepted.items()
        }
        encoded = json.dumps(
            {'temperature_K': temperature, 'mode': 'zpe_ts', 'g_corr': g_corr},
            ensure_ascii=False, sort_keys=True, separators=(',', ':'),
            allow_nan=False).encode('utf-8')
        return (
            g_corr,
            accepted,
            warnings,
            hashlib.sha256(encoded).hexdigest(),
            temperature,
        )

    def _proj_fed(self, proj, summary):
        """项目 → Li-S 放电路径 fed;成功 (fed, None),失败 (None, 中文原因)。"""
        mol_dir = self._project_molecules_dir(proj)
        if not mol_dir:
            return None, ('项目未导入有效分子参考态，且未配置分子库目录 '
                          'lis_molecules_dir(config)，无法算 ΔG 台阶')
        _state, e_slab = summary['slab']
        if e_slab is None:
            return None, '清洁表面未完成,无法算 ΔG 台阶'
        try:
            (g_corr, thermo_meta, thermo_warnings, thermo_fingerprint,
             thermo_temperature) = (
                self._report_thermo_corrections())
            fed = self._fe().path_from_project_and_molecules(
                summary['rows'], e_slab=e_slab, molecules_dir=mol_dir,
                g_corr=g_corr,
                managed_dirs=(proj.get('species_ref_jobs') or {}).values(),
                project=proj)
            try:
                fed['_comparison_reference_energies'] = self._fe().load_molecule_energies(
                    mol_dir, managed_dirs=(proj.get('species_ref_jobs') or {}).values())
            except Exception:                             # noqa: BLE001 路径本身仍可画，跨项目仅降级
                fed['_comparison_reference_energies'] = None
            if thermo_meta:
                fed['thermo_meta'] = thermo_meta
                fed['temperature_K'] = thermo_temperature
                fed['thermo_correction_fingerprint'] = thermo_fingerprint
            fed.setdefault('warnings', []).extend(thermo_warnings)
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
        mol_dir = self._project_molecules_dir(proj)
        mol_e = {}
        if mol_dir:
            try:
                mol_e = self._fe().load_molecule_energies(mol_dir)
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
            (g_corr, thermo_meta, thermo_warnings, _thermo_fingerprint,
             thermo_temperature) = self._report_thermo_corrections()
            # 通用预设的能量键可能带 ``*``，而频率配置通常使用裸物种名。
            # 只把本条路径实际用到的校正交给 free_energy_path；否则一个完全
            # 不相关的 freq_dirs 条目也会把结果错误标成 thermo_corrected。
            path_corr, path_meta = {}, {}
            for key in energies:
                source_key = next(
                    (candidate for candidate in (str(key), str(key).rstrip('*'))
                     if candidate in (g_corr or {})),
                    None,
                )
                if source_key is None:
                    continue
                path_corr[key] = g_corr[source_key]
                meta = dict((thermo_meta or {}).get(source_key) or {})
                if source_key != key:
                    meta['source_species'] = source_key
                path_meta[key] = meta
            if g_corr and not path_corr:
                thermo_warnings.append(
                    '已读取频率热校正，但其物种键与当前反应预设不匹配；'
                    '本条台阶保留电子能口径')
            fed = self._fe().free_energy_path(
                spec, energies, g_corr=path_corr or None)
            if path_meta:
                encoded = json.dumps(
                    {
                        'temperature_K': thermo_temperature,
                        'mode': 'zpe_ts',
                        'g_corr': path_corr,
                    },
                    ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                    allow_nan=False,
                ).encode('utf-8')
                fed['thermo_meta'] = path_meta
                fed['temperature_K'] = thermo_temperature
                fed['thermo_correction_fingerprint'] = hashlib.sha256(
                    encoded).hexdigest()
            fed.setdefault('warnings', []).extend(thermo_warnings)
            return fed, None, ptitle
        except ValueError as e:
            return None, str(e), ptitle

    def proj_figures(self, project_id, kinds=None, save_to=None, preset_key=None):
        try:
            record = self._resolve_project_id(project_id)
        except Exception:                                 # noqa: BLE001 public identity seam
            return self._project_identity_failure(
                files=[], skipped=[], out_dir=None)
        result = self._call_with_project_bindings(
            [record],
            lambda: self._proj_figures_for_path(
                record['path'], kinds=kinds, save_to=save_to,
                preset_key=preset_key),
            failure={'files': [], 'skipped': [], 'out_dir': None})
        return self._project_public_result(result)

    def _proj_figures_for_path(self, path, kinds=None, save_to=None, preset_key=None):
        """单项目论文级出图(原生引擎)。kinds ⊂ {'bar','table','ladder'},缺省全选。

        bar/table 只用已完成的 ΔE 行;ladder 需 config.lis_molecules_dir 分子库。
        preset_key 为空(默认)→ ladder 走既有 Li-S 放电路径(向后兼容,行为完全不变);
        给非空反应预设 key(见 reaction_presets)→ ladder 改走通用 free_energy_path 引擎,
        项目构型名映射为物种能量,映射不上的物种记 skipped 原因"缺 {物种} 的能量"。
        某类图缺数据只记 skipped(kind+中文原因),不拖垮其他图。
        返回 {'ok','files','skipped','out_dir','error'}。
        """
        try:
            proj = self._load_project_for_path((path or '').strip())
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
                        [{
                            'name': pname,
                            'G': [st['G'] for st in fed['steps']],
                            'pds_index': fed.get('pds_index'),
                            'u_l': fed.get('u_l'),
                        }],
                        os.path.join(out_dir, 'free_energy_ladder.png'),
                        step_labels=[st['label'] for st in fed['steps']],
                        pds_index=fed.get('pds_index'), show_ul=True, title=title)
                else:
                    skipped.append({'kind': kind, 'reason': '未知图类型'})
            return {'ok': True, 'files': files, 'skipped': skipped,
                    'out_dir': out_dir, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'files': [], 'skipped': [], 'out_dir': None,
                    'error': str(e)}

    def proj_compare_figures(
            self, project_ids, kinds=None, save_to=None, preset_key=None):
        try:
            records = self._resolve_project_ids(project_ids, min_count=2)
        except Exception:                                 # noqa: BLE001 public identity seam
            return self._project_identity_failure(
                files=[], skipped=[], out_dir=None)
        result = self._call_with_project_bindings(
            records,
            lambda: self._proj_compare_figures_for_paths(
                [record['path'] for record in records], kinds=kinds,
                save_to=save_to, preset_key=preset_key),
            failure={'files': [], 'skipped': [], 'out_dir': None})
        return self._project_locator_projection(result, records)

    def _proj_compare_figures_for_paths(
            self, paths, kinds=None, save_to=None, preset_key=None):
        """多项目对比出图。

        ``kinds`` 可含 heatmap/scaling/volcano/ladder。吸附能矩阵和自由能
        台阶共用 :mod:`project.comparison` 的冻结快照，保证项目多选预览、
        出图和批次报告使用同一组最稳构型及同一方法门禁。自由能路径即使
        超过 5 组也保留在同一坐标轴；步骤或校正口径不一致时明确跳过，
        不把不同 Li-S 物种的电子吸附能相减冒充台阶。
        """
        try:
            try:
                nc = self._nc()
            except ImportError:
                return {'ok': False, 'files': [], 'skipped': [], 'out_dir': None,
                        'error': '未安装 matplotlib/numpy(原生出图可选依赖):'
                                 'pip install matplotlib numpy 后重试'}
            kinds = [str(k) for k in (kinds or ['heatmap'])]
            make_report = 'report' in kinds
            items = self._comparison_items(paths, preset_key)
            snapshot = self._comparison_model().build_comparison_snapshot(
                items, preset_key=preset_key)
            projs = []
            for item in items:
                proj = item.get('project')
                if proj is None:
                    continue
                _shorts, _des, summary = self._proj_delta_data(proj)
                projs.append({'name': str(proj.get('name') or '') or '项目',
                              'proj': proj, 'summary': summary,
                              'de': self._proj_stable_delta_map(proj, summary)})
            if len(projs) < 2:
                return {'ok': False, 'files': [], 'skipped': [], 'out_dir': None,
                        'error': '多项目对比至少需要选中 2 个有效项目'}
            matrix = snapshot.get('adsorption_matrix') or {}
            cols = list(matrix.get('cols') or [])
            out_dir = (save_to or '').strip() or os.path.join(
                str(projs[0]['proj'].get('root') or '.'), 'compare_figures')
            os.makedirs(out_dir, exist_ok=True)

            files, skipped = [], []
            ladder_cache = None
            for kind in kinds:
                if kind == 'report':
                    continue
                if kind == 'heatmap':
                    values = matrix.get('values') or []
                    if not any(v is not None for row in values for v in row):
                        skipped.append({'kind': 'heatmap',
                                        'reason': '所有项目均无已完成的 ΔE'})
                        continue
                    files += nc.heatmap_matrix(
                        {'rows': matrix.get('rows') or [], 'cols': cols,
                         'values': values},
                        os.path.join(out_dir, 'delta_e_heatmap.png'),
                        cmap='cividis_r')
                elif kind == 'scaling':
                    # 标度关系保留旧的任意吸附质支持；这里只做统计相关性，
                    # 不把它当作配平反应路径或催化优劣的决定性证据。
                    raw_cols = []
                    for pr in projs:
                        for species in pr['de']:
                            if species not in raw_cols:
                                raw_cols.append(species)
                    pair = self._best_scaling_pair(projs, raw_cols)
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
                        descriptor_label=(
                            r'$\Delta G_{\mathrm{ads}}(*\mathrm{LiS}_2)$ (eV)'
                            if sp == 'LiS2' else
                            f'$\\Delta G_{{\\mathrm{{ads}}}}({sp})$ (eV)'
                        ),
                        activity_label='$U_L$ (V)')
                elif kind == 'ladder':
                    ladder_cache = self._compare_ladder_paths(projs)
                    ladder_paths, step_labels, reason = ladder_cache
                    if ladder_paths is None:
                        skipped.append({'kind': 'ladder', 'reason': reason})
                        continue
                    files += nc.free_energy_ladder(
                        ladder_paths,
                        os.path.join(out_dir, 'multi_catalyst_free_energy.png'),
                        step_labels=step_labels, show_ul=True, mark_pds=False,
                        title='Multi-catalyst Li-S free-energy pathways')
                    if reason:
                        skipped.append({
                            'kind': 'ladder',
                            'reason': '对比说明：' + reason,
                            'partial': True,
                        })
                else:
                    skipped.append({'kind': kind, 'reason': '未知图类型'})
            if make_report:
                if ladder_cache is None:
                    ladder_cache = self._compare_ladder_paths(projs)
                try:
                    from vcstudio.project import report_documents
                    model = self._compare_report_model(
                        projs, cols, ladder_cache, files, out_dir)
                    docx, pdf = report_documents.generate_report_documents(
                        model,
                        os.path.join(out_dir, 'multi_catalyst_comparison.docx'),
                        os.path.join(out_dir, 'multi_catalyst_comparison.pdf'))
                    files.extend([str(docx), str(pdf)])
                except Exception as exc:                  # noqa: BLE001 其它图仍保留
                    skipped.append({'kind': 'report', 'reason': str(exc)})
            return {'ok': True, 'files': files, 'skipped': skipped,
                    'out_dir': out_dir, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'files': [], 'skipped': [], 'out_dir': None,
                    'error': str(e)}

    def _compare_ladder_paths(self, projs):
        """聚合各催化剂权威 ΔG/PDS/U_L；物种顺序不同的项目明确跳过。"""
        paths, labels, reasons = [], None, []
        baseline_method = None
        baseline_references = None
        baseline_explicit_fingerprint = None
        baseline_molecules_dir = None
        baseline_name = None
        for item in projs:
            method = (item.get('summary') or {}).get('method_consistency') or {}
            if method.get('status') == 'incompatible':
                reasons.append(
                    f'{item["name"]}: 方法不兼容（'
                    + '；'.join(method.get('issues') or ['未给出原因']) + '）')
                continue
            if method.get('status') == 'unverified':
                reasons.append(
                    f'{item["name"]}: 已纳入探索性同图，但方法证据尚未完整核验（'
                    + '；'.join(method.get('warnings') or ['未给出原因']) + '）')
            fed, reason = self._proj_fed(item['proj'], item['summary'])
            if fed is None:
                reasons.append(f'{item["name"]}: {reason}')
                continue
            current_labels = [step.get('label') for step in fed.get('steps') or []]
            if labels is None:
                labels = current_labels
            elif current_labels != labels:
                reasons.append(f'{item["name"]}: 反应中间体顺序与首个项目不同')
                continue
            candidate_method = self._comparison_method_record(item)
            candidate_references = fed.get('_comparison_reference_energies')
            candidate_explicit_fingerprint = str(
                (item.get('proj') or {}).get('comparison_method_fingerprint') or '')
            raw_molecules_dir = str(
                (item.get('proj') or {}).get('molecules_dir') or '')
            candidate_molecules_dir = (
                os.path.normcase(os.path.abspath(raw_molecules_dir))
                if raw_molecules_dir else None
            )
            if paths:
                if (baseline_explicit_fingerprint
                        and candidate_explicit_fingerprint
                        and baseline_explicit_fingerprint == candidate_explicit_fingerprint):
                    method_check = {'status': 'verified', 'issues': [], 'warnings': []}
                else:
                    method_check = self._comparison_method_pair(
                        baseline_method, candidate_method,
                        baseline_name or '首个项目', item['name'])
                if method_check['status'] == 'incompatible':
                    reasons.append(
                        f'{item["name"]}: 与 {baseline_name} 的跨项目方法不可比（'
                        + '；'.join(method_check['issues']) + '）')
                    continue
                if method_check['status'] == 'unverified':
                    reasons.append(
                        f'{item["name"]}: 已纳入探索性同图，但跨项目方法证据不完整（'
                        + '；'.join(method_check['warnings'][:3]) + '）')
                ref_issue = self._comparison_reference_issue(
                    baseline_references, candidate_references)
                if (ref_issue and not ref_issue['blocking']
                        and baseline_molecules_dir
                        and candidate_molecules_dir == baseline_molecules_dir):
                    ref_issue = None
                if ref_issue and ref_issue['blocking']:
                    reasons.append(
                        f'{item["name"]}: 与 {baseline_name} 的分子参考不可比（'
                        f'{ref_issue["message"]}）')
                    continue
                if ref_issue:
                    reasons.append(
                        f'{item["name"]}: 已纳入探索性同图，但分子参考证据不完整（'
                        f'{ref_issue["message"]}）')
            else:
                baseline_method = candidate_method
                baseline_references = candidate_references
                baseline_explicit_fingerprint = candidate_explicit_fingerprint
                baseline_molecules_dir = candidate_molecules_dir
                baseline_name = item['name']
                if candidate_method is None and not baseline_explicit_fingerprint:
                    reasons.append(
                        f'{item["name"]}: 已作为对比基线，但跨项目方法指纹不可读')
                if (not isinstance(candidate_references, dict)
                        and not baseline_molecules_dir):
                    reasons.append(
                        f'{item["name"]}: 已作为对比基线，但分子参考能量签名不可读')
            paths.append({
                'name': item['name'],
                'G': [step.get('G') for step in fed.get('steps') or []],
                'pds_index': fed.get('pds_index'),
                'u_l': fed.get('u_l'),
            })
        if len(paths) < 2:
            detail = '；'.join(reasons[:6]) or '具备完整自由能路径的项目不足 2 个'
            return None, labels or [], detail
        return paths, labels or [], '；'.join(reasons)

    def _comparison_method_record(self, item):
        """读取一个实际参与 ΔE 的构型方法指纹，供催化剂间配对核验。"""
        from vcstudio.project import energy_gate

        proj = item.get('proj') or {}
        summary = item.get('summary') or {}
        configs = list(((proj.get('members') or {}).get('configs') or []))
        complete_names = {
            os.path.normcase(str(row.get('name') or ''))
            for row in (summary.get('rows') or [])
            if isinstance(row.get('delta_e'), (int, float))
        }
        ordered = [
            path for path in configs
            if os.path.normcase(self._base(path)) in complete_names
        ]
        ordered.extend(path for path in configs if path not in ordered)
        for job_dir in ordered:
            if not os.path.isdir(str(job_dir)):
                continue
            manifest = self._manifest.load_manifest(job_dir)
            return energy_gate.method_record(
                job_dir, manifest, f'{item.get("name") or "项目"}代表构型')
        return None

    @staticmethod
    def _comparison_method_pair(left, right, left_name, right_name):
        """跨催化剂只硬比较能量方法；自旋与 K 点按各体系设置。"""
        if left is None or right is None:
            missing = left_name if left is None else right_name
            return {
                'status': 'unverified', 'issues': [],
                'warnings': [f'{missing} 缺少可读的代表构型方法指纹'],
            }
        from vcstudio.project import energy_gate

        check = energy_gate.compare_methods(
            [left, right], require_same_kpoints=False)
        # 不同催化剂可以有各自的磁性基态；不同晶胞也不要求 KPOINTS 文本相同。
        issues = [
            issue for issue in (check.get('issues') or [])
            if not issue.startswith('ISPIN 不一致')
            and not issue.startswith('各作业 POTCAR 没有可核对的共同元素身份')
        ]
        warnings = [
            warning for warning in (check.get('warnings') or [])
            if not warning.startswith('K 点方案不一致')
        ]
        status = 'incompatible' if issues else ('verified' if not warnings else 'unverified')
        return {'status': status, 'issues': issues, 'warnings': warnings}

    @staticmethod
    def _comparison_reference_issue(left, right, tolerance=1e-3):
        """比较实际分子参考能量；不同参考库不得被悄悄叠为定量结论。"""
        if not isinstance(left, dict) or not isinstance(right, dict):
            return {'blocking': False, 'message': '至少一个项目缺少参考能量签名'}
        left_keys, right_keys = set(left), set(right)
        if left_keys != right_keys:
            missing = sorted(left_keys ^ right_keys)
            return {
                'blocking': True,
                'message': '参考物种集合不同：' + '、'.join(missing[:8]),
            }
        differences = []
        for species in sorted(left_keys):
            try:
                delta = abs(float(left[species]) - float(right[species]))
            except (TypeError, ValueError):
                return {
                    'blocking': True,
                    'message': f'{species} 的参考能量不可解析',
                }
            if delta > tolerance:
                differences.append(f'{species} 差 {delta:.6g} eV')
        if differences:
            return {
                'blocking': True,
                'message': '；'.join(differences[:8]),
            }
        return None

    @staticmethod
    def _compare_report_model(projs, cols, ladder_cache, files, out_dir):
        ladder_paths, step_labels, ladder_reason = ladder_cache
        matrix_rows = [
            [item['name']] + [
                (f'{item["de"][species]:.4f}'
                 if isinstance(item['de'].get(species), (int, float)) else '—')
                for species in cols
            ]
            for item in projs
        ]
        summary_rows = []
        for path in ladder_paths or []:
            pds = path.get('pds_index')
            valid_pds = (isinstance(pds, int)
                         and 0 <= pds < max(len(step_labels) - 1, 0))
            pds_text = ('—' if not valid_pds else
                        f'{pds + 1}: {step_labels[pds]} → {step_labels[pds + 1]}')
            ul = path.get('u_l')
            summary_rows.append([
                path.get('name') or '—',
                f'{ul:.4f}' if isinstance(ul, (int, float)) else '—',
                pds_text,
            ])
        blocks = [{
            'type': 'table', 'caption': 'Most-stable adsorption energies by catalyst',
            'columns': ['Catalyst'] + list(cols), 'rows': matrix_rows,
        }]
        pngs = [str(path) for path in files
                if str(path).lower().endswith('.png') and os.path.isfile(str(path))]
        for path in pngs:
            blocks.append({
                'type': 'figure', 'path': path,
                'caption': os.path.splitext(os.path.basename(path))[0].replace('_', ' '),
            })
        pathway_blocks = []
        if summary_rows:
            pathway_blocks.append({
                'type': 'table', 'caption': 'Limiting potentials and PDS',
                'columns': ['Catalyst', 'U_L / V', 'Potential-determining step'],
                'rows': summary_rows, 'column_widths': [1.2, 0.8, 2.4],
            })
        if ladder_reason:
            pathway_blocks.append({
                'type': 'note',
                'text': '比较说明：' + ladder_reason,
            })
        return {
            'title': '多催化剂项目对比报告',
            'subtitle': 'Most-stable adsorption energies and overlaid Li-S pathways',
            'report_label': 'MULTI-CATALYST COMPARISON',
            'base_dir': out_dir,
            'metadata': {
                'Catalyst projects': len(projs),
                'Overlay policy': 'All comparable catalyst pathways in one stair plot',
                'Generated': time.strftime('%Y-%m-%d %H:%M:%S'),
            },
            'abstract': (
                '每个催化剂项目按物种选取最低吸附能的最稳构型。'
                '只在反应中间体顺序和方法证据可比较时叠加自由能路径；'
                '即使超过 5 组，也保留在同一台阶图并使用颜色、线型和标记联合区分。'
            ),
            'sections': [
                {'title': '吸附能比较 / Adsorption comparison', 'blocks': blocks},
                {'title': '自由能路径 / Free-energy pathways',
                 'blocks': pathway_blocks or [{
                     'type': 'note',
                     'text': '没有至少两个可比较的完整自由能路径。',
                 }]},
            ],
        }

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
        """Return a path-specific ``ΔG_ads(*LiS2)`` volcano dataset.

        An arbitrary common ``ΔE_ads(Li2Sx)`` is *not* a valid substitute for
        the descriptor used by the thesis volcano relationship.  Projects
        therefore have to persist an explicit ``volcano_descriptor`` mapping
        with quantity, species, reaction path, sign convention and value.
        """
        del cols  # historical argument retained for API compatibility
        points, contexts, reasons = [], set(), []
        for pr in projs:
            fed, reason = self._proj_fed(pr['proj'], pr['summary'])
            descriptor = pr['proj'].get('volcano_descriptor') or {}
            quantity = str(descriptor.get('quantity') or '')
            species = self._comparison_model().canonical_species(
                descriptor.get('species') or descriptor.get('descriptor_species'))
            path_id = str(descriptor.get('reaction_path_id') or '')
            sign = str(descriptor.get('sign_convention') or '')
            value = descriptor.get('value_eV', descriptor.get('descriptor_value_eV'))
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = None
            u_l = fed.get('u_l') if isinstance(fed, dict) else None
            if (quantity != 'delta_G_ads' or species != 'LiS2' or
                    not path_id or sign != 'negative_is_stronger' or
                    value is None or not math.isfinite(value) or
                    not isinstance(u_l, (int, float)) or not math.isfinite(u_l)):
                detail = reason or (
                    '缺少路径专用 ΔG_ads(*LiS2) 描述符及其反应路径/符号口径')
                reasons.append(f"{pr['name']}: {detail}")
                continue
            contexts.add(path_id)
            points.append({'name': pr['name'], 'x': value, 'y': float(u_l)})
        if len(contexts) > 1:
            return None, '火山图项目使用了不同反应路径，不能套用同一个描述符关系'
        if len(points) < 3:
            why = '；'.join(reasons[:3])
            return None, (
                f'火山图需 ≥3 个项目同时具备同一路径的 ΔG_ads(*LiS2) 与 U_L'
                f'(当前 {len(points)} 个)。{why}')
        return ('LiS2', points), None

    # ── MatClaw 式对话助手（Python 原生、安全附件、限定本地工具） ───────────
    def ai_chat_sessions(self):
        try:
            return {'ok': True, 'sessions': self._chat().list_sessions(), 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'sessions': [], 'error': str(e)}

    def ai_chat_new(self, title=''):
        try:
            session = self._chat().create_session(str(title or '').strip() or None)
            return {'ok': True, 'session': session, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'session': None, 'error': str(e)}

    def ai_chat_history(self, session_id):
        try:
            chat = self._chat()
            return {
                'ok': True,
                'messages': chat.history(str(session_id or ''), include_previews=False),
                'attachments': chat.list_attachments(
                    str(session_id or ''), include_previews=False),
                'error': None,
            }
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'messages': [], 'attachments': [], 'error': str(e)}

    def ai_chat_attach(self, session_id, paths):
        """把用户明确选择的本地文件复制进会话隔离区；源路径不进数据库/模型。"""
        try:
            values = paths if isinstance(paths, (list, tuple)) else [paths]
            values = [str(path or '').strip() for path in values if str(path or '').strip()]
            if not values:
                return {'ok': False, 'attachments': [], 'error': '未选择附件'}
            if len(values) > 10:
                return {'ok': False, 'attachments': [],
                        'error': '一次最多导入 10 个附件，请分批选择'}
            chat = self._chat()
            attached, rejected = [], []
            for path in values:
                try:
                    attached.append(chat.attach(str(session_id or ''), path))
                except Exception as exc:                 # noqa: BLE001 单文件失败需如实返回部分成功
                    rejected.append({
                        'name': os.path.basename(path) or path,
                        'error': str(exc),
                    })
            if rejected:
                detail = '；'.join(
                    f'{item["name"]}: {item["error"]}' for item in rejected)
                return {
                    'ok': False,
                    'attachments': attached,
                    'rejected': rejected,
                    'error': ('部分附件导入失败；已成功导入的文件仍保留：' + detail
                              if attached else '附件导入失败：' + detail),
                }
            return {'ok': True, 'attachments': attached, 'rejected': [], 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'attachments': [], 'rejected': [], 'error': str(e)}

    def _chat_project_status_text(self, query=''):
        status = self.pipeline_status()
        if not status.get('ok'):
            return '项目状态读取失败：' + str(status.get('error') or '未知错误')
        projects = list(status.get('projects') or [])
        needle = str(query or '').strip()
        if needle:
            key = os.path.normcase(os.path.abspath(os.path.normpath(needle)))
            projects = [
                item for item in projects
                if str(item.get('name') or '').casefold() == needle.casefold()
                or os.path.normcase(os.path.abspath(
                    os.path.normpath(str(item.get('path') or '')))) == key
            ]
        if not projects:
            return ('没有找到匹配的吸附能项目。用法：/status，或 '
                    '/status 项目名（也可粘贴 project.yaml 路径）。')
        stage_names = {
            'generate': '待生成', 'submit': '待提交', 'monitor': '监控中',
            'recover': '自动续算', 'analysis': '结果分析', 'report_done': '报告完成',
        }
        runtime = self.pipeline_runtime_status()
        state = runtime.get('state') if runtime.get('ok') else {}
        if isinstance(state, dict):
            if state.get('last_error'):
                supervisor = '后台主管异常：' + str(state['last_error'])
            elif state.get('enabled') is False or state.get('paused'):
                supervisor = '后台主管：已暂停'
            elif state.get('tick_running'):
                supervisor = '后台主管：正在检查'
            elif state.get('running'):
                supervisor = '后台主管：运行中'
            else:
                supervisor = '后台主管：尚未启动'
            if state.get('next_check'):
                supervisor += f'；下次检查 {state["next_check"]}'
            if state.get('last_finished'):
                supervisor += f'；上次完成 {state["last_finished"]}'
        else:
            supervisor = '后台主管状态不可用'
        lines = [supervisor, '当前项目状态：']
        for item in projects:
            round_text = (f'，续算 {item.get("recover_round", 0)}/3'
                          if item.get('stage') == 'recover' else '')
            flag = '，需要人工处理' if item.get('needs_human') else ''
            report_note = str(item.get('report_reason') or '').strip()
            extra = f'；报告：{report_note}' if report_note else ''
            lines.append(
                f'- {item.get("name") or "(未命名)"}：'
                f'{stage_names.get(item.get("stage"), item.get("stage"))}，'
                f'{item.get("done", 0)}/{item.get("total", 0)} DONE'
                f'{round_text}{flag}{extra}')
        return '\n'.join(lines)

    def _chat_project_report_text(self, query=''):
        needle = str(query or '').strip()
        matches = []
        for path in self._adsorption.list_projects():
            proj = self._load_project_for_path(path)
            if proj is None:
                continue
            if (not needle or str(proj.get('name') or '').casefold() == needle.casefold()
                    or os.path.normcase(os.path.abspath(os.path.normpath(str(path))))
                    == os.path.normcase(os.path.abspath(os.path.normpath(needle)))):
                matches.append((path, proj))
        if not needle:
            return '请指定项目：/report 项目名（或 project.yaml 路径）。'
        if len(matches) != 1:
            return '没有找到唯一匹配的项目，请使用完整项目名或 project.yaml 路径。'
        path, proj = matches[0]
        root = str(proj.get('root') or os.path.dirname(str(path)))
        name = str(proj.get('name') or 'project')
        summary = self._adsorption.delta_e_rows(proj)
        final, reason = self._final_report_gate(proj, summary)
        report_dir = os.path.join(root, 'report')
        suffix = 'report' if final else 'diagnostic'
        try:
            report_formats = self._available_report_formats()
        except Exception as exc:                          # noqa: BLE001
            return '报告尚未生成：' + str(exc)
        result = self._proj_report_bundle_for_path(
            path, report_dir, report_formats, final=True,
            stem=f'{name}_{suffix}')
        if not result.get('ok'):
            return '报告尚未生成：' + str(result.get('error') or '未知错误')
        raw_files = result.get('files') or {}
        files = (list(raw_files.values()) if isinstance(raw_files, dict)
                 else list(raw_files))
        actual_kind = str(
            result.get('scientific_status') or result.get('kind') or ''
        ).strip().lower()
        actual_reason = str(result.get('gate_reason') or reason or '')
        heading = ('最终报告已生成：' if actual_kind == 'final' else
                   f'诊断报告已生成（未标记为最终报告：{actual_reason}）：')
        return heading + '\n' + '\n'.join(f'- {item}' for item in files if item)

    def ai_chat_send(self, session_id, text, attachment_ids=None):
        """发送对话；/status 和 /report 走本地确定性工具，其余受联网开关约束。"""
        try:
            message = str(text or '').strip()
            if not message:
                return {'ok': False, 'result': None, 'error': '消息不能为空'}
            command, _, argument = message.partition(' ')
            command = command.casefold()
            chat = self._chat()
            if command == '/help':
                reply = (
                    '可用命令：\n'
                    '/status [项目名] — 查看项目、续算轮次和报告状态\n'
                    '/report 项目名 — 在本地生成最终 HTML、Word 与 PDF 报告\n'
                    '/stop — 只停止当前 AI 回复，不会取消任何集群作业\n\n'
                    '普通消息可明确勾选附件后发送；未勾选的本地文件不会提供给模型。'
                )
                result = chat.local_reply(str(session_id or ''), message, reply)
            elif command == '/status':
                result = chat.local_reply(
                    str(session_id or ''), message,
                    self._chat_project_status_text(argument))
            elif command == '/report':
                result = chat.local_reply(
                    str(session_id or ''), message,
                    self._chat_project_report_text(argument))
            elif command == '/stop' and argument.strip():
                result = chat.local_reply(
                    str(session_id or ''), message,
                    '/stop 不接受附加文本。请单独输入 /stop；它只停止 AI 回复，'
                    '不会取消任何集群作业。')
            elif command.startswith('/') and command != '/stop':
                result = chat.local_reply(
                    str(session_id or ''), message,
                    '未知命令。输入 /help 查看可用命令；本助手不提供 shell 或任意集群操作。')
            else:
                if command != '/stop':
                    cfg = self._config.load_config()
                    llm = cfg.get('llm') if isinstance(cfg, dict) else {}
                    if not isinstance(llm, dict) or not llm.get('allow_external'):
                        return {
                            'ok': False, 'result': None,
                            'error': ('联网对话默认关闭。请在设置页配置模型并开启'
                                      '“允许将项目数据发送到外部 LLM”；/status、'
                                      '/report 和 /help 仍可离线使用。'),
                        }
                result = chat.send(
                    str(session_id or ''), message,
                    attachment_ids=list(attachment_ids or []))
            return {'ok': True, 'result': result, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'result': None, 'error': str(e)}

    def ai_chat_outbound_preview(self, session_id, text, attachment_ids=None):
        """Read-only disclosure of the exact chat payload before external send."""
        try:
            message = str(text or '').strip()
            if not message:
                return {'ok': False, 'preview': None, 'error': '消息不能为空'}
            command = message.partition(' ')[0].casefold()
            if command.startswith('/'):
                return {
                    'ok': False, 'preview': None,
                    'error': '本地命令不会发送到外部模型，不需要外部数据预览',
                }
            cfg = self._config.load_config()
            llm = cfg.get('llm') if isinstance(cfg, dict) else {}
            if not isinstance(llm, dict) or not llm.get('allow_external'):
                return {
                    'ok': False, 'preview': None,
                    'error': '联网对话未开启；当前不会向外部模型发送数据',
                }
            preview = self._chat().outbound_preview(
                str(session_id or ''), message,
                attachment_ids=list(attachment_ids or []),
            )
            return {'ok': True, 'preview': preview, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'preview': None, 'error': str(e)}

    def ai_chat_stop(self, session_id):
        try:
            result = self._chat().stop(str(session_id or ''))
            return {'ok': True, 'result': result, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'result': None, 'error': str(e)}

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

    def pick_files(self, kind='files'):
        try:
            if self._dialog_fn is not None:
                selected = self._dialog_fn(kind)
            else:
                import webview                            # 延迟:测试永不 import
                selected = webview.windows[0].create_file_dialog(
                    webview.OPEN_DIALOG, allow_multiple=True)
            if not selected:
                paths = []
            elif isinstance(selected, (list, tuple)):
                paths = [str(path) for path in selected if path]
            else:
                paths = [str(selected)]
            return {'paths': paths, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'paths': [], 'error': str(e)}

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

    # ── 设置页:实验室推荐策略(用户级原子/CAS store，不改任何作业) ──────────
    _LAB_POLICY_API_SCHEMA = 'vcstudio.lab-policy-api/v1'
    _LAB_POLICY_SERVER_ACTOR = 'manual-local-user'

    def _lab_policy_backend(self):
        from vcstudio.project import lab_policies

        return lab_policies

    def _lab_policy_selection_store(self):
        if self._lab_policy_store is None:
            self._lab_policy_store = self._lab_policy_backend().LabPolicySelectionStore()
        return self._lab_policy_store

    @staticmethod
    def _lab_policy_request(request):
        if not isinstance(request, dict):
            raise ValueError('policy request must be an object')
        allowed = {'policy_id', 'overrides', 'applicability'}
        unknown = sorted(set(request) - allowed)
        if unknown:
            raise ValueError('policy request contains unknown fields')
        return {
            'policy_id': request.get('policy_id'),
            'overrides': request.get('overrides'),
            'applicability': request.get('applicability'),
        }

    def lab_policy_catalog(self):
        try:
            result = self._lab_policy_backend().catalog()
            return self._analysis_workbench_public_value({
                'schema': self._LAB_POLICY_API_SCHEMA,
                'ok': True, 'catalog': result,
                'recommendation_only': True,
                'authorizes_submission': False,
                'error': None,
            })
        except Exception as exc:                         # noqa: BLE001 public seam
            return {
                'schema': self._LAB_POLICY_API_SCHEMA,
                'ok': False, 'catalog': None,
                'recommendation_only': True,
                'authorizes_submission': False,
                'error': self._analysis_workbench_public_value(str(exc)),
            }

    def lab_policy_read(self):
        try:
            snapshot = self._lab_policy_selection_store().read()
            return self._analysis_workbench_public_value({
                'schema': self._LAB_POLICY_API_SCHEMA,
                **snapshot,
                'recommendation_only': True,
                'authorizes_submission': False,
            })
        except Exception as exc:                         # noqa: BLE001 public seam
            return {
                'schema': self._LAB_POLICY_API_SCHEMA,
                'ok': False, 'conflict': False, 'revision': None,
                'selection': None, 'recommendation_only': True,
                'authorizes_submission': False,
                'error': self._analysis_workbench_public_value(str(exc)),
            }

    def lab_policy_preview(self, request):
        try:
            prepared = self._lab_policy_request(request)
            resolved = self._lab_policy_backend().resolve(
                prepared['policy_id'], prepared['overrides'],
                applicability=prepared['applicability'])
            snapshot = self._lab_policy_selection_store().read()
            return self._analysis_workbench_public_value({
                'schema': self._LAB_POLICY_API_SCHEMA,
                'ok': True, 'preview': resolved,
                'revision': snapshot['revision'],
                'current_selection': snapshot['selection'],
                'recommendation_only': True,
                'requires_user_confirmation': True,
                'authorizes_submission': False,
                'error': None,
            })
        except Exception as exc:                         # noqa: BLE001 public seam
            return {
                'schema': self._LAB_POLICY_API_SCHEMA,
                'ok': False, 'preview': None, 'revision': None,
                'current_selection': None,
                'recommendation_only': True,
                'requires_user_confirmation': True,
                'authorizes_submission': False,
                'error': self._analysis_workbench_public_value(str(exc)),
            }

    def lab_policy_confirm(self, request, expected_revision, confirmed=False):
        try:
            prepared = self._lab_policy_request(request)
            result = self._lab_policy_selection_store().confirm(
                prepared['policy_id'], prepared['overrides'],
                applicability=prepared['applicability'],
                actor=self._LAB_POLICY_SERVER_ACTOR,
                confirmed=confirmed, expected_revision=expected_revision)
            return self._analysis_workbench_public_value({
                'schema': self._LAB_POLICY_API_SCHEMA,
                **result,
                'recommendation_only': True,
                'requires_user_confirmation': True,
                'authorizes_submission': False,
                'actor_attribution_only': True,
            })
        except Exception as exc:                         # noqa: BLE001 public seam
            return {
                'schema': self._LAB_POLICY_API_SCHEMA,
                'ok': False, 'conflict': False, 'revision': None,
                'selection': None, 'recommendation_only': True,
                'requires_user_confirmation': True,
                'authorizes_submission': False,
                'actor_attribution_only': True,
                'error': self._analysis_workbench_public_value(str(exc)),
            }

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
            'density': (ui.get('density')
                        if ui.get('density') in _DENSITIES else 'standard'),
            'autopilot': bool(ui.get('autopilot', False)),
            'poll_interval': int(ui.get('poll_interval', 10) or 10),
            'autopilot_continue': bool(ui.get('autopilot_continue', True)),
            'autopilot_fetch': bool(ui.get('autopilot_fetch', True)),
            'autopilot_report': bool(ui.get('autopilot_report', True)),
            'autopilot_campaigns': bool(ui.get('autopilot_campaigns', False)),
            'scenario': str(ui.get('scenario') or ''),
            'active_engine': str(ui.get('active_engine') or ''),
            'active_calculation': str(ui.get('active_calculation') or ''),
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
            context_result = self.settings_context_get()
            context = (context_result.get('context')
                       if context_result.get('ok') else None)
            ui_view = self._ui_defaults(ui)
            if isinstance(context, dict):
                scenario = context.get('scenario') or {}
                ui_view.update({
                    'scenario': str(scenario.get('key') or ''),
                    'active_engine': str(context.get('engine') or ''),
                    'active_calculation': str(context.get('calculation') or ''),
                })
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
                'ui': ui_view,
                'workspace_context': context,
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

    def density_set(self, name):
        """保存三档视觉密度；非法值拒绝写入，避免污染跨会话 UI 状态。"""
        try:
            n = str(name or '').strip().lower()
            if n not in _DENSITIES:
                return {'ok': False, 'density': 'standard',
                        'error': '不支持的视觉密度'}
            self._config.set_ui_state(density=n)
            return {'ok': True, 'density': n, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    def autopilot_save(self, autopilot=None, poll_interval=None,
                       autopilot_continue=None, autopilot_fetch=None,
                       autopilot_report=None, autopilot_campaigns=None):
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
            if autopilot_campaigns is not None:
                kv['autopilot_campaigns'] = bool(autopilot_campaigns)
            self._config.set_ui_state(**kv)
            if self._pipeline_supervisor is not None:
                self._pipeline_supervisor.reconfigure()
                if kv.get('autopilot') is True:
                    self._pipeline_supervisor.wake()
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
                'report': d['autopilot_report'],
                'campaigns': d['autopilot_campaigns']}

    def _member_states(self, proj):
        """项目成员状态、续算轮次与远端结果是否已完整回到本地。"""
        out = []
        for d in self._project_member_dirs(proj):
            m = self._manifest.load_manifest(d)
            if m is None:
                out.append({'dir': d, 'state': None, 'restartable': False, 'rounds': 0})
                continue
            res = m.get('results') or {}
            dgn = res.get('diagnosis') or {}
            out.append({'dir': d, 'state': m.get('state'),
                        'restartable': bool(dgn.get('restartable')),
                        'rounds': int(res.get('continue_rounds', 0) or 0),
                        'local_results_ready': self._local_results_ready(d, m)})
        return out

    @staticmethod
    def _local_results_ready(job_dir, manifest):
        """当前调度轮次的结果已完整下载；旧轮次同名文件不能冒充新结果。"""
        if not manifest:
            return False
        remote_dir = str(manifest.get('remote_dir') or '')
        if not remote_dir:
            # 本地导入的已算结果没有远端身份，沿用其 manifest 状态/能量证据。
            return True
        job_id = str(manifest.get('scheduler_job_id') or '')
        results = manifest.get('results') or {}
        if (str(manifest.get('state') or '') != 'DONE'
                or not job_id or not results.get('fetched_at')
                or str(results.get('fetched_state') or '') != 'DONE'
                or str(results.get('fetched_job_id') or '') != job_id
                or str(results.get('fetched_remote_dir') or '') != remote_dir):
            return False
        try:
            from vcstudio.cluster.submitter import current_attempt_token
            if (str(results.get('fetched_attempt_token') or '')
                    != current_attempt_token(manifest)):
                return False
        except (ImportError, TypeError, ValueError):
            return False
        contract = results.get('fetch_contract') or {}
        if (not isinstance(contract, dict)
                or str(contract.get('state') or '') != 'DONE'
                or str(contract.get('scheduler_job_id') or '') != job_id
                or str(contract.get('remote_dir') or '') != remote_dir
                or str(contract.get('attempt_token') or '')
                != str(results.get('fetched_attempt_token') or '')):
            return False
        hashes = results.get('fetched_sha256') or {}
        sizes = results.get('fetched_sizes') or {}
        fetched = {str(name) for name in (results.get('fetched') or [])}
        for filename in fetched:
            path = os.path.join(job_dir, *filename.split('/'))
            try:
                if (not os.path.isfile(path)
                        or int(sizes.get(filename, -1)) != os.path.getsize(path)
                        or str(hashes.get(filename) or '') != _sha256_file(path)):
                    return False
            except (OSError, TypeError, ValueError):
                return False
        if str(manifest.get('task_type') or '') == 'neb':
            return bool(fetched)
        required = {'CONTCAR', 'OSZICAR', 'OUTCAR'}
        missing = {str(name) for name in (results.get('fetched_missing') or [])}
        return (required.issubset(fetched) and required.isdisjoint(missing)
                and required.issubset(set(hashes))
                and required.issubset(set(sizes)))

    def _project_stage(self, states, has_marker):
        """成员状态 + 报告标记 → (stage, needs_human, recover_round)。规则见 pipeline_status。"""
        all_states = [s['state'] for s in states]
        exhausted = any(s['state'] in _TERMINAL_FAIL and s['restartable']
                        and s['rounds'] >= 3 for s in states)
        needs_human = (any(s == 'NEEDS_HUMAN' for s in all_states) or exhausted
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
        elif any(s['restartable'] and s['state'] in _TERMINAL_FAIL and s['rounds'] < 3
                 for s in states):
            stage = 'recover'
        elif all_states and all(s == 'DONE' for s in all_states):
            if not all(s.get('local_results_ready', True) for s in states):
                stage = 'monitor'  # 已结束但结果尚未完整回收，不能提前进入分析
            else:
                stage = 'report_done' if has_marker else 'analysis'
        elif any(s in _TERMINAL_FAIL for s in all_states):
            stage = 'analysis'                            # 达上限/不可续算：转人工，不再伪装恢复中
        else:
            stage = 'generate'
        return stage, needs_human, recover_round

    def pipeline_status(self):
        """每项目管线阶段:生成→提交→监控→恢复(n/3)→分析→报告完成;NEEDS_HUMAN 红旗。"""
        try:
            snapshot = self._project_registry_snapshot()
            projs = []
            failures = copy.deepcopy(snapshot['failures'])
            failures.extend({
                'project_ref': f'ambiguous-project-{index}',
                'code': 'duplicate_project_id',
                'message': 'Registered project identity is ambiguous.',
            } for index, _project_id in enumerate(
                sorted(snapshot['duplicate_ids']), start=1))
            records = [
                record for record in snapshot['records']
                if record['project_id'] not in snapshot['duplicate_ids']
            ]
            for registered_index, record in enumerate(records, start=1):
                try:
                    proj = record['project']
                    states = self._member_states(proj)
                    summary = self._adsorption.delta_e_rows(proj)
                    raw_marker = (proj.get('autopilot_report')
                                  if isinstance(proj.get('autopilot_report'), dict)
                                  else {})
                    canonical = self._canonical_report_status(proj, summary)
                    has_marker = bool(canonical.get('artifact_current'))
                    raw_blocked = (proj.get('autopilot_report_blocked')
                                   if isinstance(proj.get('autopilot_report_blocked'), dict)
                                   else {})
                    has_legacy_blocked = self._blocked_report_marker_current(
                        proj, summary)
                    marker = raw_marker if has_marker else {}
                    use_legacy_blocked = bool(has_legacy_blocked and not raw_marker)
                    blocked = raw_blocked if use_legacy_blocked else {}
                    report_files = ((marker or {}).get('files') or
                                    (blocked or {}).get('files') or {})
                    public_report_files = {
                        str(fmt): True for fmt, value in report_files.items()
                        if value
                    }
                    raw_contracts = ((marker or {}).get('contracts') or {})
                    public_contracts = {
                        str(kind): {
                            field: contract.get(field)
                            for field in ('schema', 'sha256', 'file_sha256', 'size')
                            if field in contract
                        }
                        for kind, contract in raw_contracts.items()
                        if isinstance(contract, dict)
                    }
                    if raw_marker:
                        scientific_status = canonical['scientific_status']
                    elif use_legacy_blocked:
                        scientific_status = 'diagnostic'
                    else:
                        scientific_status = None
                    all_done = self._project_all_done(states)
                    eligible_now = bool(canonical.get('eligible_final'))
                    publication_gate_status = (
                        'pending' if not all_done else
                        'eligible' if eligible_now else 'blocked')
                    desired_report_kind = ('final' if eligible_now else 'diagnostic')
                    closes_pipeline = bool(
                        (has_marker and scientific_status != 'draft')
                        or use_legacy_blocked)
                    stage, needs_human, rr = self._project_stage(
                        states, closes_pipeline)
                    artifact_status = (
                        canonical['artifact_status'] if raw_marker else
                        'ready' if use_legacy_blocked else
                        'stale' if raw_blocked else 'missing')
                    report_reason = str(
                        (canonical.get('report_reason') if raw_marker else '')
                        or (blocked or {}).get('reason')
                        or (canonical.get('report_reason')
                            if publication_gate_status == 'blocked' else '')
                        or '')
                    qualification = (
                        (canonical.get('scientific_qualification') if raw_marker else '')
                        or ('diagnostic' if use_legacy_blocked else None))
                    projs.append({
                        'project_id': record['project_id'],
                        'name': proj.get('name', '') or '',
                        'profile': str((((proj.get('launch') or {}).get('resources') or {})
                                       .get('profile') or '')),
                        'stage': stage, 'stage_index': _STAGES.index(stage),
                        'stages': list(_STAGES), 'needs_human': needs_human,
                        'recover_round': rr,
                        'done': sum(1 for s in states if s['state'] == 'DONE'),
                        'total': len(states),
                        'artifact_status': artifact_status,
                        'artifact_current': bool(canonical.get('artifact_current')),
                        'scientific_status': scientific_status,
                        'scientific_qualification': qualification,
                        'scientific_stale': bool(canonical.get('scientific_stale')),
                        'publication_gate_status': publication_gate_status,
                        'desired_report_kind': desired_report_kind,
                        'report_kind': scientific_status,
                        # Compatibility alias: unlike the legacy implementation,
                        # this now reflects science state rather than file presence.
                        'report_status': scientific_status,
                        'report_reason': report_reason,
                        'report_files': public_report_files,
                        'report_contracts': public_contracts,
                    })
                except Exception:                         # noqa: BLE001 单个坏项目降级但不泄露路径
                    failures.append({
                        'project_ref': f'registered-project-{registered_index}',
                        'code': 'project_status_unavailable',
                        'message': 'Registered project status could not be read.',
                    })
            registered_total = snapshot['registered_total']
            successful_count = len(projs)
            failed_count = len(failures)
            if failed_count:
                status = 'unavailable' if successful_count == 0 else 'degraded'
                error = (
                    'No registered project status could be read.'
                    if status == 'unavailable' else
                    f'{failed_count} of {registered_total} registered project statuses '
                    f'could not be read; counts cover {successful_count} readable '
                    'projects only.'
                )
            else:
                status = 'ready'
                error = None
            return {
                'ok': status != 'unavailable',
                'status': status,
                'registered_total': registered_total,
                'successful_count': successful_count,
                'failed_count': failed_count,
                'failures': failures,
                'projects': projs,
                'error': error,
            }
        except Exception:                                 # noqa: BLE001 registry failure is public-safe
            return {
                'ok': False,
                'status': 'unavailable',
                'registered_total': None,
                'successful_count': 0,
                'failed_count': None,
                'failures': [{
                    'project_ref': 'project-registry',
                    'code': 'project_registry_unavailable',
                    'message': 'Registered projects could not be listed.',
                }],
                'projects': [],
                'error': 'Project registry status is unavailable.',
            }

    @staticmethod
    def _base(d):
        return os.path.basename(os.path.normpath(str(d)))

    def _tick_cluster(self, name, prof, pw, ap, events, errors):
        """单集群一轮；所有写远端动作只落在项目保存的显式提交白名单。"""
        def _key(path):
            return os.path.normcase(os.path.abspath(os.path.normpath(str(path))))

        managed_dirs = {}
        for registered_index, project_path in enumerate(
                self._adsorption.list_projects(), start=1):
            try:
                project = self._load_project_for_path(project_path)
                launch = (project or {}).get('launch') or {}
                resources = launch.get('resources') or {}
                if not (project or {}).get('autopilot_managed') or resources.get('profile') != name:
                    continue
                member_dirs = {
                    _key(path) for path in self._project_member_dirs(project)
                }
                submitted_dirs = launch.get('submitted_job_dirs') or []
                # 必须同时是该项目成员、且确由一站式提交成功后写入 launch 的目录。
                for path in submitted_dirs:
                    if path and _key(path) in member_dirs:
                        managed_dirs[_key(path)] = str(path)
            except Exception:                             # noqa: BLE001 isolated project
                errors.append(
                    f'集群「{name}」项目托管清单读取失败'
                    f'(registered-project-{registered_index})')

        def _in_scope(job_dir):
            # 空白名单必须 fail closed，绝不能退化成“台账里的所有旧任务”。
            return _key(job_dir) in managed_dirs

        sync_ok = True
        did_remote_action = False

        def _record_remote_failure(stage, _detail=None):
            nonlocal sync_ok
            sync_ok = False
            label = {'refresh': '刷新', 'continue': '续算', 'fetch': '下载'}.get(stage, stage)
            text = f'集群「{name}」{label}失败；请查看集群诊断'
            events.append({'kind': stage, 'cluster': name, 'text': text})
            errors.append(f'集群「{name}」同步失败({label})')

        # project.launch.submitted_job_dirs 是远程写操作的授权真源；台账只是 UI 索引，
        # 丢条目后仍须继续监控明确托管的项目，不能静默停管。
        entries = [(path, self._manifest.load_manifest(path))
                   for path in managed_dirs.values()]
        targets = [d for d, m in entries
                   if m and m.get('scheduler_job_id') and m.get('cluster') == name
                   and m.get('state') in ('SUBMITTED', 'QUEUED', 'RUNNING')
                   and _in_scope(d)]
        if targets:
            res = self._bo().refresh_batch(prof, pw, targets, False)
            if res.get('needs_trust'):
                events.append({'kind': 'skip',
                               'cluster': name,
                               'text': f'集群「{name}」主机指纹未信任,跳过本轮'})
                errors.append(f'集群「{name}」同步失败:主机指纹未信任')
                return False
            did_remote_action = True
            for d, note in (res.get('results') or []):
                events.append({'kind': 'refresh', 'cluster': name,
                               'text': f'集群「{name}」任务状态已刷新'})
                if str(note).startswith('查询失败:'):
                    _record_remote_failure('refresh')
        entries2 = [(path, self._manifest.load_manifest(path))
                    for path in managed_dirs.values()]
        # 续算:刷新后 restartable 终态且 attempts<3 → 自动续算(复用 filter_continuable)
        if ap['cont']:
            try:
                cdirs = [d for d, m in entries2
                         if m and m.get('cluster') == name and _in_scope(d)]
                eligible, _sk = self._bo().filter_continuable(cdirs)
                if eligible:
                    cres = self._bo().continue_batch(prof, pw, eligible, False)
                    if cres.get('needs_trust'):
                        _record_remote_failure('continue', '主机指纹未信任')
                        return False
                    did_remote_action = True
                    for d, ok, msg in (cres.get('results') or []):
                        events.append({'kind': 'continue', 'cluster': name,
                                       'text': f'集群「{name}」续算请求已处理'})
                        if not ok:
                            _record_remote_failure('continue')
            except Exception:                             # noqa: BLE001 isolated cluster
                _record_remote_failure('continue')
        # 拉回：不能只看“本轮新变 DONE”。应用可能在任务结束后才重启，此时本地
        # 没有 fetched_at/关键输出；每拍重新检查缺口，fetch_results 成功写 fetched_at，
        # 因而完整结果天然幂等，未完整的结果会在下拍继续补拉。
        if ap['fetch']:
            try:
                needs_fetch = []
                for job_dir, manifest in entries2:
                    if (not manifest or manifest.get('cluster') != name
                            or manifest.get('state') != 'DONE'
                            or not manifest.get('remote_dir') or not _in_scope(job_dir)):
                        continue
                    if not self._local_results_ready(job_dir, manifest):
                        needs_fetch.append(job_dir)
                if needs_fetch:
                    fres = self._bo().fetch_batch(prof, pw, needs_fetch, False)
                    if fres.get('needs_trust'):
                        _record_remote_failure('fetch', '主机指纹未信任')
                        return False
                    did_remote_action = True
                    for d, ok, msg in (fres.get('results') or []):
                        events.append({'kind': 'fetch', 'cluster': name,
                                       'text': f'集群「{name}」结果回收请求已处理'})
                        if not ok:
                            _record_remote_failure('fetch')
            except Exception:                             # noqa: BLE001 isolated cluster
                _record_remote_failure('fetch')
        return sync_ok and did_remote_action

    def _project_all_done(self, states):
        return bool(states) and all(s['state'] == 'DONE' for s in states)

    @staticmethod
    def _method_confirmation(project) -> dict:
        check = ((project or {}).get('preparation') or {}).get('method_check') or {}
        confirmation = check.get('confirmation') or {}
        return confirmation if isinstance(confirmation, dict) else {}

    def _final_report_gate(self, project, summary):
        """Return whether ``summary`` is a deliverable adsorption-energy result."""
        rows = list((summary or {}).get('rows') or [])
        if not (summary or {}).get('has_ref'):
            return False, '未设置有效气相/逐物种参考态；当前差值不是完整吸附能'
        if not rows:
            return False, '没有吸附构型结果'
        invalid_refs = [row.get('name') for row in rows if not row.get('reference_valid')]
        if invalid_refs:
            return False, '以下构型的参考态未通过完整性校验：' + '、'.join(
                str(name or '?') for name in invalid_refs)
        incomplete = [row.get('name') for row in rows
                      if _method_number(row.get('delta_e')) is None]
        if incomplete:
            return False, '以下构型尚无有效 ΔE：' + '、'.join(
                str(name or '?') for name in incomplete)
        method = (summary or {}).get('method_consistency') or {}
        status = str(method.get('status') or 'unverified')
        if status == 'incompatible':
            return False, '能量项方法不兼容：' + '；'.join(method.get('issues') or [])
        if status != 'verified':
            confirmation = self._method_confirmation(project)
            if not (confirmation.get('confirmed')
                    and str(confirmation.get('reason') or '').strip()):
                return False, '方法证据仍为 unverified，且没有保存带理由的人工确认'
        return True, ''

    def _report_scientific_payload(self, project, summary) -> dict:
        """Return path-independent scientific inputs for report contracts."""
        summary = summary if isinstance(summary, dict) else {}
        project_root = os.path.abspath(os.path.normpath(str(
            project.get('root') or os.curdir)))
        scope_state = summary.get('_report_scope') or {}
        scoped = isinstance(scope_state, dict) and 'included_member_ids' in scope_state
        included_member_ids = set(scope_state.get('included_member_ids') or [])
        members = []
        for index, job_dir in enumerate(self._project_member_dirs(project), 1):
            manifest = self._manifest.load_manifest(job_dir) or {}
            workspace_member_id = self._workspace_job_id(job_dir, manifest)
            if scoped and workspace_member_id not in included_member_ids:
                continue
            results = manifest.get('results') or {}
            attempts = []
            for attempt in manifest.get('attempts') or []:
                if not isinstance(attempt, dict):
                    continue
                attempts.append({
                    key: attempt.get(key) for key in (
                        'n', 'round', 'job_id', 'prev_job_id', 'action',
                        'result', 'reason', 'attempt_token', 'engine')
                    if key in attempt
                })
            members.append({
                'member_id': (workspace_member_id if scoped else
                              self._portable_member_id(
                                  job_dir, project_root, index, manifest)),
                'state': manifest.get('state'),
                'scheduler_job_id': manifest.get('scheduler_job_id'),
                'attempts': attempts,
                'energy_e0_eV': results.get('energy_e0_eV'),
                'fetched_job_id': results.get('fetched_job_id'),
                'fetched_attempt_token': results.get('fetched_attempt_token'),
                'fetched_sha256': results.get('fetched_sha256') or {},
                'fetched_sizes': results.get('fetched_sizes') or {},
            })
        reference_evidence = []
        selected_species = set(scope_state.get('selected_species') or [])
        for item in summary.get('species_reference_evidence') or []:
            if not isinstance(item, dict):
                continue
            if (scoped and selected_species
                    and str(item.get('species') or '') not in selected_species):
                continue
            reference_evidence.append({
                key: item.get(key) for key in (
                    'species', 'state', 'energy', 'valid', 'reference_valid',
                    'cache_refresh_needed', 'sha256', 'fingerprint', 'note')
                if key in item
            })
        confirmation = self._method_confirmation(project)
        return self._json_safe_report_result({
            'project_id': project.get('project_uuid') or project.get('name'),
            'members': members,
            'slab': summary.get('slab'),
            'ref': summary.get('ref'),
            'has_ref': bool(summary.get('has_ref')),
            'reference_mode': summary.get('reference_mode'),
            'species_reference_evidence': reference_evidence,
            'method_consistency': summary.get('method_consistency') or {},
            'method_confirmation': {
                key: confirmation.get(key) for key in (
                    'confirmed', 'reason', 'scope', 'confirmed_by',
                    'evidence_refs', 'allowed_claims') if key in confirmation
            },
            'rows': [{
                key: row.get(key) for key in (
                    'configuration_id', 'job_id', 'name', 'species', 'state',
                    'e_config', 'e_slab', 'e_ref', 'delta_e', 'dd_e',
                    'reference_valid', 'reference_state', 'method_status',
                    'note') if key in row
            } for row in (summary.get('rows') or [])],
        })

    def _report_scientific_fingerprint(self, project, summary) -> str:
        payload = self._report_scientific_payload(project, summary)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest()

    def _report_input_fingerprint(self, project, summary) -> str:
        """Bind a final report to member attempts, downloaded files and ΔE rows."""
        summary = summary if isinstance(summary, dict) else {}
        scope_state = summary.get('_report_scope') or {}
        scoped = isinstance(scope_state, dict) and 'included_member_ids' in scope_state
        included_member_ids = set(scope_state.get('included_member_ids') or [])
        members = []
        for job_dir in self._project_member_dirs(project):
            manifest = self._manifest.load_manifest(job_dir) or {}
            if (scoped and self._workspace_job_id(job_dir, manifest)
                    not in included_member_ids):
                continue
            results = manifest.get('results') or {}
            attempts = manifest.get('attempts') or []
            members.append({
                'dir': os.path.abspath(os.path.normpath(str(job_dir))),
                'state': manifest.get('state'),
                'job_id': manifest.get('scheduler_job_id'),
                'attempt': attempts[-1] if attempts else None,
                'energy_e0_eV': results.get('energy_e0_eV'),
                'fetched_job_id': results.get('fetched_job_id'),
                'fetched_remote_dir': results.get('fetched_remote_dir'),
                'fetched_sha256': results.get('fetched_sha256') or {},
            })
        reference_evidence = summary.get('species_reference_evidence') or []
        selected_species = set(scope_state.get('selected_species') or [])
        if scoped and selected_species:
            reference_evidence = [
                item for item in reference_evidence
                if isinstance(item, dict)
                and str(item.get('species') or '') in selected_species
            ]
        payload = {
            'project': project.get('project_uuid') or project.get('name'),
            'members': members,
            'reference_mode': summary.get('reference_mode'),
            'species_reference_evidence': reference_evidence,
            'method_consistency': summary.get('method_consistency') or {},
            'method_confirmation': self._method_confirmation(project),
            'rows': [{
                'name': row.get('name'), 'species': row.get('species'),
                'delta_e': row.get('delta_e'), 'dd_e': row.get('dd_e'),
                'reference_job': row.get('reference_job'),
                'reference_valid': row.get('reference_valid'),
            } for row in (summary.get('rows') or [])],
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), default=str).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _validated_report_contract_refs(contracts, kind, *, require_sidecars=False) -> dict:
        """Validate marker contract references; empty remains legacy-compatible."""
        refs = Api._json_safe_report_result(contracts or {})
        if not isinstance(refs, dict):
            raise TypeError('报告 contracts 必须为对象')
        if not refs:
            return {}
        expected = {
            'spec': 'vcstudio.report-spec/v1',
            'snapshot': 'vcstudio.report-snapshot/v1',
            'validation': 'vcstudio.report-validation/v1',
        }
        if set(refs) != set(expected):
            missing = sorted(set(expected) - set(refs))
            unknown = sorted(set(refs) - set(expected))
            detail = []
            if missing:
                detail.append('缺少 ' + '、'.join(missing))
            if unknown:
                detail.append('未知 ' + '、'.join(unknown))
            raise ValueError('报告 contracts 不完整：' + '；'.join(detail))
        normalized = {}
        for key, schema in expected.items():
            record = refs.get(key)
            if not isinstance(record, dict):
                raise TypeError(f'报告 contract {key} 必须为对象')
            if str(record.get('schema') or '') != schema:
                raise ValueError(f'报告 contract {key} schema 无效')
            digest = str(record.get('sha256') or '').strip().lower()
            if not re.fullmatch(r'[0-9a-f]{64}', digest):
                raise ValueError(f'报告 contract {key} sha256 无效')
            normalized[key] = dict(record)
            normalized[key]['sha256'] = digest
            if require_sidecars:
                sidecar = str(record.get('path') or '').strip()
                file_digest = str(record.get('file_sha256') or '').strip().lower()
                size = record.get('size')
                if (not sidecar or os.path.basename(sidecar) != sidecar
                        or sidecar in {'.', '..'}):
                    raise ValueError(f'报告 contract {key} sidecar 路径无效')
                if not re.fullmatch(r'[0-9a-f]{64}', file_digest):
                    raise ValueError(f'报告 contract {key} file_sha256 无效')
                if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                    raise ValueError(f'报告 contract {key} size 无效')
                normalized[key]['path'] = sidecar
                normalized[key]['file_sha256'] = file_digest
                normalized[key]['size'] = size
        validation = normalized['validation']
        status = str(validation.get('status') or '').strip().lower()
        final_allowed = validation.get('final_allowed')
        if status not in {'passed', 'passed_with_warnings', 'blocked', 'unknown'}:
            raise ValueError('报告 validation status 无效')
        if not isinstance(final_allowed, bool):
            raise TypeError('报告 validation final_allowed 必须为布尔值')
        if kind == 'final' and (
                status not in {'passed', 'passed_with_warnings'}
                or final_allowed is not True):
            raise ValueError('最终报告 marker 缺少通过且 final_allowed=true 的验证')
        if kind != 'final' and final_allowed is True:
            raise ValueError('非最终报告 marker 不得声明 final_allowed=true')
        if require_sidecars:
            report_model_digest = str(
                validation.get('report_model_sha256') or '').strip().lower()
            if not re.fullmatch(r'[0-9a-f]{64}', report_model_digest):
                raise ValueError('报告 validation report_model_sha256 无效')
            normalized['validation']['report_model_sha256'] = report_model_digest
        return normalized

    @staticmethod
    def _validate_report_contract_sidecars(files, contracts, *, kind,
                                             scientific_fingerprint,
                                             scientific_qualification,
                                             report_model_sha256) -> dict:
        """Validate full sidecar bytes, semantic digests, and cross-bindings."""
        from vcstudio.project.report_contracts import (
            ReportSnapshot,
            ReportSpec,
            ValidationResult,
            validate_bindings,
        )

        payloads = {}
        for key, record in contracts.items():
            sidecar_path = str((files or {}).get(f'contract_{key}') or '')
            if not sidecar_path or not os.path.isfile(sidecar_path):
                raise RuntimeError(
                    f'报告标记落盘失败：缺少 contract {key} sidecar')
            if os.path.basename(sidecar_path) != str(record.get('path') or ''):
                raise RuntimeError(
                    f'报告标记落盘失败：contract {key} 路径不一致')
            if _sha256_file(sidecar_path) != str(record.get('file_sha256') or ''):
                raise RuntimeError(
                    f'报告标记落盘失败：contract {key} 文件哈希不一致')
            if os.path.getsize(sidecar_path) != record.get('size'):
                raise RuntimeError(
                    f'报告标记落盘失败：contract {key} 文件大小不一致')
            try:
                with open(sidecar_path, 'r', encoding='utf-8') as handle:
                    payload = json.load(handle)
            except Exception as exc:                     # noqa: BLE001
                raise RuntimeError(
                    f'报告标记落盘失败：contract {key} 无法解析：{exc}') from exc
            if not isinstance(payload, dict):
                raise RuntimeError(
                    f'报告标记落盘失败：contract {key} 必须为对象')
            payloads[key] = payload

        try:
            spec = ReportSpec.from_mapping(payloads['spec'])
            snapshot = ReportSnapshot.from_mapping(payloads['snapshot'], spec=spec)
            validation = ValidationResult.from_mapping(
                payloads['validation'], spec=spec, snapshot=snapshot)
            validate_bindings(spec, snapshot, validation)
        except Exception as exc:                         # noqa: BLE001
            raise RuntimeError(
                f'报告标记落盘失败：contract 绑定或内容无效：{exc}') from exc

        semantic = {
            'spec': spec.semantic_sha256,
            'snapshot': snapshot.semantic_sha256,
            'validation': validation.semantic_sha256,
        }
        for key, digest in semantic.items():
            if digest != contracts[key]['sha256']:
                raise RuntimeError(
                    f'报告标记落盘失败：contract {key} 语义哈希不一致')
        if snapshot.input_fingerprint != scientific_fingerprint:
            raise RuntimeError('报告标记落盘失败：snapshot 输入指纹不一致')
        declared_snapshot_input = str(
            contracts['snapshot'].get('input_fingerprint') or '')
        if declared_snapshot_input and declared_snapshot_input != scientific_fingerprint:
            raise RuntimeError('报告标记落盘失败：snapshot ref 输入指纹不一致')
        if validation.effective_kind != kind:
            raise RuntimeError('报告标记落盘失败：validation 科学状态不一致')
        if validation.scientific_qualification != scientific_qualification:
            raise RuntimeError('报告标记落盘失败：validation 科学资格不一致')
        if validation.report_model_sha256 != report_model_sha256:
            raise RuntimeError('报告标记落盘失败：validation 正文指纹不一致')
        validation_ref = contracts['validation']
        if (str(validation_ref.get('status') or '') != validation.status
                or validation_ref.get('final_allowed') is not validation.final_allowed
                or validation_ref.get('report_model_sha256')
                != validation.report_model_sha256):
            raise RuntimeError('报告标记落盘失败：validation ref 元数据不一致')
        return payloads

    @staticmethod
    def _validate_report_manifest(path, *, kind, model_sha256,
                                  scientific_fingerprint, contracts,
                                  scientific_qualification, files,
                                  report_model_sha256, revision=None) -> dict:
        """Validate the committed manifest before it may anchor a ready marker."""
        manifest_path = str(path or '')
        if not manifest_path or not os.path.isfile(manifest_path):
            raise RuntimeError('报告标记落盘失败：manifest 不存在或不可读')
        try:
            with open(manifest_path, 'r', encoding='utf-8') as handle:
                payload = json.load(handle)
        except Exception as exc:                            # noqa: BLE001
            raise RuntimeError(f'报告标记落盘失败：manifest 无法解析：{exc}') from exc
        if not isinstance(payload, dict):
            raise RuntimeError('报告标记落盘失败：manifest 必须为对象')
        if str(payload.get('schema') or '') != 'vcstudio.paper-report.bundle/v2':
            raise RuntimeError('报告标记落盘失败：manifest schema 无效')
        if str(payload.get('artifact_status') or '') != 'complete':
            raise RuntimeError('报告标记落盘失败：manifest 未声明 complete')
        for field in ('report_kind', 'scientific_status'):
            if str(payload.get(field) or '').strip().lower() != kind:
                raise RuntimeError('报告标记落盘失败：manifest 科学状态不一致')
        if (str(payload.get('scientific_qualification') or '').strip().lower()
                != scientific_qualification):
            raise RuntimeError('报告标记落盘失败：manifest 科学资格不一致')
        declared_model = str(payload.get('model_sha256') or '').strip().lower()
        expected_model = str(model_sha256 or '').strip().lower()
        if (not re.fullmatch(r'[0-9a-f]{64}', declared_model)
                or not re.fullmatch(r'[0-9a-f]{64}', expected_model)
                or declared_model != expected_model):
            raise RuntimeError('报告标记落盘失败：manifest model_sha256 不一致')
        declared_report_model = str(
            payload.get('report_model_sha256') or '').strip().lower()
        expected_report_model = str(report_model_sha256 or '').strip().lower()
        if (not re.fullmatch(r'[0-9a-f]{64}', declared_report_model)
                or not re.fullmatch(r'[0-9a-f]{64}', expected_report_model)
                or declared_report_model != expected_report_model):
            raise RuntimeError(
                '报告标记落盘失败：manifest report_model_sha256 不一致')
        model_path = str((files or {}).get('model') or '')
        model_record = payload.get('model_file')
        if (not model_path or not os.path.isfile(model_path)
                or not isinstance(model_record, dict)):
            raise RuntimeError('报告标记落盘失败：冻结 model sidecar 缺失')
        if os.path.basename(model_path) != str(model_record.get('path') or ''):
            raise RuntimeError('报告标记落盘失败：model sidecar 路径不一致')
        model_file_sha256 = _sha256_file(model_path)
        if (model_file_sha256 != str(model_record.get('sha256') or '').lower()
                or model_file_sha256 != expected_model
                or os.path.getsize(model_path) != model_record.get('size')):
            raise RuntimeError('报告标记落盘失败：model sidecar 哈希或大小不一致')
        try:
            with open(model_path, 'rb') as handle:
                frozen_model_bytes = handle.read()
            frozen_model = json.loads(frozen_model_bytes.decode('utf-8'))
        except Exception as exc:                         # noqa: BLE001
            raise RuntimeError(
                f'报告标记落盘失败：model sidecar 无法解析：{exc}') from exc
        if not isinstance(frozen_model, dict):
            raise RuntimeError('报告标记落盘失败：model sidecar 必须为对象')
        try:
            canonical_model_bytes = json.dumps(
                frozen_model, ensure_ascii=False, sort_keys=True,
                separators=(',', ':'), allow_nan=False).encode('utf-8')
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f'报告标记落盘失败：model sidecar 不是规范 JSON：{exc}') from exc
        if (canonical_model_bytes != frozen_model_bytes
                or hashlib.sha256(canonical_model_bytes).hexdigest()
                != expected_model):
            raise RuntimeError('报告标记落盘失败：model sidecar 不是规范冻结模型')
        try:
            from vcstudio.project.paper_report import frozen_report_content_sha256

            frozen_report_model = frozen_report_content_sha256(frozen_model)
        except Exception as exc:                         # noqa: BLE001
            raise RuntimeError(
                f'报告标记落盘失败：model sidecar 正文投影无法复算：{exc}') from exc
        if frozen_report_model != expected_report_model:
            raise RuntimeError(
                '报告标记落盘失败：model sidecar 正文指纹与 validation 不一致')
        declared_input = str(payload.get('input_fingerprint') or '').strip().lower()
        if (not re.fullmatch(r'[0-9a-f]{64}', declared_input)
                or declared_input != scientific_fingerprint):
            raise RuntimeError('报告标记落盘失败：manifest 输入指纹不一致')
        declared_contracts = payload.get('contracts')
        if not isinstance(declared_contracts, dict):
            raise RuntimeError('报告标记落盘失败：manifest contracts 无效')
        if declared_contracts != contracts:
            raise RuntimeError('报告标记落盘失败：manifest contracts 不一致')
        if revision is not None:
            declared_revision = payload.get('revision')
            expected_revision = Api._json_safe_report_result(revision)
            if declared_revision != expected_revision:
                raise RuntimeError('报告标记落盘失败：manifest revision 不一致')

        report_files = {
            fmt: str(value) for fmt, value in (files or {}).items()
            if fmt in {'html', 'docx', 'pdf'}
        }
        declared_formats = payload.get('formats')
        declared_files = payload.get('files')
        if (not isinstance(declared_formats, list)
                or set(map(str, declared_formats)) != set(report_files)
                or not isinstance(declared_files, dict)
                or set(declared_files) != set(report_files)):
            raise RuntimeError('报告标记落盘失败：manifest 格式清单不一致')
        for fmt, report_path in report_files.items():
            record = declared_files.get(fmt)
            if not isinstance(record, dict):
                raise RuntimeError(
                    f'报告标记落盘失败：manifest 文件记录 {fmt} 无效')
            if os.path.basename(report_path) != str(record.get('path') or ''):
                raise RuntimeError(
                    f'报告标记落盘失败：manifest 文件路径 {fmt} 不一致')
            if (_sha256_file(report_path) != str(record.get('sha256') or '').lower()
                    or os.path.getsize(report_path) != record.get('size')):
                raise RuntimeError(
                    f'报告标记落盘失败：manifest 文件记录 {fmt} 不一致')
        return payload

    def _persist_blocked_report_marker(self, project, summary, reason, files):
        """Compatibility wrapper: new diagnostic artifacts use the canonical marker."""
        return self._persist_report_marker(
            project, summary, files, kind='diagnostic', reason=reason)

    def _persist_report_marker(self, project, summary, files, *, kind='final',
                               reason='', figures_dir=None, model_sha256=None,
                               report_model_sha256=None,
                               contracts=None, scientific_qualification=None,
                               manifest=None, project_path=None,
                               expected_input_fingerprint=None,
                               expected_scientific_fingerprint=None,
                               expected_project_id=None,
                               workspace_project_id=None, report_spec=None,
                               revision=None):
        """Atomically persist one canonical report artifact marker.

        ``kind`` is the scientific status; successful persistence only proves
        that files exist and match their hashes.  These axes must remain
        independent so a diagnostic artifact can never be displayed as final.
        """
        kind = str(kind or '').strip().lower()
        if kind not in _REPORT_KINDS:
            raise ValueError('报告 marker kind 必须为 final、diagnostic 或 draft')
        qualification = str(scientific_qualification or (
            'adsorption_result_verified' if kind == 'final' else 'diagnostic'
        )).strip().lower()
        if qualification not in _REPORT_QUALIFICATIONS:
            raise ValueError('报告 scientific_qualification 无效')
        if kind == 'final' and qualification == 'diagnostic':
            raise ValueError('最终报告不能使用 diagnostic 科学资格')
        if kind != 'final' and qualification != 'diagnostic' and not contracts:
            raise ValueError('无 contract 的非最终报告只能使用 diagnostic 科学资格')
        if kind == 'final' and not contracts:
            raise ValueError('最终报告 marker 必须包含完整 contract 链与 manifest')
        normalized_contracts = self._validated_report_contract_refs(
            contracts, kind, require_sidecars=bool(contracts))

        with self._report_marker_lock:
            current_project = project
            current_summary = summary
            if project_path:
                current_project = self._load_project_for_path(str(project_path).strip())
                if current_project is None:
                    raise _ReportInputChanged('报告已生成但项目在落盘前被移动或删除，未登记产物')
                current_summary = self._adsorption.delta_e_rows(current_project)
            current_project_id = str(
                current_project.get('project_uuid') or current_project.get('name') or '')
            if expected_project_id and current_project_id != str(expected_project_id):
                raise _ReportInputChanged('报告已生成但项目身份已变化，未登记产物')
            if report_spec is not None:
                if not str(workspace_project_id or '').strip():
                    raise _ReportInputChanged(
                        '报告已生成但缺少工作台项目身份，未登记 scoped 产物')
                try:
                    current_summary = self._report_workbench_scope_summary(
                        {
                            'project': current_project,
                            'project_id': str(workspace_project_id),
                        },
                        current_summary,
                        report_spec,
                    )
                except Exception as exc:                 # noqa: BLE001 scope CAS boundary
                    raise _ReportInputChanged(
                        f'报告生成期间冻结范围无法重放，产物未登记：{exc}') from exc
            current_input_fingerprint = self._report_input_fingerprint(
                current_project, current_summary)
            current_scientific_fingerprint = self._report_scientific_fingerprint(
                current_project, current_summary)
            if (expected_input_fingerprint
                    and current_input_fingerprint != expected_input_fingerprint):
                raise _ReportInputChanged('报告生成期间作业或结果已变化，产物未登记；请重新生成')
            if (expected_scientific_fingerprint
                    and current_scientific_fingerprint
                    != expected_scientific_fingerprint):
                raise _ReportInputChanged('报告生成期间科学输入已变化，产物未登记；请重新生成')
            if kind == 'final':
                eligible, gate_reason = self._final_report_gate(
                    current_project, current_summary)
                if not eligible:
                    raise _ReportInputChanged(
                        '报告生成期间最终门禁已变化，产物未登记：' + gate_reason)

            items = (files.items() if isinstance(files, dict)
                     else ((None, path) for path in files))
            by_format = {}
            for declared_format, raw_path in items:
                if not raw_path or not os.path.isfile(str(raw_path)):
                    continue
                output_path = os.path.abspath(str(raw_path))
                suffix = str(declared_format or '').strip().lower()
                if not suffix:
                    suffix = (os.path.splitext(output_path)[1].lower().lstrip('.')
                              or 'file')
                by_format[suffix] = output_path
            if not by_format:
                raise RuntimeError('报告标记落盘失败：没有可读的报告文件')
            if not {'html', 'docx', 'pdf'}.intersection(by_format):
                raise RuntimeError('报告标记落盘失败：没有可读的报告产物格式')
            manifest_path = os.path.abspath(str(
                manifest or by_format.get('manifest') or '')) if (
                    manifest or by_format.get('manifest')) else ''
            marker_manifest = by_format.get('manifest')
            if manifest_path and (not marker_manifest
                                  or os.path.normcase(os.path.abspath(marker_manifest))
                                  != os.path.normcase(manifest_path)):
                raise RuntimeError(
                    '报告标记落盘失败：manifest 未包含在哈希绑定文件中')
            if kind == 'final' and not manifest_path:
                raise RuntimeError('报告标记落盘失败：最终报告缺少 manifest')
            if normalized_contracts:
                normalized_report_model_sha256 = str(
                    report_model_sha256 or '').strip().lower()
                if not re.fullmatch(r'[0-9a-f]{64}', normalized_report_model_sha256):
                    raise RuntimeError(
                        '报告标记落盘失败：缺少有效 report_model_sha256')
                self._validate_report_contract_sidecars(
                    by_format, normalized_contracts, kind=kind,
                    scientific_fingerprint=current_scientific_fingerprint,
                    scientific_qualification=qualification,
                    report_model_sha256=normalized_report_model_sha256)
            else:
                normalized_report_model_sha256 = ''
            if normalized_contracts or manifest_path:
                self._validate_report_manifest(
                    manifest_path, kind=kind, model_sha256=model_sha256,
                    scientific_fingerprint=current_scientific_fingerprint,
                    contracts=normalized_contracts,
                    scientific_qualification=qualification, files=by_format,
                    report_model_sha256=normalized_report_model_sha256,
                    revision=revision)
            hashes = {fmt: _sha256_file(path) for fmt, path in by_format.items()}
            primary = (by_format.get('pdf') or by_format.get('html')
                       or next(iter(by_format.values())))
            generated_at = time.strftime('%Y-%m-%dT%H:%M:%S')
            marker = {
                'schema': 'vcstudio.report-marker/v2',
                'generated_at': generated_at,
                'input_fingerprint': current_input_fingerprint,
                'scientific_fingerprint': current_scientific_fingerprint,
                'artifact_status': 'ready',
                'kind': kind,
                'scientific_status': kind,
                'scientific_qualification': qualification,
                'gate_reason': str(reason or ''),
                'file': primary,                         # 旧界面只读兼容
                'report_sha256': _sha256_file(primary),
                'files': by_format,
                'sha256': hashes,
                'report_hashes': hashes,                 # 旧自动报告 marker 兼容
                'figures_dir': os.path.abspath(str(
                    figures_dir or os.path.dirname(primary))),
                'model_sha256': str(model_sha256 or ''),
                'report_model_sha256': normalized_report_model_sha256,
                'contracts': normalized_contracts,
                'manifest': (os.path.abspath(manifest_path)
                             if manifest_path else ''),
                'workspace_project_id': str(workspace_project_id or ''),
                'revision': self._json_safe_report_result(revision or {}),
            }
            root = current_project.get('root') or os.path.dirname(primary)
            persisted = dict(current_project)
            persisted['autopilot_report'] = marker
            if kind == 'draft':
                persisted.pop('autopilot_report_done', None)
            else:
                persisted['autopilot_report_done'] = generated_at
            persisted.pop('autopilot_report_blocked', None)
            try:
                self._adsorption.save_project(root, persisted)
            except Exception as exc:                     # noqa: BLE001
                raise RuntimeError(f'报告标记落盘失败：{exc}') from exc
            current_project['autopilot_report'] = marker
            if kind == 'draft':
                current_project.pop('autopilot_report_done', None)
            else:
                current_project['autopilot_report_done'] = generated_at
            current_project.pop('autopilot_report_blocked', None)
            if project is not current_project:
                project['autopilot_report'] = marker
                if kind == 'draft':
                    project.pop('autopilot_report_done', None)
                else:
                    project['autopilot_report_done'] = generated_at
                project.pop('autopilot_report_blocked', None)
            return marker

    def _report_marker_scoped_summary(self, project, marker, summary):
        """Reapply the frozen workbench scope before checking marker freshness."""
        current = summary or self._adsorption.delta_e_rows(project)
        workspace_project_id = str(
            (marker or {}).get('workspace_project_id') or '').strip()
        if not workspace_project_id:
            return current, None
        files = marker.get('files') if isinstance(marker.get('files'), dict) else {}
        spec_path = str(files.get('contract_spec') or '')
        if not spec_path or not os.path.isfile(spec_path):
            return None, '报告 marker 缺少冻结 ReportSpec sidecar'
        try:
            from vcstudio.project.report_contracts import ReportSpec

            with open(spec_path, 'r', encoding='utf-8') as handle:
                spec = ReportSpec.from_mapping(json.load(handle))
            spec_ref = ((marker.get('contracts') or {}).get('spec')
                        if isinstance(marker.get('contracts'), dict) else {}) or {}
            if str(spec_ref.get('sha256') or '') != spec.semantic_sha256:
                raise ValueError('ReportSpec sidecar 与 marker contract ref 不一致')
            expected_scope_sha256 = self._report_scope_sha256(spec)
            current_scope = (current.get('_report_scope')
                             if isinstance(current, dict) else None)
            if (isinstance(current_scope, dict)
                    and current_scope.get('scope_sha256') == expected_scope_sha256):
                return current, None
            # A caller may have supplied a summary filtered by a different
            # ReportSpec.  Re-expanding from the project is the only safe way to
            # replay the marker's frozen selector; filtering an already-filtered
            # projection could silently drop selected configurations.
            if isinstance(current_scope, dict):
                current = self._adsorption.delta_e_rows(project)
            context = {
                'project': project,
                'project_id': workspace_project_id,
            }
            return self._report_workbench_scope_summary(context, current, spec), None
        except Exception as exc:                          # noqa: BLE001 fail-closed status
            return None, f'冻结 ReportSpec 范围无法重放：{exc}'

    def _comparison_report_marker_science_state(self, project, marker):
        """Validate a comparison marker against every frozen project input."""
        marker = marker if isinstance(marker, dict) else {}
        raw_kind = str(marker.get('kind') or '').strip().lower()
        qualification = str(
            marker.get('scientific_qualification') or '').strip().lower()

        def stale(reason):
            return {
                'kind': raw_kind if raw_kind in _REPORT_KINDS else 'diagnostic',
                'qualification': (qualification
                                  if qualification in _REPORT_QUALIFICATIONS
                                  else 'diagnostic'),
                'reason': str(reason or '比较报告 marker 验证失败'),
                'stale': True,
                'eligible_final': False,
                'gate_reason': str(reason or ''),
            }

        try:
            if str(marker.get('scope_kind') or '') != 'comparison':
                return stale('比较报告 marker 缺少 comparison scope 标记')
            if raw_kind not in _REPORT_KINDS:
                return stale('比较报告 marker kind 无效')
            if str(marker.get('scientific_status') or '') != raw_kind:
                return stale('比较报告 marker 科学状态不一致')
            if qualification not in _REPORT_QUALIFICATIONS:
                return stale('比较报告 marker 科学资格无效')
            if raw_kind == 'final' and qualification != 'thermodynamic_path_verified':
                return stale('最终比较报告缺少 thermodynamic_path_verified 资格')
            if raw_kind != 'final' and qualification != 'diagnostic':
                return stale('非最终比较报告声明了越权科学资格')
            if str(marker.get('artifact_status') or '') != 'ready':
                return stale('比较报告 marker 未声明 ready')
            files = marker.get('files') if isinstance(marker.get('files'), dict) else {}
            spec_path = str(files.get('contract_spec') or '')
            if not spec_path or not os.path.isfile(spec_path):
                return stale('比较报告 marker 缺少 ReportSpec sidecar')
            from vcstudio.project.report_contracts import ReportSpec

            with open(spec_path, 'r', encoding='utf-8') as handle:
                spec = ReportSpec.from_mapping(json.load(handle))
            if str(spec.scope.get('kind') or '') != 'comparison':
                return stale('比较报告 ReportSpec scope 无效')
            project_ids = [str(item) for item in spec.scope.get('project_ids') or []]
            if project_ids != list(marker.get('comparison_project_ids') or []):
                return stale('比较报告 marker 与 ReportSpec 项目身份不一致')
            workspace_id = str(marker.get('workspace_project_id') or '')
            if not project_ids or workspace_id != project_ids[0]:
                return stale('比较报告锚点身份无效')
            refs = marker.get('contracts') if isinstance(marker.get('contracts'), dict) else {}
            if str((refs.get('spec') or {}).get('sha256') or '') != spec.semantic_sha256:
                return stale('比较报告 ReportSpec 与 contract ref 不一致')
            index, _projects = self._analysis_workbench_project_index()
            anchor_path = index.get(workspace_id)
            if not anchor_path:
                return stale('比较报告锚点项目已无法从注册表解析')
            current = self._comparison_report_current_state(anchor_path, spec)
            scientific_fingerprint = str(
                marker.get('scientific_fingerprint') or '').strip().lower()
            if (not re.fullmatch(r'[0-9a-f]{64}', scientific_fingerprint)
                    or scientific_fingerprint
                    != current.get('scientific_fingerprint')):
                return stale('比较报告科学输入指纹已失效')
            report_model_sha256 = str(
                marker.get('report_model_sha256') or '').strip().lower()
            normalized_contracts = self._validated_report_contract_refs(
                refs, raw_kind, require_sidecars=True)
            self._validate_report_contract_sidecars(
                files, normalized_contracts, kind=raw_kind,
                scientific_fingerprint=scientific_fingerprint,
                scientific_qualification=qualification,
                report_model_sha256=report_model_sha256)
            manifest_path = str(marker.get('manifest') or '')
            if (not manifest_path or not files.get('manifest')
                    or self._analysis_workbench_path_key(manifest_path)
                    != self._analysis_workbench_path_key(files['manifest'])):
                return stale('比较报告 manifest 未纳入 marker 文件绑定')
            self._validate_report_manifest(
                manifest_path, kind=raw_kind,
                model_sha256=marker.get('model_sha256'),
                scientific_fingerprint=scientific_fingerprint,
                contracts=normalized_contracts,
                scientific_qualification=qualification,
                files=files,
                report_model_sha256=report_model_sha256,
                revision=marker.get('revision') or {})
            eligible = bool(current.get('eligible_final'))
            desired = 'final' if eligible else 'diagnostic'
            gate_reason = str(current.get('gate_reason') or '')
            if raw_kind != 'draft' and raw_kind != desired:
                return stale(
                    '当前比较门禁已通过，诊断报告需升级为最终报告'
                    if desired == 'final' else gate_reason or '比较门禁已阻断')
            return {
                'kind': raw_kind,
                'qualification': qualification,
                'reason': '' if raw_kind == 'final' else gate_reason,
                'stale': False,
                'eligible_final': eligible,
                'gate_reason': gate_reason,
            }
        except Exception as exc:                          # noqa: BLE001 fail closed
            return stale(f'比较报告验证记录无效：{exc}')

    def _report_marker_current(self, project, summary=None) -> bool:
        marker = (project or {}).get('autopilot_report')
        if not isinstance(marker, dict):
            return False
        if marker.get('scope_kind') == 'comparison':
            try:
                declared = marker.get('files')
                hashes = marker.get('sha256') or marker.get('report_hashes')
                if (not isinstance(declared, dict) or not declared
                        or not isinstance(hashes, dict)
                        or not {'html', 'docx', 'pdf'}.intersection(declared)):
                    return False
                if not all(
                        os.path.isfile(str(path))
                        and str(hashes.get(fmt) or '')
                        and _sha256_file(str(path)) == str(hashes.get(fmt))
                        for fmt, path in declared.items()):
                    return False
                return not self._comparison_report_marker_science_state(
                    project, marker).get('stale', True)
            except Exception:                            # noqa: BLE001 stale on uncertainty
                return False
        try:
            current = summary or self._adsorption.delta_e_rows(project)
            current, scope_error = self._report_marker_scoped_summary(
                project, marker, current)
            if scope_error:
                return False
            scientific = str(marker.get('scientific_fingerprint') or '').strip().lower()
            if scientific:
                if (not re.fullmatch(r'[0-9a-f]{64}', scientific)
                        or scientific != self._report_scientific_fingerprint(
                            project, current)):
                    return False
            elif marker.get('input_fingerprint') != self._report_input_fingerprint(
                    project, current):
                return False
            declared = marker.get('files')
            hashes = marker.get('sha256') or marker.get('report_hashes')
            if isinstance(declared, dict) and isinstance(hashes, dict) and declared:
                if not {'html', 'docx', 'pdf'}.intersection(declared):
                    return False
                files_current = all(
                    os.path.isfile(str(path))
                    and str(hashes.get(fmt) or '')
                    and _sha256_file(str(path)) == str(hashes.get(fmt))
                    for fmt, path in declared.items())
                if not files_current:
                    return False
                return not self._report_marker_science_state(
                    project, marker, current).get('stale', True)
            report_path = str(marker.get('file') or '')
            if not report_path or not os.path.isfile(report_path):
                return False
            expected_hash = str(marker.get('report_sha256') or '')
            if not expected_hash or _sha256_file(report_path) != expected_hash:
                return False
            return not self._report_marker_science_state(
                project, marker, current).get('stale', True)
        except Exception:                                # noqa: BLE001 失效即重建，绝不误报完成
            return False

    def _blocked_report_marker_current(self, project, summary=None) -> bool:
        """Validate legacy ``autopilot_report_blocked`` records fail closed."""
        marker = (project or {}).get('autopilot_report_blocked')
        if not isinstance(marker, dict):
            return False
        try:
            current = summary or self._adsorption.delta_e_rows(project)
            if marker.get('input_fingerprint') != self._report_input_fingerprint(
                    project, current):
                return False
            declared = marker.get('files') or {}
            hashes = marker.get('sha256') or marker.get('report_hashes') or {}
            if not isinstance(declared, dict) or not declared:
                return False
            for fmt, path in declared.items():
                if not os.path.isfile(str(path)):
                    return False
                if hashes and _sha256_file(str(path)) != str(hashes.get(fmt) or ''):
                    return False
            return True
        except Exception:                                # noqa: BLE001
            return False

    def _report_marker_science_state(self, project, marker, summary=None) -> dict:
        """Resolve current science state and staleness without trusting marker text."""
        marker = marker if isinstance(marker, dict) else {}
        if marker.get('scope_kind') == 'comparison':
            return self._comparison_report_marker_science_state(project, marker)
        current = summary or self._adsorption.delta_e_rows(project)
        raw_kind = str(marker.get('kind') or '').strip().lower()
        stored_reason = str(
            marker.get('gate_reason') or marker.get('reason') or '')
        contracts = marker.get('contracts') if isinstance(
            marker.get('contracts'), dict) else {}

        def _stale(reason, *, kind='diagnostic', qualification='diagnostic'):
            return {
                'kind': kind,
                'qualification': qualification,
                'reason': str(reason or '报告 marker 验证失败'),
                'stale': True,
            }

        current, scope_error = self._report_marker_scoped_summary(
            project, marker, current)
        if scope_error:
            return _stale(scope_error, kind=raw_kind or 'diagnostic')
        eligible, current_reason = self._final_report_gate(project, current)
        desired_kind = 'final' if eligible else 'diagnostic'

        if raw_kind and raw_kind not in _REPORT_KINDS:
            return _stale(f'报告 marker kind 无效：{raw_kind}')

        # A missing kind is the only accepted legacy-final shape.  Both it and
        # explicit final markers must pass today's scientific gate.  Modern
        # metadata on a kind-less marker is ambiguous and therefore not legacy.
        if not raw_kind:
            if contracts or marker.get('manifest') or marker.get('scientific_fingerprint'):
                return _stale('报告 marker 缺少显式 kind，不能验证现代 contract 元数据')
            if not eligible:
                return _stale(current_reason or stored_reason)
            return {
                'kind': 'final',
                'qualification': 'adsorption_result_verified',
                'reason': '',
                'stale': False,
            }

        if str(marker.get('artifact_status') or '').strip().lower() != 'ready':
            return _stale('报告 marker 未声明 artifact_status=ready')
        declared_science = str(marker.get('scientific_status') or '').strip().lower()
        if declared_science != raw_kind:
            return _stale('报告 marker kind 与 scientific_status 不一致')
        qualification = str(
            marker.get('scientific_qualification') or '').strip().lower()
        if qualification not in _REPORT_QUALIFICATIONS:
            return _stale('报告 marker scientific_qualification 无效')
        if raw_kind == 'final' and qualification == 'diagnostic':
            return _stale('最终报告 marker 使用了 diagnostic 科学资格')
        if raw_kind != 'final' and qualification != 'diagnostic' and not contracts:
            return _stale('无 contract 的非最终报告 marker 声明了越权科学资格')

        scientific_fingerprint = str(
            marker.get('scientific_fingerprint') or '').strip().lower()
        if (not re.fullmatch(r'[0-9a-f]{64}', scientific_fingerprint)
                or scientific_fingerprint
                != self._report_scientific_fingerprint(project, current)):
            return _stale('报告 marker 科学输入指纹已失效', kind=raw_kind,
                          qualification=qualification)

        modern_files = marker.get('files') if isinstance(marker.get('files'), dict) else {}
        if raw_kind == 'final' and not contracts:
            return _stale('最终报告 marker 缺少完整 contract 验证链')
        if contracts:
            try:
                if str(marker.get('schema') or '') != 'vcstudio.report-marker/v2':
                    raise ValueError('marker schema 无效')
                report_model_sha256 = str(
                    marker.get('report_model_sha256') or '').strip().lower()
                if not re.fullmatch(r'[0-9a-f]{64}', report_model_sha256):
                    raise ValueError('report_model_sha256 无效')
                normalized_contracts = self._validated_report_contract_refs(
                    contracts, raw_kind, require_sidecars=True)
                self._validate_report_contract_sidecars(
                    modern_files, normalized_contracts, kind=raw_kind,
                    scientific_fingerprint=scientific_fingerprint,
                    scientific_qualification=qualification,
                    report_model_sha256=report_model_sha256)
                manifest_path = str(marker.get('manifest') or '')
                marker_manifest = str(modern_files.get('manifest') or '')
                if (not manifest_path or not marker_manifest
                        or os.path.normcase(os.path.abspath(manifest_path))
                        != os.path.normcase(os.path.abspath(marker_manifest))):
                    raise RuntimeError('manifest 路径未纳入 marker 文件绑定')
                self._validate_report_manifest(
                    manifest_path, kind=raw_kind,
                    model_sha256=marker.get('model_sha256'),
                    scientific_fingerprint=scientific_fingerprint,
                    contracts=normalized_contracts,
                    scientific_qualification=qualification,
                    files=modern_files,
                    report_model_sha256=report_model_sha256,
                    revision=marker.get('revision') or {})
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                return _stale(
                    f'报告验证记录无效：{exc}',
                    kind=raw_kind,
                    qualification=qualification,
                )
        elif marker.get('manifest') or any(
                key.startswith('contract_') or key == 'manifest'
                for key in modern_files):
            return _stale('报告 marker 的 manifest/contract 元数据不完整')

        if raw_kind == 'draft':
            return {
                'kind': 'draft',
                'qualification': qualification,
                'reason': stored_reason,
                'stale': False,
            }
        if raw_kind != desired_kind:
            if desired_kind == 'final':
                return _stale(
                    '当前最终门禁已通过，已有诊断报告需升级为最终报告',
                    kind=raw_kind, qualification=qualification)
            return _stale(current_reason or stored_reason,
                          kind=raw_kind, qualification=qualification)
        return {
            'kind': raw_kind,
            'qualification': qualification,
            'reason': '' if raw_kind == 'final' else (current_reason or stored_reason),
            'stale': False,
        }

    def _report_marker_kind(self, project, marker, summary=None) -> str:
        """Compatibility wrapper returning the validated marker science kind."""
        return self._report_marker_science_state(
            project, marker, summary).get('kind') or 'diagnostic'

    def _canonical_report_status(self, project, summary=None) -> dict:
        """Resolve one project's canonical artifact and science axes."""
        current = summary or self._adsorption.delta_e_rows(project)
        marker = ((project or {}).get('autopilot_report')
                  if isinstance((project or {}).get('autopilot_report'), dict)
                  else {})
        if marker.get('scope_kind') == 'comparison':
            science = self._comparison_report_marker_science_state(project, marker)
            artifact_current = self._report_marker_current(project, current)
            eligible = bool(science.get('eligible_final'))
            reason = str(science.get('reason') or '')
            if not artifact_current and not reason:
                reason = '比较报告产物文件、哈希或冻结输入已失效，请重新生成'
            return {
                'schema': 'vcstudio.report-status/v1',
                'artifact_status': 'ready' if artifact_current else 'stale',
                'artifact_current': bool(artifact_current),
                'has_marker': True,
                'marker_kind': str(marker.get('kind') or 'diagnostic'),
                'scientific_status': science.get('kind') or 'diagnostic',
                'scientific_qualification': (
                    science.get('qualification') or 'diagnostic'),
                'scientific_stale': bool(science.get('stale')),
                'eligible_final': eligible,
                'publication_gate_status': 'eligible' if eligible else 'blocked',
                'desired_report_kind': 'final' if eligible else 'diagnostic',
                'report_reason': reason,
                'files': dict(marker.get('files') or {}),
                'revision': self._json_safe_report_result(
                    marker.get('revision') or {}),
            }
        gate_current = current
        scope_error = None
        if marker:
            gate_current, scope_error = self._report_marker_scoped_summary(
                project, marker, current)
        if scope_error:
            eligible, gate_reason = False, scope_error
            gate_current = current
        else:
            eligible, gate_reason = self._final_report_gate(project, gate_current)
        desired_kind = 'final' if eligible else 'diagnostic'
        publication_gate_status = 'eligible' if eligible else 'blocked'
        if marker:
            science = self._report_marker_science_state(project, marker, gate_current)
            artifact_current = self._report_marker_current(project, gate_current)
            reason = str(science.get('reason') or '')
            if not artifact_current and not reason:
                reason = '报告产物文件、哈希或冻结输入已失效，请重新生成'
            return {
                'schema': 'vcstudio.report-status/v1',
                'artifact_status': 'ready' if artifact_current else 'stale',
                'artifact_current': bool(artifact_current),
                'has_marker': True,
                'marker_kind': str(marker.get('kind') or 'legacy-final'),
                'scientific_status': science.get('kind') or desired_kind,
                'scientific_qualification': (
                    science.get('qualification') or 'diagnostic'),
                'scientific_stale': bool(science.get('stale')),
                'eligible_final': bool(eligible),
                'publication_gate_status': publication_gate_status,
                'desired_report_kind': desired_kind,
                'report_reason': reason,
                'files': dict(marker.get('files') or {}),
                'revision': self._json_safe_report_result(
                    marker.get('revision') or {}),
            }
        return {
            'schema': 'vcstudio.report-status/v1',
            'artifact_status': 'missing',
            'artifact_current': False,
            'has_marker': False,
            'marker_kind': None,
            'scientific_status': None,
            'scientific_qualification': None,
            'scientific_stale': False,
            'eligible_final': bool(eligible),
            'publication_gate_status': publication_gate_status,
            'desired_report_kind': desired_kind,
            'report_reason': '' if eligible else str(gate_reason or ''),
            'files': {},
            'revision': {},
        }

    def _tick_reports(self, events, errors):
        """Generate an idempotent HTML/DOCX/PDF bundle once calculations finish.

        Managed submissions and imported-result projects share this close-out
        path.  If the final scientific gate is not satisfied, the renderer
        emits a clearly labelled diagnostic bundle instead of silently doing
        nothing or mislabelling it as a final result.
        """
        for pp in self._adsorption.list_projects():
            try:
                proj = self._load_project_for_path(pp)
                if proj is None:
                    continue
                is_imported = bool(
                    proj.get('import_source') or proj.get('result_import')
                    or proj.get('imported_results'))
                if not proj.get('autopilot_managed') and not is_imported:
                    continue
                # A reference-library-only import has no clean slab/config pair
                # and therefore no project report to close out.  Keep it reusable
                # as a reference library without producing a misleading
                # “adsorption report”.
                members = proj.get('members') or {}
                if (is_imported
                        and (not members.get('clean_slab')
                             or not list(members.get('configs') or []))):
                    continue
                states = self._member_states(proj)
                if not self._project_all_done(states):
                    continue
                if not all(state.get('local_results_ready', True) for state in states):
                    # 远端 DONE 但轻量结果仍未完整回到本地时，不抢跑报告；_tick_cluster
                    # 会在本拍先补拉，成功后这里重新读 manifest 即可同拍继续。
                    continue
                summary = self._adsorption.delta_e_rows(proj)
                if self._report_marker_current(proj, summary):
                    current_marker = proj.get('autopilot_report') or {}
                    current_science = self._report_marker_science_state(
                        proj, current_marker, summary)
                    if (not current_science.get('stale')
                            and current_science.get('kind') != 'draft'):
                        continue
                frozen_input_fingerprint = self._report_input_fingerprint(proj, summary)
                frozen_scientific_fingerprint = self._report_scientific_fingerprint(
                    proj, summary)
                frozen_project_id = str(
                    proj.get('project_uuid') or proj.get('name') or '')
                name = proj.get('name', '') or self._base(os.path.dirname(str(pp)))
                root = proj.get('root') or os.path.dirname(str(pp))
                report_dir = os.path.join(root, 'report')
                os.makedirs(report_dir, exist_ok=True)
                report_formats = self._available_report_formats()
                rep = self._proj_report_bundle_for_path(
                    pp, report_dir, report_formats, final=True,
                    stem=f'{name}_吸附能评估报告')
                if not rep.get('ok'):
                    errors.append(f'项目「{name}」自动报告生成失败')
                    continue
                report_files = {
                    str(fmt): str(path)
                    for fmt, path in (rep.get('files') or {}).items()
                    if path and os.path.isfile(str(path))
                }
                required = set(report_formats)
                if not required.issubset(report_files):
                    missing = '、'.join(sorted(required - set(report_files)))
                    errors.append(f'项目「{name}」报告包缺少:{missing}')
                    continue
                marker = rep.get('marker') if isinstance(rep.get('marker'), dict) else None
                if marker is None:
                    # Test doubles and legacy render adapters may only return
                    # files.  They still use the same canonical persistence seam.
                    try:
                        marker_files = dict(report_files)
                        fallback_sidecars = (rep.get('sidecar_files')
                                             if isinstance(rep.get('sidecar_files'), dict)
                                             else {})
                        model_file = (rep.get('model_file')
                                      or fallback_sidecars.get('model'))
                        if model_file:
                            marker_files['model'] = model_file
                        for key, value in (rep.get('contract_files') or {}).items():
                            marker_files[f'contract_{key}'] = value
                        if rep.get('manifest'):
                            marker_files.setdefault('manifest', rep.get('manifest'))
                        marker = self._persist_report_marker(
                            proj, summary, marker_files,
                            kind=rep.get('kind') or 'diagnostic',
                            reason=rep.get('gate_reason') or '',
                            figures_dir=os.path.join(report_dir, 'figures'),
                            model_sha256=rep.get('model_sha256'),
                            report_model_sha256=rep.get('report_model_sha256'),
                            contracts=rep.get('contracts') or {},
                            scientific_qualification=rep.get(
                                'scientific_qualification'),
                            manifest=rep.get('manifest'),
                            project_path=pp,
                            expected_input_fingerprint=frozen_input_fingerprint,
                            expected_scientific_fingerprint=(
                                frozen_scientific_fingerprint),
                            expected_project_id=frozen_project_id,
                        )
                    except Exception:                     # noqa: BLE001 isolated project
                        errors.append(f'项目「{name}」报告标记落盘失败')
                        continue
                marker_science = self._report_marker_science_state(
                    proj, marker, summary)
                if marker_science.get('stale'):
                    errors.append(f'项目「{name}」报告验证状态在落盘后失效')
                    continue
                events.append({'kind': 'report_done',
                               'project_id': self._workspace_project_id(pp, proj),
                               'project': name,
                               'formats': sorted(report_files),
                               'report_kind': marker_science['kind'],
                               'engine': 'paper_report_bundle',
                               'n_figures': len(rep.get('figures') or []),
                               'text': (f'项目「{name}」'
                                        f'{"最终" if marker_science["kind"] == "final" else "诊断"}'
                                        '报告（Word + PDF + HTML）已自动生成')})
            except Exception:                             # noqa: BLE001 isolated project
                errors.append('项目报告自动化异常')

    # ── 一键出图接线:全 DONE 项目自动出图升级(auto_figures 场景感知 + 期刊风格) ──
    def _af_scenario(self, proj):
        """项目 → auto_figures 场景(lis/general)。只读项目自身口径，不受后来 UI 切换影响。"""
        mode = str((proj or {}).get('work_mode') or
                   ((proj or {}).get('preparation') or {}).get('work_mode') or '')
        if mode == 'lis' or (proj or {}).get('config_species') or (proj or {}).get('species_refs'):
            return 'lis'
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
                    adsorption_mod=self._adsorption,
                    molecules_dir=self._project_molecules_dir(proj),
                    compose_panel=bool(prefs.get('multi_panel', True)))
                if r.get('ok'):
                    return {'out_dir': r.get('out_dir'), 'files': list(r.get('files') or []),
                            'panel': r.get('panel'), 'manifest': list(r.get('manifest') or []),
                            'engine': 'auto_figures'}
            except Exception:                             # noqa: BLE001 引擎不可用 → 回退
                pass
        fig = self._proj_figures_for_path(
            pp, ['bar', 'table', 'ladder'], out_dir)
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
        """Run at most one automation tick in this backend process."""
        if not self._pipeline_lock.acquire(blocking=False):
            return self._pipeline_public_value({
                'ok': True,
                'events': [{'kind': 'skip', 'text': '上一轮自动托管仍在执行，本轮已跳过'}],
                'errors': [], 'synced': 0,
                'last_sync': time.strftime('%Y-%m-%d %H:%M:%S'),
                'busy': True,
            })
        try:
            try:
                outcome = self._pipeline_tick_once()
            except Exception:                             # noqa: BLE001 browser boundary
                outcome = {
                    'ok': False, 'events': [],
                    'errors': ['Pipeline automation failed.'], 'synced': 0,
                    'last_sync': time.strftime('%Y-%m-%d %H:%M:%S'),
                }
            return self._pipeline_public_value(outcome)
        finally:
            self._pipeline_lock.release()

    def _pipeline_tick_once(self):
        """服务端一拍编排(幂等,全部复用现有方法):逐集群刷新/续算/拉回 + 自动报告。

        返回 {'ok','events':[{kind,text,...}],'errors':[...],'last_sync','synced'}。
        无凭据的 profile 记 skip event;各阶段失败只记 event/error,绝不抛到 JS。
        """
        events, errors, synced = [], [], 0
        ap = self._autopilot_cfg()
        if not ap['enabled']:
            # Remote automation remains fail-closed.  Report close-out is an
            # independent, local-only stage: imported completed results should
            # still receive their report when the user has left the global
            # cluster monitor disabled.  Do not load profiles/secrets/campaigns
            # on this path.
            if ap['report']:
                try:
                    self._tick_reports(events, errors)
                except Exception:                         # noqa: BLE001 isolated stage
                    errors.append('报告自动化失败')
            return {'ok': True, 'events': events, 'errors': errors, 'synced': 0,
                    'last_sync': time.strftime('%Y-%m-%d %H:%M:%S')}
        try:
            profiles = self._profiles.load_profiles()
        except Exception:                                 # noqa: BLE001 public-safe error
            profiles = {}
            errors.append('读取集群配置失败')
        # 项目仍声明托管、但配置已删除时必须明确报警；静默忽略会让用户误以为
        # 任务仍在监控。逐项目隔离，坏项目不能掩盖其它服务器。
        try:
            for registered_index, project_path in enumerate(
                    self._adsorption.list_projects(), start=1):
                try:
                    project = self._load_project_for_path(project_path)
                    if not (project or {}).get('autopilot_managed'):
                        continue
                    profile_name = str(((((project or {}).get('launch') or {})
                                        .get('resources') or {}).get('profile') or '')).strip()
                    if profile_name and profile_name not in profiles:
                        project_ref = self._workspace_project_id(
                            project_path, project)
                        errors.append(
                            f'托管项目「{project_ref}」引用的'
                            f'服务器「{profile_name}」已不存在；请恢复该配置或停止托管')
                except Exception:                         # noqa: BLE001 isolated project
                    errors.append(
                        '托管项目读取失败'
                        f'(registered-project-{registered_index})')
        except Exception:                                 # noqa: BLE001 public-safe error
            errors.append('托管项目注册表读取失败')
        runnable = []
        for name, prof in profiles.items():
            try:
                if getattr(prof, 'auth', 'key') == 'password':
                    pw = self._secrets.get_password(name)
                    if not pw:
                        events.append({'kind': 'skip', 'cluster': name,
                                       'text': f'集群「{name}」无保存凭据,跳过本轮同步'})
                        continue
                else:
                    pw = None
                runnable.append((name, prof, pw))
            except Exception:                             # noqa: BLE001 isolated credential
                errors.append(f'集群「{name}」读取凭据失败')

        def _run_cluster(name, profile, password):
            cluster_events, cluster_errors = [], []
            try:
                did_sync = bool(self._tick_cluster(
                    name, profile, password, ap, cluster_events, cluster_errors))
            except Exception:                             # noqa: BLE001 isolated cluster
                did_sync = False
                cluster_errors.append(f'集群「{name}」同步失败')
            return did_sync, cluster_events, cluster_errors

        completed = {}
        if runnable:
            with ThreadPoolExecutor(
                    max_workers=min(8, len(runnable)),
                    thread_name_prefix='vcs-pipeline') as pool:
                futures = {
                    pool.submit(_run_cluster, name, prof, pw): name
                    for name, prof, pw in runnable
                }
                for future in as_completed(futures):
                    completed[futures[future]] = future.result()
        # Preserve profile order in the user-visible event log even though the
        # network work above runs concurrently.
        for name, _prof, _pw in runnable:
            did_sync, cluster_events, cluster_errors = completed[name]
            synced += int(did_sync)
            events.extend(cluster_events)
            errors.extend(cluster_errors)
        # campaign 派生是独立能力；一站式吸附能启用续算/拉回不能顺带推进别的批次。
        if ap['campaigns']:
            try:
                self._tick_campaigns(events, errors)
            except Exception:                             # noqa: BLE001 isolated stage
                errors.append('批次全链推进失败')
        if ap['report']:
            try:
                self._tick_reports(events, errors)
            except Exception:                             # noqa: BLE001 isolated stage
                errors.append('报告自动化失败')
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

        返回 {'ok','created','projects','skipped','campaign','error'}。
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
                return {'ok': False, 'created': 0, 'projects': [],
                        'skipped': [], 'campaign': None, 'error': ';'.join(errs)}
            lib = ''
            try:
                lib = self._config.load_config().get('potcar_lib_root', '') or ''
            except Exception:                             # noqa: BLE001
                pass
            sac = self._sac()
            rots = self._rotations_tuple(rotations)
            created, projects, skipped, tasks = 0, [], [], []
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
                    # 配方注记:标记 SAC 石墨烯来源(层厚收敛据此给「单层无层厚」针对性说明)
                    self._annotate_recipe(cdir, {'kind': 'sac', 'template': template,
                                                 'metal': metal})
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
                                self._annotate_recipe(
                                    jd, {'kind': 'sac', 'template': template,
                                         'metal': metal, 'adsorbate': ads})
                                config_dirs.append(jd)
                                tasks.append({'id': os.path.basename(jd), 'dir': jd,
                                              'natoms': self._poscar_natoms(text)})
                                created += 1
                    if config_dirs:
                        record = self._save_sac_project(
                            base, proj_root, cdir, config_dirs)
                        if record:
                            projects.append({
                                'project_id': self._workspace_project_id(
                                    record['path'], record['project']),
                                'name': str(record['project'].get('name') or base),
                            })
            campaign = self._register_sac_campaign(root, tasks)
            return self._project_public_result({
                'ok': True, 'created': created, 'projects': projects,
                'skipped': skipped, 'campaign': campaign, 'error': None})
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'created': 0, 'projects': [],
                    'skipped': [], 'campaign': None,
                    'error': self._workspace_public_text(e)}

    def _save_sac_project(self, name, proj_root, clean_dir, config_dirs):
        """Persist one private SAC project record; never return it to the browser."""
        try:
            proj = {'name': name, 'root': proj_root, 'work_mode': 'lis',
                    'members': {'clean_slab': clean_dir, 'gas_ref': None,
                                'configs': list(config_dirs)}}
            self._adsorption.save_project(proj_root, proj)
            pp = os.path.join(proj_root, 'project.yaml')
            try:
                self._adsorption.register_project(pp)
            except Exception:                             # noqa: BLE001 注册失败不致命
                pass
            return {'path': pp, 'project': proj}
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

    # ── 金属 slab 建模(结构建模页;层厚收敛的可再生入口,Backlog #2) ────────────────
    def metal_slab_catalog(self):
        """支持的(结构,晶面)组合 + 晶格常数初猜表 → {'ok','surfaces','guess','error'}。

        guess 为**实验值初猜**表(发表口径须同泛函 EOS/晶胞优化),前端用于自动回填。
        """
        try:
            ms = self._msl()
            return {'ok': True, 'surfaces': ms.supported_surfaces(),
                    'guess': ms.LATTICE_GUESS, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'surfaces': [], 'guess': {}, 'error': str(e)}

    def _annotate_recipe(self, job_dir, recipe):
        """把可再生配方并入 job.yaml 的 inputs.recipe → 是否成功(失败不抛,由调用方记 warning)。"""
        try:
            m = self._manifest.load_manifest(job_dir)
            if not m:
                return False
            inputs = dict(m.get('inputs') or {})
            inputs['recipe'] = dict(recipe or {})
            m['inputs'] = inputs
            self._manifest.save_manifest(job_dir, m)
            return True
        except Exception:                                 # noqa: BLE001
            return False

    def metal_slab_build(self, element, structure, miller, layers, a=None, c=None,
                         nx=3, ny=3, vacuum=15.0, fix_bottom=0, incar_path=None,
                         out_dir=None):
        """建金属 slab 作业(fcc/bcc/hcp 低指数面)——层厚收敛的**可再生入口**。

        - 给 INCAR → 四件套链 + job.yaml + 入台账(同 SAC 矩阵口径);不给 → 仅写
          POSCAR + job.yaml,提示到②生成输入页补全(不假装是完整作业)。
        - job.yaml inputs.recipe 记全部构造参数:之后从该作业派生 conv_thickness 时
          可由配方再生不同层数 slab(不再诚实报错,见 _thickness_builder_from_job)。
        返回 {'ok','job_dir','poscar','description','natoms','warnings','error'}。
        """
        try:
            d = (out_dir or '').strip()
            if not d:
                return {'ok': False, 'job_dir': None, 'poscar': '', 'description': '',
                        'natoms': 0, 'warnings': [], 'error': '未指定输出目录'}
            inc = (incar_path or '').strip()
            if inc and not os.path.isfile(inc):
                return {'ok': False, 'job_dir': None, 'poscar': '', 'description': '',
                        'natoms': 0, 'warnings': [], 'error': f'INCAR 不存在:{inc}'}
            ms = self._msl()
            built = ms.build_metal_slab(
                element, structure, miller, int(layers or 4),
                a=(float(a) if a not in (None, '') else None),
                c=(float(c) if c not in (None, '') else None),
                nx=int(nx or 3), ny=int(ny or 3), vacuum=float(vacuum or 15.0),
                fix_bottom=int(fix_bottom or 0))
            warnings = list(built['warnings'])
            if inc:
                lib = ''
                try:
                    lib = self._config.load_config().get('potcar_lib_root', '') or ''
                except Exception:                         # noqa: BLE001
                    pass
                job_dir, w2 = self._job_from_text(built['poscar'], inc, d, lib)
                warnings += list(w2 or [])
            else:
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, 'POSCAR'), 'w', encoding='utf-8') as f:
                    f.write(built['poscar'])
                m = self._manifest.new_manifest(
                    job_id=os.path.basename(os.path.normpath(d)),
                    system=built['poscar'].splitlines()[0].strip(),
                    task_type='relax', calc_type='slab',
                    inputs={'natoms': built['natoms']}, warnings=warnings)
                self._manifest.save_manifest(d, m)
                job_dir = d
                warnings.append('未给 INCAR:仅写出 POSCAR 与 job.yaml(含可再生配方);'
                                '请到「②生成输入」页补全 INCAR/KPOINTS/POTCAR。')
            if not self._annotate_recipe(job_dir, built['recipe']):
                warnings.append('配方写入 job.yaml 失败:层厚收敛派生将退回诚实报错路径。')
            return {'ok': True, 'job_dir': job_dir, 'poscar': built['poscar'],
                    'description': built['description'], 'natoms': built['natoms'],
                    'warnings': warnings, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'job_dir': None, 'poscar': '', 'description': '',
                    'natoms': 0, 'warnings': [], 'error': str(e)}

    def _thickness_builder_from_job(self, job_dir):
        """作业 job.yaml 的 inputs.recipe → (slab_builder_fn|None, 针对性中文说明|None)。

        metal_slab 配方 → 层数再生器(层厚收敛真跑);sac 配方 → 单层无层厚概念的针对性
        说明;无配方 → (None, None)(走 conv_scan 默认诚实 note);配方损坏 → 错误说明。
        """
        try:
            m = self._manifest.load_manifest(job_dir)
        except Exception:                                 # noqa: BLE001
            m = None
        inputs = (m or {}).get('inputs')
        recipe = inputs.get('recipe') if isinstance(inputs, dict) else None
        if not isinstance(recipe, dict) or not recipe:
            return None, None
        kind = recipe.get('kind')
        if kind == 'sac':
            return None, ('该作业来自石墨烯 SAC 建模:单层二维基底没有「层厚」概念,'
                          '层厚收敛不适用;可改做超胞尺寸(nx×ny)或真空层收敛。')
        if kind != 'metal_slab':
            return None, f'job.yaml 配方 kind={kind!r} 未知,无法再生不同层数 slab。'
        try:
            return self._msl().slab_builder_from_recipe(recipe), None
        except Exception as e:                            # noqa: BLE001
            return None, f'配方无法再生 slab:{e}'

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
            result = {'ok': True, 'ground': ground, 'audits': audits, 'error': None}
            try:
                from vcstudio.project import special_report
                parent = os.path.dirname(os.path.normpath(dirs[0]))
                result['report_file'] = special_report.write(
                    '多自旋态比较报告', result, dirs,
                    os.path.join(parent, 'vcstudio-spin-comparison-report.html'))
            except Exception as report_error:             # noqa: BLE001
                result['report_file'] = None
                result['report_error'] = str(report_error)
            return result
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'ground': None, 'audits': [], 'error': str(e)}

    # ── 通用反应预设(项目页 ΔG 台阶) ──────────────────────────────────────────
    def reaction_presets(self, scenario_key=None):
        """通用反应预设 → {'ok','presets':[{key,name,description}],'error'}(供 ΔG 台阶下拉)。"""
        try:
            presets = self._rx().list_presets()
            keys = list(presets)
            if scenario_key:
                sc = self._scenarios.get_scenario(str(scenario_key))
                keys = [key for key in (sc.get('reaction_presets') or []) if key in presets]
            out = [{'key': key, 'name': presets[key].get('name') or key,
                    'name_en': presets[key].get('name_en') or key,
                    'description': presets[key].get('description') or '',
                    'description_en': presets[key].get('description_en') or ''}
                   for key in keys]
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
            'name_en': sc.get('name_en') or '',
            'description_en': sc.get('description_en') or '',
            'pages': list(sc.get('pages') or []),
            'cards': sc.get('cards') or {},
            'figure_preset_order': list(sc.get('figure_preset_order') or []),
            'reaction_presets': list(sc.get('reaction_presets') or []),
            'engines': list(sc.get('engines') or []),
            'defaults': sc.get('defaults') or {},
            'ai_context': sc.get('ai_context') or '',
            'primary': bool(sc.get('primary', False)),
            'task_keys': list(sc.get('task_keys') or []),
            'home_actions': list(sc.get('home_actions') or []),
        }

    def scenario_list(self):
        """全部内置研究场景(展示序)→ {'ok','scenarios':[view...],'error'}(首启场景选择模态用)。"""
        try:
            out = [self._scenario_view(s) for s in self._scenarios.list_scenarios()]
            return {'ok': True, 'scenarios': out, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'scenarios': [], 'error': str(e)}

    def _workspace_context_projection(self, snapshot):
        """Validate one stored tuple and return its canonical public projection."""
        rows = list(self._scenarios.list_scenarios())
        registry = {str(row.get('key') or ''): row for row in rows if row.get('key')}
        if not registry:
            raise RuntimeError('没有可用的工作模式')
        default_scenario = 'full' if 'full' in registry else next(iter(registry))
        requested_scenario = str(snapshot.get('scenario') or '').strip()
        scenario_key = (requested_scenario if requested_scenario in registry
                        else default_scenario)
        scenario = registry[scenario_key]

        registered = [self._normalize_engine_key(value)
                      for value in self._eng().available_engines()]
        registered = [value for value in registered if value]
        visible = [self._normalize_engine_key(value)
                   for value in (scenario.get('engines') or [])]
        allowed_engines = [value for value in registered if value in visible]
        if not allowed_engines:
            raise RuntimeError(f'工作模式「{scenario.get("name", scenario_key)}」没有可用引擎')
        default_engine = self._normalize_engine_key(
            (scenario.get('defaults') or {}).get('engine'))
        if default_engine not in allowed_engines:
            default_engine = ('vasp' if 'vasp' in allowed_engines else allowed_engines[0])
        requested_engine = self._normalize_engine_key(snapshot.get('engine'))
        engine = requested_engine if requested_engine in allowed_engines else default_engine

        allowed = self._engine_task_keys(engine, scenario)
        if not allowed:
            raise RuntimeError(
                f'工作模式「{scenario.get("name", scenario_key)}」与引擎 '
                f'{self._ENGINE_DISPLAY.get(engine, engine)} 没有共同计算类型')
        default_calculation = str(
            (scenario.get('defaults') or {}).get('active_calculation') or '')
        if default_calculation not in allowed:
            default_calculation = allowed[0]
        requested_calculation = str(snapshot.get('calculation') or '').strip()
        calculation = (requested_calculation if requested_calculation in allowed
                       else default_calculation)
        configured = {
            'scenario': bool(requested_scenario and requested_scenario == scenario_key),
            'engine': bool(requested_engine and requested_engine == engine),
            'calculation': bool(
                requested_calculation and requested_calculation == calculation),
        }
        return {
            'revision': int(snapshot.get('revision') or 0),
            'intent_id': snapshot.get('last_intent_id') or None,
            'scenario': self._scenario_view(scenario),
            'engine': engine,
            'calculation': calculation,
            'capability': self._engine_capability_view(engine, scenario),
            'allowed': list(allowed),
            'configured': configured,
        }

    def _workspace_context_envelope(self, store_result):
        snapshot = store_result.get('context') or {}
        context = self._workspace_context_projection(snapshot)
        return {
            'ok': bool(store_result.get('ok')),
            'conflict': bool(store_result.get('conflict')),
            'replayed': bool(store_result.get('replayed')),
            'revision': context['revision'],
            'context': context,
            'error': store_result.get('error'),
        }

    def settings_context_get(self):
        """Return the single authoritative workflow/engine/calculation snapshot."""
        try:
            snapshot = self._workspace_context().read()
            return self._workspace_context_envelope({
                'ok': True, 'conflict': False, 'replayed': False,
                'context': snapshot, 'error': None,
            })
        except Exception as e:                            # noqa: BLE001 JSON-safe bridge envelope
            return {'ok': False, 'conflict': False, 'replayed': False,
                    'revision': None, 'context': None, 'error': str(e)}

    def settings_context_update(self, patch, expected_revision, intent_id):
        """CAS-update any subset while committing one fully validated tuple."""
        try:
            if not isinstance(patch, dict) or not patch \
                    or not set(patch).issubset({'scenario', 'engine', 'calculation'}):
                raise ValueError(
                    'patch must contain scenario, engine, and/or calculation only')
            if (not isinstance(expected_revision, int)
                    or isinstance(expected_revision, bool) or expected_revision < 0):
                raise ValueError('expected_revision 必须是非负整数')
            current_snapshot = self._workspace_context().read()
            current = self._workspace_context_projection(current_snapshot)

            scenario_key = current['scenario']['key']
            requested_scenario = patch.get('scenario')
            if requested_scenario is not None:
                if not isinstance(requested_scenario, str):
                    raise ValueError('scenario 必须是字符串')
                scenario_key = requested_scenario.strip()
            registry = {
                str(row.get('key') or ''): row
                for row in self._scenarios.list_scenarios() if row.get('key')
            }
            if scenario_key not in registry:
                raise ValueError(
                    f'未知工作模式 {scenario_key!r}；可选：{", ".join(sorted(registry))}')
            scenario = registry[scenario_key]
            scenario_changed = scenario_key != current['scenario']['key']

            registered = [self._normalize_engine_key(value)
                          for value in self._eng().available_engines()]
            visible = [self._normalize_engine_key(value)
                       for value in (scenario.get('engines') or [])]
            allowed_engines = [value for value in registered if value and value in visible]
            if not allowed_engines:
                raise ValueError(f'工作模式「{scenario.get("name", scenario_key)}」没有可用引擎')
            if 'engine' in patch:
                if not isinstance(patch['engine'], str):
                    raise ValueError('engine 必须是字符串')
                engine = self._normalize_engine_key(patch['engine'])
            elif scenario_changed:
                engine = self._normalize_engine_key(
                    (scenario.get('defaults') or {}).get('engine'))
            else:
                engine = current['engine']
            if engine not in allowed_engines:
                if 'engine' in patch:
                    raise ValueError(
                        f'计算引擎 {engine!r} 未注册或不适用于工作模式'
                        f'「{scenario.get("name", scenario_key)}」')
                engine = ('vasp' if 'vasp' in allowed_engines else allowed_engines[0])

            allowed = self._engine_task_keys(engine, scenario)
            if not allowed:
                raise ValueError('当前工作模式与引擎没有共同计算类型')
            if 'calculation' in patch:
                if not isinstance(patch['calculation'], str):
                    raise ValueError('calculation 必须是字符串')
                calculation = patch['calculation'].strip()
            elif scenario_changed:
                calculation = str(
                    (scenario.get('defaults') or {}).get('active_calculation') or '')
            else:
                calculation = current['calculation']
            if calculation not in allowed:
                if 'calculation' in patch:
                    raise ValueError(
                        f'计算类型 {calculation!r} 不适用于「{scenario.get("name", "")} / '
                        f'{self._ENGINE_DISPLAY.get(engine, engine)}」')
                preferred = str(
                    (scenario.get('defaults') or {}).get('active_calculation') or '')
                calculation = preferred if preferred in allowed else allowed[0]

            stored = self._workspace_context().compare_and_swap(
                {'scenario': scenario_key, 'engine': engine,
                 'calculation': calculation},
                expected_revision=expected_revision, intent_id=intent_id)
            return self._workspace_context_envelope(stored)
        except Exception as e:                            # noqa: BLE001 JSON-safe bridge envelope
            authoritative = self.settings_context_get()
            return {
                'ok': False, 'conflict': False, 'replayed': False,
                'revision': authoritative.get('revision'),
                'context': authoritative.get('context'), 'error': str(e),
            }

    def _legacy_workspace_context_update(self, patch, expected_revision=None,
                                         intent_id=None):
        """Compatibility seam; production browser code uses context_update."""
        if expected_revision is None:
            current = self.settings_context_get()
            if not current.get('ok'):
                return current
            expected_revision = current['revision']
        token = intent_id or f'legacy:{uuid.uuid4().hex}'
        return self.settings_context_update(patch, expected_revision, token)

    def scenario_get(self):
        result = self.settings_context_get()
        context = result.get('context') or {}
        configured = context.get('configured') or {}
        return {
            **result,
            'configured': bool(configured.get('scenario')),
            'scenario': context.get('scenario'),
        }

    def scenario_set(self, key, expected_revision=None, intent_id=None):
        result = self._legacy_workspace_context_update(
            {'scenario': key}, expected_revision, intent_id)
        context = result.get('context') or {}
        scenario = context.get('scenario')
        return {**result, 'key': scenario.get('key') if scenario else None,
                'scenario': scenario}

    def calculation_get(self):
        result = self.settings_context_get()
        context = result.get('context') or {}
        configured = context.get('configured') or {}
        return {
            **result,
            'configured': bool(configured.get('calculation')),
            'engine': context.get('engine'),
            'active_calculation': context.get('calculation') or '',
            'allowed': list(context.get('allowed') or []),
        }

    def calculation_set(self, key, expected_revision=None, intent_id=None):
        result = self._legacy_workspace_context_update(
            {'calculation': key}, expected_revision, intent_id)
        context = result.get('context') or {}
        return {
            **result,
            'engine': context.get('engine'),
            'active_calculation': context.get('calculation'),
            'allowed': list(context.get('allowed') or []),
        }

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
            normalize = getattr(self._i18n, 'normalize_lang', None)
            if callable(normalize):
                lg = normalize(lg)
            self._i18n.set_lang(lg)
            return {'ok': True, 'lang': lg, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e)}

    def i18n_dict(self, lang):
        """Return keyed target and zh-source dictionaries for one UI locale."""
        try:
            lg = (lang or '').strip() or 'zh'
            bundle = getattr(self._i18n, 'export_bundle_for_js', None)
            if callable(bundle):
                payload = dict(bundle(lg))
                lg = payload.get('lang') or lg
                target = payload.get('dict') or {}
                source = payload.get('source') or {}
            else:                                       # compatibility seam
                target = self._i18n.export_for_js(lg)
                source = self._i18n.export_for_js('zh')
            return {'ok': True, 'lang': lg, 'dict': target,
                    'source': source, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'lang': lang, 'dict': {}, 'source': {},
                    'error': str(e)}

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
                    'name_en': p.get('name_en') or '',
                    'category_en': p.get('category_en') or '',
                    'description': p.get('description', ''),
                    'description_en': p.get('description_en') or '',
                    'required_data': p.get('required_data', ''),
                    'required_data_en': p.get('required_data_en') or '',
                    'thumbnail_svg': thumb,
                    'params_schema': p.get('params_schema') or {},
                })
            categories = fp.categories()
            category_en = getattr(fp, 'CATEGORY_EN', {})
            return {'ok': True, 'presets': presets,
                    'categories': categories,
                    'categories_en': [category_en.get(value, value)
                                      for value in categories],
                    'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'presets': [], 'categories': [], 'error': str(e)}

    def render_figure_preset(self, key, project_id, params=None):
        try:
            record = self._resolve_project_id(project_id)
        except Exception:                                 # noqa: BLE001 public identity seam
            return self._project_identity_failure(
                files=[], skipped=[], out_dir=None, provenance=None)
        result = self._call_with_project_bindings(
            [record],
            lambda: self._render_figure_preset_for_path(
                key, record['path'], params=params),
            failure={
                'files': [], 'skipped': [], 'out_dir': None,
                'provenance': None,
            })
        return self._project_public_result(result)

    def _render_figure_preset_for_path(self, key, project_path, params=None):
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
            proj = self._load_project_for_path((project_path or '').strip())
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
    def draft_ready(self, project_id, out):
        try:
            record = self._resolve_project_id(project_id)
        except Exception:                                 # noqa: BLE001 public identity seam
            return self._project_identity_failure(
                summary=None, issues=[], products=[], issues_total=0,
                summary_path=None, out_dir=None)
        result = self._call_with_project_bindings(
            [record], lambda: self._draft_ready_for_path(record['path'], out),
            failure={
                'summary': None, 'issues': [], 'products': [],
                'issues_total': 0, 'summary_path': None, 'out_dir': None,
            })
        return self._project_public_result(result)

    def _draft_ready_for_path(self, path, out):
        """一键成稿包:SI zip + 三线表 + 口径稽核 + Methods → {'ok','summary','issues','products'}。

        稽核不过/SI 缺关键输入仍产全套工件但 ok=False(DRAFT_READY.md 首行醒目);products 汇
        总全部落盘文件供前端列出 + open_dir。
        """
        try:
            proj = self._load_project_for_path((path or '').strip())
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
    _ENGINE_ALIASES = {
        'g16': 'gaussian', 'g09': 'gaussian', 'gaussian16': 'gaussian',
        'materials_studio': 'castep', 'materials studio': 'castep',
        'ms': 'castep', 'castep/ms': 'castep',
    }
    # 这是 Web/API 能力合同，不替代引擎实现。界面只据此展示已经接通“生成→登记→
    # 提交→回收→能量/收敛解析”的任务和字段，避免把 VASP 专用选项套到其它软件。
    _ENGINE_CAPABILITIES = {
        'vasp': {
            'support_level': 'full',
            'support_label': '完整工作流',
            'summary': '主引擎；覆盖完整 VASP 任务目录、四件套、托管与结果工具。',
            'generic_tasks': None,       # None = 取当前工作模式的完整 task_keys
            'boundaries': ['periodic', 'molecule'],
            'fields': ['poscar', 'incar', 'potcar_library', 'calc_type'],
            'input_contract': 'POSCAR / INCAR / POTCAR / KPOINTS',
            'result_contract': '按 VASP 任务自动回收 OUTCAR、OSZICAR、vasprun.xml 等关键产物',
            'limitations': [],
        },
        'cp2k': {
            'support_level': 'file_workflow',
            'support_label': '文件级工作流',
            'summary': '生成 cp2k.inp、登记提交并解析 cp2k.out；软件与 GTH 数据文件需自备。',
            'generic_tasks': ['relax', 'static', 'freq'],
            'boundaries': ['periodic', 'molecule'],
            'fields': ['poscar', 'task', 'functional', 'dispersion', 'cutoff_ry',
                       'rel_cutoff_ry', 'kpoints', 'basis_set_file', 'potential_file',
                       'periodic', 'spin', 'charge', 'multiplicity'],
            'input_contract': 'POSCAR → cp2k.inp',
            'result_contract': 'cp2k.out（总能与收敛状态）',
            'limitations': [
                'CUTOFF 是 GTH 密度网格截断（Ry），不能从 VASP ENCUT 换算。',
                '当前通用适配只开放结构优化、单点能和频率；绝对能量不可跨引擎比较。',
            ],
        },
        'gaussian': {
            'support_level': 'file_workflow',
            'support_label': '分子文件级工作流',
            'summary': '孤立分子专用；生成 .gjf、登记提交并解析 .log。',
            # 设置页只保留三种通用意图；TD/IRC/扫描/NMR/TS 等在 Gaussian 专属面板
            # 的唯一任务下拉中选择，避免两个任务选择器互相覆盖。
            'generic_tasks': ['relax', 'static', 'freq'],
            'boundaries': ['molecule'],
            'fields': ['molecule', 'gaussian_task', 'functional', 'basis', 'dispersion',
                       'solvent', 'charge', 'multiplicity', 'resources'],
            'input_contract': '分子结构 → Gaussian .gjf',
            'result_contract': 'Gaussian .log（能量、收敛与频率证据）',
            'limitations': [
                '仅支持孤立分子/团簇；周期 slab 或 bulk 必须改用 VASP、CP2K 或 CASTEP。',
                'VASP 平面波/PAW 与 Gaussian 高斯基组的绝对能量不可直接比较。',
            ],
        },
        'castep': {
            'support_level': 'file_workflow',
            'support_label': '文件级工作流',
            'summary': '生成 .cell/.param、登记提交并解析 .castep；CASTEP 许可与赝势需自备。',
            'generic_tasks': ['relax', 'static', 'freq'],
            'boundaries': ['periodic'],
            'fields': ['poscar', 'task', 'functional', 'dispersion', 'cutoff_ev',
                       'kpoints', 'periodic', 'spin', 'charge', 'multiplicity'],
            'input_contract': 'POSCAR → CASTEP .cell + .param',
            'result_contract': '.castep（总能与收敛状态）',
            'limitations': [
                '当前通用适配只开放结构优化、单点能和频率。',
                'CASTEP 赝势与 VASP PAW 不同；截断能需独立收敛，绝对能量不可跨引擎比较。',
            ],
        },
    }
    _GENERIC_TASK_NAMES = {
        'relax': '结构优化', 'static': '单点能', 'freq': '频率分析',
    }

    @classmethod
    def _normalize_engine_key(cls, engine):
        key = str(engine or '').strip().lower()
        return cls._ENGINE_ALIASES.get(key, key)

    def _engine_task_keys(self, engine, scenario=None):
        """引擎在当前工作模式真正可走通的“本次计算”交集。"""
        key = self._normalize_engine_key(engine)
        cap = self._ENGINE_CAPABILITIES.get(key) or {}
        generic = cap.get('generic_tasks')
        if scenario is not None:
            allowed = list(scenario.get('task_keys') or [])
            if 'task_keys' not in scenario:
                try:
                    allowed = [str(t.get('key')) for t in self._tc().list_catalog()]
                except Exception:                     # noqa: BLE001 legacy scenario compatibility
                    allowed = []
        else:
            try:
                allowed = [str(t.get('key')) for t in self._tc().list_catalog()]
            except Exception:                         # noqa: BLE001 元数据降级不挡引擎清单
                allowed = []
        if generic is None:
            return allowed
        return [task for task in generic if not allowed or task in allowed]

    def _engine_capability_view(self, engine, scenario=None):
        key = self._normalize_engine_key(engine)
        cap = copy.deepcopy(self._ENGINE_CAPABILITIES.get(key) or {})
        task_keys = self._engine_task_keys(key, scenario)
        names = dict(self._GENERIC_TASK_NAMES)
        try:
            names.update({str(t.get('key')): str(t.get('name_zh') or t.get('key'))
                          for t in self._tc().list_catalog()})
        except Exception:                             # noqa: BLE001 显示名可安全回退 key
            pass
        cap['task_keys'] = task_keys
        cap['tasks'] = [{'key': task, 'name': names.get(task, task)} for task in task_keys]
        if key == 'gaussian':
            try:
                cap['native_tasks'] = [
                    {'key': task, 'name': row.get('name_zh', task),
                     'note': row.get('note', '')}
                    for task, row in self._gauss().GAUSSIAN_TASKS.items()
                ]
            except Exception:                         # noqa: BLE001 专属面板仍会自行加载任务
                cap['native_tasks'] = []
        return cap

    def _active_engine(self, scenario, ui, registered):
        visible = [e for e in (scenario.get('engines') or []) if e in registered]
        requested = self._normalize_engine_key((ui or {}).get('active_engine'))
        default = self._normalize_engine_key(
            (scenario.get('defaults') or {}).get('engine') or 'vasp')
        if requested in visible:
            return requested
        if default in visible:
            return default
        return visible[0] if visible else ('vasp' if 'vasp' in registered else '')

    def engine_list(self, scenario_key=None):
        """已注册引擎清单与 UI 能力合同（按工作模式严格标可见）。

        VASP 为主引擎;CP2K/Gaussian/CASTEP 为文件级适配(实验性)。
        给 scenario_key 时严格按工作模式 engines 白名单标 visible，默认引擎也取模式声明。
        """
        try:
            names = self._eng().available_engines()
            vis = None
            default = 'vasp'
            if scenario_key is not None:
                sc = self._scenarios.get_scenario(scenario_key)
                vis = set(sc.get('engines') or [])
                default = str((sc.get('defaults') or {}).get('engine') or 'vasp')
            engines = []
            for n in names:
                row = {'key': n, 'name': self._ENGINE_DISPLAY.get(n, n.upper()),
                       'experimental': n != 'vasp',
                       'visible': True if vis is None else (n in vis)}
                row.update(self._engine_capability_view(n, sc if scenario_key is not None else None))
                engines.append(row)
            if default not in names or (vis is not None and default not in vis):
                default = next((n for n in names if vis is None or n in vis), 'vasp')
            return {'ok': True, 'engines': engines, 'default': default, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'engines': [], 'default': 'vasp', 'error': str(e)}

    def engine_get(self):
        result = self.settings_context_get()
        context = result.get('context') or {}
        configured = context.get('configured') or {}
        return {
            **result,
            'engine': context.get('engine') or 'vasp',
            'configured': bool(configured.get('engine')),
            'capability': context.get('capability') or {},
        }

    def engine_set(self, engine, expected_revision=None, intent_id=None):
        result = self._legacy_workspace_context_update(
            {'engine': engine}, expected_revision, intent_id)
        context = result.get('context') or {}
        return {
            **result,
            'engine': context.get('engine'),
            'active_calculation': context.get('calculation'),
            'capability': context.get('capability') or {},
        }

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
        生成成功后同时写标准 ``job.yaml`` 并登记台账，因此可直接到作业页提交。
        返回 {'ok','files','warnings','issues','out_dir','registered','error'}。
        """
        try:
            params = dict(params or {})
            eng = (engine or '').strip()
            engine_key = self._normalize_engine_key(eng)
            out = (out_dir or '').strip()
            if not eng:
                return {'ok': False, 'files': [], 'warnings': [], 'issues': [],
                        'out_dir': None, 'registered': False, 'error': '未指定引擎'}
            if not out:
                return {'ok': False, 'files': [], 'warnings': [], 'issues': [],
                        'out_dir': None, 'registered': False, 'error': '未选输出目录'}
            mods = self._eng()
            registered_engines = [self._normalize_engine_key(e)
                                  for e in mods.available_engines()]
            if engine_key not in registered_engines or engine_key not in self._ENGINE_CAPABILITIES:
                return {'ok': False, 'files': [], 'warnings': [], 'issues': [],
                        'out_dir': None, 'registered': False,
                        'error': f'计算引擎 {engine_key!r} 未注册或无 Web 工作流能力合同'}
            if engine_key == 'vasp':
                return {
                    'ok': False, 'files': [], 'warnings': [], 'issues': [],
                    'out_dir': None, 'registered': False,
                    'error': ('VASP 请使用主工作流的四件套/专用任务生成器；'
                              '通用 engine_generate 仅服务 CP2K、Gaussian 和 CASTEP，'
                              '避免生成缺 POTCAR 且绕过 VASP 预检的重复入口。')}
            task = str(params.get('task') or 'relax').strip().lower()
            supported = self._engine_task_keys(engine_key)
            if task not in supported:
                names = '、'.join(self._GENERIC_TASK_NAMES.get(x, x) for x in supported)
                return {'ok': False, 'files': [], 'warnings': [], 'issues': [],
                        'out_dir': None, 'registered': False,
                        'error': (f'{self._ENGINE_DISPLAY.get(engine_key, engine_key)} 当前文件级工作流'
                                  f'不支持任务 {task!r}；可选：{names}')}
            params['task'] = task
            # CP2K 的 CUTOFF/REL_CUTOFF 单位为 Ry，属于引擎私有参数；绝不能继续
            # 借用通用 cutoff_ev（那会让用户误以为 VASP ENCUT 可直接换算）。
            if engine_key == 'cp2k':
                extras = dict(params.get('extras') or {})
                for key in ('cutoff_ry', 'rel_cutoff_ry', 'basis_set_file',
                            'potential_file'):
                    if params.get(key) not in (None, ''):
                        extras[key] = params[key]
                params['extras'] = extras
                params['cutoff_ev'] = None
            spec, serr = self._spec_from_params(mods, params)
            if serr:
                return {'ok': False, 'files': [], 'warnings': [], 'issues': [],
                        'out_dir': None, 'registered': False, 'error': serr}
            boundary = 'periodic' if bool(getattr(spec, 'periodic', True)) else 'molecule'
            boundaries = self._ENGINE_CAPABILITIES[engine_key].get('boundaries') or []
            if boundary not in boundaries:
                label = '周期体系' if boundary == 'periodic' else '孤立分子/团簇'
                allowed_labels = '、'.join('周期体系' if item == 'periodic' else '孤立分子/团簇'
                                          for item in boundaries)
                return {'ok': False, 'files': [], 'warnings': [], 'issues': [],
                        'out_dir': None, 'registered': False,
                        'error': (f'{self._ENGINE_DISPLAY.get(engine_key, engine_key)} 当前适配不支持'
                                  f'{label}；请选择：{allowed_labels}')}
            issues = list(mods.validate(spec))
            if engine_key == 'cp2k':
                # CalcSpec 的 cutoff_ev 门禁服务于 VASP/CASTEP 平面波截断；CP2K 已由
                # cutoff_ry 单独表达。保留真正的 CP2K 缺项提示，不输出错误单位建议。
                issues = [x for x in issues if 'cutoff_ev' not in str(x)]
                try:
                    if float((getattr(spec, 'extras', {}) or {}).get('cutoff_ry', 0)) <= 0:
                        issues.append('CP2K 周期计算应明确给出正数 CUTOFF（Ry），并独立做收敛测试。')
                except (TypeError, ValueError):
                    issues.append('CP2K CUTOFF（Ry）必须是正数。')
            if engine_key == 'castep' and bool(getattr(spec, 'periodic', True)) \
                    and getattr(spec, 'kpoints', None) is None:
                issues.append('CASTEP 周期计算未指定 k 点网格；将回退 1×1×1，仅适合已验证的超胞。')
            backend = mods.get_backend(engine_key)
            os.makedirs(out, exist_ok=True)
            res = backend.generate_inputs(spec, out)
            files = list(res.get('files') or [])
            warnings = list(res.get('warnings') or [])
            generated_names = []
            hashes = {}
            for item in files:
                name = os.path.basename(str(item or '').strip())
                if not name or name in generated_names:
                    continue
                generated_names.append(name)
                candidate = str(item)
                if not os.path.isfile(candidate):
                    candidate = os.path.join(out, name)
                if os.path.isfile(candidate):
                    hashes[name] = _sha256_file(candidate)

            engine_key = self._ta().normalize_engine(engine_key)
            output_files = self._ta().expected_engine_output_names(
                engine_key, generated_names)
            task_type = str(getattr(spec, 'task', None) or 'relax').strip().lower()
            gaussian_task = str((getattr(spec, 'extras', {}) or {}).get(
                'gaussian_task') or '').strip().lower()
            if engine_key == 'gaussian' and gaussian_task:
                task_type = {
                    'freq': 'freq', 'opt_freq': 'freq', 'opt_ts': 'freq',
                    'sp': 'static', 'td': 'static', 'nmr': 'static',
                    'opt': 'relax', 'scan': 'relax', 'irc': 'relax',
                }.get(gaussian_task, task_type)
            if task_type not in ('relax', 'static', 'freq'):
                warnings.append(
                    f'多引擎任务 {task_type!r} 无标准清单类型，已按 static '
                    '登记以便提交；原任务保留在 inputs.task。')
                task_type = 'static'

            structure = str(getattr(spec, 'structure', '') or '')
            first_line = next((line.strip() for line in structure.splitlines()
                               if line.strip()), '')
            inputs = {
                'engine': engine_key,
                'task': str(getattr(spec, 'task', None) or 'relax'),
                'functional': str(getattr(spec, 'functional', None) or ''),
                'periodic': bool(getattr(spec, 'periodic', True)),
                'spin': bool(getattr(spec, 'spin', False)),
                'charge': int(getattr(spec, 'charge', 0) or 0),
                'files': generated_names,
                'output_files': output_files,
                'sha256': hashes,
                'structure_sha256': hashlib.sha256(
                    structure.encode('utf-8')).hexdigest(),
                'generator': 'engine_generate',
            }
            if gaussian_task:
                inputs['gaussian_task'] = gaussian_task
            source_poscar = str(params.get('poscar') or '').strip()
            if source_poscar and os.path.isfile(source_poscar):
                inputs['source_poscar'] = os.path.abspath(source_poscar)
                inputs['source_poscar_sha256'] = _sha256_file(source_poscar)

            registered = False
            try:
                calc_type = _norm_calc_type(
                    params.get('calc_type')
                    or ('slab' if bool(getattr(spec, 'periodic', True)) else 'molecule'))
                m = self._manifest.new_manifest(
                    job_id=(f'{os.path.basename(os.path.normpath(out))}-{engine_key}-'
                            f'{time.strftime("%Y%m%d-%H%M%S")}'),
                    system=first_line or os.path.basename(os.path.normpath(out)),
                    task_type=task_type, calc_type=calc_type, inputs=inputs,
                    warnings=warnings + [f'生成阶段自洽检查：{x}' for x in issues])
                self._manifest.save_manifest(out, m)
                self._ledger.register(out)
                registered = True
            except Exception as ex:                       # noqa: BLE001
                warnings.append(f'job.yaml/台账写入失败（输入文件已保留）：{ex}')
            return {'ok': True, 'files': files, 'warnings': warnings,
                    'issues': issues, 'out_dir': out, 'registered': registered,
                    'task_type': task_type, 'engine': engine_key, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'files': [], 'warnings': [], 'issues': [],
                    'out_dir': None, 'registered': False, 'error': str(e)}

    def engine_preview(self, engine, params):
        """引擎输入实时预览(不落用户盘):CalcSpec → 输入全文文本 + 字符数(②预览区用)。

        Gaussian 走 gaussian.preview(spec)(分子面板主用);其余引擎经临时目录 generate_inputs
        回读首个产物文本。validate 自洽问题并入 issues。返回
        {'ok','text','chars','warnings','issues','error'}。
        """
        try:
            eng = self._normalize_engine_key(engine)
            if not eng:
                return {'ok': False, 'text': '', 'chars': 0, 'warnings': [],
                        'issues': [], 'error': '未指定引擎'}
            mods = self._eng()
            if eng not in [self._normalize_engine_key(e) for e in mods.available_engines()] \
                    or eng not in self._ENGINE_CAPABILITIES:
                return {'ok': False, 'text': '', 'chars': 0, 'warnings': [],
                        'issues': [], 'error': f'计算引擎 {eng!r} 未注册或无 Web 工作流能力合同'}
            if eng == 'vasp':
                return {'ok': False, 'text': '', 'chars': 0, 'warnings': [],
                        'issues': [],
                        'error': 'VASP 预览请使用主四件套预览，不走非 VASP 通用适配器。'}
            params = dict(params or {})
            task = str(params.get('task') or 'relax').strip().lower()
            if task not in self._engine_task_keys(eng):
                return {'ok': False, 'text': '', 'chars': 0, 'warnings': [],
                        'issues': [], 'error': f'{eng} 当前适配不支持任务 {task!r}'}
            params['task'] = task
            if eng == 'cp2k':
                extras = dict(params.get('extras') or {})
                for key in ('cutoff_ry', 'rel_cutoff_ry', 'basis_set_file',
                            'potential_file'):
                    if params.get(key) not in (None, ''):
                        extras[key] = params[key]
                params['extras'] = extras
                params['cutoff_ev'] = None
            spec, serr = self._spec_from_params(mods, params)
            if serr:
                return {'ok': False, 'text': '', 'chars': 0, 'warnings': [],
                        'issues': [], 'error': serr}
            boundary = 'periodic' if bool(getattr(spec, 'periodic', True)) else 'molecule'
            if boundary not in (self._ENGINE_CAPABILITIES[eng].get('boundaries') or []):
                return {'ok': False, 'text': '', 'chars': 0, 'warnings': [],
                        'issues': [], 'error': f'{eng} 当前适配不支持 {boundary} 边界'}
            issues = list(mods.validate(spec))
            if eng == 'cp2k':
                issues = [x for x in issues if 'cutoff_ev' not in str(x)]
            eng_norm = eng
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

    def mol_image_b64_to_smiles(self, image_b64, suffix='.png'):
        """剪贴板图片(base64,可带 data:image/...;base64, 前缀)→ 临时 PNG → DECIMER OCSR。

        对齐 starpivot「粘贴图片」:前端 Ctrl+V 取剪贴板位图转 base64 直传,无需先存文件。
        返回 {'ok','smiles','elapsed_ms','image_path','error'};image_path 为落盘的临时
        图片(供复查/复用),识别失败原样透传引擎中文说明。
        """
        try:
            s = (image_b64 or '').strip()
            if not s:
                return {'ok': False, 'smiles': '', 'elapsed_ms': 0.0,
                        'image_path': None, 'error': '剪贴板里没有图片数据'}
            if s.lower().startswith('data:') and ',' in s:
                s = s.split(',', 1)[1]                    # 剥 dataURL 前缀
            try:
                blob = base64.b64decode(s, validate=True)
            except (binascii.Error, ValueError):
                return {'ok': False, 'smiles': '', 'elapsed_ms': 0.0, 'image_path': None,
                        'error': '图片数据不是合法 base64(请直接对分子结构截图后 Ctrl+V)'}
            if len(blob) < 64:
                return {'ok': False, 'smiles': '', 'elapsed_ms': 0.0, 'image_path': None,
                        'error': '图片数据过小,疑非图片(请粘贴分子结构截图)'}
            ext = str(suffix or '.png').lower()
            if ext not in ('.png', '.jpg', '.jpeg'):
                ext = '.png'
            tmpdir = os.path.join(tempfile.gettempdir(), 'vcstudio_paste')
            os.makedirs(tmpdir, exist_ok=True)
            path = os.path.join(tmpdir, f'paste_{int(time.time() * 1000)}{ext}')
            with open(path, 'wb') as f:
                f.write(blob)
            r = self._mb().ocsr.image_to_smiles(path)
            return {'ok': bool(r.get('ok')), 'smiles': r.get('smiles', ''),
                    'elapsed_ms': r.get('elapsed_ms', 0.0), 'image_path': path,
                    'error': r.get('error') or None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'smiles': '', 'elapsed_ms': 0.0,
                    'image_path': None, 'error': str(e)}

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
    def quick_submit_scan(self, paths, shared_incar=''):
        """只读预检父目录下的 VASP 四件套/结构，不建作业。"""
        try:
            items = [str(p) for p in (paths or []) if str(p).strip()]
            if not items:
                return {'ok': False, 'items': [], 'summary': {}, 'error': '未选择输入文件或目录'}
            result = dict(self._qs().scan_inputs(
                items, shared_incar=str(shared_incar or '').strip()) or {})
            result.setdefault('ok', True)
            result.setdefault('items', [])
            result.setdefault('summary', {})
            result.setdefault('error', None)
            return result
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'items': [], 'summary': {}, 'error': str(e)}

    def quick_submit_build(self, files, out_root, job_prefix='', shared_incar='',
                           lib_root='', calc_type='slab'):
        """任意输入文件(.gjf/.com/.inp/.cell/VASP 目录)→ 逐个建轻量作业目录 + 入台账。

        父目录会递归发现多个 VASP 作业；只有 POSCAR/CONTCAR 时可用
        ``shared_incar`` + ``lib_root`` 批量生成 KPOINTS/POTCAR。返回
        {'ok','jobs','skipped','preflight','error'}，hint 为该引擎的集群运行命令模板。
        """
        try:
            fs = [str(f) for f in (files or []) if str(f).strip()]
            root = (out_root or '').strip()
            if not fs:
                return {'ok': False, 'jobs': [], 'skipped': [], 'preflight': None,
                        'error': '未选择任何输入文件'}
            if not root:
                return {'ok': False, 'jobs': [], 'skipped': [], 'preflight': None,
                        'error': '未选输出根目录'}
            qs = self._qs()
            lib = str(lib_root or '').strip()
            if not lib:
                try:
                    lib = str((self._config.load_config() or {}).get('potcar_lib_root') or '')
                except Exception:                         # noqa: BLE001 设置不可读由 builder 报精确缺项
                    lib = ''
            res = qs.build_quick_jobs(
                fs, root, job_prefix=(job_prefix or ''),
                shared_incar=str(shared_incar or '').strip(),
                lib_root=lib, calc_type=_norm_calc_type(calc_type))
            if not res.get('ok'):
                return {'ok': False, 'jobs': [], 'skipped': list(res.get('skipped') or []),
                        'preflight': res.get('preflight'),
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
                             'mode': j.get('mode') or 'copy',
                             'warnings': list(j.get('warnings') or []),
                             'hint': qs.submit_hint(j.get('engine', '')),
                             'registered': registered})
            return {'ok': True, 'jobs': jobs, 'skipped': list(res.get('skipped') or []),
                    'preflight': res.get('preflight'), 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'jobs': [], 'skipped': [], 'preflight': None,
                    'error': str(e)}

    def jobs_cancel_batch(self, dirs, name, password, trust_new=False, idempotency_key=None):
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
            def _cancel():
                res = self._job_batch_with_idempotency(
                    self._bo().cancel_batch, prof, ds,
                    password=pw, trust_new=trust_new,
                    idempotency_key=idempotency_key)
                payload = {'ok': bool(res.get('ok')),
                           'cancelled': list(res.get('cancelled') or []),
                           'failed': list(res.get('failed') or []),
                           'needs_trust': bool(res.get('needs_trust')),
                           **self._host_key_evidence(res), 'error': res.get('error')}
                for key in ('busy', 'code', 'busy_count', 'replayed',
                            'requires_manual_recovery', 'recovery_count',
                            'scheduler_job_ids'):
                    if key in res:
                        payload[key] = copy.deepcopy(res[key])
                return payload
            return self._run_job_operation_once(
                idempotency_key, action='cancel', profile=prof.name,
                dirs=ds, invoke=_cancel)
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
                client, jump = conn.open_client(prof, pw, trust_new=trust_new)
            except conn.ConnectError as e:
                return {'ok': False, 'path': rpath, 'entries': [],
                        'needs_trust': bool(getattr(e, 'needs_trust', False)),
                        **self._host_key_evidence(e), 'error': str(e)}
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
                client, jump = conn.open_client(prof, pw, trust_new=trust_new)
            except conn.ConnectError as e:
                return {'ok': False, 'local_path': None,
                        'needs_trust': bool(getattr(e, 'needs_trust', False)),
                        **self._host_key_evidence(e), 'error': str(e)}
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
                client, jump = conn.open_client(prof, pw, trust_new=trust_new)
            except conn.ConnectError as e:
                return {'ok': False, 'results': [], 'experimental': True,
                        'needs_trust': bool(getattr(e, 'needs_trust', False)),
                        **self._host_key_evidence(e), 'error': str(e)}
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

    def wavefn_bcp(self, wavefn_file, exe=None, workdir=None):
        """AIM 临界点表:跑 aim_cp 并解析 CPprop.txt → 结构化 BCP 列表(编号/类型/ρ/键能)。

        返回 {'ok','cps':[{index,type,xyz_angst,rho,v,bond_energy_kcal}],'cpprop_path',
        'script','note','error'}。键能仅对 (3,-1) 给 **Espinosa 经验估算** E≈V(r)/2
        (kcal/mol,面向氢键等弱相互作用;共价键不适用,note 注明口径,绝不假精确)。
        Multiwfn 缺失 → 回传 stdin 脚本;产物缺失/解析为空 → 诚实报错。
        """
        try:
            wf = (wavefn_file or '').strip()
            if not wf:
                return {'ok': False, 'cps': [], 'cpprop_path': None, 'script': '',
                        'note': None, 'error': '未选择波函数文件'}
            mw = self._mw()
            parser = getattr(mw, 'cpprop_parse', None)
            if not callable(parser):
                return {'ok': False, 'cps': [], 'cpprop_path': None, 'script': '',
                        'note': None,
                        'error': '引擎待扩展:multiwfn_driver 无 cpprop_parse(请更新 vcstudio)'}
            mw_exe = (exe or self._tool_paths().get('multiwfn') or '').strip() or None
            r = mw.run(wf, 'aim_cp', exe=mw_exe, workdir=((workdir or '').strip() or None))
            if not r.get('ok'):
                return {'ok': False, 'cps': [], 'cpprop_path': None,
                        'script': r.get('script', ''), 'note': None,
                        'error': r.get('error') or 'AIM 拓扑分析失败'}
            cpp = next((p for p in (r.get('outputs') or [])
                        if str(p).lower().endswith('cpprop.txt')), None)
            if not cpp or not os.path.isfile(cpp):
                return {'ok': False, 'cps': [], 'cpprop_path': None, 'script': '',
                        'note': None,
                        'error': 'AIM 完成但未找到 CPprop.txt 产物(版本导出名差异?请查工作目录)'}
            with open(cpp, encoding='utf-8', errors='replace') as f:
                cps = parser(f.read())
            if not cps:
                return {'ok': False, 'cps': [], 'cpprop_path': cpp, 'script': '',
                        'note': None,
                        'error': 'CPprop.txt 未解析出临界点(版本格式差异,请把样例反馈给我们)'}
            return {'ok': True, 'cps': cps, 'cpprop_path': cpp, 'script': '',
                    'note': ('键能为 Espinosa 经验估算 E≈V(r)/2(换算 kcal/mol),面向氢键等'
                             '弱相互作用 BCP;共价键不适用,发表引用请注明口径。'),
                    'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'cps': [], 'cpprop_path': None, 'script': '',
                    'note': None, 'error': str(e)}

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

    def task_catalog(self, scenario_key=None, active_calculation=None, engine=None):
        """计算类型目录(五分类 23 项)→ {'ok','categories','tasks':[{key,name_zh,category,
        description,requires,outputs,figure,builder_ref,kind_badge}],'error'}。前端据此渲染卡片
        网格与参数表单;kind_badge 按 builder_ref 分「作业生成/结果计算器/INCAR 顾问」区分。

        engine 缺省保持 VASP 完整目录的兼容行为；显式指定非 VASP 引擎时只返回该
        文件级适配真正接通的 relax/static/freq，且改写为对应引擎的输入/结果合同。
        """
        try:
            tc = self._tc()
            rows = tc.list_catalog()
            sc = None
            if scenario_key:
                sc = self._scenarios.get_scenario(str(scenario_key))
                allowed = set(sc.get('task_keys') or [])
                rows = [t for t in rows if t.get('key') in allowed]
            engine_key = self._normalize_engine_key(engine or 'vasp')
            if engine_key not in self._ENGINE_CAPABILITIES:
                return {'ok': False, 'categories': [], 'tasks': [],
                        'engine': engine_key,
                        'error': f'未知计算引擎 {engine_key!r}'}
            if engine is not None:
                supported = set(self._engine_task_keys(engine_key, sc))
                rows = [t for t in rows if t.get('key') in supported]
            if active_calculation:
                rows = [t for t in rows if t.get('key') == str(active_calculation)]
            tasks = []
            cap_view = self._engine_capability_view(engine_key, sc)
            engine_contracts_en = {
                'vasp': {
                    'requires': 'POSCAR / INCAR / POTCAR / KPOINTS',
                    'outputs': 'Task-specific VASP evidence such as OUTCAR, OSZICAR, and vasprun.xml',
                },
                'cp2k': {
                    'requires': 'POSCAR → cp2k.inp',
                    'outputs': 'cp2k.out (total energy and convergence status)',
                },
                'gaussian': {
                    'requires': 'Molecular structure → Gaussian .gjf',
                    'outputs': 'Gaussian .log (energy, convergence, and frequency evidence)',
                },
                'castep': {
                    'requires': 'POSCAR → CASTEP .cell + .param',
                    'outputs': '.castep (total energy and convergence status)',
                },
            }
            for t in rows:
                cap = self._ta().capability(t['key'])
                non_vasp = engine_key != 'vasp'
                engine_name = self._ENGINE_DISPLAY.get(engine_key, engine_key.upper())
                tasks.append({
                    'key': t['key'], 'name_zh': t.get('name_zh', t['key']),
                    'name_en': t.get('name_en', ''),
                    'category': t.get('category', ''),
                    'category_en': t.get('category_en', ''),
                    'description': t.get('description', ''),
                    'description_en': t.get('description_en', ''),
                    'requires': (cap_view.get('input_contract', '') if non_vasp
                                 else t.get('requires', '')),
                    'requires_en': (engine_contracts_en.get(engine_key, {}).get('requires', '')
                                    if non_vasp else t.get('requires_en', '')),
                    'outputs': (cap_view.get('result_contract', '') if non_vasp
                                else t.get('outputs', '')),
                    'outputs_en': (engine_contracts_en.get(engine_key, {}).get('outputs', '')
                                   if non_vasp else t.get('outputs_en', '')),
                    'figure': t.get('figure'),
                    'builder_ref': t.get('builder_ref', ''),
                    'kind_badge': ('引擎输入生成' if non_vasp
                                   else self._task_badge(t.get('builder_ref', ''))),
                    'kind_badge_en': ('Engine input generator' if non_vasp else {
                        'INCAR 顾问': 'INCAR advisor',
                        '结果计算器': 'Result calculator',
                        '作业生成': 'Job generator',
                    }.get(self._task_badge(t.get('builder_ref', '')), 'Job generator')),
                    'analysis_status': cap['analysis_status'],
                    'report_supported': cap['report_supported'],
                    'next_action': (f'在生成输入页填写 {engine_name} 专属字段，生成并纳管后到任务页提交。'
                                    if non_vasp else cap['next_action']),
                    'next_action_en': (
                        f'Complete the {engine_name} fields on Generate inputs, create and register '
                        'the job, then submit it from Jobs.' if non_vasp else
                        t.get('next_action_en') or
                        'Continue from the applicable task page to run the job, inspect its evidence, '
                        'and publish only supported outputs.'),
                    'engine': engine_key,
                })
            categories = [c for c in tc.CATEGORIES
                          if any(t.get('category') == c for t in rows)]
            categories_en = []
            for category in categories:
                translated = next((str(t.get('category_en') or '') for t in rows
                                   if t.get('category') == category and t.get('category_en')), '')
                categories_en.append(translated or category)
            return {'ok': True, 'categories': categories,
                    'categories_en': categories_en,
                    'tasks': tasks, 'engine': engine_key,
                    'capability': cap_view, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'categories': [], 'tasks': [],
                    'engine': self._normalize_engine_key(engine or 'vasp'),
                    'capability': {}, 'error': str(e)}

    def task_capabilities(self):
        """全部 23 类任务的分析/报告能力矩阵，供 UI 如实显示可用程度。"""
        try:
            matrix = self._ta().capability_matrix()
            return {'ok': True, 'capabilities': matrix, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'capabilities': {}, 'error': str(e)}

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
            if k == 'vaspsol':
                from vcstudio.generate import vaspsol_pair
                res = vaspsol_pair.build_pair(
                    d, parent, eb_k=float(p.get('eb_k', 78.4) or 78.4))
                return self._derive_ret(
                    k, res['job_dirs'], res.get('changes'), res.get('warnings'),
                    extra={'pair_id': res['pair_id'],
                           'vacuum_dir': res['vacuum_dir'],
                           'solvent_dir': res['solvent_dir'],
                           'eb_k': res['eb_k']})
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

        conv_thickness(v3.2.2 接通,Backlog #2):源作业 job.yaml 若带金属 slab 建模配方
        (inputs.recipe,来自 metal_slab_build),据其构造 slab_builder_fn 再生不同层数 →
        系列真生成;SAC 配方 → 「单层无层厚」针对性说明;无配方 → conv_scan 默认诚实
        note(裸 CONTCAR 无米勒面信息)。note 并入 warnings + extra 透传,0 作业绝不假成功。
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
        else:  # conv_thickness:job.yaml 有 metal_slab 配方 → 真再生;无/不适用 → 诚实说明
            layers = [int(x) for x in (p.get('layers') or [3, 4, 5])]
            builder_fn, why = self._thickness_builder_from_job(d)
            res = cs.build_slab_thickness_series(d, out_root, layers,
                                                 slab_builder_fn=builder_fn)
            if builder_fn is None and why:
                res = dict(res)
                res['note'] = why                         # 针对性说明盖过通用 note
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

    @staticmethod
    def _neb_endpoint_energy(job_dir):
        """端点最终能量：OSZICAR E0 优先，退 OUTCAR sigma→0/vasprun.xml。"""
        oszicar = os.path.join(job_dir, 'OSZICAR')
        if os.path.isfile(oszicar):
            energy = None
            try:
                with open(oszicar, encoding='utf-8', errors='replace') as handle:
                    for line in handle:
                        match = re.search(r'\bE0=\s*([-+0-9.Ee]+)', line)
                        if match:
                            energy = float(match.group(1))
            except (OSError, ValueError):
                energy = None
            if energy is not None and math.isfinite(energy):
                return energy, 'OSZICAR:E0'
        outcar = os.path.join(job_dir, 'OUTCAR')
        if os.path.isfile(outcar):
            energy = None
            try:
                with open(outcar, encoding='utf-8', errors='replace') as handle:
                    for line in handle:
                        match = re.search(
                            r'energy\(sigma->0\)\s*=\s*([-+0-9.Ee]+)', line)
                        if match:
                            energy = float(match.group(1))
            except (OSError, ValueError):
                energy = None
            if energy is not None and math.isfinite(energy):
                return energy, 'OUTCAR:energy(sigma->0)'
        vasprun = os.path.join(job_dir, 'vasprun.xml')
        if os.path.isfile(vasprun):
            try:
                from pathlib import Path
                from vcstudio.project import result_import
                parsed = result_import._parse_vasprun(Path(vasprun))
                facts = parsed if isinstance(parsed, dict) else {}
                energy = facts.get('energy_e0_eV')
                if (isinstance(energy, (int, float)) and not isinstance(energy, bool)
                        and math.isfinite(float(energy))):
                    return float(energy), str(
                        facts.get('energy_source') or 'vasprun.xml:e_0_energy')
            except Exception:                             # noqa: BLE001
                pass
        return None, None

    def _attach_neb_endpoint_evidence(self, job_dir, start_dir, end_dir, n_images):
        """把已算端点的真实输出带入 00/N+1，并将哈希/能量来源写入 manifest。"""
        import shutil

        root = os.path.abspath(job_dir)
        pairs = (('start', os.path.abspath(start_dir), '00'),
                 ('end', os.path.abspath(end_dir), f'{int(n_images) + 1:02d}'))
        records, warnings = {}, []
        copied_total = 0
        for role, source, frame in pairs:
            destination = os.path.join(root, frame)
            record = {'role': role, 'source_dir': source, 'target_frame': frame,
                      'files': [], 'energy_e0_eV': None, 'energy_source': None,
                      'trusted': False, 'source_state': None}
            if not os.path.isdir(destination):
                warnings.append(f'NEB {frame} image 目录不存在，无法写入端点证据。')
                records[role] = record
                continue
            for name in ('OSZICAR', 'OUTCAR', 'vasprun.xml'):
                src = os.path.join(source, name)
                dst = os.path.join(destination, name)
                if os.path.isfile(dst):
                    same = False
                    if os.path.isfile(src):
                        try:
                            same = _sha256_file(src) == _sha256_file(dst)
                        except OSError:
                            same = False
                    if not same:
                        backup = (f'{dst}.vcstudio-{time.time_ns()}.'
                                  'endpoint.bak')
                        os.replace(dst, backup)
                        warnings.append(
                            f'{frame}/{name} 旧证据已可恢复备份为 {os.path.basename(backup)}。')
                if not os.path.isfile(src):
                    continue
                if not os.path.isfile(dst):
                    shutil.copy2(src, dst)
                stat = os.stat(dst)
                record['files'].append({
                    'name': name, 'sha256': _sha256_file(dst), 'size': stat.st_size,
                    'source': src,
                })
                copied_total += 1

            energy, energy_source = self._neb_endpoint_energy(destination)
            try:
                source_manifest = self._manifest.load_manifest(source) or {}
            except Exception:                             # noqa: BLE001
                source_manifest = {}
            if not isinstance(source_manifest, dict):
                source_manifest = {}
            record['source_state'] = source_manifest.get('state')
            if energy is not None:
                record['energy_e0_eV'] = energy
                record['energy_source'] = f'copied:{energy_source}'
                evidence_name = energy_source.split(':', 1)[0]
                record['trusted'] = any(
                    item.get('name') == evidence_name and item.get('sha256')
                    for item in record['files'])
            else:
                results = source_manifest.get('results') or {}
                value = results.get('energy_e0_eV') if isinstance(results, dict) else None
                declared_source = (str(results.get('energy_source') or '').strip()
                                   if isinstance(results, dict) else '')
                if (source_manifest.get('state') == 'DONE' and declared_source
                        and isinstance(value, (int, float)) and not isinstance(value, bool)
                        and math.isfinite(float(value))):
                    record['energy_e0_eV'] = float(value)
                    record['energy_source'] = f'source_manifest:{declared_source}'
                    record['source_job_id'] = source_manifest.get('job_id')
                    record['trusted'] = bool(record['source_job_id'])
            if record['energy_e0_eV'] is None:
                label = '始态' if role == 'start' else '末态'
                warnings.append(
                    f'{label} {frame} 未找到可复核能量：'
                    '请在源目录保留 OSZICAR/OUTCAR，或先将已收敛结果导入台账。')
            elif not record['trusted']:
                label = '始态' if role == 'start' else '末态'
                warnings.append(
                    f'{label} {frame} 能量缺少文件哈希或源 job_id，未标记为可信；'
                    '分析器不会用它补齐能垒。')
            records[role] = record

        # 真 neb_builder 会先落标准清单；外部构建器若无清单，则不伪造。
        try:
            manifest_value = self._manifest.load_manifest(root)
            if isinstance(manifest_value, dict):
                inputs = dict(manifest_value.get('inputs') or {})
                inputs['neb_endpoints'] = records
                manifest_value['inputs'] = inputs
                self._manifest.save_manifest(root, manifest_value)
        except Exception as ex:                           # noqa: BLE001
            warnings.append(f'NEB 端点证据写入 job.yaml 失败：{ex}')
        return records, copied_total, warnings

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
            endpoint_evidence, copied_count, endpoint_warnings = \
                self._attach_neb_endpoint_evidence(res['job_dir'], s, e, res['n_images'])
            warns.extend(endpoint_warnings)
            try:
                self._ledger.register(res['job_dir'])
            except Exception as ex:                       # noqa: BLE001
                warns.append(f'台账登记失败(不影响已生成 NEB 目录):{ex}')
            changes = [f'已生成标准 NEB 目录树:00 初态 + {res["n_images"]} 个中间 image + 末态,'
                       f'根目录共享 INCAR/POTCAR/KPOINTS(INCAR 只补不改补齐 IMAGES/SPRING 等)']
            if copied_count:
                changes.append(f'已带入 {copied_count} 份初/末态能量证据并记录 SHA256。')
            return {'ok': True, 'job_dir': res['job_dir'], 'n_images': res['n_images'],
                    'changes': changes, 'warnings': warns,
                    'endpoint_evidence': endpoint_evidence, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'job_dir': None, 'n_images': 0, 'changes': [],
                    'warnings': [], 'error': str(e)}

    # ── P0-2 形成能/结合能计算器 ───────────────────────────────────────────────
    # 位置或关键字参(非 keyword-only):前端经 pywebview 桥按位置传参,keyword-only 会断桥。
    def formation_binding_calc(self, sac_dir, substrate_dir=None,
                               atom_energies=None, chem_pots=None):
        return self._formation_binding_calc(
            sac_dir, substrate_dir, atom_energies, chem_pots,
            write_report=True)

    def _formation_binding_calc(self, sac_dir, substrate_dir=None,
                                atom_energies=None, chem_pots=None, *,
                                write_report=False):
        """形成能/结合能计算器:DONE/完整性/方法闸 → references 计算 + σ。

        - sac_dir:单原子催化剂(SAC)作业目录(读 OSZICAR 末 E0 作 E_sac,读 CONTCAR/POSCAR 组成)。
        - substrate_dir:含空位、未嵌金属的基底作业目录(E_substrate);Eb 与 Ef 都相对它。
        - atom_energies:{元素: 孤立原子能量 eV}(结合能用金属原子能;缺 → 无法算 Eb 的中文提示)。
        - chem_pots:{元素: 化学势 μ eV/atom}(形成能用;counts 取 SAC−基底 组成差)。
        返回 {'ok','e_sac','e_substrate','metal','counts','binding_energy','formation_energy',
        'sigma','stable','stability_note','hints','warnings','error'}。缺量只提示、绝不编造。
        """
        try:
            from vcstudio.project import energy_gate

            sd = (sac_dir or '').strip()
            if not sd or not os.path.isdir(sd):
                return {'ok': False, 'e_sac': None, 'e_substrate': None, 'metal': None,
                        'counts': {}, 'binding_energy': None, 'formation_energy': None,
                        'sigma': None, 'stable': None, 'stability_note': None,
                        'hints': [], 'warnings': [], 'method_check': None,
                        'error': 'SAC 作业目录不存在'}
            e_sac, sac_manifest, completion_notes = energy_gate.validate_done_energy(
                sd, 'SAC 作业', self._manifest)
            refs = self._refs()
            hints, warnings, completion_evidence = [], [], list(completion_notes)
            sac_counts = self._species_counts(self._read_struct_text(sd) or '')

            # 基底能量 + 组成
            e_sub, sub_counts, sub_manifest = None, {}, None
            sub = (substrate_dir or '').strip()
            if sub and os.path.isdir(sub):
                e_sub, sub_manifest, sub_notes = energy_gate.validate_done_energy(
                    sub, '基底作业', self._manifest)
                completion_evidence.extend(sub_notes)
                sub_counts = self._species_counts(self._read_struct_text(sub) or '')
            elif sub:
                raise ValueError('基底作业目录不存在')

            method_check = None
            if sub_manifest is not None:
                method_check = energy_gate.compare_methods([
                    energy_gate.method_record(sd, sac_manifest, 'SAC'),
                    energy_gate.method_record(sub, sub_manifest, '基底'),
                ], require_same_kpoints=True)
                warnings.extend(method_check['warnings'])
                if method_check['status'] == 'incompatible':
                    return {'ok': False, 'e_sac': e_sac, 'e_substrate': e_sub,
                            'metal': None, 'counts': {}, 'binding_energy': None,
                            'formation_energy': None, 'sigma': None, 'stable': None,
                            'stability_note': None, 'hints': [], 'warnings': warnings,
                            'method_check': method_check, 'evidence': completion_evidence,
                            'error': '方法不一致，能量不可直接相减：'
                                     + '；'.join(method_check['issues'])}

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
            if ae:
                warnings.append('孤立原子能量为手工输入；请确认其泛函、赝势、ENCUT 和自旋口径与 SAC 一致')
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
            if cp:
                warnings.append('化学势为手工输入参考；请确认其参考态与本次 DFT 方法口径一致')
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

            result = {'ok': (eb is not None) or (ef is not None),
                      'e_sac': e_sac, 'e_substrate': e_sub, 'metal': metal,
                      'counts': delta, 'binding_energy': eb, 'formation_energy': ef,
                      'sigma': sigma, 'stable': stable, 'stability_note': stab_note,
                      'hints': hints, 'warnings': warnings, 'method_check': method_check,
                      'evidence': completion_evidence, 'error': None}
            if result['ok'] and write_report:
                try:
                    from vcstudio.project import special_report
                    evidence_dirs = [sd] + ([sub] if sub else [])
                    result['report_file'] = special_report.write(
                        '形成能 / 结合能报告', result, evidence_dirs,
                        os.path.join(sd, 'vcstudio-formation-binding-report.html'))
                except Exception as report_error:         # noqa: BLE001
                    result['report_file'] = None
                    warnings.append(f'报告生成失败：{report_error}')
            else:
                result['report_file'] = None
            return result
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'e_sac': None, 'e_substrate': None, 'metal': None,
                    'counts': {}, 'binding_energy': None, 'formation_energy': None,
                    'sigma': None, 'stable': None, 'stability_note': None,
                    'hints': [], 'warnings': [], 'method_check': None, 'error': str(e)}

    # ── P0-4 差分电荷合成(三作业 CHGCAR → CHGDIFF.vasp + 面平均) ──────────────────
    def compute_chgdiff(self, ab_dir, a_dir, b_dir, out_dir=None):
        """差分电荷合成:三作业 CHGCAR → chgdiff.compute_chgdiff 出 CHGDIFF.vasp + 面平均 Δρ̄(z)。

        返回 {'ok','out','max','min','n_grid','profile':{'z','rho','axis'},'error'}。
        任一 CHGCAR 缺失/网格或晶格不一致/原子数不守恒 → 中文 error(引擎硬校验冒泡),不静默错算。
        """
        try:
            from vcstudio.project import energy_gate

            paths = {}
            method_records = []
            warnings = []
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
                _energy, job_manifest, completion_notes = \
                    energy_gate.validate_done_energy(d, f'{tag} 作业', self._manifest)
                warnings.extend(completion_notes)
                method_records.append(
                    energy_gate.method_record(d, job_manifest, tag))
                paths[tag] = cp
            method_check = energy_gate.compare_methods(
                method_records, require_same_kpoints=True)
            warnings.extend(method_check['warnings'])
            if method_check['status'] == 'incompatible':
                return {'ok': False, 'out': None, 'max': None, 'min': None,
                        'n_grid': 0, 'profile': {}, 'warnings': warnings,
                        'method_check': method_check, 'report_file': None,
                        'error': 'AB/A/B 方法不一致，差分电荷不可相减：'
                                 + '；'.join(method_check['issues'])}
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
            result = {'ok': True, 'out': res['out'], 'max': res['max'],
                      'min': res['min'], 'n_grid': res['n_grid'], 'profile': profile,
                      'warnings': warnings, 'method_check': method_check, 'error': None}
            try:
                from vcstudio.project import special_report
                result['report_file'] = special_report.write(
                    '差分电荷报告', result,
                    [(ab_dir or '').strip(), (a_dir or '').strip(), (b_dir or '').strip()],
                    os.path.join(out, 'vcstudio-charge-difference-report.html'))
            except Exception as report_error:             # noqa: BLE001
                result['report_file'] = None
                warnings.append(f'报告生成失败：{report_error}')
            return result
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'out': None, 'max': None, 'min': None, 'n_grid': 0,
                    'profile': {}, 'warnings': [], 'method_check': None,
                    'report_file': None, 'error': str(e)}

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

    def vaspsol_pair_calc(self, vacuum_dir, solvent_dir, out_path=None):
        """校验同几何真空/溶剂配对，计算 ΔE_solv 并原子写可追溯 HTML 报告。"""
        try:
            from vcstudio.project import vaspsol
            result = vaspsol.write_report(
                str(vacuum_dir or '').strip(), str(solvent_dir or '').strip(),
                str(out_path).strip() if out_path else None)
            result['error'] = None
            return result
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'error': str(e), 'report_file': None}

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
        """CONTCAR/POSCAR 晶胞体积(Å³)，复用完整 VASP 缩放语义解析。"""
        try:
            from vcstudio.generate.poscar import read_cell_vectors

            for name in ('CONTCAR', 'POSCAR'):
                p = os.path.join(job_dir, name)
                if not os.path.isfile(p):
                    continue
                with open(p, encoding='utf-8', errors='replace') as handle:
                    a = read_cell_vectors(handle.read())
                det = (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
                       - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
                       + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))
                return abs(det)
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
        """从 manifest task_type 或目录内容推断 23 类任务；未知时不猜成 VASP。"""
        try:
            m = self._manifest.load_manifest(d)
            tt = str((m or {}).get('task_type') or '').lower()
            key = self._ta().normalize_task_key(tt)
            if self._ta().capability(key).get('known'):
                return key
        except Exception:                                 # noqa: BLE001
            pass
        try:
            subs = [x for x in os.listdir(d) if os.path.isdir(os.path.join(d, x))]
        except OSError:
            subs = []
        if any(x.startswith('eos_') for x in subs):
            return 'eos'
        if any(x.split('_')[0] in ('encut', 'kmesh', 'vac', 'thick', 'slab') for x in subs):
            return 'conv_encut'
        if any(x.isdigit() and len(x) == 2 for x in subs):
            return 'neb'
        if os.path.isfile(os.path.join(d, 'LOCPOT')):
            return 'workfunction'
        if os.path.isfile(os.path.join(d, 'EIGENVAL')):
            return 'bands'
        if os.path.isfile(os.path.join(d, 'XDATCAR')):
            return 'aimd'
        if os.path.isfile(os.path.join(d, 'ACF.dat')):
            return 'bader'
        if os.path.isfile(os.path.join(d, 'ELFCAR')):
            return 'elf'
        if os.path.isfile(os.path.join(d, 'DOSCAR')):
            return 'dos_pdos'
        return ''

    def _analysis_envelope(self, result, key):
        """给旧解析器返回补统一 capability 字段，保持既有 result/figure 契约。"""
        cap = self._ta().capability(key)
        out = dict(result or {})
        out.setdefault('kind', cap.get('task_key') or key or None)
        out['supported'] = cap.get('analysis_status') not in ('unsupported', 'dedicated')
        out['analysis_status'] = cap.get('analysis_status', 'unsupported')
        out['report_supported'] = bool(cap.get('report_supported'))
        out['next_action'] = cap.get('next_action', '')
        return out

    def analyze_task(self, job_dir, kind=None):
        """统一任务解析：按 23 类能力矩阵分发；未接解析器时返回明确下一步，不编结果。"""
        try:
            d = (job_dir or '').strip()
            if not d or not os.path.isdir(d):
                return {'ok': False, 'kind': None, 'result': None, 'figure': None,
                        'summary': '', 'files': [], 'error': '作业目录不存在'}
            engine = self._ta().job_engine(d)
            if engine not in ('vasp', 'gaussian', 'cp2k', 'castep'):
                return {'ok': False, 'kind': None, 'result': None, 'figure': None,
                        'summary': '', 'files': [], 'supported': False,
                        'analysis_status': 'unsupported', 'report_supported': False,
                        'next_action': '请在 job.yaml inputs.engine 中选择已接入引擎。',
                        'error': f'未接入计算引擎：{engine}'}
            raw = (kind or '').strip().lower() or self._infer_task_kind(d)
            if not raw and engine != 'vasp':
                raw = self._ta().engine_task_key(d)
            k = self._ta().normalize_task_key(raw)
            cap = self._ta().capability(k)
            if not cap.get('known'):
                return {'ok': False, 'kind': k or None, 'result': None, 'figure': None,
                        'summary': '', 'files': [], 'supported': False,
                        'analysis_status': 'unsupported', 'report_supported': False,
                        'next_action': cap.get('next_action', ''),
                        'error': '无法识别任务类型；请在设置页选择本次计算类型。'}
            route = cap['route']
            # 非 VASP 作业统一走自己的主输出解析器；禁止因为用户选了
            # relax/static/freq 而去读取不存在的 OSZICAR/OUTCAR。
            if engine != 'vasp':
                out = self._ta().analyze_engine_outputs(d, engine=engine, task_key=k)
            elif route == 'conv':
                out = self._analyze_conv(d)
                out['kind'] = 'conv' if raw == 'conv' else k
            elif route == 'eos':
                out = self._analyze_eos(d)
            elif route == 'bands':
                out = self._analyze_bands(d)
            elif route == 'workfunction':
                out = self._analyze_workfunction(d)
            elif route == 'dos':
                out = self._ta().analyze_dos(d)
            elif route == 'bader':
                out = self._ta().analyze_bader(d)
            elif route == 'chgdiff':
                out = self._ta().analyze_chgdiff(d)
            elif route == 'elf':
                out = self._ta().analyze_elf(d)
            elif route == 'generic':
                out = self._ta().analyze_vasp_outputs(d, k)
            elif route == 'freq':
                out = self._ta().analyze_frequency(d)
            elif route == 'aimd':
                out = self._ta().analyze_aimd(d)
            elif route == 'neb':
                out = self._ta().analyze_neb(d)
            elif route == 'artifact':
                out = self._ta().inspect_artifacts(d, k)
            else:  # 专用多作业/参考态计算器，不能对单目录伪造结果
                out = {'ok': False, 'kind': k, 'result': None, 'figure': None,
                       'summary': '', 'files': [],
                       'error': f'{k} 需要专用多作业流程，不能从一个目录独立得出结论。'}
            return self._analysis_envelope(out, k)
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'kind': None, 'result': None, 'figure': None,
                    'summary': '', 'files': [], 'error': str(e)}

    def task_report(self, job_dir, save_to, kind=None):
        """为任意已登记任务生成带文件指纹的报告；解析失败会原样写入而非隐藏。"""
        try:
            d = (job_dir or '').strip()
            out = (save_to or '').strip()
            if not d or not os.path.isdir(d):
                return {'ok': False, 'file': None, 'analysis': None,
                        'error': '作业目录不存在'}
            if not out:
                return {'ok': False, 'file': None, 'analysis': None,
                        'error': '未指定报告路径'}
            analysis = self.analyze_task(d, kind=kind)
            key = self._ta().normalize_task_key(kind or analysis.get('kind'))
            saved = self._ta().write_trace_report(d, analysis, out, task_key=key)
            return {'ok': True, 'file': saved, 'analysis': analysis,
                    'analysis_ok': bool(analysis.get('ok')), 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'file': None, 'analysis': None, 'error': str(e)}

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
        return self._surface_energy_calc(
            slab_dir, bulk_dir, e_bulk_per_atom, area, write_report=True)

    def _surface_energy_calc(self, slab_dir, bulk_dir, e_bulk_per_atom=None, area=None,
                             *, write_report=False):
        """表面能计算器:选 slab + bulk 作业 → γ (J/m²)。面积从 slab POSCAR 自动算。

        slab/bulk 都必须通过 DONE + 干净收尾闸，已知方法冲突拒绝大数相减。
        e_bulk_per_atom 缺省时由 bulk 作业能量与原子数现算;area 缺省时由 slab POSCAR
        的 a×b 叉积面积算。返回 {'ok','gamma_jm2','area_a2','e_slab','n_slab','e_bulk_per_atom',
        'note','error'}。
        """
        try:
            from vcstudio.project import energy_gate

            sd = (slab_dir or '').strip()
            if not sd or not os.path.isdir(sd):
                return {'ok': False, 'gamma_jm2': None, 'warnings': [],
                        'method_check': None, 'error': 'slab 作业目录不存在'}
            e_slab, slab_manifest, completion_notes = energy_gate.validate_done_energy(
                sd, 'slab 作业', self._manifest)
            warnings, completion_evidence = [], list(completion_notes)
            n_slab = self._poscar_natoms(self._read_poscar_text(sd))
            if not n_slab:
                return {'ok': False, 'gamma_jm2': None, 'warnings': warnings,
                        'method_check': None, 'error': '无法从 slab POSCAR 读原子数'}
            se = self._se()
            if area in (None, ''):
                area = se.area_from_poscar(self._read_poscar_text(sd))
            e_bpa = e_bulk_per_atom
            method_check = None
            if e_bpa in (None, ''):
                bd = (bulk_dir or '').strip()
                if not bd or not os.path.isdir(bd):
                    return {'ok': False, 'gamma_jm2': None, 'warnings': warnings,
                            'method_check': None,
                            'error': 'bulk 作业目录不存在(或直接填体相每原子能)'}
                e_bulk, bulk_manifest, bulk_notes = energy_gate.validate_done_energy(
                    bd, 'bulk 作业', self._manifest)
                completion_evidence.extend(bulk_notes)
                n_bulk = self._poscar_natoms(self._read_poscar_text(bd))
                if not n_bulk:
                    return {'ok': False, 'gamma_jm2': None, 'warnings': warnings,
                            'method_check': None,
                            'error': 'bulk 作业无可解析能量/原子数'}
                method_check = energy_gate.compare_methods([
                    energy_gate.method_record(sd, slab_manifest, 'slab'),
                    energy_gate.method_record(bd, bulk_manifest, 'bulk'),
                ], require_same_kpoints=False)
                warnings.extend(method_check['warnings'])
                if method_check['status'] == 'incompatible':
                    return {'ok': False, 'gamma_jm2': None, 'warnings': warnings,
                            'method_check': method_check,
                            'error': '方法不一致，表面能的大数相减不可比：'
                                     + '；'.join(method_check['issues'])}
                e_bpa = e_bulk / n_bulk
            else:
                try:
                    e_bpa = float(e_bpa)
                except (TypeError, ValueError):
                    return {'ok': False, 'gamma_jm2': None, 'warnings': warnings,
                            'method_check': None, 'error': '体相每原子能不是有效数字'}
                if not math.isfinite(e_bpa):
                    return {'ok': False, 'gamma_jm2': None, 'warnings': warnings,
                            'method_check': None, 'error': '体相每原子能非有限数'}
                method_check = {'status': 'unverified', 'issues': [],
                                'warnings': ['体相每原子能为手工输入，无 bulk job.yaml '
                                             '可核对 DONE/泛函/ENCUT/POTCAR/K 点口径'],
                                'checked_fields': 0, 'labels': ['slab', '手工体相能']}
                warnings.extend(method_check['warnings'])
            result = se.surface_energy(float(e_slab), int(n_slab), float(e_bpa), float(area))
            gamma = float(result['gamma_jm2'])
            warnings.extend(list(result.get('warnings') or []))
            response = {'ok': True, 'gamma_jm2': gamma,
                        'gamma_evA2': float(result['gamma_evA2']), 'area_a2': float(area),
                        'e_slab': float(e_slab), 'n_slab': int(n_slab),
                        'e_bulk_per_atom': float(e_bpa),
                        'warnings': warnings, 'method_check': method_check,
                        'evidence': completion_evidence,
                        'note': result.get('note') or
                        f'γ = (E_slab − N·E_bulk)/2A = {gamma:.4f} J/m²', 'error': None}
            response['report_file'] = None
            if write_report:
                try:
                    from vcstudio.project import special_report
                    evidence_dirs = [sd] + ([(bulk_dir or '').strip()]
                                            if (bulk_dir or '').strip() else [])
                    response['report_file'] = special_report.write(
                        '表面能报告', response, evidence_dirs,
                        os.path.join(sd, 'vcstudio-surface-energy-report.html'))
                except Exception as report_error:         # noqa: BLE001
                    warnings.append(f'报告生成失败：{report_error}')
            return response
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'gamma_jm2': None, 'warnings': [],
                    'method_check': None, 'error': str(e)}

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

    def ai_compare(self, project_id, reference):
        try:
            record = self._resolve_project_id(project_id)
        except Exception:                                 # noqa: BLE001 public identity seam
            return self._project_identity_failure(n=0)
        result = self._call_with_project_bindings(
            [record],
            lambda: self._ai_compare_for_path(record['path'], reference),
            failure={'n': 0})
        return self._project_public_result(result)

    def _ai_compare_for_path(self, project_path, reference):
        """项目计算值 × 文献参考(体系×物种×量对齐)→ MAE/RMSE/最差3项/对照表(纯确定性)。

        reference 为 ai_extract_tables 的 tables(或已归一参考集)。返回 {'ok','n','mae','rmse',
        'worst','pairs','unmatched','summary','error'};无对齐项 ok=True 但 n=0 + 说明。
        """
        try:
            proj = self._load_project_for_path((project_path or '').strip())
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

    def ai_write_validation(self, project_id, reference, out=None):
        try:
            record = self._resolve_project_id(project_id)
        except Exception:                                 # noqa: BLE001 public identity seam
            return self._project_identity_failure(path=None, n=0)
        result = self._call_with_project_bindings(
            [record],
            lambda: self._ai_write_validation_for_path(
                record['path'], reference, out=out),
            failure={'path': None, 'n': 0})
        return self._project_public_result(result)

    def _ai_write_validation_for_path(self, project_path, reference, out=None):
        """把文献对照写成 validation.md(项目 report/ 下或指定路径)→ {'ok','path','n','error'}。"""
        try:
            proj = self._load_project_for_path((project_path or '').strip())
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

    def ai_manuscript(self, project_id, fmt='markdown', reference=None):
        try:
            record = self._resolve_project_id(project_id)
        except Exception:                                 # noqa: BLE001 public identity seam
            return self._project_identity_failure(path=None)
        result = self._call_with_project_bindings(
            [record],
            lambda: self._ai_manuscript_for_path(
                record['path'], fmt=fmt, reference=reference),
            failure={'path': None})
        return self._project_public_result(result)

    def _ai_manuscript_for_path(
            self, project_path, fmt='markdown', reference=None):
        """生成论文骨架:Methods 全自动 + Results 逐图数据句 + 占位待补 → 诚实展示自动/占位比。

        返回 {'ok','path','md_path','docx_path','docx_available','sections','stats':{auto,
        placeholder,total,auto_ratio},'note','error'}。fmt='docx' 时缺 python-docx 降级只出 .md。
        """
        try:
            proj = self._load_project_for_path((project_path or '').strip())
            if proj is None:
                return {'ok': False, 'path': None, 'error': '项目不存在或 project.yaml 已移动'}
            comparison = None
            if reference is not None:
                cr = self._ai_compare_for_path(project_path, reference)
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
    _FROZEN_DEPS_INSTALL_ERROR = (
        '当前运行的是单文件 EXE，不能在运行时安装 Python 依赖。'
        '单文件 EXE 中 sys.executable 指向软件本身，用它执行 pip 会重新打开本软件；'
        '即使改用外部 Python 安装，新包也不会可靠地进入已冻结的 EXE。'
        '请使用已内置所需组件的完整版 EXE，或在源码 Python 环境安装依赖后重新打包。'
    )

    @staticmethod
    def _runtime_deps_install_supported():
        """运行时 pip 只对源码 Python 环境开放。

        PyInstaller/Nuitka 冻结态的 ``sys.executable`` 是应用程序而不是
        Python 解释器；且单文件包的 import 集合在构建时已确定。
        """
        return not bool(getattr(sys, 'frozen', False))

    @staticmethod
    def _pkg_present(name):
        try:
            import importlib.util
            return importlib.util.find_spec(name) is not None
        except Exception:                                 # noqa: BLE001
            return False

    @staticmethod
    def _dependency_command(pip_names, *, frozen=None):
        """Return a copyable pip command for PowerShell / the current terminal.

        In source mode the exact interpreter running VCStudio is used so users do not
        accidentally install into another Python.  A frozen executable has no usable
        Python interpreter of its own; ``py`` is therefore shown only as a clearly
        labelled *source-environment* command by :meth:`deps_status`.
        """
        frozen = bool(getattr(sys, 'frozen', False)) if frozen is None else bool(frozen)
        executable = 'py' if frozen else str(sys.executable)
        windows_path = bool(re.match(r'^[A-Za-z]:[\\/]', executable))
        if windows_path:
            # PowerShell parses a quoted executable path as a string unless the
            # call operator is present.  The dialog explicitly targets PowerShell,
            # so emit a command users can paste without editing.
            executable = f'& "{executable}"'
        elif any(ch.isspace() for ch in executable):
            executable = f'"{executable}"'
        return executable + ' -m pip install ' + ' '.join(str(x) for x in pip_names)

    def deps_status(self):
        """依赖状态汇总(侧栏依赖状态区)→ {'ok','deps':[{key,name,available,detail,installable,
        note}],'runtime_install_supported','runtime_install_note','error'}。RDKit/DECIMER/matplotlib
        按 import 探测;Multiwfn/VMD 走各自 probe。"""
        try:
            deps = []
            install_supported = self._runtime_deps_install_supported()
            py = [('rdkit', 'RDKit', 'rdkit'), ('decimer', 'DECIMER', 'decimer'),
                  ('matplotlib', 'matplotlib', 'matplotlib'),
                  ('python-docx', 'python-docx', 'docx'), ('pypdf', 'pypdf', 'pypdf')]
            for key, name, mod in py:
                ok = self._pkg_present(mod)
                note = self._DEPS_INSTALLABLE.get(key, {}).get('note', '')
                pip_names = list(self._DEPS_INSTALLABLE.get(key, {}).get('pip', ()))
                if not install_supported and not ok:
                    note += '；单文件 EXE 需在打包时内置此组件'
                deps.append({'key': key, 'name': name, 'available': ok,
                             'detail': '已安装' if ok else '未安装',
                             'installable': (install_supported and
                                             key in self._DEPS_INSTALLABLE),
                             'pip': pip_names,
                             'install_command': self._dependency_command(
                                 pip_names, frozen=not install_supported),
                             'command_applies_to_current_app': install_supported,
                             'note': note})
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
                             'pip': [], 'install_command': '',
                             'command_applies_to_current_app': False,
                             'note': '外部程序,请在波函数页填路径或加入 PATH'})
            return {'ok': True, 'deps': deps,
                    'runtime_install_supported': install_supported,
                    'runtime_install_note': (None if install_supported
                                             else self._FROZEN_DEPS_INSTALL_ERROR),
                    'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'deps': [], 'runtime_install_supported': False,
                    'runtime_install_note': None, 'error': str(e)}

    @staticmethod
    def _default_deps_runner(pip_names, log_path):
        """默认后台 pip 安装器:Popen 输出重定向到日志文件。"""
        if getattr(sys, 'frozen', False):
            # 双重防线:即使调用方绕过 deps_install，也绝不能把当前
            # EXE 当成 python.exe 再启动。
            raise RuntimeError(Api._FROZEN_DEPS_INSTALL_ERROR)
        import subprocess
        logf = open(log_path, 'w', encoding='utf-8')
        try:
            return subprocess.Popen([sys.executable, '-m', 'pip', 'install', *pip_names],
                                    stdout=logf, stderr=subprocess.STDOUT)
        finally:
            # Popen 已把文件句柄交给子进程；父进程不应在整个下载
            # 期间额外占用日志文件(也避免 Popen 启动失败时泄漏)。
            logf.close()

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
            if not self._runtime_deps_install_supported():
                return {'ok': False, 'started': False, 'pkgs': [], 'pip': [],
                        'rejected': rejected, 'log_path': None,
                        'error': self._FROZEN_DEPS_INSTALL_ERROR}
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
            # v3.3.0 实耗核时(与上面的"预算估算已花"并列,口径独立:时间戳×提交核数)
            used_30d, usage_unknown_n = None, 0
            try:
                u = self._usage().usage_stats(entries, days=30,
                                              profile_cores_by_name=self._profile_cores())
                used_30d = u.get('core_hours')
                usage_unknown_n = len(u.get('unknown') or [])
            except Exception:                             # noqa: BLE001 统计失败不挡概览
                pass
            active = running + queued
            status = ('运行中' if running else ('排队中' if queued else '空闲'))
            return {'ok': True, 'jobs_30d': jobs_30d, 'jobs_total': jobs_total,
                    'core_hours_30d': round(spent, 2),
                    'used_core_hours_30d': used_30d,
                    'usage_unknown_n': usage_unknown_n,
                    'remaining_core_hours': remaining,
                    'budget_cap': round(cap_total, 2) if cap_total else None,
                    'monitor': {'running': running, 'queued': queued, 'active': active,
                                'status': status}, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'jobs_30d': 0, 'jobs_total': 0, 'core_hours_30d': 0.0,
                    'remaining_core_hours': None, 'budget_cap': None,
                    'monitor': {}, 'error': str(e)}

    def usage_stats(self, days=30):
        """近 N 天实耗核时明细(诚实口径:RUNNING→终态时间戳 × 提交核数)。

        返回 {'ok','days','core_hours','jobs':[逐作业含 core_hours/basis/running],
        'jobs_counted','running_jobs','unknown':[缺数据作业+原因],'note','error'}。
        缺核数/缺时间戳的作业不计入总数、单列原因,绝不编数。
        """
        try:
            r = self._usage().usage_stats(
                list(self._ledger.load_all()), days=int(days or 30),
                profile_cores_by_name=self._profile_cores())
            return {'ok': True, **r, 'error': None}
        except Exception as e:                            # noqa: BLE001
            return {'ok': False, 'days': int(days or 30), 'core_hours': 0.0, 'jobs': [],
                    'jobs_counted': 0, 'running_jobs': 0, 'unknown': [], 'note': '',
                    'error': str(e)}

    # ── 波函数页:分析项分组菜单(单一事实源 = 引擎 multiwfn_driver.ANALYSES) ──
    # v3.2.2:ELF-LOL/ADCH/性质汇总/Fukui-CDFT 四项已自 api 层 _EXTRA_ANALYSES 迁入引擎
    # 注册表(菜单流字节不变);api 不再自带 stdin 脚本,菜单与执行均以引擎为准——
    # 远程分析(wavefn_run_remote 走 ANALYSES)也因此自动获得这四项。

    # 分析项分组(对齐 starpivot:常用 / 实空间与截面 / 弱相互作用 / 其它)
    _WAVEFN_GROUPS = (
        ('常用', ('esp_extrema', 'density_cube', 'esp_cube', 'homo_lumo_cube')),
        ('实空间与截面', ('elf_lol_section', 'alie', 'alie_extrema', 'adch_charge')),
        ('弱相互作用', ('nci_rdg', 'igmh', 'iri')),
        ('其它', ('aim_cp', 'property_summary', 'fukui_cdft')),
    )

    def wavefn_analyses(self):
        """波函数分析项分组菜单(单一事实源:引擎 ANALYSES)→ {'ok','groups':[{group,items:[{key,
        name,note,source,outputs}]}],'error'}。source 恒为 engine(四补充项 v3.2.2 已迁入引擎);
        引擎缺某项 → 菜单诚实少该项(不虚列点不动的卡)。其它组收未分组项。"""
        try:
            eng = dict(self._mw().ANALYSES)
            merged = {}
            for k, v in eng.items():
                merged[k] = {'key': k, 'name': v.get('name', k), 'note': v.get('note', ''),
                             'outputs': list(v.get('outputs') or ()), 'source': 'engine'}
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
        """跑补充分析四项(elf_lol_section/adch_charge/property_summary/fukui_cdft)——兼容入口。

        v3.2.2 起四项在引擎 multiwfn_driver.ANALYSES 注册表,本方法直接按引擎 run() 执行
        (语义等价 wavefn_run;保留本方法兼容旧前端路由与 Fukui 面板直连)。注入的引擎若无
        该项(旧版引擎),按项返回「引擎待扩展」中文说明,绝不假成功。返回
        {'ok','results':[{analysis,ok,outputs,stdout_tail,elapsed_s,script,error}],'error'}。
        """
        try:
            wf = (wavefn_file or '').strip()
            keys = [str(a).strip() for a in (analyses or []) if str(a).strip()]
            if not wf:
                return {'ok': False, 'results': [], 'error': '未选择波函数文件'}
            if not keys:
                return {'ok': False, 'results': [], 'error': '未选择分析项'}
            mw = self._mw()
            registry = dict(getattr(mw, 'ANALYSES', None) or {})
            mw_exe = (exe or self._tool_paths().get('multiwfn') or '').strip() or None
            wd = (workdir or '').strip() or None
            p = dict(params or {})
            results = []
            for k in keys:
                if k not in registry:
                    results.append({'analysis': k, 'ok': False, 'outputs': [],
                                    'stdout_tail': '', 'script': '', 'elapsed_s': 0.0,
                                    'error': (f'引擎待扩展:multiwfn_driver.ANALYSES 无分析项 '
                                              f'{k!r}(引擎版本过旧,请更新 vcstudio)')})
                    continue
                try:
                    r = mw.run(wf, k, exe=mw_exe, workdir=wd, params=p)
                    results.append({'analysis': k, 'ok': bool(r.get('ok')),
                                    'outputs': list(r.get('outputs') or []),
                                    'stdout_tail': r.get('stdout_tail', ''),
                                    'elapsed_s': r.get('elapsed_s', 0.0),
                                    'script': r.get('script', ''),
                                    'error': r.get('error') or None})
                except Exception as e:                    # noqa: BLE001
                    results.append({'analysis': k, 'ok': False, 'outputs': [],
                                    'stdout_tail': '', 'script': '', 'elapsed_s': 0.0,
                                    'error': str(e)})
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
                client, jump = conn.open_client(prof, pw, trust_new=trust_new)
            except conn.ConnectError as e:
                return {'ok': False, 'png': None, 'tcl': tcl, 'experimental': True,
                        'needs_trust': bool(getattr(e, 'needs_trust', False)),
                        **self._host_key_evidence(e), 'error': str(e)}
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
