"""研究场景系统(workspace profiles):声明式配置包,一键把界面裁剪到某研究方向。

设计动机(见项目调研《极致易用交互设计》《v3.1 设计宪法》):
VASP Catalyst Studio 覆盖锂硫电池、电催化、热催化、体相/电解液、分子化学多个方向,
但任一研究生日常只用其中一小片——全功能界面对新手是噪声。研究场景把"显示哪些页面 /
卡片 / 图型 / 反应预设 / 方法学告警 / 默认精度 / 可见引擎 / 给 LLM 的领域上下文"打成一个
声明式数据包(dict),GUI/CLI/AI 各层按场景过滤自己该显示什么。本模块只提供纯引擎 + 数据,
不碰任何界面代码;场景如何被消费由集成层接线。

场景 schema(纯 dict 约定,不引入 class):
    {
      'key':                 str,   # 唯一标识(英文小写)
      'name':                str,   # 中文显示名
      'description':          str,   # 一句话说明
      'pages':               [str], # 显示哪些 data-page,顺序即导航序(PAGES 子集)
      'cards':               dict,  # 页内卡片显隐细粒度覆盖表(嵌套 bool;缺省即可见)
      'figure_preset_order': [str], # 图型 key 排序(即可见图型白名单 + 顺序,FIGURE_KEYS 子集)
      'reaction_presets':    [str], # 反应预设默认下拉顺序(REACTION_PRESETS 子集)
      'advisor_profile':     'all' | [str],  # 启用的方法学告警规则名(ADVISOR_RULES 子集)
      'defaults':            dict,  # 默认值(如 calc_type / precision 精度档)
      'engines':             [str], # 可见计算引擎(ENGINES 子集)
      'ai_context':           str,  # 给 LLM 的领域上下文一句话
    }

设计取舍(每个场景取舍按"该方向研究生每天用什么"设定,见各 BUILTIN 注释)。
中文注释允许,英文标识符。
"""
from __future__ import annotations

import copy
import warnings
from pathlib import Path

import yaml

# ── 合法取值域(自包含,不 import project/*;由 tests 断言与 advisor/reactions 真实同步) ──
# 导航页(与 gui_web/assets/index.html 的 data-page 清单一致;顺序为默认导航序)
PAGES = ('dashboard', 'generate', 'project', 'jobs', 'cluster', 'settings')

# 图型 key(对齐 external/native_charts.py 出图函数:bar=adsorption_bar、table=energy_matrix_table、
# ladder=free_energy_ladder、heatmap=heatmap_matrix、scaling=scaling_relation、volcano=volcano_plot、
# pdos=pdos_plot、charge_profile=charge_profile_plot;亦对齐 index.html fig-* 复选框)
FIGURE_KEYS = ('bar', 'table', 'ladder', 'heatmap', 'scaling', 'volcano', 'pdos', 'charge_profile')

# 反应预设名(必须与 project/reactions.py 的 list_presets() 键同步;test_scenarios 校验)
REACTION_PRESETS = (
    'LIS_16E', 'LIS_ASSOC_LIS', 'LIS_ASSOC_LIS2', 'LIS_DISSOC',
    'ORR_4E', 'OER_4E', 'HER', 'CO2RR_TO_CO',
)

# 方法学告警规则名(必须与 project/advisor.py 的 advise() 里 out.append 的规则名同步;test 校验)
ADVISOR_RULES = frozenset({
    'ENCUT_UNIFY', 'GAS_REF_SMEARING', 'GAS_REF_OPEN_SHELL_SPIN', 'GAS_BOX_TOO_SMALL',
    'LDIPOL_WITHOUT_IDIPOL', 'NSW_ZERO', 'ADS_SLAB_NO_DIPOLE', 'TETRAHEDRON_RELAX',
    'VACUUM_TOO_THIN', 'NO_EDIFFG', 'NO_DISPERSION', 'LDIPOL_WITHOUT_DIPOL', 'O2_NUPDOWN',
})

