"""图表预设注册表 —— 把"我要画哪种图"沉淀成一张可枚举、可选参、可溯源的清单。

定位(2026-07):出图函数散落在 external.native_charts / external.lobster / project.neb,
每个都有自己的数据契约与参数。本模块是**预设层**:每个预设声明「名字/分类/需要什么数据/
缩略示意图/落到哪个渲染函数/可调参数」,GUI 据此列菜单、校验数据、一键出图,并产出图溯源工件。

铁律:预设只**声明与分发**,绝不自造数据;渲染前做数据契约校验,缺什么直接中文 ValueError;
未实装的图型(renderer='todo')明确抛 NotImplementedError,绝不静默或假装出图。

渲染函数一律**延迟 import**(matplotlib 属可选分析层依赖),注册表本身零重依赖可枚举。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import copy
import importlib

# ── 渲染函数落点(renderer 名 → 模块.函数);延迟 import,注册表零重依赖 ──────────
_RENDERER_TARGETS = {
    'adsorption_bar':      ('vcstudio.external.native_charts', 'adsorption_bar'),
    'heatmap_matrix':      ('vcstudio.external.native_charts', 'heatmap_matrix'),
    'scaling_relation':    ('vcstudio.external.native_charts', 'scaling_relation'),
    'volcano_plot':        ('vcstudio.external.native_charts', 'volcano_plot'),
    'energy_matrix_table': ('vcstudio.external.native_charts', 'energy_matrix_table'),
    'free_energy_ladder':  ('vcstudio.external.native_charts', 'free_energy_ladder'),
    'pdos_plot':           ('vcstudio.external.native_charts', 'pdos_plot'),
    'charge_profile_plot': ('vcstudio.external.native_charts', 'charge_profile_plot'),
    'convergence_plot':    ('vcstudio.generate.conv_scan', 'conv_plot'),
    'energy_time_plot':    ('vcstudio.external.native_charts', 'energy_time_plot'),
    'cohp_plot':           ('vcstudio.external.lobster', 'cohp_plot'),
    'neb_profile_plot':    ('vcstudio.project.neb', 'neb_profile_plot'),
}


def _load_renderer(renderer: str):
    """renderer 名 → 真实可调函数(延迟 import;调用时 getattr,便于测试 monkeypatch)。"""
    mod_name, fn_name = _RENDERER_TARGETS[renderer]
    return getattr(importlib.import_module(mod_name), fn_name)


# ── 数据契约校验(缺什么直接中文报错;不碰渲染库)──────────────────────────────

def _need(data, keys: list, human: str) -> None:
    """校验 data 为 dict 且含非空 keys;缺项 → 中文 ValueError 点名。"""
    if not isinstance(data, dict):
        raise ValueError(f'{human}:data 须为 dict(需含 {keys})')
    miss = []
    for k in keys:
        v = data.get(k)
        if v is None:
            miss.append(k)
            continue
        try:
            if len(v) == 0:                 # 空列表/空 dict/空数组 视作缺
                miss.append(k)
        except TypeError:                   # 标量无 len:视作已提供
            pass
    if miss:
        raise ValueError(f'{human}:数据契约不满足,缺少 {miss}(需要:{keys})')


# ── 各 renderer 的入参适配器:(fn, data, out_path, params) → 路径列表 ─────────────

def _a_adsorption_bar(fn, data, out_path, params):
    _need(data, ['adsorbates', 'substrates'], '吸附能柱状图')
    return fn(data, out_path, **params)


def _a_heatmap(fn, data, out_path, params):
    _need(data, ['rows', 'cols', 'values'], 'ΔE 热图')
    return fn(data, out_path, **params)


def _a_matrix_table(fn, data, out_path, params):
    _need(data, ['adsorbates', 'substrates'], '数据矩阵表')
    return fn(data, out_path, **params)


def _a_scaling(fn, data, out_path, params):
    _need(data, ['xs', 'ys'], '标度关系图')
    return fn(data['xs'], data['ys'], out_path, labels=data.get('labels'), **params)


def _a_volcano(fn, data, out_path, params):
    _need(data, ['points'], '火山图')
    return fn(data['points'], out_path, legs=data.get('legs'), **params)


def _a_ladder(fn, data, out_path, params):
    _need(data, ['paths'], 'ΔG 自由能台阶图')
    kw = dict(params)
    if data.get('step_labels') is not None:
        kw.setdefault('step_labels', data['step_labels'])
    if data.get('pds_index') is not None:            # 逐电子决速步须透传,否则高亮错步
        kw.setdefault('pds_index', data['pds_index'])
    return fn(data['paths'], out_path, **kw)


def _a_pdos(fn, data, out_path, params):
    _need(data, ['series'], 'PDOS 图')
    kw = dict(params)
    for k in ('efermi', 'band_centers'):
        if data.get(k) is not None:
            kw.setdefault(k, data[k])
    return fn(data['series'], out_path, **kw)


def _a_cohp(fn, data, out_path, params):
    _need(data, ['energies', 'pairs'], 'COHP 图')
    return fn(data, out_path, **params)


def _a_neb(fn, data, out_path, params):
    _need(data, ['rel'], 'NEB 剖面图')
    return fn(data, out_path, **params)


def _a_charge(fn, data, out_path, params):
    _need(data, ['z', 'rho'], '差分电荷面平均图')
    return fn(data['z'], data['rho'], out_path, regions=data.get('regions'), **params)


def _a_convergence(fn, data, out_path, params):
    _need(data, ['points'], '收敛测试曲线')
    kw = dict(params)
    for key in ('converged_at', 'threshold_mev', 'natoms', 'xlabel'):
        if data.get(key) is not None:
            kw.setdefault(key, data[key])
    return fn(data['points'], out_path, **kw)


def _a_aimd(fn, data, out_path, params):
    _need(data, ['steps'], 'AIMD 能量-温度诊断图')
    # A frozen server view may already carry an explicit time coordinate.  In
    # that case dt_fs is provenance, not another scaling instruction.
    has_time = any(isinstance(item, dict) and item.get('time') is not None
                   for item in data['steps'])
    dt_fs = None if has_time else data.get('dt_fs')
    return fn(data['steps'], out_path, dt_fs=dt_fs, **params)


_ADAPTERS = {
    'adsorption_bar': _a_adsorption_bar,
    'heatmap_matrix': _a_heatmap,
    'energy_matrix_table': _a_matrix_table,
    'scaling_relation': _a_scaling,
    'volcano_plot': _a_volcano,
    'free_energy_ladder': _a_ladder,
    'pdos_plot': _a_pdos,
    'cohp_plot': _a_cohp,
    'neb_profile_plot': _a_neb,
    'charge_profile_plot': _a_charge,
    'convergence_plot': _a_convergence,
    'energy_time_plot': _a_aimd,
}


# ── 极简缩略示意 SVG(80×60,手绘线条形态;仅示意图型,非真实数据)─────────────
_SVG_BAR = ('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="60" viewBox="0 0 80 60">'
            '<line x1="10" y1="50" x2="72" y2="50" stroke="#888"/>'
            '<rect x="16" y="28" width="8" height="22" fill="#3C5488"/>'
            '<rect x="28" y="18" width="8" height="32" fill="#E64B35"/>'
            '<rect x="40" y="34" width="8" height="16" fill="#00A087"/>'
            '<rect x="52" y="24" width="8" height="26" fill="#4DBBD5"/></svg>')

_SVG_HEATMAP = ('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="60" viewBox="0 0 80 60">'
                '<g stroke="#fff" stroke-width="1.2">'
                '<rect x="12" y="10" width="14" height="12" fill="#1a9850"/>'
                '<rect x="26" y="10" width="14" height="12" fill="#91cf60"/>'
                '<rect x="40" y="10" width="14" height="12" fill="#fee08b"/>'
                '<rect x="54" y="10" width="14" height="12" fill="#fc8d59"/>'
                '<rect x="12" y="22" width="14" height="12" fill="#91cf60"/>'
                '<rect x="26" y="22" width="14" height="12" fill="#fee08b"/>'
                '<rect x="40" y="22" width="14" height="12" fill="#fc8d59"/>'
                '<rect x="54" y="22" width="14" height="12" fill="#d73027"/>'
                '<rect x="12" y="34" width="14" height="12" fill="#fee08b"/>'
                '<rect x="26" y="34" width="14" height="12" fill="#1a9850"/>'
                '<rect x="40" y="34" width="14" height="12" fill="#91cf60"/>'
                '<rect x="54" y="34" width="14" height="12" fill="#fc8d59"/></g></svg>')

_SVG_SCALING = ('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="60" viewBox="0 0 80 60">'
                '<line x1="12" y1="50" x2="12" y2="8" stroke="#888"/>'
                '<line x1="12" y1="50" x2="72" y2="50" stroke="#888"/>'
                '<line x1="16" y1="46" x2="68" y2="14" stroke="#E64B35" stroke-width="1.5"/>'
                '<circle cx="22" cy="44" r="2.6" fill="#3C5488"/>'
                '<circle cx="34" cy="36" r="2.6" fill="#3C5488"/>'
                '<circle cx="48" cy="26" r="2.6" fill="#3C5488"/>'
                '<circle cx="62" cy="18" r="2.6" fill="#3C5488"/></svg>')

_SVG_VOLCANO = ('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="60" viewBox="0 0 80 60">'
                '<line x1="10" y1="50" x2="72" y2="50" stroke="#888"/>'
                '<polyline points="14,46 40,14 66,46" fill="none" stroke="#4477AA" stroke-width="1.5"/>'
                '<polygon points="40,8 44,14 40,20 36,14" fill="#DDAA33" stroke="#333" stroke-width="0.4"/>'
                '<circle cx="22" cy="40" r="2" fill="#333"/>'
                '<circle cx="31" cy="28" r="2" fill="#333"/>'
                '<circle cx="50" cy="30" r="2" fill="#333"/>'
                '<circle cx="60" cy="40" r="2" fill="#333"/></svg>')

_SVG_MATRIX_TABLE = ('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="60" viewBox="0 0 80 60">'
                     '<line x1="8" y1="12" x2="72" y2="12" stroke="#222" stroke-width="2"/>'
                     '<line x1="8" y1="22" x2="72" y2="22" stroke="#222" stroke-width="1"/>'
                     '<line x1="8" y1="50" x2="72" y2="50" stroke="#222" stroke-width="2"/>'
                     '<g stroke="#bbb" stroke-width="1.4">'
                     '<line x1="14" y1="17" x2="26" y2="17"/><line x1="34" y1="17" x2="46" y2="17"/>'
                     '<line x1="54" y1="17" x2="66" y2="17"/>'
                     '<line x1="14" y1="30" x2="26" y2="30"/><line x1="34" y1="30" x2="46" y2="30"/>'
                     '<line x1="54" y1="30" x2="66" y2="30"/>'
                     '<line x1="14" y1="40" x2="26" y2="40"/><line x1="34" y1="40" x2="46" y2="40"/>'
                     '<line x1="54" y1="40" x2="66" y2="40"/></g></svg>')

_SVG_LADDER = ('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="60" viewBox="0 0 80 60">'
               '<line x1="8" y1="30" x2="20" y2="30" stroke="#4477AA" stroke-width="2.4"/>'
               '<line x1="20" y1="30" x2="28" y2="20" stroke="#999" stroke-dasharray="3,2"/>'
               '<line x1="28" y1="20" x2="40" y2="20" stroke="#4477AA" stroke-width="2.4"/>'
               '<line x1="40" y1="20" x2="48" y2="40" stroke="#C92A2A" stroke-width="1.8"/>'
               '<line x1="48" y1="40" x2="60" y2="40" stroke="#4477AA" stroke-width="2.4"/>'
               '<line x1="60" y1="40" x2="68" y2="30" stroke="#999" stroke-dasharray="3,2"/>'
               '<line x1="68" y1="30" x2="76" y2="30" stroke="#4477AA" stroke-width="2.4"/></svg>')

_SVG_LADDER_MULTI = ('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="60" viewBox="0 0 80 60">'
                     '<g stroke="#4477AA" stroke-width="2"><line x1="8" y1="26" x2="20" y2="26"/>'
                     '<line x1="28" y1="18" x2="40" y2="18"/><line x1="48" y1="34" x2="60" y2="34"/>'
                     '<line x1="68" y1="24" x2="76" y2="24"/></g>'
                     '<g stroke="#EE6677" stroke-width="2"><line x1="8" y1="34" x2="20" y2="34"/>'
                     '<line x1="28" y1="28" x2="40" y2="28"/><line x1="48" y1="44" x2="60" y2="44"/>'
                     '<line x1="68" y1="36" x2="76" y2="36"/></g>'
                     '<g stroke="#bbb" stroke-dasharray="2,2">'
                     '<line x1="20" y1="26" x2="28" y2="18"/><line x1="40" y1="18" x2="48" y2="34"/>'
                     '<line x1="60" y1="34" x2="68" y2="24"/></g></svg>')

_SVG_NEB = ('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="60" viewBox="0 0 80 60">'
            '<line x1="10" y1="48" x2="72" y2="48" stroke="#888"/>'
            '<path d="M14,44 Q27,44 40,16 Q53,44 68,42" fill="none" stroke="#4477AA" stroke-width="1.6"/>'
            '<circle cx="40" cy="16" r="3" fill="#C92A2A" stroke="#333" stroke-width="0.4"/>'
            '<circle cx="14" cy="44" r="2" fill="#4477AA"/><circle cx="68" cy="42" r="2" fill="#4477AA"/></svg>')

_SVG_PDOS = ('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="60" viewBox="0 0 80 60">'
             '<line x1="10" y1="30" x2="72" y2="30" stroke="#888"/>'
             '<line x1="40" y1="8" x2="40" y2="52" stroke="#999" stroke-dasharray="3,2"/>'
             '<path d="M12,30 C22,30 22,14 32,14 C40,14 40,30 48,30 C58,30 58,20 66,20 L70,30" '
             'fill="none" stroke="#4477AA" stroke-width="1.3"/>'
             '<path d="M12,30 C22,30 22,46 32,46 C40,46 40,30 48,30 C58,30 58,40 66,40 L70,30" '
             'fill="none" stroke="#EE6677" stroke-width="1.3"/></svg>')

_SVG_COHP = ('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="60" viewBox="0 0 80 60">'
             '<line x1="40" y1="8" x2="40" y2="52" stroke="#888"/>'
             '<line x1="12" y1="30" x2="68" y2="30" stroke="#999" stroke-dasharray="3,2"/>'
             '<path d="M40,10 C54,16 30,23 44,30 C58,37 32,44 40,50" fill="none" '
             'stroke="#4477AA" stroke-width="1.4"/></svg>')

_SVG_CHARGE = ('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="60" viewBox="0 0 80 60">'
               '<line x1="10" y1="34" x2="72" y2="34" stroke="#888"/>'
               '<path d="M12,34 C22,34 24,16 34,16 C40,16 42,34 46,34 L12,34Z" fill="#EE6677" opacity="0.35"/>'
               '<path d="M46,34 C52,34 54,50 62,50 C66,50 68,40 72,38 L72,34 46,34Z" fill="#4477AA" opacity="0.35"/>'
               '<path d="M12,34 C22,34 24,16 34,16 C40,16 42,34 46,34 C52,34 54,50 62,50 C66,50 68,40 72,38" '
               'fill="none" stroke="#333" stroke-width="1.2"/></svg>')

_SVG_CONVERGE = ('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="60" viewBox="0 0 80 60">'
                 '<line x1="12" y1="50" x2="72" y2="50" stroke="#888"/>'
                 '<line x1="12" y1="50" x2="12" y2="8" stroke="#888"/>'
                 '<line x1="12" y1="24" x2="70" y2="24" stroke="#bbb" stroke-dasharray="3,2"/>'
                 '<polyline points="16,12 24,34 32,20 40,28 48,23 56,25 64,24 70,24" '
                 'fill="none" stroke="#4477AA" stroke-width="1.3"/>'
                 '<circle cx="48" cy="23" r="3" fill="#DDAA33" stroke="#333"/>'
                 '</svg>')

_SVG_AIMD = ('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="60" viewBox="0 0 80 60">'
             '<line x1="10" y1="50" x2="72" y2="50" stroke="#888"/>'
             '<path d="M12,31 L18,27 L24,34 L30,25 L36,29 L42,26 L48,31 L54,27 L60,30 L68,28" '
             'fill="none" stroke="#4477AA" stroke-width="1.3"/>'
             '<path d="M12,40 L18,38 L24,42 L30,35 L36,39 L42,34 L48,37 L54,33 L60,36 L68,32" '
             'fill="none" stroke="#EE6677" stroke-width="1.1"/></svg>')


# ── 预设注册表(≥12;renderer 名须为 _RENDERER_TARGETS 键或 'todo')──────────────
PRESETS = [
    {
        'key': 'adsorption_bar', 'name': '吸附能柱状图', 'category': '能量学',
        'description': '一组吸附质 × 一组基底的吸附能分组柱状图,柱顶标数值(对标论文图 3.23a)。',
        'required_data': "adsorbates(吸附质列表)+ substrates({基底: [各吸附质 ΔE]},缺值 None);每基底列表长度须等于 adsorbates。",
        'thumbnail_svg': _SVG_BAR, 'renderer': 'adsorption_bar',
        'params_schema': {'negative_up': False, 'value_labels': True,
                          'palette': 'npg', 'title': ''},
    },
    {
        'key': 'delta_e_heatmap', 'name': 'ΔE/ΔG 矩阵热图', 'category': '能量学',
        'description': '催化剂 × 吸附质的能量矩阵热图,格内标数值 + 色条,横向对比多催化剂。',
        'required_data': "rows(基底/催化剂)+ cols(吸附质)+ values(逐行 ΔE 矩阵,缺值 None → 空白格)。",
        'thumbnail_svg': _SVG_HEATMAP, 'renderer': 'heatmap_matrix',
        'params_schema': {'cmap': 'RdYlGn_r', 'annotate': True,
                          'cbar_label': r'$E_\mathrm{ads}$ (eV)', 'title': ''},
    },
    {
        'key': 'scaling_relation', 'name': '标度关系图', 'category': '能量学',
        'description': '两个吸附量的散点 + 最小二乘直线,标拟合式与 R²(标度关系诊断)。',
        'required_data': "xs、ys(两个等长描述符序列,如两吸附质的 ΔE);labels 可选(点名,如基底名)。",
        'thumbnail_svg': _SVG_SCALING, 'renderer': 'scaling_relation',
        'params_schema': {'xlabel': 'descriptor x', 'ylabel': 'descriptor y',
                          'fit': True, 'title': ''},
    },
    {
        'key': 'volcano', 'name': '火山图(含双腿)', 'category': '能量学',
        'description': 'Sabatier 活性 vs 吸附描述符;两条拟合腿自动求交点=峰顶(legs 提供时)。',
        'required_data': "points=[{name, x=描述符, y=活性}];legs 可选=两条腿 [{slope, intercept}](自动求 Sabatier 峰顶)。",
        'thumbnail_svg': _SVG_VOLCANO, 'renderer': 'volcano_plot',
        'params_schema': {'descriptor_label': r'descriptor', 'activity_label': r'$U_\mathrm{L}$ (V)',
                          'mark_top': True, 'annotate_points': True, 'title': ''},
    },
    {
        'key': 'energy_matrix_table', 'name': '数据矩阵表(三线表)', 'category': '能量学',
        'description': '吸附质 × 基底能量三线表(PNG/PDF/CSV),可含 ΔE 与可选 ΔG 列(对标论文表 3-2)。',
        'required_data': "adsorbates + substrates(每基底 ΔE 列);dg 可选({基底: [ΔG]},每基底一列)。",
        'thumbnail_svg': _SVG_MATRIX_TABLE, 'renderer': 'energy_matrix_table',
        'params_schema': {'de_header': 'ΔE (eV)', 'dg_header': 'ΔG (eV)', 'title': ''},
    },
    {
        'key': 'free_energy_ladder', 'name': 'ΔG 自由能台阶图(单)', 'category': '电池',
        'description': '单体系自由能台阶图:实线平台 + 虚线连接,可标决速步(PDS)。',
        'required_data': "paths=单体系 {name, G:[各步累计 ΔG]};step_labels 可选;pds_index 可选(逐电子决速步序号)。",
        'thumbnail_svg': _SVG_LADDER, 'renderer': 'free_energy_ladder',
        'params_schema': {'mark_pds': True, 'show_ul': False,
                          'ylabel': r'$\Delta G$ (eV)', 'title': ''},
    },
    {
        'key': 'free_energy_ladder_multi', 'name': 'ΔG 台阶图(多电位/多体系)', 'category': '电池',
        'description': '多体系/多电位自由能台阶叠加(U=0/U_eq/U_L 三线对比或多催化剂对比)。',
        'required_data': "paths=多体系 [{name, G:[...]}, ...](不同电位或催化剂);pds_index 逐电子口径透传。",
        'thumbnail_svg': _SVG_LADDER_MULTI, 'renderer': 'free_energy_ladder',
        'params_schema': {'mark_pds': True, 'show_ul': True,
                          'ylabel': r'$\Delta G$ (eV)', 'title': ''},
    },
    {
        'key': 'neb_profile', 'name': 'NEB 最小能量路径剖面', 'category': '电池',
        'description': 'CI-NEB 相对能量-反应坐标折线 + 过渡态红点 + 正/逆向能垒标注。',
        'required_data': "parse_neb_energies 的返回(含 rel 相对初态能量、ts_index、barrier_f/barrier_r)。",
        'thumbnail_svg': _SVG_NEB, 'renderer': 'neb_profile_plot',
        'params_schema': {'labels': ('IS', 'TS', 'FS'), 'point_labels': False, 'title': ''},
    },
    {
        'key': 'pdos', 'name': 'PDOS(自旋镜像)', 'category': '电子结构',
        'description': '投影态密度:多条投影叠加,自旋向下镜像至 y<0,标费米零点与 d 带中心。',
        'required_data': "series=[{label, energies, dos_up, dos_down?}];efermi、band_centers 可选。",
        'thumbnail_svg': _SVG_PDOS, 'renderer': 'pdos_plot',
        'params_schema': {'mirror_spin': True, 'xlim': (-8, 4), 'title': ''},
    },
    {
        'key': 'cohp', 'name': 'COHP 键强图', 'category': '电子结构',
        'description': 'LOBSTER −pCOHP 曲线(成键正值在右)+ 费米零线 + ICOHP 键强标注。',
        'required_data': "parse_cohpcar 的返回(energies + pairs,每对 cohp_up/cohp_down + icohp)。",
        'thumbnail_svg': _SVG_COHP, 'renderer': 'cohp_plot',
        'params_schema': {'flip': True, 'icohp_annotate': True},
    },
    {
        'key': 'charge_profile', 'name': '差分电荷面平均 Δρ̄(z)', 'category': '结构',
        'description': '面平均差分电荷曲线:聚集(>0)/耗散(<0)分色填充,可标区间底色。',
        'required_data': "z、rho(面平均序列,取自 chgdiff.plane_averaged);regions 可选(区间底色标注)。",
        'thumbnail_svg': _SVG_CHARGE, 'renderer': 'charge_profile_plot',
        'params_schema': {'title': ''},
    },
    {
        'key': 'convergence_curve', 'name': '收敛测试曲线', 'category': '结构',
        'description': '截断能/K 点/真空/层厚的服务器冻结收敛点、阈值带与推荐点。',
        'required_data': "points=[{x,energy}] + 显式 threshold_mev/natoms/converged_at;仅绑定真实扫描历史。",
        'thumbnail_svg': _SVG_CONVERGE, 'renderer': 'convergence_plot',
        'params_schema': {'xlabel': 'parameter', 'title': ''},
    },
    {
        'key': 'aimd_diagnostic', 'name': 'AIMD 能量-温度诊断图', 'category': '结构',
        'description': '服务器冻结的总能与温度时间线；仅作轨迹诊断，不宣称长期热稳定。',
        'required_data': "steps=[{time,energy,temperature}]，可附 dt_fs；数值须来自权威 AIMD 解析视图。",
        'thumbnail_svg': _SVG_AIMD, 'renderer': 'energy_time_plot',
        'params_schema': {'title': ''},
    },
]


CATEGORY_EN = {
    '能量学': 'Energetics',
    '电子结构': 'Electronic structure',
    '结构': 'Structure',
    '电池': 'Electrochemistry',
}

_PRESET_EN = {
    'adsorption_bar': {
        'name_en': 'Grouped adsorption-energy bar chart',
        'description_en': (
            'Grouped adsorption energies for multiple adsorbates and substrates, '
            'with value labels above the bars.'),
        'required_data_en': (
            'adsorbates (list) + substrates ({substrate: [ΔE for each adsorbate]}; '
            'use None for missing values); each substrate list must match the '
            'adsorbates length.'),
    },
    'delta_e_heatmap': {
        'name_en': 'ΔE/ΔG matrix heatmap',
        'description_en': (
            'A catalyst-by-adsorbate energy heatmap with cell annotations and a '
            'color scale for cross-catalyst comparison.'),
        'required_data_en': (
            'rows (substrates or catalysts) + cols (adsorbates) + values (row-wise '
            'ΔE matrix); use None for a blank cell.'),
    },
    'scaling_relation': {
        'name_en': 'Scaling-relation plot',
        'description_en': (
            'Scatter plot for two adsorption descriptors with a least-squares line, '
            'fitted equation, and R² diagnostic.'),
        'required_data_en': (
            'xs and ys (equal-length descriptor series, such as ΔE for two '
            'adsorbates); labels is optional and names each point.'),
    },
    'volcano': {
        'name_en': 'Two-leg volcano plot',
        'description_en': (
            'Sabatier activity versus an adsorption descriptor; when two fitted '
            'legs are provided, their intersection defines the volcano peak.'),
        'required_data_en': (
            'points=[{name, x=descriptor, y=activity}]; optional legs contains two '
            '[{slope, intercept}] records used to determine the peak.'),
    },
    'energy_matrix_table': {
        'name_en': 'Three-line energy matrix table',
        'description_en': (
            'Publication-style adsorbate-by-substrate energy table in PNG, PDF, and '
            'CSV, with ΔE and optional ΔG columns.'),
        'required_data_en': (
            'adsorbates + substrates (one ΔE column per substrate); optional dg maps '
            'each substrate to one ΔG column.'),
    },
    'free_energy_ladder': {
        'name_en': 'Single-path ΔG free-energy ladder',
        'description_en': (
            'Single-system free-energy ladder with solid plateaus, dashed '
            'connections, and optional potential-determining-step annotation.'),
        'required_data_en': (
            'paths={name, G:[cumulative ΔG for each step]}; step_labels and '
            'pds_index are optional.'),
    },
    'free_energy_ladder_multi': {
        'name_en': 'Multi-potential or multi-system ΔG ladder',
        'description_en': (
            'Overlay of free-energy ladders for several systems or potentials, such '
            'as U=0, U_eq, and U_L or a catalyst comparison.'),
        'required_data_en': (
            'paths=[{name, G:[...]}, ...] for different potentials or catalysts; '
            'pds_index follows the per-electron convention.'),
    },
    'neb_profile': {
        'name_en': 'NEB minimum-energy path profile',
        'description_en': (
            'CI-NEB relative energy along the reaction coordinate with the '
            'transition-state point and forward/reverse barriers annotated.'),
        'required_data_en': (
            'The parse_neb_energies result, including rel energies, ts_index, '
            'barrier_f, and barrier_r.'),
    },
    'pdos': {
        'name_en': 'Spin-mirrored PDOS',
        'description_en': (
            'Overlay of projected densities of states with spin-down mirrored below '
            'zero, the Fermi level marked, and optional d-band centers.'),
        'required_data_en': (
            'series=[{label, energies, dos_up, dos_down?}]; efermi and band_centers '
            'are optional.'),
    },
    'cohp': {
        'name_en': 'COHP bond-strength plot',
        'description_en': (
            'LOBSTER −pCOHP curves with bonding plotted positive, a Fermi zero line, '
            'and ICOHP bond-strength annotations.'),
        'required_data_en': (
            'The parse_cohpcar result: energies plus pairs containing cohp_up, '
            'optional cohp_down, and icohp.'),
    },
    'charge_profile': {
        'name_en': 'Planar-averaged difference charge Δρ̄(z)',
        'description_en': (
            'Planar-averaged difference-charge profile with accumulation (>0) and '
            'depletion (<0) filled separately and optional region shading.'),
        'required_data_en': (
            'z and rho planar-average series from chgdiff.plane_averaged; regions is '
            'optional and defines shaded intervals.'),
    },
    'convergence_curve': {
        'name_en': 'Convergence-test curve',
        'description_en': (
            'Server-frozen cutoff-energy, k-point, vacuum, or slab-thickness '
            'points with an explicit threshold band and recommended point.'),
        'required_data_en': (
            'points=[{x, energy}] plus explicit threshold_mev, natoms, and '
            'converged_at values bound to real scan history.'),
    },
    'aimd_diagnostic': {
        'name_en': 'AIMD energy-temperature diagnostic',
        'description_en': (
            'Server-frozen total-energy and temperature timeline for trajectory '
            'diagnostics only; it is not a long-time thermal-stability claim.'),
        'required_data_en': (
            'steps=[{time, energy, temperature}] with optional dt_fs, produced by '
            'the authoritative AIMD analysis view.'),
    },
}

for _preset in PRESETS:
    _preset.update(_PRESET_EN[_preset['key']])
    _preset['category_en'] = CATEGORY_EN[_preset['category']]


# ── 公开 API ─────────────────────────────────────────────────────────────────

def list_presets(category: str | None = None) -> list:
    """轻量清单(**不含** thumbnail_svg,含 params_schema 参数开关);category 给定则过滤。

    返回深拷贝,调用方随意改动不影响注册表本体。category 不存在 → 空列表。
    """
    out = []
    for p in PRESETS:
        if category is not None and p['category'] != category:
            continue
        item = copy.deepcopy(p)
        item.pop('thumbnail_svg', None)
        out.append(item)
    return out


def get_preset(key: str) -> dict:
    """按 key 取**完整版**预设(含 thumbnail_svg;深拷贝)。未知 key → KeyError(中文)。"""
    for p in PRESETS:
        if p['key'] == key:
            return copy.deepcopy(p)
    raise KeyError(f'未知图表预设 key:{key!r}(可用:{[p["key"] for p in PRESETS]})')


def categories() -> list:
    """注册表出现过的分类(保出现序):能量学/电池/电子结构/结构。"""
    seen: list = []
    for p in PRESETS:
        if p['category'] not in seen:
            seen.append(p['category'])
    return seen


def render_preset(key: str, data, out_path, *, renderer_fn=None, **params) -> list:
    """按预设出图:校验数据契约 → 分发到 native_charts/neb/lobster 对应函数 → 返回导出路径列表。

    - renderer='todo' → NotImplementedError(中文,明说后续版本提供),绝不静默;
    - 数据契约不满足 → ValueError(中文,点名缺什么);
    - params 覆盖 params_schema 默认值;renderer_fn 可注入替身(测试/自定义渲染)。
    """
    preset = get_preset(key)
    renderer = preset['renderer']
    if renderer == 'todo':
        raise NotImplementedError(f'预设「{preset["name"]}」的图型将在后续版本提供(renderer=todo)。')
    if renderer not in _ADAPTERS:
        raise KeyError(f'预设「{key}」的 renderer={renderer!r} 未登记分发器。')
    merged = {**preset.get('params_schema', {}), **params}
    fn = renderer_fn if renderer_fn is not None else _load_renderer(renderer)
    result = _ADAPTERS[renderer](fn, data, out_path, merged)
    return list(result) if result is not None else []


def preset_provenance(key: str, data_source: dict, *, params: dict | None = None) -> dict:
    """图溯源工件:``{'preset','name','category','renderer','params','data_source'}``。

    供 report_gate / figure 工件记录"这张图用哪个预设、什么参数、数据从哪来"。
    params 覆盖 params_schema 默认值;data_source 由调用方给(如 {'project':..., 'source':...})。
    """
    preset = get_preset(key)
    merged = {**preset.get('params_schema', {}), **(params or {})}
    return {
        'preset': key,
        'name': preset['name'],
        'category': preset['category'],
        'renderer': preset['renderer'],
        'params': merged,
        'data_source': dict(data_source) if isinstance(data_source, dict) else data_source,
    }
