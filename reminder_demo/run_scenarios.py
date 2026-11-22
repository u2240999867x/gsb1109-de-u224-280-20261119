"""脚本化轨迹：三次保存/恢复实验。

公共起点（每个场景独立重建）：
  甲(A)：只收到数据文件，初版到期 T1 后提醒已可执行
  乙(B)：同样只收到数据、同时到期；之后按自己的事件顺序运行，不受甲恢复影响

  场景一：甲的续期在旧提醒实际发送前生效 → 新时点仍缺件，只发新提醒
  场景二：旧提醒已实际发出但执行器没收到确认 → 续期后任何时点都不再发第二条
  场景三：旧提醒已可执行但还没发，续期后甲补齐校验 → 旧、新两条都不发

运行：.venv/bin/python -m reminder_demo.run_scenarios
"""

import time
from datetime import datetime, timedelta, timezone

from SpiffWorkflow import TaskState

from .driver import Scheduler, MSG_DATA, MSG_VALIDATION

T1_DELAY = 0.4          # 初版到期偏移（秒）
T2_DELAY = 0.5          # 续期后的到期偏移（秒）
FAILURES = []


def iso_after(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def check(cond, label):
    print(f"  {'✓' if cond else '✗ FAIL'} {label}")
    if not cond:
        FAILURES.append(label)


def build_world(a_send_delay):
    """公共起点：甲乙各建单、各收到数据文件，到期 T1 相同。"""
    sched = Scheduler()
    due = iso_after(T1_DELAY)
    sched.add_batch('A', '甲', due, send_delay=a_send_delay)
    sched.add_batch('B', '乙', due, send_delay=0.05)
    sched.post_message('A', MSG_DATA, {'doc': 'data-v1'})
    sched.post_message('B', MSG_DATA, {'doc': 'data-v1'})
    sched.run_round('初始化：甲乙各收到数据文件，等待到期')
    return sched


def ready_dispatch(sched, batch_id):
    return [t for t in sched.batches[batch_id].workflow.get_tasks(state=TaskState.READY, manual=True)
            if t.task_spec.name == 'dispatch_reminder']


def waiting_timer_due(sched, batch_id):
    for t in sched.batches[batch_id].workflow.get_tasks(state=TaskState.WAITING):
        if t.task_spec.name == 'due_timer':
            return t.internal_data.get('event_value')
    return None


def scenario_1():
    print("\n" + "=" * 78)
    print("场景一（恢复一）：续期在旧提醒实际发送前生效 → 新时点仍缺件，只发新提醒")
    print("=" * 78)
    sched = build_world(a_send_delay=60.0)   # 旧提醒延迟发送，留出续期窗口
    time.sleep(T1_DELAY + 0.1)
    sched.run_round('初版到期：甲乙提醒均已可执行并派发（甲的发送被服务延迟）')
    check(sched.ledgers['A'].get('A#r1')['state'] == 'DISPATCHED', '甲 r1 已交服务、确认滞后')

    sched.post_renewal('A', iso_after(T2_DELAY))
    sched.run_round('甲续期：服务侧撤销旧派发（尚未真正发送），重挂新到期时点')
    check(sched.ledgers['A'].get('A#r1')['state'] == 'CANCELLED', '甲 r1 已撤销（确定未发）')

    blob = sched.checkpoint('A')
    sched.service.degrade(0.5, time.time())   # 恢复初期：服务一时无法证明是否已发
    sched.restore('A', blob)
    sched.batches['A'].send_delay = 0.05      # 新计划按正常延迟发送
    check(sched.ledgers['A'].get('A#r1')['state'] == 'CANCELLED', '恢复后台账仍持有 r1 撤销证据')
    check(waiting_timer_due(sched, 'A') is not None, '恢复后甲的新定时器在等')
    sched.run_round('恢复后首轮：服务降级只回 UNKNOWN，甲凭台账终态证据照常推进')

    time.sleep(T2_DELAY + 0.05)
    sched.run_round('甲新时点到：仍缺校验件 → 派发新提醒 r2')
    time.sleep(0.1)
    sched.service.advance(time.time())        # 服务异步到点，真正发出 r2
    sched.post_message('B', MSG_VALIDATION, {'doc': 'val-v1'})
    sched.run_round('服务恢复：对账确认甲 r2；乙补齐校验件后放行')

    sent_a = sched.service.sent_keys('A')
    check(sent_a == ['A#r2'], f'甲只发出新提醒 r2（实际发送记录={sent_a}）')
    check(sched.ledgers['A'].get('A#r2')['state'] == 'CONFIRMED_SENT', '甲 r2 已确认送达')
    check(sched.service.sent_keys('B') == ['B#r1'], '乙按自己的事件顺序只发一次')
    check(sched.batches['B'].workflow.completed, '乙文件齐已放行、流程完结')


def scenario_2():
    print("\n" + "=" * 78)
    print("场景二（恢复二）：旧提醒已实际发出但执行器没收到确认 → 续期后不再发第二条")
    print("=" * 78)
    sched = build_world(a_send_delay=0.05)
    time.sleep(T1_DELAY + 0.1)
    sched.run_round('初版到期：甲乙提醒均已可执行并派发')
    time.sleep(0.1)
    sched.run_round('服务到点真正发出甲 r1，但确认滞后，执行器台账仍是 DISPATCHED')
    check(sched.service.sent_keys('A') == ['A#r1'], '事实：甲 r1 已实际发到供应商')
    check(sched.ledgers['A'].get('A#r1')['state'] == 'DISPATCHED', '执行器尚未收到确认')

    blob = sched.checkpoint('A')
    sched.service.degrade(0.9, time.time())   # 恢复初期：服务一时无法证明是否已发
    sched.restore('A', blob)

    sched.post_renewal('A', iso_after(T2_DELAY))
    sched.post_message('B', MSG_VALIDATION, {'doc': 'val-v1'})
    sched.run_round('甲续期：撤销旧派发得到 UNKNOWN → 待核对、保守停发；乙文件照收并放行')
    check(sched.batches['A'].send_blocked, '甲进入待核对（发送隔离）')
    check(sched.batches['B'].workflow.completed, '乙不受甲恢复影响，已放行完结')

    time.sleep(T2_DELAY + 0.05)
    sched.run_round('甲新时点到：派发决策取证仍得 UNKNOWN → 新提醒被拦下')
    check(sched.ledgers['A'].get('A#r2')['state'] == 'SUPPRESSED', '甲 r2 被证据拦下（未发）')

    time.sleep(0.5)
    sched.run_round('服务恢复：对账确认甲 r1 已送达 → 封盘')
    sched.run_round('观察轮：任何时点都不再出现甲的第二次派发')

    sent_a = sched.service.sent_keys('A')
    check(sent_a == ['A#r1'], f'甲全程只发出 r1 一条（实际发送记录={sent_a}）')
    check(sched.ledgers['A'].get('A#r1')['state'] == 'CONFIRMED_SENT', '甲 r1 终态为确认送达')
    check(sched.service.sent_keys('B') == ['B#r1'], '乙按自己的事件顺序只发一次')


def scenario_3():
    print("\n" + "=" * 78)
    print("场景三（恢复三）：旧提醒已可执行但还没发，续期后甲补齐校验 → 旧新都不发")
    print("=" * 78)
    sched = build_world(a_send_delay=0.05)
    sched.batches['A'].dispatch_enabled = False   # 甲的派发器暂停：制造"可执行未发"
    time.sleep(T1_DELAY + 0.1)
    sched.run_round('初版到期：甲提醒已可执行但还没发（派发器暂停）；乙正常派发')
    check(len(ready_dispatch(sched, 'A')) == 1, '甲的派发任务 READY（可执行未发）')

    blob = sched.checkpoint('A')
    sched.restore('A', blob)
    check(len(ready_dispatch(sched, 'A')) == 1, '恢复后甲的派发任务仍可执行未发')

    sched.post_renewal('A', iso_after(T2_DELAY))
    sched.run_round('甲续期：旧派发任务被中断取消，旧计划被取代，重挂新到期时点')
    check(len(ready_dispatch(sched, 'A')) == 0, '旧派发任务已取消')
    check(sched.ledgers['A'].get('A#r1')['state'] == 'SUPERSEDED', '甲 r1 未派发即被取代')

    sched.post_message('A', MSG_VALIDATION, {'doc': 'val-v1'})
    sched.post_message('B', MSG_VALIDATION, {'doc': 'val-v1'})
    sched.run_round('甲补齐校验件 → 放行（terminate 取消新定时器）；乙也补齐放行')
    check(sched.batches['A'].workflow.completed, '甲文件齐已放行、流程完结')
    check(waiting_timer_due(sched, 'A') is None, '甲的新定时器已被放行取消')

    sched.batches['A'].dispatch_enabled = True    # 恢复派发器，观察旧新两条都不会发
    time.sleep(T2_DELAY + 0.1)
    sched.run_round('过了甲的新时点：无任何派发发生')
    sched.run_round('再观察一轮')

    check(sched.service.sent_keys('A') == [], f"甲一条提醒都没发（实际发送记录={sched.service.sent_keys('A')}）")
    check('A#r2' not in sched.ledgers['A'].entries, '甲的新计划从未进入派发')
    check(sched.service.sent_keys('B') == ['B#r1'], '乙按自己的事件顺序只发一次')
    check(sched.batches['B'].workflow.completed, '乙已放行完结')


if __name__ == '__main__':
    scenario_1()
    scenario_2()
    scenario_3()
    print("\n" + "=" * 78)
    if FAILURES:
        print(f"结果：{len(FAILURES)} 项核对失败：")
        for item in FAILURES:
            print(f"  ✗ {item}")
        raise SystemExit(1)
    print("结果：三个保存/恢复场景全部核对通过。")