# 可见计算引擎:vasp 为核心引擎;gaussian 供分子化学场景(前瞻,分子单点/反应能)
ENGINES = ('vasp', 'gaussian')

# 默认计算类型(对齐 index.html gen-calc 下拉:slab/bulk/molecule)
CALC_TYPES = ('slab', 'bulk', 'molecule')

# 场景 schema 必备键
_REQUIRED_KEYS = (
    'key', 'name', 'description', 'pages', 'cards', 'figure_preset_order',
    'reaction_presets', 'advisor_profile', 'defaults', 'engines', 'ai_context',
)

DEFAULT_KEY = 'full'          # 兜底默认场景 key


# ── 方法学规则"全集哨兵" ──────────────────────────────────────────────────────
class _AllRules(frozenset):
    """哨兵:表示"启用全部方法学告警规则"。

    __contains__ 恒真——调用方统一用 `name in profile` 过滤即可,advisor 将来新增规则也自动纳入,
    无需回改任何场景;迭代/取长时给出当前已知规则名集合(ADVISOR_RULES)。
    """
    __slots__ = ()

    def __contains__(self, item) -> bool:      # noqa: D401 恒真
        return True

    def __repr__(self) -> str:
        return 'scenarios.ALL_RULES'


ALL_RULES = _AllRules(ADVISOR_RULES)


# ─────────────────────────────────────────────────────────────────────────────
# 内置官方场景(BUILTIN):5 个方向 + full 兜底
# ─────────────────────────────────────────────────────────────────────────────

# full:不预设方向,开放一切——新手与跨方向研究的兜底默认。
_FULL = {
    'key': 'full',
    'name': '通用 / 全功能',
    'description': '不预设研究方向,开放全部页面、图型与方法学告警——新用户与跨方向研究的兜底默认。',
    'pages': list(PAGES),
    'cards': {},                                   # 无任何隐藏
    'figure_preset_order': list(FIGURE_KEYS),      # 全部图型,默认顺序
    'reaction_presets': list(REACTION_PRESETS),    # 全部反应预设
    'advisor_profile': 'all',
    'defaults': {'calc_type': 'slab', 'precision': 'standard'},
    'engines': ['vasp'],
    'ai_context': '通用 VASP 催化 / 材料计算,无特定领域先验。',
}

# lis:锂硫电池正极催化全流程——本软件的起家方向,全功能 + Li–S 反应组置顶、台阶图前置。
_LIS = {
    'key': 'lis',
    'name': '锂硫电池(Li–S)',
    'description': (
        '锂硫正极催化全流程:清洁表面 + 多硫化物构型族 + 气相参考,'
        'Li–S 放电台阶图与 SAC 批量筛选一应俱全。'),
    'pages': list(PAGES),
    'cards': {},                                   # Li–S 用到全部功能,不隐藏
    # 台阶图(ladder)是 Li–S 的核心产出,火山图次之,故置前
    'figure_preset_order': ['ladder', 'volcano', 'bar', 'table',
                            'heatmap', 'scaling', 'pdos', 'charge_profile'],
    # Li–S 反应组置顶,其余电化学预设垫后
    'reaction_presets': ['LIS_16E', 'LIS_ASSOC_LIS', 'LIS_ASSOC_LIS2', 'LIS_DISSOC',
                         'ORR_4E', 'OER_4E', 'HER', 'CO2RR_TO_CO'],
    'advisor_profile': 'all',
    'defaults': {'calc_type': 'slab', 'precision': 'standard'},
    'engines': ['vasp'],
    'ai_context': (
        '锂硫电池正极单原子催化剂对多硫化物(LiPS)的吸附与转化,参比电对 Li/Li⁺,'
        '关注穿梭效应抑制与放电台阶自由能。'),
}

