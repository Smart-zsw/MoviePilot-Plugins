"""镜像引擎：A -> B（上传到 115）与 A -> C（非视频文件按字节复制）。

A 只读不写；C 中的 STRM 由其他工具生成，本引擎不触碰。每个动作前后都重新核对 A 的指纹，过时即丢弃。
"""
from pathlib import Path, PurePosixPath
import collections
import contextlib
import dataclasses
from functools import wraps
import json
import os
import copy
import sys
import threading
import time
import types

from .adapters import (CookieDeleter, RemoteUnknown, RemoteConflict, RateLimiter, Stopped, describe,
                       tree_export_available)
from .remote_index import RemoteIndex
from .directory_delete import process_deleted_directories
from .notify import Notifier, new_state
from .filesystem import ReadOnlyFilesystem, LocalOutput, OutputError
from .persistence import FINAL, b_unseen, owned_ids, stable, tree_adopted
from .log import (logger, where, size_text, note_label, error_detail, duration_text, MODE_LABEL, TARGET_LABEL,
                  TARGET_PLACE)


def _runtime():
    """进程内共享、插件模块重载后仍保留的状态容器。"""
    holder = sys.modules.get('_localmirror115_runtime')
    if holder is None:
        holder = sys.modules.setdefault('_localmirror115_runtime', types.ModuleType('_localmirror115_runtime'))
    for name, factory in (('lock', threading.Lock), ('instances', dict), ('notifications', dict)):
        if not hasattr(holder, name):
            setattr(holder, name, factory())
    return holder


def instance_state(data_path):
    """返回按数据目录区分、跨重载保留的实例状态（工作锁、限速器、导出锁、停止时间）。
    工作锁让重载后的新引擎等待进行中的上传而不并发；限速器保留使保存配置不会清零风控冷却。"""
    holder = _runtime()
    with holder.lock:
        return holder.instances.setdefault(str(data_path), types.SimpleNamespace(
            b_lock=threading.Lock(), c_lock=threading.Lock(), export_lock=threading.Lock(),
            limiter=None, stopped_at=None))


def notification_state(data_path):
    """返回指定数据目录对应的通知状态（进程内共享，插件重载后保留）。"""
    return _runtime().notifications.setdefault(str(data_path), new_state())


def _activity(method):
    """装饰器：在通知器的活动区间内执行被装饰的方法。"""
    @wraps(method)
    def guarded(self, *args, **kwargs):
        """在 notifier.activity() 上下文中调用原方法。"""
        with self.notifier.activity():
            return method(self, *args, **kwargs)
    return guarded


MTIME_TOLERANCE = 120   # 认领已有文件时，A 的 mtime 与 115 上传时间允许相差的秒数
LISTING_TTL = 120
FRESH = 30              # 同一批次内复用 115 目录列表的最长秒数
DEFER = 30
BATCH = 1000
SHORT_RESTART = 600     # 在此秒数内重启则沿用上次全量扫描结果
HEARTBEAT = 30          # 扫描记录心跳写入间隔秒数
WORK_BATCH = 1000       # 工作线程每次从队列取出的文件数
PLANNED_RECHECK = 86400  # 未变化的计划（试运行、开关关闭）两次复查之间的秒数
RATE_WINDOW = 120  # 估算实时速度/剩余时间所用的已完成工作时间窗口（秒）
MAX_UNCONFIRMED_UPLOADS = 3
NO_OP_PLANS = ('would_adopt_existing', 'would_adopt_identical', 'would_reconfirm_existing')  # 计划中目标已有相同文件
AUDIT_SUSPECT = 500     # 快照核对中已同步却不在快照里的文件超过此数，视快照可疑，不据此重传
BIG_UPLOAD = 4 << 30     # 不小于此大小（4 GB）的文件开始上传时也记一笔
SCAN_RETRY = 600        # 全量扫描未完成时，隔多少秒自动重新扫描（只读本地，不访问115）
DELETE_QUIET = 30       # 本地连续删除停下这么多秒后，再合并删除115上的对应文件
DELETE_QUIET_MAX = 300  # 本地一直在删除时，单个文件最多等待的秒数


def encodable(relative):
    """文件名能否编码为 UTF-8（含无法解码字节的名称无法写入数据库，跳过）。"""
    try:
        relative.encode('utf-8')
    except UnicodeEncodeError:
        return False
    return True


def backoff(attempts):
    """按失败次数返回指数退避的重试等待秒数（30 秒起，最长 1 小时）。"""
    return min(3600.0, 30.0 * (2 ** min(attempts, 7)))


