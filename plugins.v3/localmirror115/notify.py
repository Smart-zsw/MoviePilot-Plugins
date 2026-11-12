"""同步结果通知：整轮结束后只发一条汇总，紧急告警按类别限流。"""
from contextlib import contextmanager
import threading
import time

from .log import duration_text, logger

QUIET_SECONDS = 30
PROBE_INTERVAL = 5  # 检查到还有工作在进行后，隔几秒再检查本轮是否结束
ALERT_THROTTLE = 600
LIST_LIMIT = 10
RETRY_SECONDS = 60
FAILED_NAMES = 1000  # 失败明细最多保留的条数，其余只计数

SECTIONS = (
    ('uploaded', '上传115'), ('reuploaded', '115缺失补传'), ('copied', '复制到STRM目录'),
    ('deleted_b', '删除115文件'), ('deleted_c', '删除STRM文件'),
    ('adopted', '115已存在，无需上传'), ('adopted_c', 'STRM目录已存在，无需复制'),
)


def new_state():
    """创建通知共享状态。"""
    # 只含数据和锁：模块重载后仍可用，已退役的工作线程也能继续记录结果。
    return {'lock': threading.RLock(), 'counts': {}, 'items': {}, 'first_at': None,
            'last_at': None, 'idle_since': None, 'started_at': None, 'last_alert': {}, 'active': 0,
            'generation': 0, 'owner': None, 'sending': False, 'retry_at': 0, 'alerts': [], 'resolved': set(),
            'failed_more': 0, 'probe_at': 0}