# electrocat:水系电催化——火山图前置、CHE 反应组(ORR/HER/OER/CO₂RR)置顶,不含 Li–S。
_ELECTROCAT = {
    'key': 'electrocat',
    'name': '电催化(ORR / HER / OER / CO₂RR)',
    'description': (
        '水系电催化:CHE 计算氢电极台阶图与 Sabatier 火山图前置,'
        'ORR / HER / OER / CO₂RR 反应组默认置顶。'),
    'pages': list(PAGES),
    'cards': {},
    # 火山图(Sabatier 峰顶)是电催化筛选的招牌图,置最前;标度关系次之
    'figure_preset_order': ['volcano', 'ladder', 'scaling', 'heatmap',
                            'bar', 'table', 'pdos', 'charge_profile'],
    'reaction_presets': ['ORR_4E', 'HER', 'OER_4E', 'CO2RR_TO_CO'],   # 电化学四组,排除 Li–S
    'advisor_profile': 'all',
    'defaults': {'calc_type': 'slab', 'precision': 'standard'},
    'engines': ['vasp'],
    'ai_context': (
        '水相电催化(ORR / HER / OER / CO₂RR),RHE 氢标 + 计算氢电极(CHE),'
        '关注极限电位 U_L、过电位 η 与标度关系。'),
}

# thermocat:气固热催化表面反应——NEB/频率前置,电位类图后置,隐藏电化学反应下拉。
_THERMOCAT = {
    'key': 'thermocat',
    'name': '热催化(表面反应)',
    'description': (
        '气固热催化表面反应:NEB 过渡态与频率(ZPE / 热校正)前置,'
        '电位类图型(火山图 / 台阶图)后置。'),
    'pages': list(PAGES),
    # 热催化非电化学:隐藏项目页的反应(台阶图)预设卡片
    'cards': {'project': {'reactions': False}},
    # 能量学/热图/标度前置,电位类(台阶图、火山图)后置
    'figure_preset_order': ['bar', 'table', 'heatmap', 'scaling',
                            'pdos', 'charge_profile', 'ladder', 'volcano'],
    'reaction_presets': [],                        # 无外加电位,不默认任何电化学反应组
    'advisor_profile': 'all',                      # 仍是 slab 吸附,全部方法学告警适用
    'defaults': {'calc_type': 'slab', 'precision': 'standard'},
    'engines': ['vasp'],
    'ai_context': (
        '气固多相热催化表面基元反应,关注吸附能、过渡态(NEB)与反应能垒,'
        '频率计算给 ZPE 与吉布斯热校正,无外加电位。'),
}

# battery_bulk:电极体相/电解液——bulk 为主,吸附/表面卡片弱化,方法学按体相口径裁剪。
_BATTERY_BULK = {
    'key': 'battery_bulk',
    'name': '电池体相 / 电解液',
    'description': (
        '电极体相与电解液:以 bulk 与分子为主,吸附 / 表面卡片弱化,'
        '方法学告警按体相口径裁剪(无真空 / 偶极项)。'),
    'pages': list(PAGES),
    # 吸附卡片弱化:隐藏 SAC 表面批量建模;隐藏火山图/标度(吸附质筛选专用)
    'cards': {
        'generate': {'sac_matrix': False},
        'project': {'figures': {'volcano': False, 'scaling': False}},
    },
    # 体相/电解液:态密度(pdos)、电荷密度剖面(charge_profile)、台阶图靠前;去掉火山图/标度
    'figure_preset_order': ['pdos', 'charge_profile', 'ladder', 'bar', 'table', 'heatmap'],
    'reaction_presets': ['LIS_16E', 'LIS_ASSOC_LIS', 'LIS_ASSOC_LIS2', 'LIS_DISSOC'],
    # 体相无真空层/表面偶极:剔除 slab/偶极/四面体弛豫等 slab 专属规则
    'advisor_profile': ['ENCUT_UNIFY', 'NO_EDIFFG', 'NO_DISPERSION',
                        'GAS_REF_SMEARING', 'GAS_REF_OPEN_SHELL_SPIN'],
    'defaults': {'calc_type': 'bulk', 'precision': 'standard'},
    'engines': ['vasp'],
    'ai_context': (
        '锂电池电极体相结构与电解液分子,关注体相能量学、脱嵌电位与电解液分解,'
        '一般不含真空层与表面偶极。'),
}

