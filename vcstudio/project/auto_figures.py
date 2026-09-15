"""管线终点自动出图引擎:任务全绿后,零点击产出整套投稿级图 + 组合大图 + 溯源清单。

定位(2026-07,用户核心诉求"提交后全程零点击直到拿到整套图"):管线把作业跑完、
拉回、验收后,本模块按**场景**(锂硫 / 电催化 / 热催化 / 通用)选定一张"图计划",
逐项检查数据可用性(ΔE 完成度 / 静态产物 / 频率 / 体系数),只画数据齐的图,缺的
记中文原因跳过,再把成图拼成一张多面板组合图,并为每张图落一条**溯源清单**
(数据源作业 / 参数 / 时间戳)供 report_gate 核验。

设计红线(与全仓一致):
- **绝不编数**:数据不齐只跳过并记中文原因,绝不假装出图或补零。
- **纯引擎**:本模块只做"选图 + 取数 + 调渲染 + 拼版 + 记清单",不碰调度/远程/GUI;
  gui_web 的 pipeline_tick 后续接线时消费本模块(接线由 GUI 批完成)。
- 渲染走 project.figure_presets(数据契约校验 + 分发 native_charts),拼版走
  project.panel_composer;matplotlib 属可选依赖,全部延迟 import。

取数口径:bar/table/ladder(锂硫)可直接从吸附能项目(project.adsorption)现算;
pdos/chgdiff/volcano 等需上游产物(静态/多体系),经 ``project['figure_inputs']`` 传入
(GUI/pipeline 在派生产物解析后填充),缺则如实跳过。中文注释允许,英文标识符。
"""
from __future__ import annotations

import os
import time

from vcstudio.external.native_charts import DOUBLE_COL, SINGLE_COL

# ── 期刊风格预设(透传 native_charts 的 style_kw + palette + 目标栏宽)──────────
JOURNAL_STYLES = {
    # nature:单栏 89 mm、8 pt serif、去顶右边框、NPG 配色
    'nature': {'font': 'serif', 'base_size': 8.0, 'box': False, 'palette': 'npg',
               'col_mm': 89, 'width_single': SINGLE_COL, 'width_double': DOUBLE_COL},
    # acs:双栏为主、8 pt sans、Okabe-Ito
    'acs': {'font': 'sans', 'base_size': 8.0, 'box': False, 'palette': 'okabe_ito',
            'col_mm': 83, 'width_single': 3.25, 'width_double': 7.00},
    # prb:四边框(box)、8.5 pt serif、Paul Tol
    'prb': {'font': 'serif', 'base_size': 8.5, 'box': True, 'palette': 'tol_bright',
            'col_mm': 86, 'width_single': 3.375, 'width_double': 6.75},
    # science:双栏、8 pt sans
    'science': {'font': 'sans', 'base_size': 8.0, 'box': False, 'palette': 'tol_bright',
                'col_mm': 87, 'width_single': 3.42, 'width_double': 7.00},
}


def journal_style(journal: str = 'nature') -> dict:
    """期刊名 → 风格字典(含可直接透传 native_charts 的 ``style_kw`` 与 palette/栏宽)。

    未知期刊名回落 nature。返回深拷贝,调用方随意改动不影响注册表。
    键:font/base_size/box/palette/col_mm/width_single/width_double + 便捷 style_kw。
    """
    base = JOURNAL_STYLES.get(str(journal).lower(), JOURNAL_STYLES['nature'])
    out = dict(base)
    out['journal'] = str(journal).lower() if str(journal).lower() in JOURNAL_STYLES else 'nature'
    out['style_kw'] = {'font': base['font'], 'base_size': base['base_size'],
                       'box': base['box']}
    return out


# ── 数据可用性判定(纯函数,name → 谓词 + 中文缺因)──────────────────────────────
# 每个条件 = {'test': fn(avail, params)->bool, 'reason': fn(avail, params)->str}。

