"""调度器：以固定轮转顺序驱动各批次的提醒流程实例。

轮转顺序（每一轮严格按此执行，可核对）：
  1) 投送收件箱事件（文件消息 / 续期请求；批次内按提交顺序，批次间按批次号排序）
  2) 引擎推进（refresh_timers + do_engine_steps；到期钩子登记 PLANNED 台账）
  3) 应用续期（READY 的 apply_renewal 人工任务；先于派发，故同轮续期优先）
  4) 派发决策（READY 的 dispatch_reminder 人工任务；全流程唯一发送点）
  5) 对账（就 DISPATCHED 未确认条目查询通知服务；服务可能回 UNKNOWN）
  6) 通知服务推进（到点的已接受派发在此真正发出）
  7) 生成值班员看板快照

真实等待点（均为引擎真实状态，可用 get_tasks 核对）：
  WAITING due_timer        定时等待（到期时点存于任务 internal_data.event_value）
  READY   dispatch_reminder 提醒已可执行但还没发（人工任务，等派发器）
  WAITING wait_confirm     已派发、等待送达确认
  READY   apply_renewal    续期请求已捕获、等待调度器应用
  WAITING wait_data / wait_validation  等待文件
"""

import json
import os
import time

from SpiffWorkflow import TaskState
from SpiffWorkflow.bpmn import BpmnWorkflow, BpmnEvent
from SpiffWorkflow.bpmn.parser import BpmnParser
from SpiffWorkflow.bpmn.serializer import BpmnWorkflowSerializer
from SpiffWorkflow.bpmn.specs.event_definitions import MessageEventDefinition

from .services import NotificationService, ReminderLedger, DocRegistry

BPMN_FILE = os.path.join(os.path.dirname(__file__), 'reminder_plan.bpmn')
PROCESS_ID = 'supplier_batch_reminder'

MSG_DATA = 'docs_data'
MSG_VALIDATION = 'docs_validation'
MSG_RENEWAL = 'renewal_requested'
MSG_CONFIRMED = 'delivery_confirmed'


class BatchRuntime:
    """单个批次的运行时：一个独立的工作流实例 + 独立台账。"""

    def __init__(self, batch_id, label, workflow):
        self.batch_id = batch_id
        self.label = label
        self.workflow = workflow
        self.inbox = []            # 待投送事件：('message', name, payload) 或 ('renewal', new_due_iso)
        self.pending_renewal = None
        self.renewals_used = 0
        self.send_blocked = False  # 待核对：旧派发结果未知，保守停发
        self.send_closed = False   # 已有送达证据：本批永不再发
        self.send_delay = 0.05     # 交给通知服务后延迟多久真正发送
        self.dispatch_enabled = True  # 派发器开关（演示"可执行但还没发"）