# molecular:分子化学——Gaussian 引擎可见,隐藏周期性(slab/真空)卡片与吸附能项目页。
_MOLECULAR = {
    'key': 'molecular',
    'name': '分子化学',
    'description': (
        '分子体系(团簇 / 自由基 / 反应能):可见 Gaussian 引擎,'
        '隐藏周期性(slab / 真空)相关卡片,以分子单点与反应能为主。'),
    # 分子化学不走"吸附能项目"(表面 slab 工作流),隐藏该页
    'pages': ['dashboard', 'generate', 'jobs', 'cluster', 'settings'],
    # SAC 批量建模是周期 slab 建模,隐藏;多自旋家族对自由基仍有用,保留
    'cards': {'generate': {'sac_matrix': False}},
    # 分子:反应能台阶图 + 能量柱状;无火山图/热图/标度/态密度
    'figure_preset_order': ['ladder', 'bar', 'table'],
    'reaction_presets': [],                        # 分子化学默认不走 CHE 电化学反应组
    # 气相/分子相关规则,剔除 slab/偶极规则
    'advisor_profile': ['ENCUT_UNIFY', 'GAS_REF_SMEARING', 'GAS_REF_OPEN_SHELL_SPIN',
                        'GAS_BOX_TOO_SMALL', 'NO_DISPERSION', 'NSW_ZERO', 'NO_EDIFFG'],
    'defaults': {'calc_type': 'molecule', 'precision': 'standard'},
    'engines': ['vasp', 'gaussian'],               # Gaussian 引擎可见
    'ai_context': (
        '孤立分子 / 团簇 / 自由基化学,关注构型、反应能与前线轨道;'
        '开壳层体系需 ISPIN=2 与大盒真空,可选 Gaussian 引擎。'),
}

# 注册表(顺序即场景选择器展示序:通用兜底在前,5 个方向在后)
_BUILTIN = {
    s['key']: s for s in (
        _FULL, _LIS, _ELECTROCAT, _THERMOCAT, _BATTERY_BULK, _MOLECULAR,
    )
}

# 对外暴露的内置场景 key 元组(展示序)
BUILTIN_KEYS = tuple(_BUILTIN.keys())


# ─────────────────────────────────────────────────────────────────────────────
# 查询 API
# ─────────────────────────────────────────────────────────────────────────────

def list_scenarios() -> list:
    """返回全部内置场景(深拷贝列表,展示序);外部改动不影响注册表。"""
    return [copy.deepcopy(s) for s in _BUILTIN.values()]


def get_scenario(key: str | None) -> dict:
    """按 key 取场景(深拷贝)。未知 key → 回退 full 并发 UserWarning。"""
    if key in _BUILTIN:
        return copy.deepcopy(_BUILTIN[key])
    warnings.warn(
        f'未知研究场景 {key!r},已回退到默认场景 {DEFAULT_KEY!r};'
        f'可选:{", ".join(BUILTIN_KEYS)}',
        stacklevel=2,
    )
    return copy.deepcopy(_BUILTIN[DEFAULT_KEY])


def active_scenario(config: dict | None = None) -> dict:
    """读 config 的 ui.scenario 决定当前场景(缺省 full)。config 缺省则不读盘,直接 full。"""
    key = DEFAULT_KEY
    if isinstance(config, dict):
        ui = config.get('ui')
        if isinstance(ui, dict) and ui.get('scenario'):
            key = ui['scenario']
    return get_scenario(key)