def _n_delta(avail):
    return int(avail.get('n_delta', 0) or 0)


_CONDITIONS = {
    'has_delta': {
        'test': lambda a, p: _n_delta(a) >= 1,
        'reason': lambda a, p: '无已完成的 ΔE(需构型+清洁表面+参考全 DONE)',
    },
    'has_ladder': {
        'test': lambda a, p: bool(a.get('has_ladder')),
        'reason': lambda a, p: a.get('ladder_reason')
        or '缺自由能台阶数据(需分子库 + 清洁表面/中间体能量)',
    },
    'min_systems_3': {
        'test': lambda a, p: int(a.get('n_systems', 0) or 0) >= 3 and bool(a.get('has_volcano')),
        'reason': lambda a, p: (
            f'对比体系不足 3 个(当前 {int(a.get("n_systems", 0) or 0)}),'
            '火山图/标度关系需 ≥3 个催化剂且各具描述符与活性'),
    },
    'has_static': {
        'test': lambda a, p: bool(a.get('has_pdos')),
        'reason': lambda a, p: '无静态电子结构产物(需 relax 后派生 PDOS 静态单点并解析)',
    },
    'has_chgdiff': {
        'test': lambda a, p: bool(a.get('has_chgdiff')),
        'reason': lambda a, p: '无差分电荷产物(需吸附/清洁/吸附质三体系 CHGCAR 相减)',
    },
    'has_freq': {
        'test': lambda a, p: bool(a.get('has_freq')),
        'reason': lambda a, p: '无频率产物(需对吸附中间体做 Γ 点频率)',
    },
}


def _condition_ok(name, avail, params):
    cond = _CONDITIONS.get(name)
    if cond is None:                       # 未知条件名视作不可用(显式,不静默放行)
        return False, f'未知数据条件「{name}」'
    if cond['test'](avail, params):
        return True, ''
    return False, cond['reason'](avail, params)


# ── 图计划注册表(按场景;每项 preset_key 须为 figure_presets 的预设 key)─────────
FIGURE_PLANS = {
    # 锂硫(张洪毅体系):柱状 + 三线表 + 16e 放电台阶 + 火山(≥3) + PDOS + 差分电荷
    'lis': [
        {'key': 'adsorption_bar', 'condition': 'has_delta',
         'params': {'negative_up': True}},
        {'key': 'energy_matrix_table', 'condition': 'has_delta', 'params': {}},
        {'key': 'free_energy_ladder', 'condition': 'has_ladder',
         'params': {'show_ul': True}, 'reaction': 'lis_16e'},
        {'key': 'volcano', 'condition': 'min_systems_3', 'params': {}},
        {'key': 'pdos', 'condition': 'has_static', 'params': {}},
        {'key': 'charge_profile', 'condition': 'has_chgdiff', 'params': {}},
    ],
    # 电催化(ORR/OER/HER/CO2RR):同上,台阶走 CHE 通用引擎(经 figure_inputs)
    'electrocatalysis': [
        {'key': 'adsorption_bar', 'condition': 'has_delta', 'params': {}},
        {'key': 'energy_matrix_table', 'condition': 'has_delta', 'params': {}},
        {'key': 'free_energy_ladder', 'condition': 'has_ladder',
         'params': {'show_ul': True}},
        {'key': 'volcano', 'condition': 'min_systems_3', 'params': {}},
        {'key': 'pdos', 'condition': 'has_static', 'params': {}},
        {'key': 'charge_profile', 'condition': 'has_chgdiff', 'params': {}},
    ],
    # 热催化:能量学 + 电子结构(无电位/无火山 U_L;多体系走标度关系)
    'thermocatalysis': [
        {'key': 'adsorption_bar', 'condition': 'has_delta', 'params': {}},
        {'key': 'energy_matrix_table', 'condition': 'has_delta', 'params': {}},
        {'key': 'scaling_relation', 'condition': 'min_systems_3', 'params': {}},
        {'key': 'pdos', 'condition': 'has_static', 'params': {}},
        {'key': 'charge_profile', 'condition': 'has_chgdiff', 'params': {}},
    ],
    # 通用兜底:能量学柱/表 + 电子结构
    'general': [
        {'key': 'adsorption_bar', 'condition': 'has_delta', 'params': {}},
        {'key': 'energy_matrix_table', 'condition': 'has_delta', 'params': {}},
        {'key': 'pdos', 'condition': 'has_static', 'params': {}},
        {'key': 'charge_profile', 'condition': 'has_chgdiff', 'params': {}},
    ],
}


