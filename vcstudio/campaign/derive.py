"""派生层(蓝图 E1):就绪 / 阶段 / 进度都是纯函数派生量,**永不落盘**。

进程死了重启 = 重跑这些函数;「续跑 = 重新派生」,零内存状态恢复。同一份代码两处用——
既喂自动驾驶主循环,又直接渲染 GUI 仪表盘的「就绪/阻塞/为什么阻塞」。

- `derive_ready(campaign)`:按依赖满足 + rung 条件分桶 ready/blocked/running/done。
  依赖「满足」= 被依赖任务 rung ∈ {validated, accepted}(不建在未验证结果上)。
- `campaign_stage(campaign)`:整体阶段(生成→提交→监控→恢复→分析→报告),
  兼容现有 pipeline_status 的 generate→submit→monitor→recover→analysis→report 语义。
- `progress_summary(campaign)`:各态计数 + 就绪/阻塞数。

校验器若在任何文件里发现持久化的 ready 字段即应判 FAIL——就绪永远现算。
中文注释允许,英文标识符。
"""
from __future__ import annotations

# 依赖「满足」所需的最低 rung:被依赖任务须至少 validated,才允许下游变 ready
DEP_SATISFIED_RUNGS = ('validated', 'accepted')

# 已产出结果(不再需要提交/运行)的 rung:进 done 桶
_DONE_RUNGS = ('completed', 'validated', 'accepted')

# 整体阶段词汇(与 gui_web 的 generate→submit→monitor→recover→analysis→report_done 对齐)
STAGES = ('生成', '提交', '监控', '恢复', '分析', '报告')


def _task_list(campaign) -> list:
    """兼容:复合结构 {'tasks':[...]} / 直接任务列表。"""
    if isinstance(campaign, dict) and 'tasks' in campaign:
        return list(campaign.get('tasks') or [])
    if isinstance(campaign, list):
        return list(campaign)
    return []


def derive_ready(campaign) -> dict:
    """分桶:{'ready':[id],'blocked':[{'task','why'}],'running':[id],'done':[id]}。

    - running:rung=running。
    - done:rung ∈ {completed, validated, accepted}(已产出结果,不再提交)。
    - failed:进 blocked,附「执行失败」原因(待自愈/人工)。
    - pending:依赖全满足 → ready;否则 → blocked,逐条列出卡在哪个依赖(中文)。
    """
    tasks = _task_list(campaign)
    by_id = {t.get('id'): t for t in tasks if t.get('id')}
    ready: list = []
    blocked: list = []
    running: list = []
    done: list = []

    for t in tasks:
        tid = t.get('id')
        r = t.get('rung', 'pending')
        if r == 'running':
            running.append(tid)
        elif r in _DONE_RUNGS:
            done.append(tid)
        elif r == 'failed':
            blocked.append({'task': tid, 'why': '任务执行失败(rung=failed),待自愈或人工处理'})
        else:  # pending
            reasons: list = []
            for dep in (t.get('depends_on') or []):
                dt = by_id.get(dep)
                if dt is None:
                    reasons.append(f'依赖任务「{dep}」不存在')
                elif dt.get('rung', 'pending') not in DEP_SATISFIED_RUNGS:
                    reasons.append(f'依赖任务「{dep}」未就绪'
                                   f'(当前 rung={dt.get("rung", "pending")},需 validated 及以上)')
            if reasons:
                blocked.append({'task': tid, 'why': '；'.join(reasons)})
            else:
                ready.append(tid)

    return {'ready': ready, 'blocked': blocked, 'running': running, 'done': done}


def campaign_stage(campaign) -> str:
    """整体阶段。优先级对齐现有 pipeline_status:空→生成;有待提交→提交;有在跑→监控;
    有失败→恢复;全 accepted→报告;全部已产出但未全 accepted→分析;兜底→生成。
    """
    tasks = _task_list(campaign)
    if not tasks:
        return '生成'
    rungs = [t.get('rung', 'pending') for t in tasks]
    if any(r == 'pending' for r in rungs):
        return '提交'
    if any(r == 'running' for r in rungs):
        return '监控'
    if any(r == 'failed' for r in rungs):
        return '恢复'
    if all(r == 'accepted' for r in rungs):
        return '报告'
    if all(r in _DONE_RUNGS for r in rungs):
        return '分析'
    return '生成'


def progress_summary(campaign) -> dict:
    """各 rung 计数 + total + 派生的 ready/blocked 数,供仪表盘一眼看全局。"""
    tasks = _task_list(campaign)
    counts = {r: 0 for r in ('pending', 'running', 'failed',
                             'completed', 'validated', 'accepted')}
    for t in tasks:
        r = t.get('rung', 'pending')
        counts[r] = counts.get(r, 0) + 1
    counts['total'] = len(tasks)
    d = derive_ready(campaign)
    counts['ready'] = len(d['ready'])
    counts['blocked'] = len(d['blocked'])
    counts['stage'] = campaign_stage(campaign)
    return counts