def set_scenario(key: str, config_path=None) -> Path:
    """把当前场景 key 写入 config 的 ui.scenario 并持久化。返回写入路径。

    未知 key 也照写(允许导入的社区场景先落库、后由集成层校验),但会发 UserWarning 提示。
    """
    if key not in _BUILTIN:
        warnings.warn(
            f'写入的场景 key {key!r} 不是内置场景;若非社区导入场景请检查拼写。',
            stacklevel=2,
        )
    from vcstudio.shared.config import set_ui_state
    return set_ui_state(config_path, scenario=key)


# ─────────────────────────────────────────────────────────────────────────────
# 显隐判定
# ─────────────────────────────────────────────────────────────────────────────

def is_visible(scenario: dict, item_path: str) -> bool:
    """场景 + 点分路径 → 该元素是否可见。

    支持的路径族:
      - 'pages.<page>'          页面是否在导航序中(白名单)
      - 'engines.<engine>'      引擎是否可见(白名单)
      - 'reactions.<preset>'    反应预设是否在默认下拉中(白名单)
      - 'figures.<key>'         图型是否可见(即是否在 figure_preset_order 白名单中)
      - 'cards.<page>.<...>'    页内卡片细粒度覆盖:命中 cards 覆盖表的 bool 即用其值;
                                未覆盖 → 默认可见(True)。场景只需声明"要隐藏什么"。

    未知路径族 → 保守返回 True(fail-open,绝不因判定失误而误藏用户功能)。
    """
    if not item_path:
        return True
    parts = item_path.split('.')
    head = parts[0]
    rest = parts[1:]

    if head == 'pages':
        return bool(rest) and rest[0] in (scenario.get('pages') or [])
    if head == 'engines':
        return bool(rest) and rest[0] in (scenario.get('engines') or [])
    if head == 'reactions':
        return bool(rest) and rest[0] in (scenario.get('reaction_presets') or [])
    if head == 'figures':
        return bool(rest) and rest[0] in (scenario.get('figure_preset_order') or [])
    if head == 'cards':
        node = scenario.get('cards') or {}
        for k in rest:
            if not isinstance(node, dict) or k not in node:
                return True                        # 未覆盖 → 默认可见
            node = node[k]
        # 落到 bool 叶子用其值;落到 dict(卡片组存在)→ 组可见
        return bool(node) if isinstance(node, bool) else True
    return True


def apply_to_advisor(scenario: dict) -> frozenset:
    """场景 → 启用的 advisor 规则名集合(调用方 `name in 集合` 过滤)。

    advisor_profile == 'all' → 返回 ALL_RULES 哨兵(__contains__ 恒真);否则返回其规则名 frozenset。
    """
    prof = scenario.get('advisor_profile')
    if prof == 'all':
        return ALL_RULES
    if isinstance(prof, (list, tuple, set, frozenset)):
        return frozenset(prof)
    # 缺省/异常口径:保守启用全部,避免静默漏报方法学坑
    return ALL_RULES


# ─────────────────────────────────────────────────────────────────────────────
# 校验 / 导入 / 导出(社区分享单元)
# ─────────────────────────────────────────────────────────────────────────────

def _validate_cards(cards, issues: list) -> None:
    """卡片覆盖表:顶层键须为合法 page;叶子须为 bool;figures 子键须为合法图型。"""
    if not isinstance(cards, dict):
        issues.append(f'cards 必须是 dict,当前为 {type(cards).__name__}')
        return
    for page, group in cards.items():
        if page not in PAGES:
            issues.append(f'cards 顶层键 {page!r} 不是合法页面(可选:{", ".join(PAGES)})')
        if isinstance(group, bool):
            continue
        if not isinstance(group, dict):
            issues.append(f'cards.{page} 必须是 dict 或 bool,当前为 {type(group).__name__}')
            continue
        for name, val in group.items():
            if name == 'figures' and isinstance(val, dict):
                for fk, fv in val.items():
                    if fk not in FIGURE_KEYS:
                        issues.append(f'cards.{page}.figures.{fk} 不是合法图型 key')
                    if not isinstance(fv, bool):
                        issues.append(f'cards.{page}.figures.{fk} 值须为 bool')
            elif not isinstance(val, (bool, dict)):
                issues.append(f'cards.{page}.{name} 值须为 bool 或 dict')