def list_scenarios() -> list:
    """已注册的出图场景键(lis/electrocatalysis/thermocatalysis/general)。"""
    return list(FIGURE_PLANS)


def _plan_items(scenario):
    plan = FIGURE_PLANS.get(str(scenario))
    if plan is None:
        raise ValueError(
            f'未知出图场景 {scenario!r},可选:{", ".join(FIGURE_PLANS)}')
    return plan


# ── 取数:吸附能项目 + figure_inputs 种子 → 可用性 avail(含 _data 渲染数据)─────

def _resolve_molecules_dir(molecules_dir):
    """None → 试读 config.lis_molecules_dir;任何失败静默回落 None。"""
    if molecules_dir:
        return str(molecules_dir) if os.path.isdir(str(molecules_dir)) else None
    try:
        from vcstudio.shared import config
        d = (config.load_config() or {}).get('lis_molecules_dir') or ''
        return str(d) if d and os.path.isdir(str(d)) else None
    except Exception:                      # noqa: BLE001 配置读失败 → 无分子库
        return None


def _delta_from_project(project, adsorption_mod):
    """吸附能项目 → (adsorbates, substrates, summary) 或 None(取数失败/无成员)。

    取每物种**最稳**且 ΔE 非 None 的行,adsorbate=species、value=ΔE;单基底=项目名。
    """
    try:
        summary = adsorption_mod.delta_e_rows(project)
    except Exception:                      # noqa: BLE001 非吸附项目/畸形 → 无 ΔE
        return None
    rows = summary.get('rows') or []
    stable = [r for r in rows
              if r.get('is_most_stable') and r.get('delta_e') is not None]
    if not stable:
        return [], {}, summary
    name = str(project.get('name') or '') or '项目'
    adsorbates = [str(r['species']) for r in stable]
    values = [r['delta_e'] for r in stable]
    return adsorbates, {name: values}, summary


def _ladder_from_project(project, summary, scenario, molecules_dir):
    """锂硫场景:项目 ΔE 行 + 分子库 → free_energy_ladder 渲染数据 或 (None, 中文原因)。"""
    if scenario != 'lis':
        return None, '当前场景的自由能台阶需经 figure_inputs 提供(非锂硫内置口径)'
    if not molecules_dir:
        return None, '未配置分子库目录 lis_molecules_dir,无法算 ΔG 台阶'
    if summary is None:
        return None, '无 ΔE 汇总,无法算 ΔG 台阶'
    _state, e_slab = summary.get('slab', (None, None))
    if e_slab is None:
        return None, '清洁表面未完成,无法算 ΔG 台阶'
    try:
        from vcstudio.project import freeenergy
        fed = freeenergy.path_from_project_and_molecules(
            summary.get('rows') or [], e_slab=e_slab, molecules_dir=molecules_dir,
            managed_dirs=((project or {}).get('species_ref_jobs') or {}).values(),
            project=project)
    except Exception as e:                 # noqa: BLE001 缺中间体/分子能量
        return None, str(e)
    name = str(project.get('name') or '') or '项目'
    data = {
        'paths': [{'name': name, 'G': [st['G'] for st in fed['steps']]}],
        'step_labels': [st['label'] for st in fed['steps']],
        'pds_index': fed.get('pds_index'),
        '_u_l': fed.get('u_l'),
    }
    return data, ''