class Scheduler:

    def __init__(self, verbose=True):
        parser = BpmnParser()
        parser.add_bpmn_file(BPMN_FILE)
        self.spec = parser.get_spec(PROCESS_ID)
        self.subprocess_specs = parser.get_subprocess_specs(PROCESS_ID)
        self.serializer = BpmnWorkflowSerializer(BpmnWorkflowSerializer.configure())
        self.service = NotificationService()
        self.docs = DocRegistry()
        self.batches = {}          # batch_id -> BatchRuntime
        self.ledgers = {}          # batch_id -> ReminderLedger（每批次独立一本）
        self.round_idx = 0
        self.t0 = time.time()
        self.verbose = verbose
        self.trace = []

    # ---------------------------------------------------------------- 基础

    def log(self, line):
        self.trace.append(line)
        if self.verbose:
            print(line)

    def add_batch(self, batch_id, label, due_iso, send_delay=0.05):
        workflow = BpmnWorkflow(self.spec, self.subprocess_specs)
        start = workflow.get_tasks(state=TaskState.READY)[0]
        start.set_data(batch_id=batch_id, due_time_iso=due_iso, renewals_used=0)
        rt = BatchRuntime(batch_id, label, workflow)
        rt.send_delay = send_delay
        self.batches[batch_id] = rt
        self.ledgers[batch_id] = ReminderLedger()
        self.log(f"批次 {label}({batch_id}) 建单：到期 {due_iso}，发送延迟 {send_delay}s")
        workflow.refresh_timers()
        self._engine_steps(rt)  # 推进到各真实等待点
        return rt

    def _engine_steps(self, rt):
        def did_complete(task):
            if task.task_spec.name == 'due_timer':
                seq = task.data.get('renewals_used', 0) + 1
                key = f"{rt.batch_id}#r{seq}"
                self.ledgers[rt.batch_id].plan(key, seq, time.time())
                self.log(f"  [{rt.label}] 到期定时器完成 → 台账登记 PLANNED {key}")
        rt.workflow.do_engine_steps(did_complete_task=did_complete)

    # ---------------------------------------------------------------- 收件箱

    def post_message(self, batch_id, msg_name, payload=None):
        self.batches[batch_id].inbox.append(('message', msg_name, payload or {}))

    def post_renewal(self, batch_id, new_due_iso):
        self.batches[batch_id].inbox.append(('renewal', new_due_iso))

    def _deliver_inbox(self, rt, now):
        wf = rt.workflow
        while rt.inbox:
            event = rt.inbox.pop(0)
            if event[0] == 'message':
                _, name, payload = event
                caught = wf.event_manager.catch(BpmnEvent(MessageEventDefinition(name), payload))
                kind = {MSG_DATA: 'data', MSG_VALIDATION: 'validation'}.get(name)
                if kind is not None:
                    self.docs.receive(rt.batch_id, kind)
                self.log(f"  [{rt.label}] 投送消息 {name} → 捕获 {caught} 处")
            else:
                _, new_due = event
                if rt.renewals_used >= 1:
                    self.log(f"  [{rt.label}] 续期请求被拒绝：每批只允许改一次到期时点")
                    continue
                caught = wf.event_manager.catch(BpmnEvent(MessageEventDefinition(MSG_RENEWAL),
                                                          {'requested_due': new_due}))
                if caught:
                    rt.pending_renewal = {'new_due': new_due}
                    self.log(f"  [{rt.label}] 续期请求已捕获（新到期 {new_due}），待应用")
                else:
                    self.log(f"  [{rt.label}] 续期请求未生效：提醒计划已关闭")

    # ---------------------------------------------------------------- 续期

    def _process_renewals(self, rt, now):
        wf = rt.workflow
        ledger = self.ledgers[rt.batch_id]
        for task in sorted(wf.get_tasks(state=TaskState.READY, manual=True), key=lambda t: str(t.id)):
            if task.task_spec.name != 'apply_renewal':
                continue
            params = rt.pending_renewal or {}
            # 续期生效前，先处置旧派发的证据
            for key in ledger.find('DISPATCHED'):
                ans = self.service.cancel(key, now)
                if ans == NotificationService.CANCELLED:
                    ledger.mark(key, 'CANCELLED', '续期时已在通知服务侧撤销', now)
                    self.log(f"  [{rt.label}] 旧派发 {key} 已在服务侧撤销（确定未发）")
                elif ans == NotificationService.ALREADY_SENT:
                    ledger.mark(key, 'CONFIRMED_SENT', '撤销时得知已送达', now)
                    rt.send_closed = True
                    self.log(f"  [{rt.label}] 旧派发 {key} 已送达 → 本批封盘，不再发")
                elif ans == NotificationService.NOT_FOUND:
                    ledger.mark(key, 'LOST', '服务无此派发记录', now)
                    self.log(f"  [{rt.label}] 旧派发 {key} 服务无记录（确定未发）")
                else:  # UNKNOWN
                    rt.send_blocked = True
                    self.log(f"  [{rt.label}] 旧派发 {key} 结果待核对（服务回 UNKNOWN）→ 保守停发")
            for key in ledger.find('PLANNED'):
                ledger.mark(key, 'SUPERSEDED', '尚未派发即被续期取代', now)
                self.log(f"  [{rt.label}] 旧计划 {key} 未派发，被续期取代")
            rt.renewals_used += 1
            task.set_data(due_time_iso=params['new_due'], renewals_used=rt.renewals_used)
            rt.pending_renewal = None
            task.run()
            self._engine_steps(rt)
            self.log(f"  [{rt.label}] 续期已应用：第 {rt.renewals_used + 1} 版计划，"
                     f"新到期 {params['new_due']}（已收文件保留）")

    # ---------------------------------------------------------------- 派发

    def _decide(self, rt, key, now):
        """派发决策：返回 (台账终态, 说明)；('SEND', '') 表示允许发送。"""
        ledger = self.ledgers[rt.batch_id]
        svc = self.service
        if self.docs.complete(rt.batch_id):
            return 'VOID', '文件已齐，提醒作废'
        if rt.send_closed or ledger.any_confirmed():
            return 'SUPPRESSED', '已有确认送达证据，本批不再发第二条'
        priors = ledger.find('DISPATCHED')
        if priors:
            ans = svc.query(priors[0], now)
            if ans == svc.SENT:
                ledger.mark(priors[0], 'CONFIRMED_SENT', '派发前取证：已送达', now)
                rt.send_closed = True
                return 'SUPPRESSED', f"旧派发 {priors[0]} 已确认送达"
            if ans == svc.ACCEPTED_NOT_SENT:
                return 'SUPPRESSED', f"旧派发 {priors[0]} 已被服务接受，等送达，不重复发"
            if ans == svc.UNKNOWN:
                rt.send_blocked = True
                return 'SUPPRESSED', f"旧派发 {priors[0]} 结果待核对（UNKNOWN），保守停发"
            # CANCELLED / NOT_FOUND：服务证明旧派发未生效，可安全发新提醒
            state = 'CANCELLED' if ans == svc.CANCELLED else 'LOST'
            ledger.mark(priors[0], state, '派发前取证：旧派发未生效', now)
        if rt.send_blocked:
            return 'SUPPRESSED', '待核对未解除，继续停发'
        return 'SEND', ''

    def _process_dispatches(self, rt, now):
        if not rt.dispatch_enabled or rt.workflow.completed:
            return
        wf = rt.workflow
        ledger = self.ledgers[rt.batch_id]
        for task in sorted(wf.get_tasks(state=TaskState.READY, manual=True), key=lambda t: str(t.id)):
            if task.task_spec.name != 'dispatch_reminder':
                continue
            seq = task.data.get('renewals_used', 0) + 1
            key = f"{rt.batch_id}#r{seq}"
            ledger.plan(key, seq, now)
            verdict, note = self._decide(rt, key, now)
            if verdict == 'SEND':
                self.service.submit(key, rt.batch_id, now, send_delay=rt.send_delay)
                ledger.mark(key, 'DISPATCHED', '已交本地通知服务（送达未确认）', now)
                task.set_data(dispatched=True, dispatch_note='已派发')
                self.log(f"  [{rt.label}] 派发 {key} → 通知服务已接受（键即幂等身份，确认将滞后）")
            else:
                ledger.mark(key, verdict, note, now)
                task.set_data(dispatched=False, dispatch_note=note)
                self.log(f"  [{rt.label}] 派发 {key} 被拦下（{verdict}）：{note}")
            task.run()
            self._engine_steps(rt)

    # ---------------------------------------------------------------- 对账

    def _reconcile(self, rt, now):
        ledger = self.ledgers[rt.batch_id]
        svc = self.service
        for key in ledger.find('DISPATCHED'):
            ans = svc.query(key, now)
            if ans == svc.SENT:
                ledger.mark(key, 'CONFIRMED_SENT', '对账确认送达', now)
                rt.send_closed = True
                rt.send_blocked = False
                caught = rt.workflow.event_manager.catch(
                    BpmnEvent(MessageEventDefinition(MSG_CONFIRMED), {'key': key}))
                self._engine_steps(rt)
                self.log(f"  [{rt.label}] 对账：{key} 已送达 → CONFIRMED_SENT"
                         f"（关闭确认等待 {caught} 处）")
            elif ans == svc.CANCELLED:
                ledger.mark(key, 'CANCELLED', '对账得知已撤销', now)
                self.log(f"  [{rt.label}] 对账：{key} 已撤销")
            elif ans == svc.NOT_FOUND:
                ledger.mark(key, 'LOST', '服务无记录', now)
                rt.send_blocked = False
                self.log(f"  [{rt.label}] 对账：{key} 服务无记录（确定未发）")
            elif ans == svc.UNKNOWN:
                self.log(f"  [{rt.label}] 对账：{key} 仍待核对（服务回 UNKNOWN）")
            # ACCEPTED_NOT_SENT：保持 DISPATCHED，等服务真正发出

    # ---------------------------------------------------------------- 轮转

    def run_round(self, label):
        now = time.time()
        self.round_idx += 1
        self.log(f"\n== 轮次 {self.round_idx} [t=+{now - self.t0:.2f}s] {label} ==")
        for bid in sorted(self.batches):                       # 1) 投送收件箱
            self._deliver_inbox(self.batches[bid], now)
        for bid in sorted(self.batches):                       # 2) 引擎推进
            rt = self.batches[bid]
            if not rt.workflow.completed:
                rt.workflow.refresh_timers()
                self._engine_steps(rt)
        for bid in sorted(self.batches):                       # 3) 应用续期
            self._process_renewals(self.batches[bid], now)
        for bid in sorted(self.batches):                       # 4) 派发决策
            self._process_dispatches(self.batches[bid], now)
        for bid in sorted(self.batches):                       # 5) 对账
            self._reconcile(self.batches[bid], now)
        self.service.advance(now)                              # 6) 服务真正发送
        for bid in sorted(self.batches):                       # 7) 值班员看板
            self.log('  ' + self._board_line(self.batches[bid], now))
        degraded = '是（查询只回 UNKNOWN）' if now < self.service.degraded_until else '否'
        self.log(f"  服务: 已实际发送={self.service.send_log or '[]'} 降级={degraded}")

    # ---------------------------------------------------------------- 保存/恢复

    def checkpoint(self, batch_id):
        """保存某批次（工作流 + 台账 + 调度标志）。其他批次不受影响。"""
        rt = self.batches[batch_id]
        blob = {
            'workflow': self.serializer.serialize_json(rt.workflow),
            'ledger': self.ledgers[batch_id].to_dict(),
            'renewals_used': rt.renewals_used,
            'send_blocked': rt.send_blocked,
            'send_closed': rt.send_closed,
            'send_delay': rt.send_delay,
            'dispatch_enabled': rt.dispatch_enabled,
        }
        self.log(f"  [{rt.label}] ★ 保存检查点（工作流 + 台账 {self.ledgers[batch_id].summary()}）")
        return json.dumps(blob)  # 落盘为 JSON，证明可序列化

    def restore(self, batch_id, blob_str):
        """从检查点恢复某批次。通知服务与文件登记处是外部世界，不回滚。"""
        blob = json.loads(blob_str)
        rt = self.batches[batch_id]
        workflow = self.serializer.deserialize_json(blob['workflow'])
        new_rt = BatchRuntime(batch_id, rt.label, workflow)
        new_rt.renewals_used = blob['renewals_used']
        new_rt.send_blocked = blob['send_blocked']
        new_rt.send_closed = blob['send_closed']
        new_rt.send_delay = blob['send_delay']
        new_rt.dispatch_enabled = blob['dispatch_enabled']
        self.batches[batch_id] = new_rt
        self.ledgers[batch_id] = ReminderLedger.from_dict(blob['ledger'])
        self.log(f"  [{rt.label}] ★ 已恢复（台账 {self.ledgers[batch_id].summary()}）")
        return new_rt

    # ---------------------------------------------------------------- 看板

    def _waiting_points(self, rt):
        wf = rt.workflow
        points = []
        for task in wf.get_tasks():
            name, state = task.task_spec.name, task.state
            if name == 'due_timer' and state == TaskState.WAITING:
                due = task.internal_data.get('event_value', '?')
                points.append(f"定时等待(到期{due})")
            elif name == 'dispatch_reminder' and state == TaskState.READY:
                points.append("派发可执行·未发")
            elif name == 'wait_confirm' and state == TaskState.WAITING:
                points.append("等待送达确认")
            elif name == 'apply_renewal' and state == TaskState.READY:
                points.append("续期待应用")
        if wf.completed:
            points.append("流程完结")
        return points

    def _board_line(self, rt, now):
        ledger = self.ledgers[rt.batch_id]
        flags = []
        if rt.send_blocked:
            flags.append("待核对·发送隔离中(文件接收照常)")
        if rt.send_closed:
            flags.append("已封盘(有送达证据)")
        if rt.renewals_used:
            flags.append(f"续期已用{rt.renewals_used}次")
        return (f"[{rt.label}] 文件[{self.docs.summary(rt.batch_id)}] "
                f"等待点[{', '.join(self._waiting_points(rt)) or '无'}] "
                f"台账[{ledger.summary()}] "
                f"标志[{', '.join(flags) or '正常'}]")
