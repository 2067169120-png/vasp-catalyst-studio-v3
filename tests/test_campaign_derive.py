"""campaign.derive 测试:纯函数派生就绪/阶段/进度(永不落盘)。"""
from vcstudio.campaign import derive, schema, states


def _campaign_with(rungs):
    """按 {id: rung} 造一个内存 campaign(a←b←c 链式依赖)。"""
    a = schema.new_task('a', 'relax', rung=rungs.get('a', 'pending'))
    b = schema.new_task('b', 'static', depends_on=['a'], rung=rungs.get('b', 'pending'))
    c = schema.new_task('c', 'analysis', depends_on=['b'], rung=rungs.get('c', 'pending'))
    return {'meta': {}, 'tasks': [a, b, c]}


def test_ready_only_root_when_all_pending():
    d = derive.derive_ready(_campaign_with({}))
    assert d['ready'] == ['a']
    blocked_ids = [x['task'] for x in d['blocked']]
    assert blocked_ids == ['b', 'c']
    assert '未就绪' in d['blocked'][0]['why']


def test_dependency_satisfied_at_validated():
    # a 仅 completed 时 b 仍阻塞;a 到 validated 后 b 就绪
    d1 = derive.derive_ready(_campaign_with({'a': 'completed'}))
    assert d1['ready'] == []
    assert 'a' in d1['done']                       # completed 计入 done 桶(已产出)
    assert [x['task'] for x in d1['blocked']] == ['b', 'c']

    d2 = derive.derive_ready(_campaign_with({'a': 'validated'}))
    assert d2['ready'] == ['b']


def test_running_and_done_buckets():
    d = derive.derive_ready(_campaign_with({'a': 'accepted', 'b': 'running'}))
    assert d['running'] == ['b']
    assert 'a' in d['done']


def test_failed_task_is_blocked_with_reason():
    d = derive.derive_ready(_campaign_with({'a': 'failed'}))
    why = next(x['why'] for x in d['blocked'] if x['task'] == 'a')
    assert '执行失败' in why


def test_missing_dependency_reported():
    t = schema.new_task('x', 'static', depends_on=['ghost'])
    d = derive.derive_ready({'tasks': [t]})
    assert d['blocked'][0]['task'] == 'x'
    assert '不存在' in d['blocked'][0]['why']


def test_campaign_stage_transitions():
    assert derive.campaign_stage({'tasks': []}) == '生成'
    assert derive.campaign_stage(_campaign_with({})) == '提交'
    # 有 pending 优先算「提交」;须无 pending 才显现 监控/恢复(对齐 pipeline_status 优先级)
    assert derive.campaign_stage(_campaign_with(
        {'a': 'accepted', 'b': 'running', 'c': 'accepted'})) == '监控'
    assert derive.campaign_stage(_campaign_with(
        {'a': 'accepted', 'b': 'failed', 'c': 'accepted'})) == '恢复'
    assert derive.campaign_stage(_campaign_with(
        {'a': 'accepted', 'b': 'validated', 'c': 'completed'})) == '分析'
    assert derive.campaign_stage(_campaign_with(
        {'a': 'accepted', 'b': 'accepted', 'c': 'accepted'})) == '报告'


def test_progress_summary_counts():
    s = derive.progress_summary(_campaign_with({'a': 'accepted', 'b': 'validated'}))
    assert s['total'] == 3
    assert s['accepted'] == 1 and s['validated'] == 1 and s['pending'] == 1
    assert s['ready'] == 1            # c 的依赖 b 已 validated → c 就绪
    assert s['stage'] == '提交'        # c 仍 pending,整体阶段回落到 提交


def test_derive_accepts_composite_and_list():
    tasks = [schema.new_task('a', 'relax', rung='validated')]
    assert derive.derive_ready(tasks)['done'] == ['a']
    assert derive.derive_ready({'tasks': tasks})['done'] == ['a']


def test_stage_matches_state_machine_end_to_end(tmp_path):
    """经真实三态推进后,stage 从 提交 → 分析 → 报告。"""
    camp = schema.init_campaign(str(tmp_path), 'demo',
                                tasks=[schema.new_task('a', 'relax')])
    assert derive.campaign_stage(camp) == '提交'
    t = camp['tasks'][0]
    states.mark_completed(t)
    states.promote_validated(t, [{'name': 'x', 'ok': True}])
    assert derive.campaign_stage(camp) == '分析'
    states.promote_accepted(t, {'gate': 'accept_gate', 'status': 'pass'})
    assert derive.campaign_stage(camp) == '报告'