def analyze_project(project, scenario, *, adsorption_mod=None,
                    molecules_dir=None) -> dict:
    """场景取数 → 可用性事实 avail(含渲染数据 avail['_data'])。

    数据来源优先级:``project['figure_inputs']``(GUI/pipeline 预解析的产物)> 吸附能
    项目现算(bar/table/锂硫 ladder)。avail 字段:name/scenario/n_delta/n_systems/
    has_bar/has_table/has_ladder/has_volcano/has_pdos/has_chgdiff/has_freq。
    """
    _plan_items(scenario)                  # 场景校验(未知即中文报错)
    if adsorption_mod is None:
        from vcstudio.project import adsorption as adsorption_mod
    molecules_dir = _resolve_molecules_dir(molecules_dir)
    fin = dict((project or {}).get('figure_inputs') or {})
    name = str((project or {}).get('name') or '') or '项目'

    data: dict = {}
    avail: dict = {'name': name, 'scenario': str(scenario), 'n_delta': 0,
                   'n_systems': 0, 'has_bar': False, 'has_table': False,
                   'has_ladder': False, 'has_volcano': False, 'has_pdos': False,
                   'has_chgdiff': False, 'has_freq': False, 'member_dirs': []}

    members = (project or {}).get('members') or {}
    mdirs = [members.get('clean_slab'), members.get('gas_ref')]
    mdirs += list(members.get('configs') or [])
    avail['member_dirs'] = [d for d in mdirs if d]

    # ── bar/table 数据:figure_inputs 优先,否则吸附能项目现算 ──
    summary = None
    if fin.get('adsorbates') and fin.get('substrates'):
        adsorbates = list(fin['adsorbates'])
        substrates = dict(fin['substrates'])
        n_delta = sum(1 for v in (substrates.get(name) or next(iter(substrates.values()), []))
                      if v is not None)
    else:
        got = _delta_from_project(project, adsorption_mod)
        if got is None:
            adsorbates, substrates, summary = [], {}, None
        else:
            adsorbates, substrates, summary = got
        n_delta = len(adsorbates)
    if adsorbates and substrates:
        avail['n_delta'] = n_delta
        avail['n_systems'] = len(substrates)
        de_data = {'adsorbates': adsorbates, 'substrates': substrates}
        if any(any(v is not None for v in vals) for vals in substrates.values()):
            avail['has_bar'] = avail['has_table'] = True
            data['adsorption_bar'] = dict(de_data)
            tbl = dict(de_data)
            if fin.get('dg'):
                tbl['dg'] = dict(fin['dg'])
            data['energy_matrix_table'] = tbl

    # ── ladder:figure_inputs 优先,否则锂硫内置引擎 ──
    if fin.get('ladder'):
        data['free_energy_ladder'] = dict(fin['ladder'])
        avail['has_ladder'] = True
    else:
        lad, reason = _ladder_from_project(project, summary, str(scenario), molecules_dir)
        if lad is not None:
            data['free_energy_ladder'] = lad
            avail['has_ladder'] = True
        else:
            avail['ladder_reason'] = reason

    # ── volcano / scaling:需 ≥3 体系;经 figure_inputs 提供描述符-活性点 ──
    pts = fin.get('volcano_points') or fin.get('points')
    if pts and len(pts) >= 3:
        avail['has_volcano'] = True
        avail['n_systems'] = max(int(avail['n_systems']), len(pts))
        data['volcano'] = {'points': list(pts), 'legs': fin.get('legs')}
        sx = [p.get('x') for p in pts]
        sy = [p.get('y') for p in pts]
        data['scaling_relation'] = {'xs': sx, 'ys': sy,
                                    'labels': [p.get('name') for p in pts]}

    # ── pdos / chgdiff / freq:经 figure_inputs 提供已解析产物 ──
    pdos = fin.get('pdos')
    if pdos and pdos.get('series'):
        avail['has_pdos'] = True
        data['pdos'] = dict(pdos)
    chg = fin.get('chgdiff')
    if chg and chg.get('z') is not None and chg.get('rho') is not None:
        avail['has_chgdiff'] = True
        data['charge_profile'] = {'z': chg['z'], 'rho': chg['rho'],
                                  'regions': chg.get('regions')}
    if fin.get('freq') or fin.get('has_freq'):
        avail['has_freq'] = True

    avail['_data'] = data
    return avail