def validate_scenario(scenario) -> list:
    """校验场景 schema,返回中文 issue 清单(空 = 合法)。不抛异常,供导入与测试复用。"""
    issues: list = []
    if not isinstance(scenario, dict):
        return [f'场景必须是 dict,当前为 {type(scenario).__name__}']

    for k in _REQUIRED_KEYS:
        if k not in scenario:
            issues.append(f'缺少必备键 {k!r}')

    for page in scenario.get('pages') or []:
        if page not in PAGES:
            issues.append(f'未知页面 {page!r}(可选:{", ".join(PAGES)})')
    if 'pages' in scenario and not (scenario.get('pages')):
        issues.append('pages 不能为空(至少要有一个页面)')

    for fk in scenario.get('figure_preset_order') or []:
        if fk not in FIGURE_KEYS:
            issues.append(f'未知图型 key {fk!r}(可选:{", ".join(FIGURE_KEYS)})')

    for rp in scenario.get('reaction_presets') or []:
        if rp not in REACTION_PRESETS:
            issues.append(f'未知反应预设 {rp!r}(可选:{", ".join(REACTION_PRESETS)})')

    for eng in scenario.get('engines') or []:
        if eng not in ENGINES:
            issues.append(f'未知引擎 {eng!r}(可选:{", ".join(ENGINES)})')

    prof = scenario.get('advisor_profile')
    if prof != 'all':
        if isinstance(prof, (list, tuple)):
            for r in prof:
                if r not in ADVISOR_RULES:
                    issues.append(f'未知方法学规则名 {r!r}')
        elif 'advisor_profile' in scenario:
            issues.append("advisor_profile 须为 'all' 或规则名列表")

    if 'cards' in scenario:
        _validate_cards(scenario.get('cards'), issues)

    defaults = scenario.get('defaults')
    if defaults is not None:
        if not isinstance(defaults, dict):
            issues.append(f'defaults 必须是 dict,当前为 {type(defaults).__name__}')
        else:
            ct = defaults.get('calc_type')
            if ct is not None and ct not in CALC_TYPES:
                issues.append(f'defaults.calc_type {ct!r} 非法(可选:{", ".join(CALC_TYPES)})')

    return issues


def export_scenario(scenario: dict, path) -> Path:
    """把场景导出为 YAML 文件(UTF-8,社区分享单元)。返回写入路径。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, 'w', encoding='utf-8') as f:
        yaml.safe_dump(scenario, f, allow_unicode=True, sort_keys=False)
    return target


def import_scenario(path) -> dict:
    """从 YAML 文件导入场景 → {'ok': bool, 'scenario': dict|None, 'issues': [中文...]}。

    读盘/解析失败或非 dict → ok=False、scenario=None、issues 说明原因;
    结构可读但 schema 有问题 → ok=False、scenario=原样返回(供调用方检视)、issues 列明。
    """
    p = Path(path)
    try:
        with open(p, 'r', encoding='utf-8') as f:
            loaded = yaml.safe_load(f)
    except FileNotFoundError:
        return {'ok': False, 'scenario': None, 'issues': [f'文件不存在:{p}']}
    except yaml.YAMLError as e:
        return {'ok': False, 'scenario': None, 'issues': [f'YAML 解析失败:{e}']}

    if loaded is None:
        return {'ok': False, 'scenario': None, 'issues': ['文件为空']}
    if not isinstance(loaded, dict):
        return {'ok': False, 'scenario': None,
                'issues': [f'场景文件顶层必须是映射(dict),当前为 {type(loaded).__name__}']}

    issues = validate_scenario(loaded)
    return {'ok': not issues, 'scenario': loaded, 'issues': issues}
