"""各映射 115 目录内容的快照。

- tree：通过 cookie 网页接口导出目录树，只有路径，请求数少。
- detail：每个目录一次 Open API 列表请求，含大小、id 和 mtime。

快照保存在磁盘上，按计划或按需重建，并随插件自身的删除和上传更新。
失败或不完整的构建绝不替换正常快照，失败后退避重试。
"""
import gzip
import json
import os
import threading
import time
import uuid
from itertools import islice
from contextlib import contextmanager
from pathlib import Path

from .log import logger
from .adapters import RemoteUnknown, Stopped, cleanup_export, describe, export_tree, tree_export_available

SAVE_BATCH = 5000  # 每批编码并写入 gzip 的快照条目数


def compact(entry):
    """把 detail 条目压缩为 [size, fileid, mtime, duplicates]，使大型快照保持小巧。"""
    if entry is None:
        return None
    return [entry.get('size'), entry.get('fileid'), entry.get('mtime'), entry.get('duplicates', 1)]


def expand(value):
    """把压缩值还原为条目字典；tree 模式的 None 原样返回。"""
    if value is None:
        return None
    if isinstance(value, int):          # 带重复副本数的 tree 条目
        return {'duplicates': value}
    size, fileid, mtime, duplicates = value
    return {'size': size, 'fileid': fileid, 'mtime': mtime, 'duplicates': duplicates, 'type': 'file'}


class Snapshot:
    """单个映射的 115 快照：模式、时间戳与按相对路径索引的条目。"""

    def __init__(self, mode, requested, started_at, built_at, entries):
        """保存快照数据并创建修改锁。"""
        self.mode = mode              # 实际构建的模式：'tree' | 'detail'
        self.requested = requested    # 配置所要求的模式
        self.started_at = started_at  # 列表获取时间不早于此
        self.built_at = built_at
        self.entries = entries        # {相对路径: 压缩值}；tree 模式为 None 或重复副本数
        self.lock = threading.Lock()

    def has(self, relative):
        """快照是否包含该路径。"""
        return relative in self.entries

    def get(self, relative):
        """返回该路径的条目字典，不存在时返回 None。"""
        return expand(self.entries.get(relative))

    def forget(self, relative):
        """从快照中移除单个路径。"""
        with self.lock:
            self.entries.pop(relative, None)

    def forget_prefix(self, prefix):
        """从快照中移除目录本身及其下全部条目，返回移除的条目数。"""
        prefix = prefix.rstrip('/')
        with self.lock:
            doomed = [key for key in self.entries if key == prefix or key.startswith(prefix + '/')]
            for key in doomed:
                del self.entries[key]
            return len(doomed)

    def note(self, relative, entry):
        """记录插件自身上传的文件；tree 模式只记路径，detail 模式记压缩后的条目。"""
        with self.lock:
            self.entries[relative] = None if self.mode == 'tree' else compact(entry)

    def to_json(self):
        """在锁内导出可序列化的字典（条目为副本）。"""
        with self.lock:
            return {'mode': self.mode, 'requested': self.requested, 'started_at': self.started_at,
                    'built_at': self.built_at, 'entries': dict(self.entries)}

    @classmethod
    def from_json(cls, data):
        """从持久化字典恢复快照，兼容缺少 requested/started_at 的旧数据。"""
        return cls(data['mode'], data.get('requested', data['mode']), data.get('started_at', data['built_at']),
                   data['built_at'], data['entries'])


