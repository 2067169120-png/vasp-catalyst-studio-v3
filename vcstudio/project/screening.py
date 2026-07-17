"""候选筛选引擎:跨项目汇总描述符 → 标度关系拟合 → 火山图自动构造。

定位(2026-07):吸附能/ΔG/d 带中心/ICOHP/U_L 这些描述符散落在各个项目里,判断
"哪个催化剂好、哪个描述符最好使"需要把它们收进一张表,再做标度关系与火山图。
本模块是筛选层:**注入式取数**(不自己猜数据在哪、缺就记 missing),纯函数拟合与
火山构造(零重依赖,离线可测),输出结构直接可喂 native_charts 的 volcano_plot /
scaling_relation 出图。

口径约定:
- ΔE 只收 delta_e_rows 的 is_most_stable 行(每物种最稳构型,单一真相源)。
- d 带中心/ICOHP/U_L 由调用方经 dos_fn/icohp_fn/ul_fn 提供(拿不到就 None + 记 missing),
  本模块**绝不编数**。
- 火山图:按描述符排序,活性最大点分左右两支各做最小二乘(每支 n≥3),两支线交点=峰顶;
  R²<0.6 判"标度关系弱"、全单调判"未见火山形"、点数不足直接报错——都显式暴露,不假装出峰。

中文注释允许,英文标识符。
"""
from __future__ import annotations

import csv
import math
import os


# ── 描述符汇总(跨项目,注入式取数)────────────────────────────────────────

def _default_adsorption():
    """默认取数模块(可被 collect_descriptors 的 adsorption_mod 注入替换,便于测试)。"""
    from vcstudio.project import adsorption
    return adsorption


def _call_provider(fn, ctx):
    """调用可选描述符提供者。fn 为 None → None;fn 抛异常 → None(提供者绝不拖垮汇总)。"""
    if fn is None:
        return None
    try:
        return fn(ctx)
    except Exception:            # noqa: BLE001 提供者失败降级为"缺该描述符",不中断全表
        return None


def collect_descriptors(project_paths, *, adsorption_mod=None, dos_fn=None,
                        icohp_fn=None, ul_fn=None, dg_fn=None) -> dict:
    """跨项目汇总描述符表(注入式取数,缺失记 missing 不猜)。

    参数:
        project_paths: 项目 project.yaml 路径(或项目根目录)列表。
        adsorption_mod: 取数模块(默认 vcstudio.project.adsorption);须有 load_project /
                        delta_e_rows。测试可注入替身。
        dos_fn/icohp_fn/ul_fn/dg_fn: 可选描述符提供者,签名 ``fn(ctx) -> value|None``;
            ctx = {'catalyst','project','project_path','summary','de'}。分别供
            d 带中心 / ICOHP(键强)/ U_L(极限电位)/ ΔG 字典。缺(None 或抛异常)→
            该描述符置 None 并记 missing。

    返回::

        {'rows': [{'catalyst','de':{物种:ΔE},'dg':{...},'d_band_center',
                   'icohp_ms','u_l','extras'}, ...],
         'missing': [{'catalyst','field','reason'?}, ...]}

    - de 只含 is_most_stable 且 delta_e 非 None 的行(物种→ΔE)。
    - 项目加载失败 / 无最稳 ΔE 行 / 描述符提供者缺 → 各记一条 missing。
    """
    ads = adsorption_mod or _default_adsorption()
    rows, missing = [], []
    for path in project_paths:
        project = ads.load_project(path)
        if project is None:
            missing.append({'catalyst': None, 'project_path': str(path),
                            'field': 'project',
                            'reason': '项目无法加载(路径无效或 project.yaml 畸形)'})
            continue
        name = str(project.get('name') or os.path.basename(str(path)) or '未命名')
        summary = ads.delta_e_rows(project)
        de = {r['species']: r['delta_e'] for r in summary.get('rows', [])
              if r.get('is_most_stable') and r.get('delta_e') is not None}
        if not de:
            missing.append({'catalyst': name, 'field': 'de',
                            'reason': '无 is_most_stable 的 ΔE 行(构型/参考未完成?)'})
        ctx = {'catalyst': name, 'project': project, 'project_path': str(path),
               'summary': summary, 'de': de}
        dbc = _call_provider(dos_fn, ctx)
        icohp_ms = _call_provider(icohp_fn, ctx)
        u_l = _call_provider(ul_fn, ctx)
        dg = _call_provider(dg_fn, ctx)
        for field, val in (('d_band_center', dbc), ('icohp_ms', icohp_ms),
                           ('u_l', u_l)):
            if val is None:
                missing.append({'catalyst': name, 'field': field,
                                'reason': '未提供对应描述符或提供者返回空'})
        rows.append({'catalyst': name, 'de': de,
                     'dg': dict(dg) if isinstance(dg, dict) else {},
                     'd_band_center': dbc, 'icohp_ms': icohp_ms, 'u_l': u_l,
                     'extras': {}})
    return {'rows': rows, 'missing': missing}


