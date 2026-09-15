"""campaign.budget 测试:粗估公式、实际累计、剩余与超限判定。"""
from vcstudio.campaign import budget, schema


def test_estimate_positive_and_kind_ordering():
    relax = budget.estimate_job(50, 10, 'relax', 64)
    static = budget.estimate_job(50, 10, 'static', 64)
    assert relax > 0 and static > 0
    assert relax > static                      # 弛豫等效步数远多于单点
    assert budget.estimate_job(50, 10, 'analysis', 64) == 0.0   # 分析节点不耗机时


def test_estimate_scales_with_size():
    small = budget.estimate_job(10, 4, 'static', 32)
    big = budget.estimate_job(80, 4, 'static', 32)
    assert big > small                         # ~O(原子^3) 标度


def test_estimate_freq_uses_displacements():
    # freq = 6·原子 个位移单点,应显著大于同规模单点
    assert budget.estimate_job(20, 8, 'freq', 32) > budget.estimate_job(20, 8, 'static', 32)


def test_estimate_coeff_override():
    base = budget.estimate_job(30, 6, 'relax', 32)
    doubled = budget.estimate_job(30, 6, 'relax', 32, coeff={'base': 4.0e-7})
    assert doubled > base


def test_record_actual_accumulates(tmp_path):
    cdir = str(tmp_path)
    budget.record_actual(cdir, 't1', 30.0)
    budget.record_actual(cdir, 't1', 12.0)     # 续算再累加
    budget.record_actual(cdir, 't2', 8.0)
    assert budget.load_budget(cdir)['actuals']['t1'] == 42.0
    assert budget.consumed(cdir) == 50.0


def test_remaining_and_over_budget(tmp_path):
    camp = schema.init_campaign(str(tmp_path), 'demo',
                                tasks=[schema.new_task('a', 'relax')],
                                budget_core_hours=100.0)
    budget.record_actual(camp['dir'], 'a', 70.0)
    assert budget.remaining(camp) == 30.0
    assert budget.over_budget(camp, 40.0) is True
    assert budget.over_budget(camp, 25.0) is False


def test_remaining_unlimited_is_none(tmp_path):
    camp = schema.init_campaign(str(tmp_path), 'demo',
                                tasks=[schema.new_task('a', 'relax')],
                                budget_core_hours=None)
    assert budget.remaining(camp) is None
    assert budget.over_budget(camp, 1e12) is False


def test_load_budget_default_when_missing(tmp_path):
    data = budget.load_budget(str(tmp_path))
    assert data == {'estimates': {}, 'actuals': {}}


def test_record_estimate(tmp_path):
    budget.record_estimate(str(tmp_path), 't1', 123.0)
    assert budget.load_budget(str(tmp_path))['estimates']['t1'] == 123.0