class SyncEngine:
    """同步引擎：接收 A 的变化，稳定后分别驱动 B（115 上传/删除）和 C（非视频文件复制/删除）。"""
    def __init__(self, config, repository, remote=None, data_path=None, clock=time.time, notifier=None):
        """初始化引擎状态、限流器、115 快照索引和输出目录，并绑定各源根目录。"""
        self.config, self.repository = config, repository
        self.notifier = notifier or Notifier()
        self.remote = remote
        self.clock = clock
        self.runtime = instance_state(data_path)
        self.fs = ReadOnlyFilesystem()
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.b_wake = threading.Event()
        self.c_wake = threading.Event()
        self.scan_requested = threading.Event()
        self.full_scan = False
        self.scan_keys = None   # 待执行的全量扫描覆盖的映射；None 表示全部
        self.data_path = Path(data_path) if data_path else None
        self.scan_record = self._load_record()
        self.subtree_lock = threading.Lock()
        self.subtrees = set()   # 等待局部扫描的（映射 key，相对目录）
        self.threads = []
        self.listings = {}
        self.listing_cleanup_at = 0.0
        self.scan_retry = {}    # 映射 key -> 全量扫描未完成后自动重扫的时间
        self.unchanged = collections.Counter()  # (目标, 映射, 说明) -> 本批无需实际操作的文件数，批末汇总成一条日志
        self.last_absence = {}  # 映射 key -> 最近一次确认有文件消失的时间
        self.quieting = set()   # 正在等待本地删除停下的映射
        self.status_lock = threading.RLock()
        self.status = {'mode': config.mode, 'running': False, 'watchers': {}, 'roots': {}, 'root_detail': {},
                       'last_event': None, 'last_scan': None, 'last_error': None,
                       'b_busy': None, 'c_busy': None,
                       'scan': None, 'last_scan_info': None,
                       'switches': {k: getattr(config, k) for k in (
                           'watch_enabled', 'transfer_event_enabled', 'scan_enabled', 'adopt_existing',
                           'copy_enabled', 'upload_enabled', 'delete_enabled', 'delete_b_enabled',
                           'delete_c_enabled')}}
        self.mapping_by_id = {m.key: m for m in config.mappings}
        self.done_times = {'b': collections.deque(maxlen=2000), 'c': collections.deque(maxlen=2000)}
        self.passes = {'b': [0, None], 'c': [0, None]}  # 已处理文件数 / 本轮开始时间
        limits = (config.api_qps, config.failure_threshold, config.cooldown_base_seconds,
                  config.cooldown_max_seconds, self._cooldown)
        if self.runtime.limiter is None:
            self.runtime.limiter = RateLimiter(*limits[:4], on_cooldown=limits[4])
        else:
            self.runtime.limiter.configure(*limits)
        self.limiter = self.runtime.limiter
        if remote is not None:
            remote.limiter, remote.stop = self.limiter, self.stop_event
            # 有 Cookie 且装了 p115client 时，一轮中的删除合并为批量请求；否则逐个经 MP 删除
            remote.batch_delete = CookieDeleter(config.p115_cookie, self.limiter, self.stop_event) \
                if config.p115_cookie and tree_export_available() else None
        self.index = RemoteIndex(config, remote, data_path, self.limiter, clock, self.stop_event,
                                 export_lock=self.runtime.export_lock) if remote is not None else None
        self.prepared = threading.Event()
        self.index_requested = threading.Event()
        self.index_force = False
        self.remote_audit = {}
        self.preparing = False
        self.outputs = {m.key: LocalOutput(m.output, config.is_video)
                        for m in config.mappings}
        for m in config.mappings:
            try:
                self.fs.bind_root(m.source)
                # 删除前必须先由本引擎完成一次成功扫描。
                self.status['roots'][m.key] = 'awaiting_scan'
            except OSError as error:
                self.status['roots'][m.key] = 'unavailable'
                self.status['root_detail'][m.key] = str(error)[:200]
                logger.warning('【启动】[%s] 本地目录不可用，挂载后会自动接上：%s（%s）', m.key, m.source, error)

    def _cooldown(self, wait):
        """115 进入风控冷却时记录日志并发出冷却提醒。"""
        logger.warning('【风控】115连续请求失败，可能触发了风控，暂停访问 %d 秒后自动恢复', wait)
        self.notifier.warning('cooldown', '115访问冷却',
                              '115同步暂停，元数据复制不受影响', '%s后自动重试' % duration_text(wait))

    def _bind_late(self, mapping):
        """启动时尚未挂载的根目录出现后再绑定，成功时返回 True。"""
        try:
            self.fs.bind_root(mapping.source)
        except OSError as error:
            self._root(mapping.key, 'unavailable', str(error)[:200])
            return False
        logger.info('【启动】[%s] 本地目录已可用：%s', mapping.key, mapping.source)
        self._root(mapping.key, 'awaiting_scan')
        self._start_watcher(mapping)
        return True

    def _start_watcher(self, mapping):
        """引擎已启动且开启实时监控时，为该映射启动一个监控线程。"""
        if self.config.watch_enabled and self.threads and not self.stop_event.is_set():
            thread = threading.Thread(target=self._watch, args=(mapping,), daemon=True, name='localmirror115')
            self.threads.append(thread)
            thread.start()

    def _root(self, key, state, detail=None):
        """线程安全地更新某映射根目录的状态和详情。"""
        with self.status_lock:
            self.status['roots'][key] = state
            self.status['root_detail'][key] = note_label(detail) if detail else detail

    # ---- 变更接收 ---------------------------------------------------
    def hint(self, path, origin='filesystem'):
        """接收一个路径变化；路径属于某映射时返回 True。"""
        return self.hint_many([path], origin)[0]

    @_activity
    def hint_many(self, paths, origin='filesystem'):
        """接收一批路径变化：目录改为局部扫描，文件入队等待稳定（整批一次写入）；返回各路径是否属于某映射。"""
        if self.stop_event.is_set():
            return [False] * len(paths)
        batch = []
        results = [self._hint_one(Path(path), origin, batch) for path in paths]
        if batch:
            self.repository.enqueue_many(batch)
            for key, relative, observation, _, _ in batch:
                # 消失的路径仅当它曾是目录（有后代记录）时才扫描，避免为消失的普通文件安排空扫描。
                if (observation['status'] in ('missing_candidate', 'unknown')
                        and self.repository.has_descendants(key, relative)):
                    self.request_subtree(self.mapping_by_id[key], relative)
            key, relative = batch[-1][:2]
            with self.status_lock:
                self.status['last_event'] = {'mapping': key, 'relative': relative, 'origin': origin,
                                             'at': self.clock()}
            self.wake.set()
        return results

    def _hint_one(self, path, origin, batch):
        """处理一个路径变化：目录安排局部扫描，文件的观测加入 batch；路径属于某映射时返回 True。"""
        if not path.is_absolute() or '..' in path.parts:
            return False
        for m in self.config.mappings:
            try:
                relative = path.relative_to(m.source).as_posix()
            except ValueError:
                continue
            if relative == '.':
                self.request_scan(reason='映射 %s 的根目录本身发生变化' % m.key)
                return True
            if self.config.excluded(relative):
                return True
            if not encodable(relative):
                return True
            self.notifier.touch()
            observation = self.fs.observe(m.source, relative)
            if observation.status == 'directory':
                self.request_subtree(m, relative)
                return True
            batch.append((m.key, relative, observation.to_dict(), self.clock() + self.config.settle_seconds, origin))
            return True
        return False

    @_activity
    def request_scan(self, keys=None, reason=''):
        """安排一次全量扫描（可限定映射），并与已待执行的扫描范围合并。"""
        logger.info('【扫描】安排全量扫描%s%s', '（映射：%s）' % '、'.join(sorted(keys)) if keys else '',
                    '：' + reason if reason else '')
        self.notifier.touch()
        with self.subtree_lock:
            if keys is None or (self.full_scan and self.scan_keys is None):
                self.scan_keys = None
            else:
                self.scan_keys = set(keys) | (self.scan_keys or set() if self.full_scan else set())
            self.full_scan = True
        self.scan_requested.set()

    # ---- 短暂重启：沿用上次全量扫描 --------------------------------------
    def _record_path(self):
        """返回扫描状态记录文件路径；未配置数据目录时返回 None。"""
        return self.data_path / 'scan_state.json' if self.data_path else None

    def _load_record(self):
        """读取扫描状态记录；文件缺失或无法读取时返回空字典。"""
        path = self._record_path()
        try:
            with open(path, encoding='utf-8') as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_record(self, **fields):
        """合并字段、刷新心跳并原子写入扫描状态记录；写入失败时忽略。"""
        path = self._record_path()
        if path is None:
            return
        with self.status_lock:
            self.scan_record.update(fields, heartbeat=self.clock())
            data = json.dumps(self.scan_record, ensure_ascii=False)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name('.%s.%d.tmp' % (path.name, threading.get_ident()))
            temp.write_text(data, encoding='utf-8')
            os.replace(temp, path)
        except OSError:
            pass  # 写失败最多导致下次启动多扫描一次

    def _baseline(self, mapping):
        """返回全量扫描结果所依赖内容（源根目录与文件规则）的哈希。"""
        return self.config._hash(self.config.revision[mapping.key], sorted(self.config.excludes),
                                 sorted(self.config.video_extensions), self.config.max_files)

    def _forget_scan(self, key):
        """删除该映射的扫描基准，使全量扫描失败后短暂重载也会重新扫描。"""
        scans = dict(self.scan_record.get('scans') or {})
        if scans.pop(key, None) is not None:
            self._save_record(scans=scans)

    def _note_scanned(self, mapping):
        """记录该映射本次全量扫描的基准与根目录身份，供短暂重启时沿用。"""
        try:
            identity = list(self.fs._root_identity(mapping.source))
        except OSError:
            return
        scans = dict(self.scan_record.get('scans') or {})
        scans[mapping.key] = {'baseline': self._baseline(mapping), 'identity': identity, 'at': self.clock()}
        self._save_record(scans=scans)

    def _resumable(self):
        """返回可沿用上次全量扫描的映射：插件只短暂停止且扫描依据与根目录身份未变。"""
        # 只认同一进程内的重载：进程整体重启期间的变化实时监控看不到。
        stopped_at = self.runtime.stopped_at
        if (self.scan_record.get('scan_pending') or stopped_at is None
                or not 0 <= self.clock() - stopped_at <= SHORT_RESTART):
            return set()
        keys = set()
        for m in self.config.mappings:
            entry = (self.scan_record.get('scans') or {}).get(m.key) or {}
            if (self.status['roots'].get(m.key) != 'awaiting_scan' or self.fs.placeholders.get(str(m.source))
                    or entry.get('baseline') != self._baseline(m) or not self.repository.has_records(m.key)):
                continue  # 数据库被清空（如重置插件数据）时扫描记录已不可信，必须重新扫描
            try:
                if list(self.fs._root_identity(m.source)) != entry.get('identity'):
                    continue
            except OSError:
                continue
            keys.add(m.key)
        return keys

    @_activity
    def request_subtree(self, mapping, relative):
        """安排只扫描某个出现、移动或消失的目录，而不是整个媒体库。"""
        self.notifier.touch()
        with self.subtree_lock:
            if any(key == mapping.key and (relative == parent or relative.startswith(parent + '/'))
                   for key, parent in self.subtrees):
                return
            self.subtrees = {(key, child) for key, child in self.subtrees
                             if key != mapping.key or not child.startswith(relative + '/')}
            self.subtrees.add((mapping.key, relative))
        self.scan_requested.set()

    @_activity
    def scan_subtree(self, mapping, relative):
        """按与全量扫描相同的方式，把单个目录下的差异加入队列。"""
        self.notifier.touch()
        prefix = relative + '/'
        with self.status_lock:
            self.status['scan'] = {'mapping': mapping.key, 'position': 1, 'mappings': 1, 'step': 'walk', 'seen': 0,
                                   'expected': None, 'dir': relative, 'queued': 0, 'started': self.clock(),
                                   'scope': relative}
        try:
            manifest = self.fs.scan(mapping.source, self.config.max_files, self.stop_event, self.config.excluded,
                                    progress=lambda seen, folder: self._scan_progress(seen=seen, dir=folder),
                                    start=relative)
            known = {k: v for k, v in self.repository.fingerprints(mapping.key, prefix).items()
                     if not self.config.excluded(k)}
            queued, _ = self._enqueue_diff(mapping, manifest, known, 'directory')
            logger.info('【扫描】目录有变化，已重新扫描：%s（现有 %d 个文件，其中 %d 个有变化）',
                        where(mapping.key, relative), len(manifest), queued)
        except InterruptedError:
            pass
        except OSError as error:
            logger.warning('【扫描】目录扫描失败：%s %s（%s），改为全量扫描', mapping.key, relative, error)
            self.request_scan(reason='目录扫描失败')  # 根目录本身存疑：退回全量扫描
        finally:
            with self.status_lock:
                self.status['scan'] = None
        self.wake.set()

    def _enqueue_diff(self, mapping, manifest, known, origin):
        """把扫描清单与已知记录的差异（新增、变化、消失）分批加入稳定队列。
        返回 (入队数, 跳过的无法编码文件名数)；插件停止时抛出 InterruptedError。"""
        due, batch, queued, skipped = self.clock() + self.config.settle_seconds, [], 0, 0

        def flush():
            """写入当前这一批并更新扫描进度。"""
            self.repository.enqueue_many(batch)
            batch.clear()
            self._scan_progress(queued=queued)

        def changed():
            """依次产出需要入队的 (相对路径, 观测)：先是新增或变化的，再是消失的。"""
            nonlocal skipped
            for relative, observation in manifest.items():
                if not encodable(relative):
                    skipped += 1  # 无法编码的文件名跳过，不阻塞整批
                elif not self._unchanged(relative, observation, known.get(relative)):
                    yield relative, observation
            for relative in known.keys() - manifest.keys():
                yield relative, self.fs.observe(mapping.source, relative)

        for relative, observation in changed():
            if self.stop_event.is_set():
                raise InterruptedError('scan_enqueue_stopped')
            batch.append((mapping.key, relative, observation.to_dict(), due, origin))
            queued += 1
            if len(batch) >= BATCH:
                flush()
        if batch:
            flush()
        return queued, skipped

    def _unchanged(self, relative, observation, known):
        """扫描结果与已记录的指纹和分类都一致时返回 True（无需入队）。
        分类按当前视频后缀判断，后缀设置变化后旧记录会重新入队。"""
        if observation.status != 'present' or known is None:
            return False
        state, size, mtime_ns, kind = known
        fp = observation.fingerprint
        return (state == 'present' and size == fp['size'] and mtime_ns == fp['mtime_ns']
                and kind == ('video' if self.config.is_video(relative) else 'meta'))

    def _scan_progress(self, **fields):
        """线程安全地更新当前扫描的进度字段（没有扫描进行时忽略）。"""
        with self.status_lock:
            if self.status['scan'] is not None:
                self.status['scan'].update(fields)

    @_activity
    def scan_once(self, keys=None):
        """全量扫描指定映射（默认全部），把差异批量入队。"""
        self.notifier.touch()
        # 先落盘待扫描标记：即使中途崩溃，重启后也不会沿用旧基准。
        self._save_record(scan_pending=True)
        started = self.clock()
        files = changes = 0
        mappings = [m for m in self.config.mappings if keys is None or m.key in keys]
        for position, m in enumerate(mappings, 1):
            if self.stop_event.is_set():
                break
            with self.status_lock:
                self.status['scan'] = {'mapping': m.key, 'position': position, 'mappings': len(mappings),
                                       'step': 'walk', 'seen': 0, 'expected': None, 'dir': '', 'queued': 0,
                                       'started': started}
            if self.status['roots'].get(m.key) == 'unavailable' and not self._bind_late(m):
                continue
            if not self.fs.healthy(m.source) and self.fs.rebind_placeholder(m.source):
                self._root(m.key, 'awaiting_scan', 'placeholder_replaced_by_mount')
                self._start_watcher(m)
            began = self.clock()
            logger.info('【扫描】[%s] 开始全量扫描：%s（只读取文件属性）', m.key, m.source)
            try:
                # 先读已知记录：遍历期间新增并已稳定的文件不在清单里，不能被误算为“消失”。
                known = self.repository.fingerprints(m.key)
                self._scan_progress(expected=len(known) or None)
                manifest = self.fs.scan(m.source, self.config.max_files, self.stop_event, self.config.excluded,
                                        progress=lambda seen, folder: self._scan_progress(seen=seen, dir=folder))
                self._scan_progress(step='compare', seen=len(manifest), dir='')
                files += len(manifest)
                excluded = {k for k in known if self.config.excluded(k)}
                if excluded:
                    logger.info('【扫描】[%s] 有 %d 个文件命中新的排除规则，停止跟踪（不会删除）', m.key, len(excluded))
                    self.repository.forget(m.key, excluded)
                    known = {k: v for k, v in known.items() if k not in excluded}
                # 意外为空的目录树绝不能作为删除基准。
                if not manifest and known:
                    raise OSError('empty_tree_requires_manual_review')
                gone = sum(1 for k, (state, *_) in known.items() if state == 'present' and k not in manifest)
                queued, skipped = self._enqueue_diff(m, manifest, known, 'scan')
                changes += queued
                with self.status_lock:
                    self.status.setdefault('skipped_names', {})[m.key] = skipped
                self._root(m.key, 'ready')
                self._note_scanned(m)
                self.scan_retry.pop(m.key, None)
                logger.info('【扫描】[%s] 全量扫描完成：%d 个文件，发现变化 %d 个，消失 %d 个，用时 %.1f 秒%s',
                            m.key, len(manifest), queued, gone, self.clock() - began,
                            '；%d 个文件名无法编码已跳过' % skipped if skipped else '')
            except InterruptedError:
                logger.info('【扫描】[%s] 全量扫描被中止（插件停止或重启）', m.key)
                self._root(m.key, 'scan_incomplete_or_unsafe', 'scan_stopped')
                self._forget_scan(m.key)
            except OSError as error:
                logger.warning('【扫描】[%s] 全量扫描未完成，暂停该映射的同步删除，%s后自动重新扫描：%s', m.key,
                               duration_text(SCAN_RETRY), note_label(str(error)))
                self._root(m.key, 'scan_incomplete_or_unsafe', str(error)[:200])
                self._forget_scan(m.key)
                self.scan_retry[m.key] = self.clock() + SCAN_RETRY
            except Exception as error:
                logger.error('【扫描】[%s] 全量扫描出错，%s后自动重新扫描：%s', m.key, duration_text(SCAN_RETRY), error,
                             exc_info=True)
                self._root(m.key, 'scan_incomplete_or_unsafe', type(error).__name__)
                self._forget_scan(m.key)
                self.scan_retry[m.key] = self.clock() + SCAN_RETRY
        with self.status_lock:
            self.status['last_scan'] = self.clock()
            self.status['scan'] = None
            self.status['last_scan_info'] = {'started': started, 'seconds': self.clock() - started,
                                             'files': files, 'changes': changes}
            info = dict(self.status['last_scan_info'])
        # 被中断时保留待扫描标记，下次启动会重新扫描。
        self._save_record(last_scan_info=info, **({} if self.stop_event.is_set() else {'scan_pending': False}))
        self.wake.set()

    @_activity
    def tick(self):
        """把已稳定的观测转入 Asset 行，仍在变化的重新入队，源不健康的延后；取满一批时返回 True。"""
        tasks = self.repository.due(self.clock(), limit=BATCH * 2)
        if not tasks:
            return False
        self.notifier.touch()
        observed, healthy = [], {}
        for task in tasks:
            if self.stop_event.is_set():
                return False
            mapping = self.mapping_by_id.get(task['mapping'])
            if mapping is None:
                continue
            if mapping.key not in healthy:  # 每批每个映射只确认一次源根，不逐个文件确认
                healthy[mapping.key] = self.fs.healthy(mapping.source)
            current = self.fs.observe(mapping.source, task['relative'], checked=True) \
                if healthy[mapping.key] else None
            observed.append((task, mapping, current))
        # 批次结束再确认一次：期间源根掉线的映射，这一批观测全部作废（视为未知），绝不推断为缺失
        lost = {key for key, ok in healthy.items() if ok and not self.fs.healthy(self.mapping_by_id[key].source)}
        if lost:
            observed = [(task, mapping, None if mapping.key in lost else current)
                        for task, mapping, current in observed]
        devices = self.repository.devices([(task['mapping'], task['relative']) for task, _, current in observed
                                           if current is not None and current.status == 'missing_candidate'])
        ready, postponed, rechecks = [], [], []
        due = self.clock() + self.config.settle_seconds
        for task, mapping, current in observed:
            if current is not None and current.status == 'missing_candidate' and not self._same_device(
                    mapping, task['relative'], devices.get((task['mapping'], task['relative']))):
                current = None  # 原所在的子挂载已不在：视为未知而不是删除
            if current is None or current.status == 'unknown':
                postponed.append(task)  # 源不健康：延后再看，绝不推断为缺失
            elif stable(current.to_dict()) != stable(task['observation']):
                rechecks.append((mapping.key, task['relative'], current.to_dict(), due, 'recheck'))
            else:
                kind = 'video' if self.config.is_video(task['relative']) else 'meta'
                ready.append((task, kind, current.to_dict()))
        if postponed:
            self.repository.postpone(postponed, self.clock() + max(60.0, self.config.settle_seconds))
        if rechecks:
            self.repository.enqueue_many(rechecks)
        settled = 0
        by_mapping = {}
        for item in ready:
            by_mapping.setdefault(item[0]['mapping'], []).append(item)
        for key, items in by_mapping.items():
            vanished = sum(1 for _, _, observation in items if observation['status'] == 'missing_candidate')
            if vanished:
                self.last_absence[key] = self.clock()
            count = sum(self.repository.settle_many(items[start:start + BATCH], self.clock())
                        for start in range(0, len(items), BATCH))
            if count:
                logger.info('【发现】[%s] %d 个文件有变化并已稳定，开始处理（新增或修改 %d 个，消失 %d 个）',
                            key, len(items), len(items) - vanished, vanished)
            settled += count
        if settled:
            self.b_wake.set()
            self.c_wake.set()
        return len(tasks) >= BATCH * 2

    # ---- 公共辅助方法 --------------------------------------------------
    def _fresh(self, mapping, asset, checked=False):
        """A 仍与规划此工作项时的指纹一致时返回 True；checked=True 表示已确认过源根健康。"""
        observation = self.fs.observe(mapping.source, asset['relative'], checked=checked)
        if asset['a_state'] == 'missing':
            return (observation.status == 'missing_candidate'
                    and self._same_device(mapping, asset['relative'], asset['a_dev']))
        if observation.status != 'present':
            return False
        fp = observation.fingerprint
        return {'size': fp['size'], 'mtime_ns': fp['mtime_ns']} == asset['a_fp']

    def _same_device(self, mapping, relative, device):
        """最近的现存祖先目录仍在原设备上时缺失才成立，防止子挂载卸载被当成删除。"""
        if device is None:
            return True
        return self.fs.ancestor_device(mapping.source, relative) == device

    def _defer(self, target, asset, seconds=DEFER):
        """本地文件仍在变化：把该目标的处理推迟若干秒（正常现象，不写日志）。"""
        self.repository.record(target, asset, **{target + '_next': self.clock() + seconds})

    def _delete_blocker(self, target, mapping, asset, root=False):
        """返回阻止删除这个条目的原因；允许删除时返回 None。
        root 为已算好的映射级结果（_root_blocker），一轮中同一映射只算一次；False 表示现算。"""
        blocker = self._root_blocker(target, mapping) if root is False else root
        if blocker:
            return blocker
        # 缺失已在稳定间隔后复查过，默认立即删除；仅配置了延迟时额外等待。
        if self.config.delete_delay_hours > 0 and \
                self.clock() - (asset['missing_since'] or self.clock()) < self.config.delete_delay_hours * 3600:
            return 'delete_delay'
        return None

    def _root_blocker(self, target, mapping):
        """映射级的删除阻止原因（试运行、开关、源根状态）；可以删除时返回 None。"""
        if not self.config.writes:
            return 'dry_run'
        if not self.config.delete_enabled:
            return 'delete_disabled'
        if not (self.config.delete_b_enabled if target == 'b' else self.config.delete_c_enabled):
            return 'delete_%s_disabled' % target
        if not self.fs.healthy(mapping.source):
            return 'source_root_unhealthy'
        root_state = self.status['roots'].get(mapping.key)
        if root_state != 'ready':
            return 'root_' + str(root_state)
        return None

    def _error(self, target, asset, note, state='error', detail=''):
        """记录一次处理失败并按退避安排重试；首次失败或冲突时发出通知。detail 为异常详情，只写日志。"""
        attempts = asset[target + '_attempts'] + 1
        label, text = TARGET_LABEL[target], note_label(note)
        reason = text + ('，详情：%s' % detail if detail else '')
        if state == 'conflict':
            logger.error('【%s】需要人工处理：%s（%s）', label, where(asset), reason)
        else:
            logger.warning('【%s】处理失败：%s（%s；第 %d 次失败，%s 后自动重试）', label, where(asset), reason,
                           attempts, duration_text(backoff(attempts)))
        fields = {target + '_note': note, target + '_attempts': attempts,
                  target + '_next': self.clock() + backoff(attempts), 'updated_at': self.clock()}
        if not (target == 'b' and asset.get('b_pending')):
            fields[target + '_state'] = state  # 未确认的上传保留它自己的状态
        if not self.repository.record(target, asset, **fields):
            return
        self._last_error(target, note, relative=asset['relative'])
        if attempts == 1 or state == 'conflict':
            self.notifier.record('failed', self._notice_name(target, asset), text)
        if state == 'conflict':
            self.notifier.warning('conflict', '有文件需要处理',
                '%s同步暂缓' % ('115' if target == 'b' else '元数据'), '检查文件冲突后重试',
                '%s：%s' % (where(asset), text))

    def _last_error(self, target, note, **extra):
        """记下最近一次错误，供状态页显示。"""
        with self.status_lock:
            self.status['last_error'] = dict(extra, target=target, note=note, at=self.clock())

    def _resolve_notice(self, target, asset):
        """曾经失败的条目已同步或进入终态时，撤销其失败通知。"""
        if not asset.get(target + '_attempts'):
            return
        row = self.repository.get(asset['id'])
        if row is None or row[target + '_state'] in ('synced',) + FINAL:
            self.notifier.resolve(self._notice_name(target, asset))

    @staticmethod
    def _notice_name(target, asset):
        """失败通知中条目的名称；记录与撤销必须完全一致。"""
        return '%s（%s）' % (where(asset), TARGET_PLACE[target])

    def _later(self, target, asset, state, note):
        """把条目置为稍后处理的状态（计划中或暂缓删除）。"""
        self._log_later(target, asset, state, note)
        self.repository.record(target, asset, **self._later_fields(target, state, note))

    def _later_fields(self, target, state, note):
        """生成稍后处理需写入的字段。"""
        # 计划只因文件或配置变化而变，两者都会立即触发重评，故每天复查一次；
        # 被阻止的删除取决于时间或根目录状态，按扫描间隔复查。
        wait = PLANNED_RECHECK if state == 'planned' else self.config.scan_seconds
        return {target + '_state': state, target + '_note': note, target + '_next': self.clock() + wait,
                'updated_at': self.clock()}

    def _log_later(self, target, asset, state, note):
        """仅在状态或原因变化时记录试运行、开关关闭或暂不删除的日志。"""
        label = TARGET_LABEL[target]
        if asset.get(target + '_note') != note or asset.get(target + '_state') != state:
            if state == 'planned':
                logger.info('【%s】%s，未执行：%s（%s）', label, '试运行' if not self.config.writes else '对应开关未开启',
                            where(asset), note_label(note))
            else:
                logger.info('【%s】暂不删除：%s（%s）', label, where(asset), note_label(note))

    def _tally(self, target, asset, what):
        """无需实际操作的结果（如目标上已有相同文件）只计数，本批结束时按映射汇总成一条日志。"""
        self.unchanged[(target, asset['mapping'], what)] += 1

    def _flush_unchanged(self, target):
        """写出本批无需实际操作的汇总日志。"""
        for key in sorted(k for k in self.unchanged if k[0] == target):
            logger.info('【%s】[%s] %d 个文件%s', TARGET_LABEL[target], key[1], self.unchanged.pop(key), key[2])

    def _finish_missing(self, asset):
        """清理已处理完的缺失条目。"""
        self.repository.drop_finished([asset])

    # ---- C：非视频文件的本地字节复制 -----------------------------
    @_activity
    def run_c(self):
        """C 工作线程的一轮：复制或删除一批到期的非视频文件；取满一批时返回 True。"""
        with self.runtime.c_lock:
            began = self.clock()
            assets = self.repository.work('c', self.clock(), WORK_BATCH)
            if assets:
                self.notifier.touch()
            for asset in self._c_plan_batch(assets):
                if self.stop_event.is_set():
                    break
                mapping = self.mapping_by_id.get(asset['mapping'])
                if mapping is not None:
                    with self._working('c', asset['relative']):
                        self._guard('c', asset, self._c_one, mapping, asset)
            self._flush_unchanged('c')
            self._pass_done('c', len(assets), began, full=len(assets) >= WORK_BATCH)
            return len(assets) >= WORK_BATCH

    def _c_plan_batch(self, assets):
        """整批判断试运行或复制关闭时的计划并一次性保存；返回需逐个处理的条目。"""
        if self.config.writes and self.config.copy_enabled:
            return assets
        rest, decided = [], []
        for asset in assets:
            if self.stop_event.is_set():
                rest.append(asset)
                continue
            # 不检查新鲜度：计划不写任何内容，文件若变化会重新入队并替换计划。
            mapping = self.mapping_by_id.get(asset['mapping'])
            if mapping is None or asset['a_state'] != 'present':
                rest.append(asset)
                continue
            out, relative = self.outputs[mapping.key], asset['relative']
            first = asset['c_fp'] is None and not asset['c_written']
            try:
                present = out.status(relative)['status'] == 'present'
                same = present and (not first or self.config.adopt_existing) and \
                    out.matches(relative, mapping.source / relative) is not None
            except (OSError, OutputError):
                rest.append(asset)  # 交给逐个处理以正确记录失败
                continue
            if same and self.config.writes:
                rest.append(asset)  # 认领需写入身份信息，交给逐个处理
                continue
            note = 'would_adopt_identical' if same else ('would_replace' if present else 'would_copy')
            decided.append((asset, note))
        self._store_plans('c', decided)
        return rest

    def _store_plans(self, target, decided):
        """记录计划日志，并在一个事务里写入全部计划状态。"""
        if not decided:
            return
        for asset, note in decided:
            if note not in NO_OP_PLANS:
                self._log_later(target, asset, 'planned', note)
            elif asset.get(target + '_note') != note or asset.get(target + '_state') != 'planned':
                self._tally(target, asset, '%s：目标上已有相同文件，无需处理' % (
                    '试运行' if not self.config.writes else '对应开关未开启'))
        self.repository.record_many(target, [(asset, self._later_fields(target, 'planned', note))
                                             for asset, note in decided])

    def _c_one(self, mapping, asset):
        """处理单个 C 条目：A 存在时复制或认领；A 消失时删除我们写入的副本（写入后被改过的保留）。"""
        out = self.outputs[mapping.key]
        relative = asset['relative']
        if asset['a_state'] == 'present':
            if not self._fresh(mapping, asset):
                return self._defer('c', asset)
            source = mapping.source / relative
            first = asset['c_fp'] is None and not asset['c_written']
            digest = out.matches(relative, source) if not first or self.config.adopt_existing else None
            if digest:
                if not self.config.writes:
                    return self._later('c', asset, 'planned', 'would_adopt_identical')
                owner = asset['c_owner'] if not first else 'adopted'
                self._tally('c', asset, '已有相同文件，记为已同步')
                self.notifier.count('adopted_c')
                self.repository.record('c', asset, c_fp=asset['a_fp'], c_written=out.identity(relative, digest),
                    c_owner=owner or 'adopted', c_state='synced', c_note='adopted_identical' if first else 'identical',
                    c_attempts=0, updated_at=self.clock())
                return
            if not (self.config.writes and self.config.copy_enabled):
                present = out.status(relative)['status'] == 'present'
                return self._later('c', asset, 'planned', 'would_replace' if present else 'would_copy')
            try:
                written = out.copy(relative, source)
            except OutputError as error:
                if str(error) != 'copy_verification_failed':
                    raise
                return self._defer('c', asset)  # 复制过程中 A 变了：不是冲突，等新版本稳定后重做
            if not self._fresh(mapping, asset):
                return self._defer('c', asset)
            if self.repository.record('c', asset, c_fp=asset['a_fp'], c_written=written, c_owner='written',
                                      c_state='synced', c_note='copied', c_attempts=0, updated_at=self.clock()):
                logger.info('【STRM】已复制：%s（%s）', where(asset), size_text(asset['a_fp'].get('size')))
                self.notifier.count('copied')
            return
        # A 已消失
        if not asset['c_written']:
            self.repository.record('c', asset, c_state='absent', c_note='never_written', updated_at=self.clock())
            return self._finish_missing(asset)
        blocker = self._delete_blocker('c', mapping, asset)
        if blocker:
            return self._later('c', asset, 'delete_planned', blocker)
        if not self._fresh(mapping, asset, checked=True):
            return self._defer('c', asset)
        result = out.remove(relative, asset['c_written'])
        state = 'kept' if result == 'changed_by_others_kept' else 'deleted'
        if result == 'deleted':
            logger.info('【STRM】已删除：%s', where(asset))
            self.notifier.count('deleted_c')
        elif result == 'changed_by_others_kept':
            logger.info('【STRM】写入后被他人修改过，保留不删：%s', where(asset))
        self.repository.record('c', asset, c_state=state, c_note=result, c_fp=None, c_written=None,
            c_owner=None, updated_at=self.clock())
        self._finish_missing(asset)

    # ---- B：上传到 115 ----------------------------------------------------
    def _listing(self, remote_dir, max_age=LISTING_TTL):
        """返回 115 目录列表，复用不超过 max_age 秒的缓存；max_age=0 表示强制实时列出。"""
        now = self.clock()
        if now >= self.listing_cleanup_at:
            for key, (created, _) in list(self.listings.items()):
                if now - created >= LISTING_TTL:
                    self.listings.pop(key, None)
            self.listing_cleanup_at = now + LISTING_TTL
        key = str(remote_dir)
        cached = self.listings.get(key)
        if cached and now - cached[0] < max_age:
            return cached[1]
        listing = self.remote.list_dir(remote_dir)
        self.listings[key] = (self.clock(), listing)
        return listing

    def _forget_listing(self, remote_dir, subtree=False):
        """插件写过这个 115 目录后丢弃它的列表缓存；subtree=True 时连同它的父目录和所有子目录一起丢弃。"""
        key = str(remote_dir)
        self.listings.pop(key, None)
        if subtree:
            self.listings.pop(str(PurePosixPath(key).parent), None)
            for other in [other for other in self.listings if other.startswith(key + '/')]:
                self.listings.pop(other, None)

    @staticmethod
    def _copies(listing, name):
        """返回目录列表中同名的文件条目（不含目录）；name=None 时返回全部文件。"""
        if listing is None:
            return []
        return [e for e in listing['entries'] if (name is None or e['name'] == name) and e['type'] == 'file']

    def _adoptable(self, asset, copies):
        """115 上恰好一个副本、大小相同且不早于 A 的版本（含容差）时返回 True。"""
        if (len(copies) != 1 or copies[0].get('duplicates', 1) > 1 or copies[0].get('size') != asset['a_fp']['size']
                or not copies[0].get('fileid')):
            return False
        mtime = copies[0].get('mtime')
        return mtime is None or float(mtime) >= asset['a_fp']['mtime_ns'] / 1e9 - MTIME_TOLERANCE

    @_activity
    def run_b(self):
        """B 工作线程的一轮：上传、认领或删除一批到期的 115 文件；取满一批时返回 True。"""
        if self.remote is None:
            return
        with self.runtime.b_lock:
            began = self.clock()
            assets = self.repository.work('b', self.clock(), WORK_BATCH)
            if assets:
                self.notifier.touch()
            ready = self._await_quiet(assets)
            try:
                rest = self._b_plan_batch(process_deleted_directories(self, ready))
                self._delete_files([asset for asset in rest if asset['a_state'] == 'missing'])
                for asset in rest:
                    if self.stop_event.is_set():
                        break
                    mapping = self.mapping_by_id.get(asset['mapping'])
                    if mapping is None or asset['a_state'] == 'missing':
                        continue
                    with self._working('b', asset['relative']):
                        self._guard('b', asset, self._b_one, mapping, asset)
            except Stopped:
                pass  # 正在停止：剩余条目留到下次启动，不算失败
            self._flush_unchanged('b')
            self._pass_done('b', len(ready), began, full=len(assets) >= WORK_BATCH)
            return len(assets) >= WORK_BATCH

    def _await_quiet(self, assets):
        """本地还在连续删除时先不删115：映射最近 DELETE_QUIET 秒内仍有文件消失，就把消失的文件推迟到删除停下后。
        MP 删除整部剧时先逐集删文件、最后删空目录，等它停下再处理，整部剧只需按目录删除一次。
        单个文件最多等 DELETE_QUIET_MAX 秒。"""
        now, waiting = self.clock(), []
        for asset in assets:
            last = self.last_absence.get(asset['mapping'])
            if asset['a_state'] != 'missing' or last is None or now - last >= DELETE_QUIET:
                continue
            cap = (asset['missing_since'] or now) + DELETE_QUIET_MAX
            if now < cap:
                waiting.append((asset, {'b_next': min(last + DELETE_QUIET, cap)}))
        quiet = {asset['mapping'] for asset, _ in waiting}
        for key in quiet - self.quieting:
            logger.info('【115】[%s] 本地正在连续删除文件，等删除停下 %d 秒后再合并删除115上的对应文件',
                        key, DELETE_QUIET)
        self.quieting = (self.quieting | quiet) - {asset['mapping'] for asset in assets
                                                   if asset['a_state'] == 'missing' and asset['mapping'] not in quiet}
        if not waiting:
            return assets
        self.repository.record_many('b', waiting)
        ids = {asset['id'] for asset, _ in waiting}
        return [asset for asset in assets if asset['id'] not in ids]

    def _guard(self, target, asset, step, *args):
        """执行单个条目的一步并按异常类型记录失败：成功时撤销其失败通知并返回结果，失败时返回 None。
        插件正在停止时抛出 Stopped，由工作线程结束本轮（不算这个文件的失败）。"""
        try:
            result = step(*args)
        except (RemoteConflict, OutputError) as error:
            self._error(target, asset, str(error), 'conflict')
        except RemoteUnknown as error:
            if isinstance(error, Stopped) or self.stop_event.is_set():
                raise Stopped('stopped') from error
            self._error(target, asset, str(error), detail=error_detail(error.__cause__))
        except OSError as error:
            self._error(target, asset, 'local_io_failed', detail=error_detail(error))
        except Exception as error:
            if target != 'b':
                raise
            # 详情只写日志且已隐去链接和凭据，绝不保存到数据库或通知。
            self._error('b', asset, 'remote_call_failed', detail=error_detail(error))
        else:
            self._resolve_notice(target, asset)
            return result
        return None

    @contextlib.contextmanager
    def _working(self, target, text, count=1):
        """在状态页标记正在处理的内容；结束后记下 count 个完成时间，用于估算剩余时间。"""
        with self.status_lock:
            self.status[target + '_busy'] = text
        try:
            yield
        finally:
            with self.status_lock:
                self.status[target + '_busy'] = None
            self.done_times[target].extend([self.clock()] * count)

    def _pass_done(self, target, processed, began, full):
        """累计本轮处理数，队列处理完（这一批没取满）才写一条汇总日志。"""
        stats = self.passes[target]
        if processed:
            stats[0] += processed
            stats[1] = stats[1] if stats[1] is not None else began
        if stats[0] and not full:
            logger.info('【%s】本批检查结束：%d 个文件，用时 %.1f 秒', TARGET_LABEL[target], stats[0],
                        self.clock() - stats[1])
            self.passes[target] = [0, None]

    def _synced_fields(self, asset, fileid, note):
        """生成把 B 标记为已同步时需写入的字段。"""
        return {'b_fp': asset['a_fp'], 'b_fileid': fileid, 'b_pending': None, 'b_state': 'synced',
                'b_note': note, 'b_attempts': 0, 'updated_at': self.clock()}

    def _synced_b(self, asset, fileid, note):
        """把 B 记为已同步，记录日志与通知，并写入 115 快照索引。"""
        if self.repository.record('b', asset, **self._synced_fields(asset, fileid, note)):
            if note.startswith('verified'):
                took = (asset.get('b_pending') or {}).get('took')
                details = [size_text(asset['a_fp'].get('size'))] + (['用时 %.1f 秒' % took] if took is not None else [])
                if note == 'verified_duplicates_removed':
                    details.append('已清理重复副本')
                logger.info('【115】已上传：%s（%s）', where(asset), '，'.join(details))
                self.notifier.count('reuploaded' if asset.get('b_note') == 'missing_on_115' else 'uploaded')
            else:
                self._tally('b', asset, '115上已有相同文件，记为已同步')
                self.notifier.count('adopted')
            mapping = self.mapping_by_id.get(asset['mapping'])
            if self.index is not None and mapping is not None:
                self.index.note(mapping, asset['relative'],
                                {'size': asset['a_fp']['size'], 'fileid': fileid, 'mtime': self.clock()})

    def _forget_remote(self, asset):
        """从快照中移除该路径，避免已删除的 B 副本被再次认领。"""
        mapping = self.mapping_by_id.get(asset['mapping'])
        if self.index is not None and mapping is not None:
            self.index.forget(mapping, asset['relative'])

    def _verify_pending(self, remote_dir, name, asset):
        """确认已上传但未确认的上传；已处理时返回 True。"""
        pending = asset['b_pending']
        copies = self._copies(self._listing(remote_dir, max_age=FRESH), name)
        size_ok = [c for c in copies if c['size'] == pending['fp']['size'] and c['fileid']]
        window = self.config.verify_window_minutes * 60
        if copies and len(size_ok) == len(copies):
            # 替换步骤上传前已删除所有同名副本，所以大小正确的副本都是我们的上传（索引延迟或重试产生）。
            keep = next((c for c in size_ok if c['fileid'] == pending.get('fileid')), None) or max(
                size_ok, key=lambda c: (float(c['mtime'] or 0), str(c['fileid'])))
            extras = [c for c in copies if c['fileid'] != keep['fileid']]
            if extras:
                logger.info('【115】发现 %d 个重复副本，保留一个、其余删除：%s', len(extras), where(asset))
                if not (self.config.writes and self.config.upload_enabled):
                    self._later('b', asset, 'planned', 'would_remove_%d_duplicate_uploads' % len(extras))
                    return True
                if len(self._delete_copies(remote_dir, name, extras)) < len(extras):
                    raise RemoteUnknown('delete_extra_copy_failed')
                self._forget_remote(asset)
            if pending.get('uploads', 1) > 1 and self.clock() - pending['at'] < window:
                # 更早的上传可能仍会出现：再等一个窗口后再次确认。
                self.repository.record('b', asset, b_state='verify', b_note='awaiting_late_copies',
                    b_next=pending['at'] + window, updated_at=self.clock())
                return True
            self._synced_b(asset, keep['fileid'], 'verified_duplicates_removed' if extras else 'verified')
            return True
        age = self.clock() - pending['at']
        if age < window:
            note = 'awaiting_remote_index' if not copies else 'awaiting_remote_index_%d_copies' % len(copies)
            wait = min(600.0, max(60.0, age / 2))  # 索引越久不出现，查得越稀
            if asset.get('b_note') != note:
                logger.info('【115】已上传，等115索引出现后确认：%s', where(asset))
            self.repository.record('b', asset, b_state='verify', b_note=note,
                b_next=self.clock() + wait, updated_at=self.clock())
            return True
        if pending.get('uploads', 1) >= MAX_UNCONFIRMED_UPLOADS:
            logger.error('【115】需要人工处理：%s（已上传 %d 次仍未在115中出现，可能被115改名，停止重传）',
                         where(asset), pending.get('uploads', 1))
            self.repository.record('b', asset, b_state='conflict',
                b_note='upload_never_confirmed_after_%d_uploads' % pending.get('uploads', 1),
                b_next=self.clock() + PLANNED_RECHECK, updated_at=self.clock())
            return True
        # 确认窗口已过：退回常规替换（删同名副本、重传、确认），而不是无限等待。
        logger.warning('【115】上传后 %d 分钟仍未在115确认，将重新上传：%s', self.config.verify_window_minutes,
                       where(asset))
        return False

    def _snapshot_decision(self, asset, snap):
        """依据 115 快照判断首次见到的文件：返回 ('adopt', item)、('plan', note)，或 (None, None) 表示需实时核对或上传。"""
        if snap.has(asset['relative']):
            item = snap.get(asset['relative'])
            if self.config.adopt_existing and (item is None or self._adoptable(asset, [item])):
                return ('adopt', item) if self.config.writes else ('plan', 'would_adopt_existing')
            return None, None
        if not (self.config.writes and self.config.upload_enabled):
            return 'plan', 'would_upload'
        return None, None

    def _b_plan_batch(self, assets):
        """把首次见到的文件整批与 115 快照比对并一次性保存结果；返回需逐个处理的条目。"""
        if self.index is None:
            return assets
        rest, plans, adopted, waiting = [], [], [], []
        for asset in assets:
            mapping = self.mapping_by_id.get(asset['mapping'])
            first = b_unseen(asset)
            snap = self.index.snapshot(mapping) if mapping is not None and first else None
            if (snap is None and first and mapping is not None and asset['a_state'] == 'present'
                    and not self.index.failing(mapping)):
                waiting.append(asset)  # 快照生成后统一唤醒，期间不逐个反复检查
                continue
            if snap is None or asset['a_state'] != 'present' or self.stop_event.is_set():
                rest.append(asset)
                continue
            action, value = self._snapshot_decision(asset, snap)
            if action == 'plan':
                plans.append((asset, value))
            elif action == 'adopt' and self._fresh(mapping, asset):  # 认领会记录 A 的指纹
                adopted.append((asset, value))
            else:
                rest.append(asset)
        if waiting:
            self.request_index()
            self.repository.record_many('b', [(asset, {'b_note': 'awaiting_remote_snapshot',
                                                       'b_next': self.clock() + 3600}) for asset in waiting])
        self._store_plans('b', plans)
        if adopted:
            stored = self.repository.record_many('b', [(asset, self._synced_fields(
                asset, item['fileid'] if item else None, 'adopted' if item else 'adopted_by_tree'))
                for asset, item in adopted])
            for (asset, item), ok in zip(adopted, stored):
                if ok:
                    self._tally('b', asset, '115上已有相同文件，记为已同步')
                    self.notifier.count('adopted')
                    self._resolve_notice('b', asset)
        return rest

    def _b_one(self, mapping, asset):
        """处理单个 A 存在的 B 条目：认领、确认或上传替换（A 消失的条目由 _delete_files 批量处理）。"""
        relative = PurePosixPath(asset['relative'])
        remote_dir, name = mapping.remote / relative.parent, relative.name  # 父目录为 . 时就是映射根
        pending = asset['b_pending']
        if not self._fresh(mapping, asset):
            return self._defer('b', asset)
        if pending and pending['fp'] == asset['a_fp'] and self._verify_pending(remote_dir, name, asset):
            return
        # 首次见到的文件已在 _b_plan_batch 中整批按快照判断过；快照只在没有115处理时生成（见 index_once），
        # 同一批处理中不会变化，这里不必再判断。
        first = b_unseen(asset)
        # 插件每次写这个目录（上传、删除）都会清掉它的列表缓存，所以缓存里的列表都取自插件最后一次写入之后：
        # 同一目录连续上传多集时，上一集确认上传用的列表直接给下一集用，每集省一次请求。
        # 快照核对发现 115 上被外部删除的文件除外：这是插件之外的变化，必须实时列出。
        listing = self._listing(remote_dir, max_age=0 if asset.get('b_note') == 'missing_on_115' else FRESH)
        copies = self._copies(listing, name)
        if asset['b_fileid'] and asset['b_fp'] is None and not pending:
            # 恢复或复查：按 id 重新确认已知副本，而不是重新上传。
            known = [c for c in copies if c['fileid'] == asset['b_fileid']]
            if len(copies) == 1 and known and self._adoptable(asset, known):
                if not self.config.writes:
                    return self._later('b', asset, 'planned', 'would_reconfirm_existing')
                return self._synced_b(asset, asset['b_fileid'], 'reconfirmed')
        if first and self.config.adopt_existing and self._adoptable(asset, copies):
            if not self.config.writes:
                return self._later('b', asset, 'planned', 'would_adopt_existing')
            return self._synced_b(asset, copies[0]['fileid'], 'adopted')
        if not (self.config.writes and self.config.upload_enabled):
            note = 'would_upload' if not copies else 'would_replace_%d_copies' % len(copies)
            return self._later('b', asset, 'planned', note)
        if copies:
            # 删除旧副本前先撤销 B 的持有记录：中断只会导致重传，不会丢副本。
            if not self.repository.record('b', asset, b_fp=None, b_fileid=None):
                return  # 期间 A 已变化
            self._forget_remote(asset)
            logger.info('【115】内容有变化，先删除115上的 %d 个旧副本（进入回收站）：%s', len(copies), where(asset))
            if len(self._delete_copies(remote_dir, name, copies)) < len(copies):
                raise RemoteUnknown('delete_old_copy_failed')
            listing = self._listing(remote_dir, max_age=0)
            if self._copies(listing, name):
                raise RemoteUnknown('old_copy_still_listed')
        folder = listing['folder'] if listing else self.remote.ensure_dir(remote_dir)
        if not self._fresh(mapping, asset):
            return self._defer('b', asset)
        previous = pending if pending and pending['fp'] == asset['a_fp'] else None
        uploads = (previous.get('uploads', 1) if previous else 0) + 1
        if uploads > 1 or asset['a_fp'].get('size', 0) >= BIG_UPLOAD:  # 大文件和重传先记一笔，便于看进度
            logger.info('【115】开始上传：%s（%s%s）', where(asset), size_text(asset['a_fp'].get('size')),
                        '，第 %d 次上传' % uploads if uploads > 1 else '')
        began = self.clock()
        result = self.remote.upload(folder, mapping.source / str(relative), name)
        self._forget_listing(remote_dir)
        if result is None:
            # 上传没有结果：下次重试会先实时列目录，115 若其实已保存，会被认领而不是重复上传。
            raise RemoteUnknown('upload_failed')
        # 在任何可能失败的操作之前先持久化已执行的上传，即使 A 期间已变化：
        # 副本无论如何都已存在于 B，不能被误认领，也不能成为孤儿。
        marker = {'fp': asset['a_fp'], 'at': self.clock(), 'fileid': result.get('fileid'), 'uploads': uploads,
                  'took': round(self.clock() - began, 1)}
        self.repository.mark_pending(asset, marker)
        if not self._fresh(mapping, asset):
            # 上传期间 A 变了，副本可能不完整：保持未确认，由新版本的替换或删除路径清理。
            return
        note = asset.get('b_note')  # 保留原因：115 缺失后的补传要单独计数
        self.repository.record('b', asset, b_state='verify', b_attempts=0,
                               b_note='uploaded_unconfirmed', updated_at=self.clock())
        current = self.repository.get(asset['id'])
        if current is None or not self._verify_pending(remote_dir, name, dict(current, b_note=note)):
            raise RemoteUnknown('upload_not_listed')

    def _delete_files(self, assets):
        """处理一批 A 已消失的条目：逐个核对并找出要删的 115 副本，合并为一次批量删除，再整批记录结果。"""
        plans, finished, roots = [], [], {}
        for asset in assets:
            if self.stop_event.is_set():
                raise Stopped('stopped')
            mapping = self.mapping_by_id.get(asset['mapping'])
            if mapping is None:
                continue
            if mapping.key not in roots:
                roots[mapping.key] = self._root_blocker('b', mapping)
            plan = self._guard('b', asset, self._delete_targets, mapping, asset, roots[mapping.key])
            if plan == 'already_gone':
                finished.append((asset, plan))
            elif plan is not None:
                plans.append((asset, plan))
        if not plans:
            return self._record_deleted(finished)
        items = [(remote_dir / name, item['fileid'], 'file') for _, (remote_dir, name, targets) in plans
                 for item in targets]
        busy = '删除115上的 %d 个文件' % len(items) if len(items) > 1 else plans[0][0]['relative']
        with self._working('b', busy, count=len(plans)):
            try:
                deleted = self.remote.delete_many(items)
            except Stopped:
                raise
            except Exception as error:
                if self.stop_event.is_set():
                    raise Stopped('stopped') from error
                logger.warning('【115】批量删除请求出错，逐个目录重新确认结果（%s）', error_detail(error))
                deleted = set()
        relisted = {}
        for asset, plan in plans:
            if self._guard('b', asset, self._deleted, asset, plan, deleted, relisted):
                finished.append((asset, 'deleted_to_recycle_bin'))
        self._record_deleted(finished)

    def _delete_targets(self, mapping, asset, root=False):
        """核对 A 已消失的条目并找出属于本插件的 115 副本。
        返回 (远端目录, 文件名, 副本列表)；115 上已没有时返回 'already_gone'；暂不删除等已记录时返回 None。"""
        relative = PurePosixPath(asset['relative'])
        remote_dir, name = mapping.remote / relative.parent, relative.name  # 父目录为 . 时就是映射根
        pending = asset['b_pending']
        if b_unseen(asset):
            self.repository.record('b', asset, b_state='absent', b_note='never_uploaded', updated_at=self.clock())
            return self._finish_missing(asset)
        blocker = self._delete_blocker('b', mapping, asset, root)
        if blocker:
            return self._later('b', asset, 'delete_planned', blocker)
        if not self._fresh(mapping, asset, checked=True):
            return self._defer('b', asset)
        ids = owned_ids(asset)
        copies = self._copies(self._listing(remote_dir, max_age=FRESH), name)
        targets = [c for c in copies if c['fileid'] in ids]
        if pending and not pending.get('fileid'):
            # 没有 id 的未确认上传：同名且大小与上传时相同的副本是我们的。
            targets += [c for c in copies if c['size'] == pending['fp']['size'] and c not in targets]
        if tree_adopted(asset):
            # 从目录树认领（不知道 id）：只有同名且同大小的才算我们的。
            targets += [c for c in copies if c['size'] == asset['b_fp']['size'] and c not in targets]
        if not targets:
            logger.info('【115】115上已不存在，无需删除：%s', where(asset))
            return 'already_gone'
        self._forget_remote(asset)
        return remote_dir, name, targets

    def _deleted(self, asset, plan, deleted, relisted):
        """确认一个条目的删除结果，成功返回 True（由调用方整批记录）。有副本没删成功时实时列出目录
        （同一目录只列一次）确认：115 上仍在则记为失败，已不在（例如被其他程序先删了）按已删除收尾。"""
        remote_dir, name, targets = plan
        key = str(remote_dir)
        failed = [item for item in targets if str(item['fileid']) not in deleted]
        if failed:
            if key not in relisted:
                relisted[key] = {str(c['fileid']) for c in self._copies(self._listing(remote_dir, max_age=0), None)}
            if any(str(item['fileid']) in relisted[key] for item in failed):
                raise RemoteUnknown('delete_failed')
        self._forget_listing(remote_dir)
        logger.info('【115】已删除（进入回收站）：%s%s', where(asset),
                    '（含 %d 个同名副本）' % len(targets) if len(targets) > 1 else '')
        self.notifier.count('deleted_b')
        return True

    def _delete_copies(self, remote_dir, name, copies):
        """删除同一文件在 115 上的若干副本（合并为一次请求），返回已删除的 fileid 集合。"""
        deleted = self.remote.delete_many([(remote_dir / name, item['fileid'], 'file') for item in copies])
        self._forget_listing(remote_dir)
        return deleted

    def _record_deleted(self, finished):
        """把 [(条目, 备注)] 的 B 整批记为已删除，清理已收尾的缺失条目并撤销其失败通知。"""
        if not finished:
            return
        stored = self.repository.record_many('b', [(asset, {
            'b_state': 'deleted', 'b_fp': None, 'b_fileid': None, 'b_pending': None, 'b_note': note,
            'updated_at': self.clock()}) for asset, note in finished])
        done = [asset for (asset, _), ok in zip(finished, stored) if ok]
        self.repository.drop_finished(done)
        for asset in done:
            self._resolve_notice('b', asset)

    # ---- 115 快照 -------------------------------------------------------
    @_activity
    def request_index(self, force=False):
        """请求刷新快照：force=True 重建所有快照（页面按钮）；否则只重建过期的。"""
        self.notifier.touch()
        self.index_force = self.index_force or force
        self.index_requested.set()

    @_activity
    def index_once(self, force=False):
        """刷新过期的（强制时为全部）快照，并据此核对已同步的行。
        每个映射的生成、发布和核对都在 115 处理锁内进行：快照只在没有上传、删除进行时生成，
        反映的是稳定状态，一批 115 处理中途也不会冒出新快照。"""
        if self.index is None:
            return
        for m in self.config.mappings:
            if self.stop_event.is_set():
                break
            if force or self.index.stale(m):
                with self.runtime.b_lock:
                    self._index_mapping(m)
        self.b_wake.set()

    def _index_mapping(self, m):
        """生成一个映射的快照，唤醒依据旧快照做出的计划，并核对已同步的行。"""
        self.notifier.touch()
        mode = MODE_LABEL.get(self.index.effective_mode()[0], '')
        logger.info('【快照】[%s] 开始生成115目录快照：%s（方式：%s，不下载文件）', m.key, m.remote, mode)
        began = self.clock()
        try:
            snap = self.index.build(m)
            self.listings.clear()  # 快照比任何缓存的目录列表都新
            woken = self.repository.wake_planned(m.key)  # 计划是依据上一份快照做出的
            if woken:
                logger.info('【快照】[%s] 有 %d 个115计划按新快照重新比对', m.key, woken)
            logger.info('【快照】[%s] 115目录快照完成：%d 条，方式：%s，用时 %.1f 秒%s', m.key, len(snap.entries),
                        MODE_LABEL.get(snap.mode, snap.mode),
                        self.clock() - began,
                        '（目录树不可用，已改用逐个详细比对）' if snap.mode != snap.requested else '')
        except Exception as error:
            if isinstance(error, Stopped) or self.stop_event.is_set():
                return  # 插件正在停止，不算失败
            logger.warning('【快照】[%s] 115目录快照生成失败：%s，稍后自动重试', m.key, describe(error))
            self.repository.wake_planned(m.key, waiting_only=True)  # 等快照的新文件改用实时列目录，不再干等
            count = self.index.failures.get(m.key, (0, 0))[0]
            if count >= 3:
                note = self.index.describe().get(m.key, {}).get('note', '')
                self.notifier.warning('snapshot_' + m.key, '115快照暂不可用',
                    '%s：连续失败 %d 次，暂用目录实时查询' % (m.key, count), '稍后自动重试', note)
            return
        self._audit(m, snap)

    def _audit(self, mapping, snap):
        """用快照核对：已同步但 115 不再列出的行安排重新上传。
        缺失过多时视快照可疑（目录不对或导出不完整），忽略本次核对。"""
        synced = self.repository.synced_on_remote(mapping.key, before=snap.started_at)
        gone = [row_id for row_id, relative in synced if not snap.has(relative)]
        if len(gone) > AUDIT_SUSPECT:
            logger.warning('【快照】[%s] 有 %d/%d 个已同步文件不在115快照里，数量过多，快照可能有误，忽略本次核对',
                           mapping.key, len(gone), len(synced))
            result = {'state': 'suspect', 'missing': len(gone), 'synced': len(synced), 'at': self.clock()}
            self.notifier.warning('audit_' + mapping.key, '115快照核对暂缓',
                '%s：%d/%d 个已同步文件未出现在快照中' % (mapping.key, len(gone), len(synced)),
                '已忽略本次核对，避免错误重传；请检查115目录与授权')
        else:
            if gone:
                logger.info('【快照】[%s] 有 %d 个已同步文件在115上已不存在，%s', mapping.key, len(gone),
                            '已安排重新上传' if self.config.writes else '试运行不处理')
            elif synced:
                logger.info('【快照】[%s] 核对 %d 个已同步文件，115上都在', mapping.key, len(synced))
            if gone and self.config.writes:
                self.repository.mark_missing_on_remote(gone, self.clock())
            result = {'state': 'ok', 'missing': len(gone), 'synced': len(synced), 'at': self.clock(),
                      'applied': bool(gone and self.config.writes)}
        self.remote_audit = dict(self.remote_audit, **{mapping.key: result})  # 整体替换，读取方看到的始终是完整结果

    def _indexer(self):
        """快照线程主循环：收到请求或定时醒来后刷新快照。"""
        while not self.stop_event.is_set():
            self.index_requested.wait(60)
            with self.notifier.activity():
                self.index_requested.clear()
                if self.stop_event.is_set():
                    break
                force, self.index_force = self.index_force, False
                try:
                    self.index_once(force=force)
                    self.index.flush()
                except Exception:
                    logger.error('【快照】115目录快照处理出错', exc_info=True)
                    self._last_error('index', 'index_failed')

    # ---- 线程 -----------------------------------------------------------
    def _loop(self, step, event, label, idle=5.0):
        """通用工作线程循环：取满一批时几乎立即继续，否则等待事件或空闲超时。"""
        if label in ('b', 'c'):
            # 启动整理（revision 对齐、停放闲置行）完成前不处理，避免按旧 revision 工作。
            while not self.prepared.wait(1) and not self.stop_event.is_set():
                pass
        while not self.stop_event.is_set():
            more = False
            try:
                more = step()
            except Exception:
                logger.error('【%s】处理线程出错，稍后自动重试', TARGET_LABEL.get(label, '稳定'), exc_info=True)
                self._last_error(label, 'worker_failed_retrying')
            if more:
                self.stop_event.wait(0.02)
                continue
            event.wait(idle)
            event.clear()

    @_activity
    def prepare(self):
        """启动整理，在稳定线程上执行以保持插件启动迅速。"""
        self.notifier.touch()
        self.repository.realign()
        settings = self.config._hash(sorted(  # 除 cookie 外的全部设置，与顺序无关
            (f.name, sorted(v) if isinstance(v, (set, frozenset)) else v)
            for f in dataclasses.fields(self.config) if f.name != 'p115_cookie'
            for v in [getattr(self.config, f.name)]))
        if self.scan_record.get('settings') != settings:
            logger.info('【启动】配置有变化，重新核对所有待执行的计划')
            self.repository.wake_planned()
            self._save_record(settings=settings)
        held = self.repository.clear_holds()
        if held:
            logger.info('【删除】旧版本暂扣的 %d 个删除将按正常流程处理（115上删除的文件进入回收站，可找回）', held)
        self.repository.reset_stability(self.clock(), self.config.settle_seconds)
        self.repository.park_idle()

    def _stability(self):
        """稳定线程入口：先执行启动整理，再循环执行 tick。"""
        try:
            self.prepare()
        except Exception:
            logger.error('【启动】启动准备出错', exc_info=True)
            self._last_error('startup', 'prepare_failed')
            self.notifier.warning('startup', '启动准备未完成', '暂不发送完成总结', '检查插件日志后重新启用')
        else:
            self.preparing = False
        self.prepared.set()
        self.b_wake.set()
        self.c_wake.set()
        self._loop(self.tick, self.wake, 'stability', idle=min(self.config.settle_seconds, 1.0))

    def _notification_status(self):
        """为汇总通知探测引擎是否仍在忙及被推迟的条目数。"""
        with self.subtree_lock:
            queued = self.full_scan or bool(self.subtrees) or self.scan_requested.is_set()
        if self.stop_event.is_set() or self.preparing or queued or self.index_requested.is_set():
            return {'busy': True}
        work = self.repository.notification_work(self.clock(),
            b_enabled=self.config.writes and (self.config.upload_enabled or
                self.config.delete_enabled and self.config.delete_b_enabled),
            c_enabled=self.config.writes and (self.config.copy_enabled or
                self.config.delete_enabled and self.config.delete_c_enabled))
        with self.status_lock:
            unhealthy = sum(state not in ('ready',) for state in self.status['roots'].values())
            watcher_errors = sum(state == 'failed_scan_fallback_only' for state in self.status['watchers'].values())
        return {'busy': bool(work['pending'] or work['active']),
                'deferred': work['deferred'] + unhealthy + watcher_errors}

    def flush_notifications(self, force=False):
        """以当前引擎状态为探测，尝试发送汇总通知。"""
        return self.notifier.flush(force=force, probe=self._notification_status)

    def _scanner(self):
        """扫描线程主循环：发送通知、定期写心跳并执行待处理的扫描。"""
        beat = 0.0
        while not self.stop_event.is_set():
            self.notifier.flush_alerts()
            self.flush_notifications()
            if self.clock() - beat >= HEARTBEAT:
                beat = self.clock()
                self._save_record()
            due = [key for key, at in list(self.scan_retry.items()) if self.clock() >= at]
            if due:
                for key in due:
                    self.scan_retry.pop(key, None)
                self.request_scan(due, reason='上次全量扫描未完成，自动重新扫描')
            if self.scan_requested.wait(0.5) and not self.stop_event.is_set():
                self._scan_pending()

    @_activity
    def _scan_pending(self):
        """执行待处理的扫描：有全量请求时全量扫描，否则逐个扫描目录。"""
        self.notifier.touch()
        self.scan_requested.clear()
        with self.subtree_lock:
            full, keys, self.full_scan, self.scan_keys = self.full_scan, self.scan_keys, False, None
            subtrees, self.subtrees = self.subtrees, set()
        try:
            if not full:
                for key, relative in sorted(subtrees):
                    mapping = self.mapping_by_id.get(key)
                    if mapping is not None and not self.stop_event.is_set():
                        self.scan_subtree(mapping, relative)
                return
            self.scan_once(keys)  # 全量扫描同时覆盖所有待扫描目录
            if keys is not None:  # 部分映射的扫描则不覆盖：保留其余目录
                for key, relative in subtrees:
                    if key not in keys:
                        self.request_subtree(self.mapping_by_id[key], relative)
        except Exception:
            logger.error('【扫描】扫描出错', exc_info=True)
            self._last_error('scan', 'scan_failed')

    def _watch(self, mapping):
        """监控线程：实时监控映射源目录并交给 hint；失败时改为依靠全量扫描。"""
        try:
            from watchfiles import watch, Change
            with self.status_lock:
                self.status['watchers'][mapping.key] = 'polling' if self.config.force_polling else 'native_requested'
            logger.info('【监控】[%s] 开始实时监控：%s（%s）', mapping.key, mapping.source,
                        '兼容模式，每 %g 秒轮询' % self.config.poll_seconds if self.config.force_polling else '性能模式，系统文件事件')
            for changes in watch(mapping.source, stop_event=self.stop_event, recursive=True,
                                 watch_filter=None, force_polling=self.config.force_polling,
                                 poll_delay_ms=int(self.config.poll_seconds * 1000),
                                 debounce=300, step=50, rust_timeout=500):
                # 目录的修改事件只是子项变化带来的 mtime 更新，子项自有事件。
                paths = [path for kind, path in changes if not (kind == Change.modified and Path(path).is_dir())]
                try:
                    self.hint_many(paths, 'filesystem')
                except Exception as error:  # 例如数据库短暂繁忙：继续监控并补扫这些变化所在的目录
                    logger.warning('【监控】处理 %d 个变化失败，稍后补扫所在目录（%s）', len(paths), type(error).__name__)
                    for parent in {Path(path).parent for path in paths}:
                        if parent != mapping.source and mapping.source in parent.parents:
                            self.request_subtree(mapping, parent.relative_to(mapping.source).as_posix())
                        else:
                            self.request_scan([mapping.key], reason='处理文件变化失败')
        except Exception as error:
            if self.stop_event.is_set():
                return
            logger.error('【监控】[%s] 实时监控失败：%s，改为靠全量扫描发现变化', mapping.key, error, exc_info=True)
            with self.status_lock:
                self.status['watchers'][mapping.key] = 'failed_scan_fallback_only'
            self.request_scan(reason='映射 %s 的实时监控失败' % mapping.key)

    def start(self):
        """启动各工作与监控线程，并沿用上次扫描结果或安排启动扫描。"""
        if self.threads:
            raise RuntimeError('Engine already started')
        self.preparing = True
        self.notifier.touch()
        self.status['running'] = True
        targets = [(self._stability, ()), (self._scanner, ()),
                   (self._loop, (self.run_c, self.c_wake, 'c')),
                   (self._loop, (self.run_b, self.b_wake, 'b'))]
        if self.index is not None:
            targets.append((self._indexer, ()))
            self.index_requested.set()
        if self.config.watch_enabled:
            targets += [(self._watch, (m,)) for m in self.config.mappings if self.fs.healthy(m.source)]
        else:
            for m in self.config.mappings:
                self.status['watchers'][m.key] = 'disabled'
        resumed = self._resumable()
        for key in resumed:
            self._root(key, 'ready')
        if resumed:
            logger.info('【启动】插件只是短暂重启（%d 秒内）且目录和文件规则未变，沿用上次全量扫描结果，不重新扫描：%s',
                        SHORT_RESTART, '、'.join(sorted(resumed)))
            with self.status_lock:
                self.status['resumed'] = sorted(resumed)
                info = self.scan_record.get('last_scan_info')
                if isinstance(info, dict) and {'started', 'seconds', 'files', 'changes'} <= set(info):
                    self.status['last_scan_info'] = dict(info)
        self._save_record()
        rest = [m.key for m in self.config.mappings if m.key not in resumed]
        if rest:
            self.request_scan(None if not resumed else rest, reason='插件启动，建立本地文件基准')
        for target, args in targets:
            thread = threading.Thread(target=target, args=args, daemon=True, name='localmirror115')
            self.threads.append(thread)
            thread.start()

    def stop(self, timeout=10):
        """停止引擎并在超时内等待线程退出；全部退出时返回 True。"""
        self.notifier.suspend()
        with self.subtree_lock:
            incomplete_scan = self.full_scan or bool(self.subtrees) or self.scan_requested.is_set()
        with self.status_lock:
            incomplete_scan = incomplete_scan or bool(self.status['scan']) or self.preparing
        self.stop_event.set()
        for event in (self.wake, self.b_wake, self.c_wake, self.scan_requested, self.index_requested):
            event.set()
        deadline = time.monotonic() + timeout
        for thread in self.threads:
            thread.join(max(0, deadline - time.monotonic()))
        stopped = all(not thread.is_alive() for thread in self.threads)
        with self.status_lock:
            self.status['running'] = not stopped
        if self.index is not None:
            self.index.flush()
        # 排队中或被中断的扫描在短暂热重载后也必须被重新发现。
        self._save_record(**({'scans': {}} if incomplete_scan else {}))
        self.runtime.stopped_at = self.clock()
        if stopped:
            logger.info('【停止】同步引擎已停止')
        else:
            logger.warning('【停止】有文件正在上传，等它完成后工作线程再退出')
        return stopped

    def snapshot(self):
        """返回供页面展示的状态副本，附带快照索引、限流器状态和处理速率。"""
        with self.status_lock:
            out = copy.deepcopy(self.status)
        if self.index is not None:
            mode, note = self.index.effective_mode()
            out['remote_index'] = {'mode': mode, 'note': note, 'mappings': self.index.describe(),
                                   'audit': copy.deepcopy(self.remote_audit)}
        out['limiter'] = self.limiter.state()
        now = self.clock()
        out['rate'] = {t: sum(1 for at in list(times) if now - at <= RATE_WINDOW) / RATE_WINDOW
                       for t, times in self.done_times.items()}
        out['now'] = now
        return out