# ── 计划:逐项判定数据可用性 → planned / skipped ────────────────────────────────

def plan_for_project(project, scenario, *, availability=None, adsorption_mod=None,
                     molecules_dir=None) -> dict:
    """按场景图计划逐项检查数据可用性 → {'planned': [...], 'skipped': [{'key','reason'}]}。

    availability 给定(GUI/测试预算好的可用性 avail)则直接据其判定;否则据 project 现算。
    planned 每项含 key/condition/params;skipped 每项 key + 中文 reason。
    """
    plan = _plan_items(scenario)
    avail = availability if availability is not None else analyze_project(
        project, scenario, adsorption_mod=adsorption_mod, molecules_dir=molecules_dir)
    planned, skipped = [], []
    for item in plan:
        ok, reason = _condition_ok(item['condition'], avail, item.get('params') or {})
        if ok:
            planned.append({'key': item['key'], 'condition': item['condition'],
                            'params': dict(item.get('params') or {})})
        else:
            skipped.append({'key': item['key'], 'reason': reason})
    return {'planned': planned, 'skipped': skipped, 'scenario': str(scenario),
            'availability': {k: v for k, v in avail.items() if k != '_data'}}


# ── 渲染参数拼装(注入期刊 style_kw / 选择性 palette / 标题)──────────────────────

_TITLES = {
    'adsorption_bar': '', 'energy_matrix_table': '',
    'free_energy_ladder': '', 'volcano': '', 'scaling_relation': '',
    'pdos': '', 'charge_profile': '',
}


def _render_params(key, item, js, figure_presets_mod):
    params = dict(item.get('params') or {})
    params.setdefault('style_kw', dict(js['style_kw']))
    try:
        schema = figure_presets_mod.get_preset(key).get('params_schema') or {}
    except Exception:                      # noqa: BLE001
        schema = {}
    if 'palette' in schema and 'palette' not in params:
        params['palette'] = js['palette']
    # 锂硫台阶:标题带 U_L(有则)
    if key == 'free_energy_ladder':
        u_l = (item.get('_u_l'))
        if u_l is not None and 'title' not in params:
            params['title'] = rf'$U_\mathrm{{L}}$ = {u_l:.2f} V'
    return params