class RemoteIndex:
    """管理各映射 115 快照的构建、加载、保存、过期判断与失效处理。"""

    def __init__(self, config, remote, data_path, limiter, clock=time.time, stop=None, export_lock=None):
        """初始化状态；已保存的快照首次使用时才加载，不拖慢插件启动。"""
        self.config, self.remote, self.limiter = config, remote, limiter
        self.clock, self.stop = clock, stop
        self.export_lock = export_lock or threading.Lock()
        self.dir = Path(data_path or '/tmp') / 'remote_index'
        self.snapshots = {}
        self.status = {}
        self.failures = {}         # 映射 -> (失败次数, 重试时间)
        self.generations = {}     # 破坏性变更后使进行中的旧构建失效
        self.changing = set()     # 目录删除进行中时不得发布中途构建的快照
        self.dirty = set()        # 内存中已修改、尚未保存的快照
        self.forgotten = {}       # 构建进行中被插件删除的路径：发布新快照前剔除
        self.lock = threading.Lock()
        self.loaded = False
        self.pending_exports = self._load_exports()  # 导出文件可能仍留在 115 根目录中的导出 id

    def _ensure_loaded(self):
        """首次使用时加载各映射已保存的快照。"""
        with self.lock:
            if self.loaded:
                return
            self.loaded = True
            for m in self.config.mappings:
                self._load(m)

    # ---- 模式选择 ----------------------------------------------------------------
    def effective_mode(self):
        """返回 (实际比对模式, 降级说明)：tree 缺少 Cookie 或 p115client 时改用 detail。"""
        if self.config.compare_mode == 'tree':
            if not self.config.p115_cookie:
                return 'detail', '目录树模式需要填写115 Cookie，已改用逐个详细比对'
            if not tree_export_available():
                return 'detail', '未安装p115client，目录树模式不可用，已改用逐个详细比对'
            return 'tree', ''
        return 'detail', ''

    # ---- 持久化 ------------------------------------------------------------------
    def _file(self, mapping):
        """返回该映射快照文件的路径。"""
        return self.dir / ('%s.json.gz' % mapping.key)

    def _load(self, mapping):
        """从磁盘加载快照，仅在远端根路径一致时采用；读取失败时忽略。"""
        path = self._file(mapping)
        try:
            with gzip.open(path, 'rt', encoding='utf-8') as handle:
                data = json.load(handle)
            if data.get('remote') == str(mapping.remote):
                self.snapshots[mapping.key] = Snapshot.from_json(data)
        except Exception:
            pass  # 文件缺失、截断或格式过旧：稍后重建，不让启动失败

    def _save(self, mapping, snapshot):
        """把快照流式写入 gzip 临时文件，fsync 后原子替换。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._file(mapping)
        temp = path.with_name('.%s.%s.tmp' % (path.name, uuid.uuid4().hex))
        data = snapshot.to_json()
        data['remote'] = str(mapping.remote)
        try:
            with open(temp, 'wb') as raw:
                # 分批写入条目：避免为 30 万以上条目生成完整的字符串副本
                with gzip.GzipFile(fileobj=raw, mode='wb') as handle:
                    entries = data.pop('entries')
                    encode = json.JSONEncoder(ensure_ascii=False, separators=(',', ':')).encode
                    handle.write((encode(data)[:-1] + ',"entries":{').encode('utf-8'))
                    items = iter(entries.items())
                    first = True
                    while True:
                        batch = [encode(key) + ':' + encode(value) for key, value in islice(items, SAVE_BATCH)]
                        if not batch:
                            break
                        handle.write(((',' if not first else '') + ','.join(batch)).encode('utf-8'))
                        first = False
                    handle.write(b'}}')
                raw.flush()
                os.fsync(raw.fileno())
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()

    # ---- 查询 --------------------------------------------------------------------
    def snapshot(self, mapping):
        """返回该映射当前的快照，没有时返回 None。"""
        self._ensure_loaded()
        return self.snapshots.get(mapping.key)

    def failing(self, mapping):
        """最近的构建失败时返回 True（调用方退回逐目录列表）。"""
        count, _ = self.failures.get(mapping.key, (0, 0))
        return count > 0

    def stale(self, mapping):
        """快照是否需要重建：不存在、模式不符或已过刷新周期；失败退避期内返回 False。"""
        self._ensure_loaded()
        _, retry_at = self.failures.get(mapping.key, (0, 0))
        if self.clock() < retry_at:
            return False
        snap = self.snapshots.get(mapping.key)
        requested, _ = self.effective_mode()
        if snap is None or snap.requested != requested:
            return True
        return self.clock() - snap.built_at >= self.config.remote_refresh_hours * 3600

    def forget(self, mapping, relative):
        """插件删除了该路径的 115 副本：从快照移除并标记待保存。
        进行中的构建发布前也剔除它，避免已删除的文件被重新认领为“115已有”。"""
        self._ensure_loaded()
        with self.lock:
            if mapping.key in self.forgotten:
                self.forgotten[mapping.key].add(relative)
            snap = self.snapshots.get(mapping.key)
            if snap is not None:
                snap.forget(relative)
                self.dirty.add(mapping.key)

    def flush(self):
        """保存内存中已修改的快照（快照线程定期调用，停止时也调用）。"""
        with self.lock:
            for key in list(self.dirty):
                mapping = next((m for m in self.config.mappings if m.key == key), None)
                snap = self.snapshots.get(key)
                if mapping is not None and snap is not None:
                    try:
                        self._save(mapping, snap)
                    except OSError:
                        continue  # 下次再试；最坏情况只是重启后重新核对该路径
                self.dirty.discard(key)

    @contextmanager
    def deleting_prefix(self, mapping, relative):
        """目录删除期间阻止发布快照，结束后使进行中的构建失效。"""
        with self.lock:
            self.changing.add(mapping.key)
        try:
            self.invalidate_prefix(mapping, relative)  # 必须在任何破坏性 I/O 之前持久化
            yield
        finally:
            with self.lock:
                self.generations[mapping.key] = self.generations.get(mapping.key, 0) + 1
                self.changing.discard(mapping.key)

    def invalidate_prefix(self, mapping, relative):
        """持久地移除被删除的子树，并使此前开始的构建失效。
        在锁内保存，慢速构建无法覆盖内存或磁盘上的快照；保存失败则删除快照文件，
        连删除也失败时抛出异常，调用方不得发出远端删除。"""
        if not relative or relative.startswith('/') or '..' in relative.split('/'):
            raise ValueError('Expected a non-root relative directory')
        with self.lock:
            self.generations[mapping.key] = self.generations.get(mapping.key, 0) + 1
            snap = self.snapshots.get(mapping.key)
            if snap is not None:
                if not snap.forget_prefix(relative) and mapping.key not in self.dirty:
                    return  # 内存和磁盘上都没有这个子树：不必重写整个快照文件
                try:
                    self._save(mapping, snap)
                    self.dirty.discard(mapping.key)
                    return
                except OSError:
                    pass
            self._file(mapping).unlink(missing_ok=True)

    def note(self, mapping, relative, entry):
        """在快照中记录插件自身上传的文件（仅内存）。"""
        snap = self.snapshots.get(mapping.key)
        if snap is not None:
            snap.note(relative, entry)

    # ---- 构建 --------------------------------------------------------------------
    def _set_status(self, mapping, **info):
        """更新该映射的构建状态。"""
        with self.lock:
            self.status[mapping.key] = info

    def _load_exports(self):
        """读取尚未清理的导出 id；文件缺失或损坏时返回空列表。"""
        try:
            data = json.loads((self.dir / 'exports.json').read_text(encoding='utf-8'))
            return [item for item in data if isinstance(item, (int, str))][-10:]
        except Exception:
            return []

    def _save_exports(self):
        """持久化待清理的导出 id，重启后仍能删除 115 根目录中残留的导出文件。"""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / 'exports.json').write_text(json.dumps(self.pending_exports[-10:]), encoding='utf-8')
        except OSError:
            pass

    def _cleanup_exports(self):
        """清理残留在 115 中的目录树导出文件，失败的最多保留最近 10 个待重试。"""
        still = []
        for export_id in list(self.pending_exports):
            try:
                if not cleanup_export(self.config.p115_cookie, export_id, self.limiter, self.stop):
                    still.append(export_id)
            except Exception:
                still.append(export_id)
        self.pending_exports = still[-10:]
        self._save_exports()

    def build(self, mapping):
        """重建单个映射的快照并返回；失败时抛出 RemoteUnknown。"""
        requested, note = self.effective_mode()
        mode = requested
        started = self.clock()
        with self.lock:
            if mapping.key in self.changing:
                raise RemoteUnknown('snapshot_directory_delete_in_progress')
            generation = self.generations.get(mapping.key, 0)
            self.forgotten[mapping.key] = set()
        self._set_status(mapping, state='building', mode=mode, note=note, started=started)
        try:
            entries = None
            if mode == 'tree':
                throttled = self.limiter.recently_failed()  # 导出前 115 是否已在限流
                try:
                    self._cleanup_exports()
                    root = self.remote.folder(mapping.remote)
                    if root is None:
                        entries = {}
                    else:
                        fileid = root.get('fileid') if isinstance(root, dict) else getattr(root, 'fileid', None)
                        if not fileid:
                            raise RemoteUnknown('tree_root_has_no_id')
                        try:
                            paths = export_tree(self.config.p115_cookie, fileid, mapping.remote, self.limiter,
                                                timeout=self.config.tree_timeout_minutes * 60, stop=self.stop,
                                                pending=self.pending_exports, lock=self.export_lock)
                        finally:
                            self._save_exports()
                        entries = {p: (None if n == 1 else n) for p, n in paths.items()}
                except Exception as error:
                    if isinstance(error, Stopped):
                        raise
                    if throttled or self.limiter.state()['cooldown_seconds']:
                        raise  # 115 正在限流：不紧接着发起大量逐目录请求，稍后整体重试
                    # Cookie 无效或导出繁忙不能卡住镜像：退回 detail 模式
                    reason = describe(error)
                    logger.warning('【快照】[%s] 目录树导出失败（%s），本次改用逐个详细比对', mapping.key, reason)
                    mode, note = 'detail', '目录树导出失败（%s），本次改用逐个详细比对' % reason
            if entries is None:
                entries = {k: compact(v) for k, v in self.remote.walk(mapping.remote, self.stop).items()}
        except Exception as error:
            with self.lock:
                self.forgotten.pop(mapping.key, None)
            if isinstance(error, Stopped):
                raise  # 插件正在停止，不算一次失败
            count, _ = self.failures.get(mapping.key, (0, 0))
            count += 1
            self.failures[mapping.key] = (count, self.clock() + min(3600.0, 300.0 * (2 ** (count - 1))))
            reason = describe(error)
            self._set_status(mapping, state='failed', mode=mode, note=reason, started=started, finished=self.clock())
            if isinstance(error, RemoteUnknown):
                raise
            raise RemoteUnknown('snapshot_build_failed') from error
        self.failures.pop(mapping.key, None)
        snap = Snapshot(mode, requested, started, self.clock(), entries)
        self._ensure_loaded()
        with self.lock:
            for relative in self.forgotten.pop(mapping.key, ()):
                entries.pop(relative, None)
            if generation != self.generations.get(mapping.key, 0):
                self.status[mapping.key] = {'state': 'invalidated', 'note': 'snapshot_changed_during_build'}
                raise RemoteUnknown('snapshot_changed_during_build')
            self.snapshots[mapping.key] = snap
            self.dirty.discard(mapping.key)
            try:
                self._save(mapping, snap)
            except OSError:
                pass  # 内存中的快照仍可供本次运行使用
        self._set_status(mapping, state='ready', mode=mode, note=note, started=started, finished=self.clock(),
                         entries=len(entries))
        return snap

    def describe(self):
        """返回各映射快照的状态摘要，供页面展示。"""

        out = {}
        with self.lock:
            status = {k: dict(v) for k, v in self.status.items()}
        for m in self.config.mappings:
            snap = self.snapshots.get(m.key)
            info = status.get(m.key, {})
            if snap is not None:
                info.setdefault('state', 'ready')
                info.update({'snapshot_mode': snap.mode, 'built_at': snap.built_at, 'entries': len(snap.entries)})
            out[m.key] = info
        return out