def _fmt(v) -> str:
    """CSV 单元格数值格式;None/非数 → 空(不编数)。"""
    return f'{v:.4f}' if isinstance(v, (int, float)) and not isinstance(v, bool) else ''


def screening_table_csv(data: dict, out_path) -> str:
    """发刊级筛选表 CSV:催化剂×描述符矩阵 + 数据来源列(utf-8-sig,Excel 友好)。

    data 为 collect_descriptors 的返回。物种列取全体催化剂 de 键的并集(排序)。
    每行末列 '数据来源' 记该催化剂各描述符的口径,缺失描述符留空(不编数)。
    返回写入路径。
    """
    rows = data.get('rows') or []
    species = sorted({sp for r in rows for sp in (r.get('de') or {})})
    out_path = str(out_path)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or '.', exist_ok=True)
    header = (['催化剂'] + [f'ΔE({sp})/eV' for sp in species] +
              ['d带中心/eV', 'ICOHP_ms/eV', 'U_L/V', '数据来源'])
    with open(out_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            de = r.get('de') or {}
            sources = ['ΔE:吸附能最稳构型(delta_e_rows)']
            if r.get('d_band_center') is not None:
                sources.append('d带:投影DOS一阶矩')
            if r.get('icohp_ms') is not None:
                sources.append('ICOHP:LOBSTER')
            if r.get('u_l') is not None:
                sources.append('U_L:CHE 自由能路径')
            w.writerow(
                [r.get('catalyst', '')] +
                [_fmt(de.get(sp)) for sp in species] +
                [_fmt(r.get('d_band_center')), _fmt(r.get('icohp_ms')),
                 _fmt(r.get('u_l')), '；'.join(sources)])
    return out_path


# ── 最小二乘 / 皮尔逊相关(纯 python,零依赖)──────────────────────────────

def fit_scaling(xs, ys) -> dict:
    """一元最小二乘线性拟合 → {'slope','intercept','r2','n'}。

    n<3 → ValueError(中文;标度关系至少需 3 点才有统计意义);
    x 全相同(无法定斜率)→ ValueError。
    """
    xs = [float(x) for x in xs]
    ys = [float(y) for y in ys]
    if len(xs) != len(ys):
        raise ValueError(f'xs 与 ys 长度不一致:{len(xs)} != {len(ys)}')
    n = len(xs)
    if n < 3:
        raise ValueError(f'标度关系拟合至少需 3 个数据点(当前 {n} 个);点太少拟合无统计意义')
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0.0:
        raise ValueError('xs 全部相同,无法确定斜率(描述符无区分度)')
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    if ss_tot <= 0.0:
        r2 = 1.0 if ss_res < 1e-12 else 0.0
    else:
        r2 = 1.0 - ss_res / ss_tot
    return {'slope': slope, 'intercept': intercept, 'r2': r2, 'n': n}


def _pearson(xs, ys):
    """皮尔逊相关系数;n<3 或任一方差为 0 → None(相关性无意义,不编数)。"""
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0.0 or syy <= 0.0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


# ── 火山图自动构造 ───────────────────────────────────────────────────────────

def _descriptor_value(row: dict, key: str):
    """取行内描述符值。key 以 'de:' 开头 → 取 de[物种];否则取顶层字段。取不到 → None。"""
    if key.startswith('de:'):
        return (row.get('de') or {}).get(key[3:])
    return row.get(key)


def _extract_points(rows, descriptor_key, activity_key):
    """rows → [{'name','x','y'}](两值齐备者),按 x 升序。"""
    pts = []
    for r in rows:
        x = _descriptor_value(r, descriptor_key)
        y = _descriptor_value(r, activity_key)
        if isinstance(x, (int, float)) and not isinstance(x, bool) and \
           isinstance(y, (int, float)) and not isinstance(y, bool):
            pts.append({'name': str(r.get('catalyst', '')), 'x': float(x),
                        'y': float(y)})
    pts.sort(key=lambda p: p['x'])
    return pts


def _split_index(points, split):
    """确定左右分割:'auto'→活性最大点;数值→x 阈值(左 x≤阈,右 x≥阈);名字→该催化剂。

    返回 (left_pts, right_pts)。含分割点者两支共享(峰顶落两支交点)。
    """
    if split == 'auto':
        k = max(range(len(points)), key=lambda i: points[i]['y'])
        return points[:k + 1], points[k:]
    # 显式催化剂名
    if isinstance(split, str):
        names = [p['name'] for p in points]
        if split in names:
            k = names.index(split)
            return points[:k + 1], points[k:]
        raise ValueError(f"split={split!r} 不是 'auto'、数值阈值或有效催化剂名")
    # 显式数值 x 阈值
    x0 = float(split)
    left = [p for p in points if p['x'] <= x0]
    right = [p for p in points if p['x'] >= x0]
    return left, right


def volcano_construct(rows, *, descriptor_key, activity_key='u_l',
                      split='auto') -> dict:
    """火山图自动构造:描述符排序 → 活性峰分左右两支各拟合 → 交点=峰顶 + 质量闸。

    参数:
        rows: collect_descriptors 的 rows(或等价 [{'catalyst','de','u_l',...}])。
        descriptor_key: 横轴描述符键;'de:Li2S2' 取 de 里物种,否则顶层字段
                        (如 'd_band_center'/'icohp_ms')。
        activity_key: 纵轴活性键(默认 'u_l')。
        split: 'auto'(活性最大点分割)/ 数值(x 阈值)/ 催化剂名(显式分割点)。

    返回::

        {'legs': [{'slope','intercept','r2','side'}, ...],
         'apex': {'x','y'}|None,
         'points': [{'name','x','y'}, ...],
         'quality': {'ok': bool, 'issues': [...]}}

    points + legs 可直接喂 native_charts.volcano_plot。质量闸:
      - 任一支 R²<0.6 → issue「标度关系弱,火山图解释力有限」;
      - 无法分出两支各 ≥3 点(峰在端点/某支太短)→ 单支回退 + issue「未见火山形,
        活性单调依赖描述符」;
      - 两支平行/交点非峰(斜率未构成 ∧)→ issue;
      - 有效点 <3 → ValueError(点数不足,明确报错)。
    """
    points = _extract_points(rows, descriptor_key, activity_key)
    n = len(points)
    if n < 3:
        raise ValueError(
            f'火山图构造至少需 3 个同时有描述符({descriptor_key})与活性({activity_key})'
            f'的数据点,当前仅 {n} 个;请补算缺失描述符/活性')

    left, right = _split_index(points, split)
    issues: list[str] = []

    # 两支任一不足 3 点 → 退化为单支拟合(判"未见火山形/单调")
    if len(left) < 3 or len(right) < 3:
        issues.append('未见火山形:活性对描述符近单调依赖(峰值点在端点或某支点数<3),'
                      '无法两支拟合,已回退单支标度关系')
        single = fit_scaling([p['x'] for p in points], [p['y'] for p in points])
        if single['r2'] < 0.6:
            issues.append(f"标度关系弱(R²={single['r2']:.2f}<0.6),该描述符解释力有限")
        return {'legs': [{'slope': single['slope'], 'intercept': single['intercept'],
                          'r2': single['r2'], 'side': 'single'}],
                'apex': None, 'points': points,
                'quality': {'ok': False, 'issues': issues}}

    lf = fit_scaling([p['x'] for p in left], [p['y'] for p in left])
    rf = fit_scaling([p['x'] for p in right], [p['y'] for p in right])
    legs = [{'slope': lf['slope'], 'intercept': lf['intercept'],
             'r2': lf['r2'], 'side': 'left'},
            {'slope': rf['slope'], 'intercept': rf['intercept'],
             'r2': rf['r2'], 'side': 'right'}]

    # 峰顶 = 两支交点;斜率相等(平行)→ 无交点
    apex = None
    if abs(lf['slope'] - rf['slope']) < 1e-12:
        issues.append('两支斜率相等(平行),无交点,未构成火山峰')
    else:
        ax = (rf['intercept'] - lf['intercept']) / (lf['slope'] - rf['slope'])
        ay = lf['slope'] * ax + lf['intercept']
        apex = {'x': ax, 'y': ay}
        # 峰形校验:左支应上升、右支应下降(左斜率>右斜率才构成 ∧ 峰)
        if lf['slope'] <= rf['slope']:
            issues.append('两支斜率未构成峰形(左支未升/右支未降),交点为谷而非峰,'
                          '火山图解释存疑')

    for leg in legs:
        if leg['r2'] < 0.6:
            issues.append(f"{leg['side']} 支标度关系弱(R²={leg['r2']:.2f}<0.6),"
                          '火山图解释力有限')

    return {'legs': legs, 'apex': apex, 'points': points,
            'quality': {'ok': not issues, 'issues': issues}}


def descriptor_correlation(rows, keys) -> list:
    """描述符两两皮尔逊相关 → [{'pair','r','n'}](供判断"哪个描述符最好使")。

    keys: 描述符键列表(顶层字段或 'de:物种')。对每一对 (i<j),只在两值都齐的行上
    算相关;n<3 或某方差为 0 → r=None(相关性无意义,不编数)。
    """
    keys = list(keys)
    out = []
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            ka, kb = keys[i], keys[j]
            xs, ys = [], []
            for r in rows:
                va = _descriptor_value(r, ka)
                vb = _descriptor_value(r, kb)
                if isinstance(va, (int, float)) and not isinstance(va, bool) and \
                   isinstance(vb, (int, float)) and not isinstance(vb, bool):
                    xs.append(float(va))
                    ys.append(float(vb))
            r_val = _pearson(xs, ys)
            out.append({'pair': (ka, kb),
                        'r': (round(r_val, 4) if r_val is not None else None),
                        'n': len(xs)})
    return out