def _manifest_entry(key, params, files, avail, scenario, project,
                    figure_presets_mod):
    """一张图的溯源清单条目:预设/参数/数据源作业/时间戳(report_gate 核验用)。"""
    name = avail.get('name') or (project or {}).get('name') or '项目'
    origin = {
        'adsorption_bar': 'ΔE 吸附能(最稳构型,delta_e_rows)',
        'energy_matrix_table': 'ΔE 吸附能矩阵(delta_e_rows)',
        'free_energy_ladder': '自由能台阶(freeenergy 放电/CHE 路径)',
        'volcano': '火山图(多体系描述符-活性)',
        'scaling_relation': '标度关系(多体系描述符)',
        'pdos': 'PDOS(静态单点解析)',
        'charge_profile': '差分电荷面平均(chgdiff)',
    }.get(key, key)
    data_source = {'project': name, 'scenario': str(scenario), 'origin': origin,
                   'jobs': list(avail.get('member_dirs') or [])}
    try:
        prov = figure_presets_mod.preset_provenance(key, data_source, params=params)
    except Exception:                      # noqa: BLE001 未登记预设也要留痕
        prov = {'preset': key, 'params': params, 'data_source': data_source}
    prov['key'] = key
    prov['files'] = list(files)
    prov['timestamp'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    return prov


# ── 主入口:计划 → 逐图渲染 → 拼多面板 → 溯源清单 ──────────────────────────────

def run_auto_figures(project, scenario, out_dir, *, journal: str = 'nature',
                     adsorption_mod=None, molecules_dir=None,
                     figure_presets_mod=None, panel_composer_mod=None,
                     compose_panel: bool = True) -> dict:
    """管线终点一键出整套图:选图 → 取数 → 逐图渲染 → 拼多面板 → 落溯源清单。

    全部成品落 ``<out_dir>/figures/``。返回::

        {'ok', 'files': [...绝对路径...], 'skipped': [{'key','reason'}],
         'panel': {'files':[png,pdf],'n':k}|None, 'manifest': [每图溯源条目],
         'out_dir': figures 目录, 'error': None|中文}

    单张图渲染失败只记 skipped(不拖垮其他图);全流程异常兜底 ok=False。
    figure_presets_mod / panel_composer_mod / adsorption_mod 可注入替身(测试/自定义)。
    """
    if figure_presets_mod is None:
        from vcstudio.project import figure_presets as figure_presets_mod
    if panel_composer_mod is None:
        from vcstudio.project import panel_composer as panel_composer_mod
    try:
        js = journal_style(journal)
        avail = analyze_project(project, scenario, adsorption_mod=adsorption_mod,
                                molecules_dir=molecules_dir)
        data = avail.get('_data') or {}
        plan = plan_for_project(project, scenario, availability=avail)

        figures_dir = os.path.join(str(out_dir), 'figures')
        os.makedirs(figures_dir, exist_ok=True)

        files, manifest, panel_pngs = [], [], []
        skipped = list(plan['skipped'])
        for item in plan['planned']:
            key = item['key']
            fig_data = data.get(key)
            if fig_data is None:
                skipped.append({'key': key, 'reason': '计划命中但渲染数据缺失(内部不一致)'})
                continue
            # 台阶标题需要 u_l:从数据里回填到 item
            if key == 'free_energy_ladder':
                item = {**item, '_u_l': fig_data.get('_u_l')}
            params = _render_params(key, item, js, figure_presets_mod)
            out_png = os.path.join(figures_dir, f'{key}.png')
            try:
                fs = figure_presets_mod.render_preset(key, fig_data, out_png, **params)
            except Exception as e:         # noqa: BLE001 单图失败不拖垮其他
                skipped.append({'key': key, 'reason': f'渲染失败:{e}'})
                continue
            fs = list(fs)
            files += fs
            png = next((f for f in fs if str(f).lower().endswith('.png')), None)
            if png:
                panel_pngs.append(png)
            manifest.append(_manifest_entry(key, params, fs, avail, scenario,
                                            project, figure_presets_mod))

        # ── 多面板拼版(≥2 张成图才拼;单图无需组合)──
        panel = None
        if compose_panel and len(panel_pngs) >= 2:
            try:
                panels = [{'file': p, 'label': chr(ord('a') + i)}
                          for i, p in enumerate(panel_pngs)]
                ppaths = panel_composer_mod.compose(
                    panels, os.path.join(figures_dir, 'panel_combined.png'),
                    journal=js['journal'], width='double')
                files += list(ppaths)
                panel = {'files': list(ppaths), 'n': len(panel_pngs)}
            except Exception as e:         # noqa: BLE001 拼版失败不拖垮单图产物
                skipped.append({'key': 'panel_combined', 'reason': f'拼版失败:{e}'})

        return {'ok': True, 'files': files, 'skipped': skipped, 'panel': panel,
                'manifest': manifest, 'out_dir': figures_dir, 'error': None}
    except Exception as e:                 # noqa: BLE001 全流程兜底
        return {'ok': False, 'files': [], 'skipped': [], 'panel': None,
                'manifest': [], 'out_dir': None, 'error': str(e)}