class Notifier:
    """汇总同步结果并发送批量通知与限流告警；发送失败不影响同步。"""

    def __init__(self, send=None, enabled=False, clock=time.time, state=None):
        """绑定发送函数与共享状态，并登记为状态所有者。"""
        self.send = send
        self.enabled = bool(enabled and send)
        self.clock = clock
        self.state = state if state is not None else new_state()
        self.lock = self.state['lock']
        self.owner = object()
        with self.lock:
            self.state['owner'] = self.owner

    @property
    def counts(self):
        """本轮各类结果的计数。"""
        return self.state['counts']

    @property
    def items(self):
        """本轮明细（如失败项及原因）。"""
        return self.state['items']

    def suspend(self):
        """放弃状态所有权，使已停止或被替换的引擎无法发布不完整的汇总。"""
        with self.lock:
            if self.state['owner'] is self.owner:
                self.state['owner'] = None

    @contextmanager
    def activity(self):
        """标记有处理正在进行；期间不发送汇总。"""
        with self.lock:
            self.state['active'] += 1
        try:
            yield
        finally:
            with self.lock:
                self.state['active'] -= 1

    def touch(self):
        """记录一次真实变化或请求（非空轮询），重新开始静默计时。"""
        with self.lock:
            self.state['generation'] += 1
            self.state['idle_since'] = None
            if not any(self.state['counts'].values()):
                self.state['started_at'] = self.clock()

    def record(self, kind, name=None, reason=None, n=1):
        """记录一项结果：失败项（kind='failed'）按名称保存原因，其他类别只累加计数（见 count）。"""
        if not self.enabled or n <= 0:
            return
        now = self.clock()
        with self.lock:
            s = self.state
            if kind == 'failed' and name is not None:
                failures = s['items'].setdefault('failed', {})
                if name in failures or len(failures) < FAILED_NAMES:
                    failures[name] = reason or '处理失败'
                else:
                    s['failed_more'] = s.get('failed_more', 0) + 1
                s['counts']['failed'] = len(failures) + s.get('failed_more', 0)
            else:
                s['counts'][kind] = s['counts'].get(kind, 0) + n
            if s['first_at'] is None:
                s['first_at'] = s['started_at'] if s['started_at'] is not None else now
            s['last_at'] = now
            s['idle_since'] = None
            s['generation'] += 1

    def resolve(self, name):
        """移除已重试成功的失败项。"""
        with self.lock:
            failures = self.state['items'].get('failed', {})
            if self.state['sending']:
                self.state.setdefault('resolved', set()).add(name)  # 也从发送失败后回填的批次中剔除
            if name in failures:
                del failures[name]
                self.state['counts']['failed'] = len(failures) + self.state.get('failed_more', 0)
                self.state['generation'] += 1

    def count(self, kind, n=1):
        """按类别累加计数。"""
        self.record(kind, n=n)

    def flush(self, force=False, probe=None):
        """认领已结束的一轮并发送汇总；force 也不会跳过未完成的工作。

        probe（可能读数据库）与发送都在锁外执行，慢渠道不会阻塞同步；probe 期间有新活动则本次作废。
        发送失败时把批次合并回状态并延后重试。"""
        if not self.enabled:
            return False
        with self.lock:
            s = self.state
            if (s['owner'] is not self.owner or s['sending'] or not any(s['counts'].values())
                    or self.clock() < s['retry_at']):
                return False
            if s['active']:
                return False
            # 静默未满则提前返回、不必 probe；真实工作到达时 touch()/record() 会重置 idle_since。
            if not force and s['idle_since'] is not None and self.clock() - s['idle_since'] < QUIET_SECONDS:
                return False
            if not force and self.clock() < s.get('probe_at', 0):
                return False  # 刚检查过还有工作在进行
            generation = s['generation']
        try:
            status = probe() if probe is not None else {}
        except Exception:
            logger.warning('【通知】暂无法确认本轮是否结束，稍后再检查')
            return False
        with self.lock:
            s = self.state
            if (s['owner'] is not self.owner or s['sending'] or s['active']
                    or s['generation'] != generation):
                return False
            now = self.clock()
            if status.get('busy'):
                s['idle_since'], s['probe_at'] = None, now + PROBE_INTERVAL
                return False
            if s['idle_since'] is None:
                s['idle_since'] = now if probe is not None else s['last_at']
            if not force and now - s['idle_since'] < QUIET_SECONDS:
                return False
            counts, items, first_at, last_at = s['counts'], s['items'], s['first_at'], s['last_at']
            s.update(counts={}, items={}, first_at=None, last_at=None, idle_since=None, started_at=None, sending=True,
                     failed_more=0)
        sent = self._send(*self.compose(items, counts, elapsed=(last_at if last_at is not None else now)
                                       - (first_at if first_at is not None else now),
                                       deferred=status.get('deferred', 0)))
        with self.lock:
            s = self.state
            s['sending'] = False
            if not sent:
                # 发送期间可能有新结果到达：只能合并，绝不覆盖。
                for kind, n in counts.items():
                    if kind != 'failed':
                        s['counts'][kind] = s['counts'].get(kind, 0) + n
                failures = {name: reason for name, reason in items.get('failed', {}).items()
                            if name not in s.get('resolved', ())}
                failures.update(s['items'].get('failed', {}))
                if failures:
                    s['items']['failed'] = failures
                    s['counts']['failed'] = len(failures)
                s['first_at'] = min(x for x in (first_at, s['first_at']) if x is not None)
                s['last_at'] = max(x for x in (last_at, s['last_at']) if x is not None)
                s['retry_at'] = self.clock() + RETRY_SECONDS
            s['resolved'] = set()
        return sent

    def alert(self, kind, title, text):
        """按类别限流发送告警；有处理进行中时先排队。返回是否已发送或入队。"""
        if not self.enabled:
            return False
        now = self.clock()
        with self.lock:
            alerts = self.state['last_alert']
            if now - alerts.get(kind, -ALERT_THROTTLE) < ALERT_THROTTLE:
                return False
            alerts[kind] = now
            if self.state['active']:
                self.state['alerts'].append((title, text))
                return True
        return self._send(title, text)

    def flush_alerts(self):
        """由状态所有者发出排队的告警。"""
        if not self.enabled:
            return
        with self.lock:
            if self.state['owner'] is not self.owner:
                return
            alerts, self.state['alerts'] = self.state['alerts'], []
        for title, text in alerts:
            self._send(title, text)

    def warning(self, kind, status, impact, action, detail=None):
        """按“状态/影响/处理/详情”格式组装警告并限流发送。"""
        lines = ['状态  ' + status, '影响  ' + impact, '处理  ' + action]
        if detail:
            lines.append('详情  ' + detail)
        return self.alert(kind, '⚠️ 本地115镜像｜' + status, '\n'.join(lines))

    @staticmethod
    def compose(items, counts, elapsed=None, deferred=0):
        """生成汇总通知的标题和正文；失败项最多列出 LIST_LIMIT 条。"""
        failed = counts.get('failed', 0)
        title = ('⚠️ 本地115镜像｜本轮需关注' if failed or deferred else '✅ 本地115镜像｜本轮完成')
        lines = []
        for kind, label in SECTIONS:
            n = counts.get(kind, 0)
            if n:
                dirs = counts.get('deleted_dirs_b') if kind == 'deleted_b' else 0
                suffix = '（%d 个目录）' % dirs if dirs else ''
                lines.append('%s  %d 个%s' % (label, n, suffix))
        if counts.get('deleted_dirs_b') and not counts.get('deleted_b'):
            lines.append('删除115目录  %d 个' % counts['deleted_dirs_b'])
        if elapsed is not None:
            lines.append('处理用时  ' + duration_text(elapsed))
        if deferred:
            lines.append('暂缓处理  %d 项（受开关、冲突或删除延迟限制）' % deferred)
        if failed:
            lines.append('\n需处理  %d 项' % failed)
            failures = items.get('failed', {})
            failures = list(failures.items()) if isinstance(failures, dict) else failures
            for name, reason in failures[:LIST_LIMIT]:
                lines.append('• %s：%s' % (name, reason or '处理失败'))
            if failed > LIST_LIMIT:
                lines.append('其余 %d 项请查看插件日志。' % (failed - LIST_LIMIT))
        lines.append('\n' + ('自动任务已收尾，请检查需关注项。' if failed or deferred else '本轮任务已完成，继续监控新变化。'))
        return title, '\n'.join(lines)

    def _send(self, title, text):
        """发送通知并记录日志；异常时返回 False，不影响同步。"""
        try:
            self.send(title, text)
            logger.info('【通知】已发送：%s', title)
            return True
        except Exception as error:
            logger.warning('【通知】发送失败（不影响同步）：%s，%s', title, type(error).__name__)
            return False
