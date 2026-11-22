"""外部系统：通知服务、发送证据台账、文件登记处。

- NotificationService 是"通知供应商的本地服务"：接受派发后可以延迟真正发送，
  确认可以滞后，查询在服务降级窗口内只回 UNKNOWN。
- ReminderLedger 是引擎侧的发送证据台账（每批次一本），随工作流一起保存/恢复。
- DocRegistry 是文件接收系统，独立于引擎，保存/恢复不影响已收文件。
"""


class NotificationService:
    """本地通知服务（外部世界，引擎重启不影响它）。"""

    UNKNOWN = 'UNKNOWN'
    ACCEPTED_NOT_SENT = 'ACCEPTED_NOT_SENT'
    SENT = 'SENT'
    CANCELLED = 'CANCELLED'
    NOT_FOUND = 'NOT_FOUND'
    ALREADY_SENT = 'ALREADY_SENT'

    def __init__(self):
        self.entries = {}        # key -> dict(state, batch, accepted_at, send_after, sent_at)
        self.degraded_until = 0.0  # 在此时间点之前，query/cancel 只回 UNKNOWN
        self.send_log = []       # 实际发到供应商的记录（事实源，用于核对）

    def degrade(self, seconds, now):
        """进入降级窗口：窗口内无法证明任何派发是否已发。"""
        self.degraded_until = now + seconds

    def submit(self, key, batch, now, send_delay):
        """接受一次派发。键即幂等身份：同键重复提交不会产生第二次发送。"""
        entry = self.entries.get(key)
        if entry is not None:
            return 'DUPLICATE'
        self.entries[key] = {
            'batch': batch, 'state': 'accepted',
            'accepted_at': now, 'send_after': now + send_delay, 'sent_at': None,
        }
        return 'ACCEPTED'

    def cancel(self, key, now):
        """撤销一次尚未真正发出的派发。降级窗口内返回 UNKNOWN。"""
        if now < self.degraded_until:
            return self.UNKNOWN
        entry = self.entries.get(key)
        if entry is None:
            return self.NOT_FOUND
        if entry['state'] == 'sent':
            return self.ALREADY_SENT
        entry['state'] = 'cancelled'
        return self.CANCELLED

    def query(self, key, now):
        """查询某次派发是否已发到对方。降级窗口内返回 UNKNOWN。"""
        if now < self.degraded_until:
            return self.UNKNOWN
        entry = self.entries.get(key)
        if entry is None:
            return self.NOT_FOUND
        return {'accepted': self.ACCEPTED_NOT_SENT,
                'sent': self.SENT,
                'cancelled': self.CANCELLED}[entry['state']]

    def advance(self, now):
        """把到点的已接受派发真正发出去。"""
        for key, entry in self.entries.items():
            if entry['state'] == 'accepted' and now >= entry['send_after']:
                entry['state'] = 'sent'
                entry['sent_at'] = now
                self.send_log.append((key, entry['batch'], now))

    def sent_keys(self, batch):
        return [key for key, b, _ in self.send_log if b == batch]


class ReminderLedger:
    """发送证据台账（每批次一本，随该批次工作流一起保存/恢复）。

    状态机：
      PLANNED     到期任务已完成、派发任务已可执行（尚未交给通知服务）
      DISPATCHED  已交本地通知服务并取得幂等键（送达未确认）
      CONFIRMED_SENT  已取证确认送达（终态，本批永不再发）
      CANCELLED   续期时已在通知服务侧撤销（终态，确定未发）
      SUPERSEDED  尚未派发即被续期取代（终态，确定未发）
      SUPPRESSED  派发决策点被证据拦下（终态，未发）
      VOID        派发前文件已齐（终态，未发）
      LOST        服务侧无此派发记录（终态，确定未发）
    """

    TERMINAL = ('CONFIRMED_SENT', 'CANCELLED', 'SUPERSEDED', 'SUPPRESSED', 'VOID', 'LOST')

    def __init__(self):
        self.entries = {}  # key -> dict(seq, state, note, history)

    def plan(self, key, seq, now):
        if key not in self.entries:
            self.entries[key] = {'seq': seq, 'state': 'PLANNED', 'note': '到期，待派发',
                                 'history': [('PLANNED', now)]}

    def mark(self, key, state, note, now):
        entry = self.entries[key]
        entry['state'] = state
        entry['note'] = note
        entry['history'].append((state, now))

    def get(self, key):
        return self.entries.get(key)

    def find(self, state):
        return [key for key, e in self.entries.items() if e['state'] == state]

    def any_confirmed(self):
        return len(self.find('CONFIRMED_SENT')) > 0

    def to_dict(self):
        return dict(self.entries)

    @classmethod
    def from_dict(cls, data):
        ledger = cls()
        ledger.entries = dict(data)
        return ledger

    def summary(self):
        return ' '.join(f"{key.split('#')[-1]}={e['state']}" for key, e in self.entries.items()) or '(空)'


class DocRegistry:
    """文件接收系统（外部），保存/恢复工作流不影响已收文件。"""

    def __init__(self):
        self.received = {}  # batch -> set(['data', 'validation'])

    def receive(self, batch, kind):
        self.received.setdefault(batch, set()).add(kind)

    def complete(self, batch):
        return self.received.get(batch, set()) >= {'data', 'validation'}

    def summary(self, batch):
        got = self.received.get(batch, set())
        return f"数据{'✓' if 'data' in got else '✗'} 校验{'✓' if 'validation' in got else '✗'}"
