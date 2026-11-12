"""插件自有的状态表，不使用 MoviePilot 的业务表。

表名带 schema 版本：变更列时新建表或提供 get_database_migrations() 迁移，create_all 不会修改已有表。
"""
import json
import threading
from contextlib import contextmanager
from typing import Optional

from sqlalchemy import (BigInteger, Float, Index, Integer, String, Text, UniqueConstraint, and_, bindparam, case,
                        delete, func, not_, or_, select, update)
from sqlalchemy.orm import Mapped, mapped_column

from app.sdk.database import plugin_declarative_base

Base = plugin_declarative_base()

FINAL = ('deleted', 'absent', 'kept')
DONE = ('synced',) + FINAL
PARKED = 1e12  # 无事可做的行的 b_next/c_next：不进入工作队列的索引范围
STUCK_ATTEMPTS = 3  # 重试中的文件失败达到该次数后，不再阻塞本轮汇总


class Asset(Base):
    """资产表：每个映射下的 A 路径一行，记录 A 的状态、指纹及 B/C 的同步状态。"""
    __tablename__ = 'lm115_asset_v1'
    __table_args__ = (UniqueConstraint('mapping', 'relative', name='uq_lm115_asset_v1_path'),
                      Index('ix_lm115_asset_v1_b_next', 'b_next', 'id'),
                      Index('ix_lm115_asset_v1_c_next', 'c_next', 'id'))
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mapping: Mapped[str] = mapped_column(String(64))
    revision: Mapped[str] = mapped_column(String(64))                 # A 根目录标识
    relative: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(8))                      # video | meta
    a_state: Mapped[str] = mapped_column(String(16))                  # present | missing
    a_fp: Mapped[Optional[str]] = mapped_column(Text)                 # {"size", "mtime_ns"}
    a_dev: Mapped[Optional[int]] = mapped_column(BigInteger)
    missing_since: Mapped[Optional[float]] = mapped_column(Float)
    # 以下三列已停用，仅为兼容已有数据库而保留
    missing_scans: Mapped[int] = mapped_column(Integer, default=0)
    missing_scan_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    missing_scan_at: Mapped[Optional[float]] = mapped_column(Float)
    held: Mapped[int] = mapped_column(Integer, default=0)             # 已停用：启动时清零（见 clear_holds）
    b_rev: Mapped[Optional[str]] = mapped_column(String(64))
    b_fp: Mapped[Optional[str]] = mapped_column(Text)                 # B 持有的 A 指纹
    b_fileid: Mapped[Optional[str]] = mapped_column(String(64))
    b_pending: Mapped[Optional[str]] = mapped_column(Text)            # 已上传未确认：{"fp", "at", "fileid", "uploads", "took"}
    b_state: Mapped[str] = mapped_column(String(24), default='')
    b_attempts: Mapped[int] = mapped_column(Integer, default=0)
    b_next: Mapped[float] = mapped_column(Float, default=0)
    b_note: Mapped[str] = mapped_column(Text, default='')
    c_rev: Mapped[Optional[str]] = mapped_column(String(64))
    c_fp: Mapped[Optional[str]] = mapped_column(Text)                 # C 持有的 A 指纹
    c_written: Mapped[Optional[str]] = mapped_column(Text)            # {"size", "mtime_ns", "sha256"}
    c_owner: Mapped[Optional[str]] = mapped_column(String(16))        # written | adopted
    c_state: Mapped[str] = mapped_column(String(24), default='')
    c_attempts: Mapped[int] = mapped_column(Integer, default=0)
    c_next: Mapped[float] = mapped_column(Float, default=0)
    c_note: Mapped[str] = mapped_column(Text, default='')
    updated_at: Mapped[float] = mapped_column(Float)


class Pending(Base):
    """稳定队列表：每个路径一行，记录最近一次未稳定的观测、来源与到期时间。"""
    __tablename__ = 'lm115_pending_v1'
    __table_args__ = (UniqueConstraint('mapping', 'relative', name='uq_lm115_pending_v1_path'),
                      Index('ix_lm115_pending_v1_due', 'due'))
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mapping: Mapped[str] = mapped_column(String(64))
    revision: Mapped[str] = mapped_column(String(64))
    relative: Mapped[str] = mapped_column(Text)
    observation: Mapped[str] = mapped_column(Text)
    due: Mapped[float] = mapped_column(Float)
    origin: Mapped[str] = mapped_column(String(64))
    scan_id: Mapped[Optional[int]] = mapped_column(BigInteger)        # 已停用，为兼容已有数据库保留


JSON_FIELDS = ('a_fp', 'b_fp', 'b_pending', 'c_fp', 'c_written')


def stable(observation):
    """提取观测中参与稳定判断的部分。
    不含 inode/ctime/dev：它们在 CIFS/FUSE 挂载上可能变化，会让文件永远无法稳定。"""
    fp = observation.get('fingerprint') or {}
    return {'status': observation.get('status'), 'size': fp.get('size'), 'mtime_ns': fp.get('mtime_ns')}


def encode(value):
    """编码为键排序的 JSON 字符串；None 保持为 None。"""
    return json.dumps(value, sort_keys=True, ensure_ascii=False) if value is not None else None


def decode(value):
    """解码 JSON 字符串；空值返回 None。"""
    return json.loads(value) if value else None


def as_dict(row):
    """把 Asset 行转换为字典并解码 JSON 字段。"""
    out = {c.name: getattr(row, c.name) for c in Asset.__table__.columns}
    for key in JSON_FIELDS:
        out[key] = decode(out[key])
    return out


def b_unseen(row):
    """115 上从未有过这个文件的副本：没有已同步指纹、没有待确认上传、没有 fileid。"""
    return row['b_fp'] is None and not row['b_pending'] and not row['b_fileid']


def tree_adopted(row):
    """按目录树认领的行：已同步但不知道 115 文件 id（也没有待确认上传），只能按名称和大小认副本。"""
    return bool(row['b_fp']) and not row['b_fileid'] and not row['b_pending']


def owned_ids(row):
    """本插件持有的 115 文件 id：已同步的 b_fileid 与待确认上传的 fileid（不含空值）。"""
    return {value for value in (row['b_fileid'], (row['b_pending'] or {}).get('fileid')) if value}


def _next_due(row, target):
    """目标需要重新处理时的 next：视频行没有 C 副本，C 侧保持停放。"""
    return PARKED if target == 'c' and row.kind == 'video' else 0


def _reset_target(row, target):
    """清空目标（b 或 c）的同步记录与重试状态，使其重新确认。"""
    if target == 'b':
        row.b_fp = row.b_fileid = row.b_pending = None
    else:
        row.c_fp = row.c_written = row.c_owner = None
    setattr(row, target + '_state', '')
    setattr(row, target + '_attempts', 0)
    setattr(row, target + '_next', _next_due(row, target))
    setattr(row, target + '_note', '')


def _under(s, column, prefix):
    """条件：column 位于以 / 结尾的目录 prefix 之下。
    用 substr 而非 LIKE，名称中的 % 和 _ 无需转义；仅 SQLite 另加范围条件走索引（其他库排序未必按字节）。"""
    exact = func.substr(column, 1, len(prefix)) == prefix
    if s.get_bind().dialect.name != 'sqlite':
        return exact
    return and_(column >= prefix, column < prefix[:-1] + chr(ord(prefix[-1]) + 1), exact)


class Repository:
    """持久化仓库：进程内唯一写入者，写操作由进程级锁串行化。"""
    def __init__(self, session_factory, revision, revision_b=None, revision_c=None, engine=None):
        """保存会话工厂与各映射的 A/B/C revision；给出 engine 时补建已有表缺少的索引。"""
        self.sessions = session_factory
        self.revision = revision
        self.revision_b = revision_b or revision
        self.revision_c = revision_c or revision
        self.lock = threading.RLock()
        if engine is not None:
            for table in (Asset.__table__, Pending.__table__):
                for index in table.indexes:
                    index.create(engine, checkfirst=True)

    def active(self, model):
        """过滤条件：只匹配各映射当前 revision 的行；没有映射时为 False。"""
        clauses = [and_(model.mapping == key, model.revision == value)
                   for key, value in self.revision.items()]
        return or_(*clauses) if clauses else False

    def queued(self, model):
        """同 active，但把映射列包在表达式里，迫使数据库走 next 索引而不是扫描 (mapping, relative) 唯一索引。"""
        clauses = [and_(func.coalesce(model.mapping, '') == key, model.revision == value)
                   for key, value in self.revision.items()]
        return or_(*clauses) if clauses else False

    def target_rev(self, target, mapping):
        """返回目标（b 或 c）在该映射下的当前 revision。"""
        return (self.revision_b if target == 'b' else self.revision_c)[mapping]

    @contextmanager
    def transaction(self):
        """在进程级锁内打开写会话：成功提交，异常回滚。"""
        with self.lock:
            session = self.sessions()
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    @contextmanager
    def reading(self):
        """只读会话，不占写锁，页面与统计查询不阻塞同步线程。"""
        session = self.sessions()
        try:
            yield session
        finally:
            session.rollback()
            session.close()

    # ---- 稳定队列 ---------------------------------------------------------
    def enqueue_many(self, items):
        """批量加入稳定队列。"""
        with self.transaction() as s:
            rows = self._by_path(s, Pending, [(item[0], item[1]) for item in items])
            for item in items:
                key = (item[0], item[1])
                rows[key] = self._enqueue_in(s, rows.get(key), *item)

    @staticmethod
    def _by_path(s, model, paths):
        """按块读取给定路径的行，返回 {(mapping, relative): row}。"""
        found, wanted = {}, {}
        for mapping, relative in paths:
            wanted.setdefault(mapping, set()).add(relative)
        for mapping, relatives in wanted.items():
            relatives = list(relatives)
            for start in range(0, len(relatives), 500):
                for row in s.scalars(select(model).where(model.mapping == mapping,
                                                         model.relative.in_(relatives[start:start + 500]))):
                    found[(mapping, row.relative)] = row
        return found

    def _enqueue_in(self, s, row, mapping, relative, observation, due, origin):
        """把观测应用到已排队的行（None 时新建）并返回该行。
        相同状态的重复提示不推迟到期时间，否则持续的提示会让文件永远等不到处理。"""
        payload = encode(observation)
        if row is None:
            row = Pending(mapping=mapping, relative=relative, revision=self.revision[mapping],
                          observation=payload, due=due, origin=origin)
            s.add(row)
        elif row.revision != self.revision[mapping] or stable(json.loads(row.observation)) != stable(observation):
            row.revision, row.observation, row.due, row.origin = self.revision[mapping], payload, due, origin
        return row

    def postpone(self, tasks, due):
        """推迟源暂时无法确认的排队任务，并标记为 postponed（不算本轮未完成工作）。"""
        ids = [task['id'] for task in tasks]
        with self.transaction() as s:
            for start in range(0, len(ids), 500):
                s.execute(update(Pending).where(Pending.id.in_(ids[start:start + 500]))
                          .values(due=due, origin='postponed'))

    def due(self, now, limit=500):
        """返回当前 revision 下已到期的排队任务，按到期时间排序。"""
        with self.transaction() as s:
            rows = s.scalars(select(Pending).where(self.active(Pending),
                Pending.due <= now).order_by(Pending.due).limit(limit)).all()
            return [dict(id=r.id, mapping=r.mapping, relative=r.relative,
                observation=json.loads(r.observation), due=r.due, origin=r.origin) for r in rows]

    def settle_many(self, items, now):
        """在一个事务中批量 settle [(task, kind, observation)]，返回成功数量。"""
        with self.transaction() as s:
            ids = [task['id'] for task, _, _ in items]
            pending = {}
            for start in range(0, len(ids), 500):
                pending.update((row.id, row) for row in s.scalars(
                    select(Pending).where(Pending.id.in_(ids[start:start + 500]))))
            assets = self._by_path(s, Asset, [(task['mapping'], task['relative']) for task, _, _ in items])
            settled = 0
            for task, kind, observation in items:
                key = (task['mapping'], task['relative'])
                ok, row = self._settle_in(s, pending.get(task['id']), assets.get(key), task, kind,
                                          observation, now)
                if ok:
                    pending.pop(task['id'], None)  # pending 行已删除，同一任务的重复项不能再 settle
                if row is not None:
                    assets[key] = row
                settled += ok
            return settled

    def _settle_in(self, s, pending, row, task, kind, observation, now):
        """先对 pending 行做 CAS 再应用观测；返回 (是否已 settle, asset 行)。"""
        key = task['mapping']
        if (pending is None or pending.revision != self.revision[key]
                or pending.observation != encode(task['observation']) or pending.due != task['due']):
            return False, row
        status = observation['status']
        if status == 'present':
            fp = encode({'size': observation['fingerprint']['size'],
                         'mtime_ns': observation['fingerprint']['mtime_ns']})
            if row is None:
                row = Asset(mapping=key, relative=task['relative'], kind=kind, revision=self.revision[key],
                            b_rev=self.revision_b[key], c_rev=self.revision_c[key],
                            a_state='present', a_fp=fp, updated_at=now,
                            b_state='', b_attempts=0, b_next=0, b_note='',
                            c_state='', c_attempts=0, c_next=PARKED if kind == 'video' else 0, c_note='')
                s.add(row)
            else:
                self._align_revisions(row, key)
                if row.a_state == 'missing':
                    # missing 期间恢复：删除可能已执行，两个目标都不可信；保留 id 与归属以便重新确认而非重传
                    for target in ('b', 'c'):
                        if getattr(row, target + '_state') in FINAL:
                            _reset_target(row, target)
                    row.b_fp = row.c_fp = None
                    row.b_state = row.c_state = ''
                changed = row.a_fp != fp or row.a_state != 'present' or row.kind != kind
                row.kind = kind  # 视频后缀设置变化时重新分类
                if changed:
                    for target in ('b', 'c'):
                        setattr(row, target + '_attempts', 0)
                        setattr(row, target + '_next', _next_due(row, target))
                        if getattr(row, target + '_state') in ('verify', 'error', 'planned', 'delete_planned'):
                            setattr(row, target + '_state', '')
                row.a_state, row.a_fp = 'present', fp
                row.missing_since, row.updated_at = None, now
            row.a_dev = observation['fingerprint'].get('dev')
        elif status == 'missing_candidate' and row is not None:
            self._align_revisions(row, key)
            if row.a_state != 'missing':
                row.a_state, row.missing_since = 'missing', now
                row.b_attempts = row.c_attempts = 0
            # 新的缺失或再次确认：立即重新评估能否删除
            if row.b_state not in FINAL:
                row.b_next = 0
            if row.c_state not in FINAL:
                row.c_next = _next_due(row, 'c')
            row.updated_at = now
        s.delete(pending)
        return True, row

    def _align_revisions(self, row, key):
        """对齐 revision：目标路径变化时重置该目标，旧目标上的同步结果对新目标无效。"""
        row.revision = self.revision[key]
        if row.b_rev != self.revision_b[key]:
            _reset_target(row, 'b')
            row.b_rev = self.revision_b[key]
        if row.c_rev != self.revision_c[key]:
            _reset_target(row, 'c')
            row.c_rev = self.revision_c[key]

    def reset_stability(self, now, settle_seconds):
        """重启后重新计时：停机期间无法证明排队文件保持稳定；旧 revision 的行直接删除。"""
        with self.transaction() as s:
            s.execute(delete(Pending).where(not_(self.active(Pending))))
            s.execute(update(Pending).where(self.active(Pending)).values(due=now + settle_seconds))

    def park_idle(self):
        """把无事可做的行的 next 停放到 PARKED，使工作队列只扫描待处理行；返回停放数量。"""
        parked = 0
        with self.transaction() as s:
            for target in ('b', 'c'):
                nxt = getattr(Asset, target + '_next')
                parked += s.execute(update(Asset).where(self.active(Asset), nxt < PARKED,
                                                        not_(and_(*self._needs_work(target))))
                                    .values({nxt: PARKED})).rowcount
        return parked

    def realign(self):
        """启动时把目标 revision 的变更一次性应用到所有行。"""
        with self.transaction() as s:
            for key in self.revision:
                rows = s.scalars(select(Asset).where(Asset.mapping == key, or_(
                    Asset.revision != self.revision[key], Asset.b_rev != self.revision_b[key],
                    Asset.c_rev != self.revision_c[key], Asset.b_rev.is_(None), Asset.c_rev.is_(None)))).all()
                for row in rows:
                    self._align_revisions(row, key)

    # ---- 资产 --------------------------------------------------------------
    def fingerprints(self, mapping, prefix=None):
        """返回扫描比对用的 {relative: (a_state, size, mtime_ns, kind)}，含 missing 行；prefix 限定目录。
        用元组而非字典：30 万行时内存差别明显。"""
        with self.reading() as s:
            query = select(Asset.relative, Asset.a_state, Asset.a_fp, Asset.kind).where(
                Asset.mapping == mapping, Asset.revision == self.revision[mapping])
            if prefix:
                query = query.where(_under(s, Asset.relative, prefix))
            out = {}
            for relative, state, fp, kind in s.execute(query):
                fp = decode(fp) or {}
                out[relative] = (state, fp.get('size'), fp.get('mtime_ns'), kind)
            return out

    def forget(self, mapping, relatives):
        """停止跟踪已被排除的路径；不会删除任何位置的文件。"""
        relatives = list(relatives)
        with self.transaction() as s:
            for start in range(0, len(relatives), 500):
                chunk = relatives[start:start + 500]
                s.execute(delete(Asset).where(Asset.mapping == mapping, Asset.relative.in_(chunk)))
                s.execute(delete(Pending).where(Pending.mapping == mapping, Pending.relative.in_(chunk)))

    def mark_pending(self, asset, marker):
        """记录已执行但未确认的上传；A 期间变化也照样记录，因为副本已存在于 B。"""
        with self.transaction() as s:
            row = s.get(Asset, asset['id'])
            if row is None or row.revision != asset['revision'] or row.b_rev != asset['b_rev']:
                return False
            row.b_pending = encode(marker)
            return True

    def synced_on_remote(self, mapping, before=None):
        """返回 B 被认为持有的 present 行 [(id, relative)]。
        before 排除快照构建期间才更新的行：该快照无法判断它们。"""
        with self.transaction() as s:
            query = select(Asset.id, Asset.relative).where(
                Asset.mapping == mapping, Asset.revision == self.revision[mapping],
                Asset.b_rev == self.revision_b[mapping], Asset.a_state == 'present',
                Asset.b_fp.isnot(None), Asset.b_pending.is_(None))
            if before is not None:
                query = query.where(Asset.updated_at < before)
            return s.execute(query).all()

    def mark_missing_on_remote(self, ids, now):
        """115 不再列出这些文件：安排仍视为已同步的行重新核对。
        保留 b_fileid，快照误报时可按 id 重新确认而不必删除重传。"""
        with self.transaction() as s:
            for chunk in range(0, len(ids), 500):
                s.execute(update(Asset).where(Asset.id.in_(ids[chunk:chunk + 500]), Asset.a_state == 'present',
                                              Asset.b_pending.is_(None))
                          .values(b_fp=None, b_state='', b_note='missing_on_115',
                                  b_next=0, b_attempts=0, updated_at=now))

    def wake_planned(self, mapping=None, waiting_only=False):
        """立即重新评估 planned 行（配置变更或某映射快照重建后），不等每日复查。
        waiting_only=True 时只唤醒等待快照的首次判断（快照生成失败时，它们改用实时列目录）。"""
        with self.transaction() as s:
            if mapping:  # 快照重建只影响 B 侧决策，含等待快照的首次判断
                which = Asset.b_note == 'awaiting_remote_snapshot'
                if not waiting_only:
                    which = or_(Asset.b_state == 'planned', which)
                return s.execute(update(Asset).where(self.active(Asset), Asset.mapping == mapping, which)
                                 .values(b_next=0)).rowcount
            s.execute(update(Asset).where(self.active(Asset), or_(
                Asset.b_state.in_(('planned', 'delete_planned')), Asset.c_state.in_(('planned', 'delete_planned'))))
                .values(b_next=0, c_next=0))

    def directory_subtree(self, mapping, relative):
        """返回目录下的全部活动记录（不是一个工作批次）及是否有未稳定的提示。
        包含目录路径本身：那里曾有的文件或提示同样阻止删除。"""
        prefix = relative.rstrip('/') + '/'
        with self.transaction() as s:
            def within(model):
                """过滤条件：当前 revision 下该目录自身或其子路径。"""
                return and_(model.mapping == mapping, model.revision == self.revision[mapping], or_(
                    model.relative == relative, _under(s, model.relative, prefix)))
            rows = s.scalars(select(Asset).where(within(Asset)).order_by(Asset.id)).all()
            pending = s.scalar(select(func.count()).select_from(Pending).where(within(Pending)))
            return [as_dict(row) for row in rows], bool(pending)

    def has_records(self, mapping):
        """当前 revision 下该映射是否有任何记录（已跟踪的文件，或等待稳定的变化）。"""
        with self.reading() as s:
            return any(s.scalar(select(model.id).where(
                model.mapping == mapping, model.revision == self.revision[mapping]).limit(1)) is not None
                for model in (Asset, Pending))

    def has_descendants(self, mapping, relative):
        """当前 revision 下该目录之下是否还有已跟踪的资产。"""
        prefix = relative.rstrip('/') + '/'
        with self.reading() as s:
            return s.scalar(select(Asset.id).where(
                Asset.mapping == mapping, Asset.revision == self.revision[mapping],
                _under(s, Asset.relative, prefix)).limit(1)) is not None

    def finish_directory_delete(self, assets, now, note='deleted_to_recycle_bin'):
        """在一个事务中应用目录删除结果；返回已标记为删除的资产。
        note=None 表示远端结果未知：missing 行保持可重试，但持有受影响 id 的已恢复源仍须重新确认 B。
        只按 CAS 修改 B：不覆盖 C、更新的源或上传、新的映射/目标 revision。"""
        done = []
        with self.transaction() as s:
            for start in range(0, len(assets), 500):
                batch = assets[start:start + 500]
                rows = {row.id: row for row in s.scalars(select(Asset).where(
                    Asset.id.in_([asset['id'] for asset in batch])))}
                for asset in batch:
                    row = rows.get(asset['id'])
                    if (row is None or row.revision != asset['revision'] or row.b_rev != asset['b_rev']
                            or row.revision != self.revision.get(row.mapping)
                            or row.b_rev != self.revision_b.get(row.mapping)):
                        continue
                    if (note is not None and row.a_state == asset['a_state'] == 'missing'
                            and row.a_fp == encode(asset['a_fp']) and row.missing_since == asset['missing_since']
                            and row.b_fileid == asset['b_fileid'] and row.b_pending == encode(asset['b_pending'])
                            and row.b_fp == encode(asset['b_fp'])):
                        row.b_fp = row.b_fileid = row.b_pending = None
                        row.b_state, row.b_note = 'deleted', note
                        row.b_next, row.b_attempts, row.updated_at = PARKED, 0, now
                        done.append(asset)
                    elif row.a_state == 'present':
                        ids = {str(value) for value in owned_ids(asset)}
                        pending = decode(row.b_pending)
                        current_ids = {str(value) for value in (row.b_fileid, (pending or {}).get('fileid')) if value}
                        if (current_ids and current_ids <= ids
                                and (not pending or pending.get('fileid'))):
                            # 该上传不再等待索引；保留其 id 以重新列目录确认，而非仅凭名称认领
                            if not row.b_fileid:
                                row.b_fileid = str(pending['fileid'])
                            row.b_pending = None
                            row.b_fp, row.b_state, row.b_next, row.b_attempts = None, '', 0, 0
                            row.b_note, row.updated_at = 'directory_delete_recheck', now
        return done

    def devices(self, paths):
        """返回 {(mapping, relative): 记录的 A 设备号}。"""
        with self.reading() as s:
            return {key: row.a_dev for key, row in self._by_path(s, Asset, paths).items()}

    def clear_holds(self):
        """清除旧版本“批量消失暂扣”留下的标记（该机制已取消），返回其中仍待删除的行数。"""
        with self.transaction() as s:
            waiting = s.scalar(select(func.count()).select_from(Asset).where(
                self.active(Asset), Asset.held == 1, Asset.a_state == 'missing'))
            s.execute(update(Asset).where(Asset.held != 0).values(held=0))
            return waiting

    def get(self, asset_id):
        """按 id 读取资产字典；不存在时返回 None。"""
        with self.transaction() as s:
            row = s.get(Asset, asset_id)
            return as_dict(row) if row is not None else None

    def _needs_work(self, target, scope=None):
        """返回 worker 队列、进度与完成计数共用的待处理条件；scope 默认为 active。"""
        fp, state = {'b': (Asset.b_fp, Asset.b_state), 'c': (Asset.c_fp, Asset.c_state)}[target]
        conditions = [(scope or self.active)(Asset), or_(
            and_(Asset.a_state == 'present', or_(fp.is_(None), fp != Asset.a_fp)),
            and_(Asset.a_state == 'missing', state.notin_(FINAL)))]
        if target == 'c':
            conditions.append(Asset.kind == 'meta')
        return conditions

    def work(self, target, now, limit=100):
        """返回已到期且目标需要处理（副本与 A 不一致或 A 已消失）的资产。"""
        conditions = self._needs_work(target, self.queued)
        nxt = getattr(Asset, target + '_next')
        with self.transaction() as s:
            query = select(Asset).where(*conditions, nxt <= now)
            rows = s.scalars(query.order_by(nxt, Asset.id).limit(limit)).all()
            return [as_dict(r) for r in rows]

    def remaining(self, now):
        """返回 {'b'|'c': (待处理数, 已到期数)}。"""
        out = {}
        with self.reading() as s:
            for target in ('b', 'c'):
                nxt = getattr(Asset, target + '_next')
                # 待处理行的 next 必小于 PARKED：加此条件走索引，不扫描整表
                out[target] = tuple(s.execute(select(func.count(), func.count(case((nxt <= now, 1))))
                                             .select_from(Asset)
                                             .where(nxt < PARKED, *self._needs_work(target, self.queued))).one())
        return out

    def notification_work(self, now, *, b_enabled=True, c_enabled=True):
        """统计判断通知能否发送所需的工作量；推迟的计划只算暂缓。"""
        out = {'pending': 0, 'active': 0, 'deferred': 0}
        with self.reading() as s:
            out['pending'] = s.scalar(select(func.count()).select_from(Pending).where(
                self.active(Pending), Pending.origin != 'postponed'))
            for target, enabled in (('b', b_enabled), ('c', c_enabled)):
                state, nxt, note = [getattr(Asset, target + '_' + suffix)
                                    for suffix in ('state', 'next', 'note')]
                needed = self._needs_work(target, self.queued)
                # 删除延迟可能长达数天，只算暂缓；目录删除很快重试，仍算本轮工作
                retry_delete = and_(state == 'delete_planned', func.substr(note, 1, 9) == 'directory')
                attempts = getattr(Asset, target + '_attempts')
                actionable = and_(
                    state != 'conflict',
                    # 反复失败的文件只报告不等待：后台继续重试，汇总照常发送
                    or_(state != 'error', attempts < STUCK_ATTEMPTS),
                    or_(state != 'planned', nxt <= now),
                    or_(state != 'delete_planned', retry_delete))
                total, active = s.execute(select(func.count(), func.count(case((actionable, 1))))
                                          .select_from(Asset).where(nxt < PARKED, *needed)).one()
                if not enabled:
                    active = 0
                out['active'] += active
                out['deferred'] += total - active
        return out

    def record(self, target, asset, **fields):
        """保存目标结果；A 与 revision 自开始处理后有变化时放弃（返回 False）。"""
        with self.transaction() as s:
            return self._record_in(s, target, asset, fields)

    def record_many(self, target, items):
        """批量 record [(asset, fields)]，每项返回 bool。
        同组字段只执行一条带相同 CAS 条件的 UPDATE，整批开销与单行相当。"""
        table = Asset.__table__
        rev_col = table.c.b_rev if target == 'b' else table.c.c_rev
        results = [False] * len(items)
        groups, single = {}, []
        items = [(asset, self._parked(target, asset, fields)) for asset, fields in items]
        for i, (asset, fields) in enumerate(items):
            if (asset['revision'] != self.revision.get(asset['mapping'])
                    or asset[target + '_rev'] != self.target_rev(target, asset['mapping'])):
                continue  # 过期的工作项，record() 同样会拒绝
            if asset['a_fp'] is None:
                single.append(i)  # SQL 中 NULL 不相等，改为逐行检查
            else:
                groups.setdefault(tuple(sorted(fields)), []).append(i)
        with self.transaction() as s:
            for keys, indexes in groups.items():
                statement = update(table).where(
                    table.c.id == bindparam('_id'), table.c.revision == bindparam('_rev'),
                    rev_col == bindparam('_trev'), table.c.a_fp == bindparam('_afp'),
                    table.c.a_state == bindparam('_astate')).values({k: bindparam('v_' + k) for k in keys})
                params = []
                for i in indexes:
                    asset, fields = items[i]
                    row = {'_id': asset['id'], '_rev': asset['revision'], '_trev': asset[target + '_rev'],
                           '_afp': encode(asset['a_fp']), '_astate': asset['a_state']}
                    row.update({'v_' + k: encode(v) if k in JSON_FIELDS else v for k, v in fields.items()})
                    params.append(row)
                done = s.execute(statement, params).rowcount
                if s.get_bind().dialect.supports_sane_multi_rowcount and done == len(params):
                    for i in indexes:
                        results[i] = True
                    continue
                # 驱动不支持批量行数或有行未匹配：读回这一组逐项确认

                check = {r.id: r for r in s.execute(select(table).where(
                    table.c.id.in_([items[i][0]['id'] for i in indexes])))}
                for i in indexes:
                    current = check.get(items[i][0]['id'])
                    results[i] = current is not None and all(
                        getattr(current, k) == (encode(v) if k in JSON_FIELDS else v)
                        for k, v in items[i][1].items())
            for i in single:
                results[i] = self._record_in(s, target, *items[i])
        return results

    @staticmethod
    def _parked(target, asset, fields):
        """写入后目标已无事可做且调用方未指定 next 时，把 next 停放到 PARKED。"""
        if fields.get(target + '_state') not in DONE or target + '_next' in fields:
            return fields
        merged = dict(asset, **fields)
        if merged['a_state'] == 'present' and (merged[target + '_fp'] is None
                                               or merged[target + '_fp'] != merged['a_fp']):
            return fields
        return dict(fields, **{target + '_next': PARKED})

    def _record_in(self, s, target, asset, fields):
        """在给定会话中做 record 的 CAS 检查并写入；条件不满足时返回 False。"""
        fields = self._parked(target, asset, fields)
        row = s.get(Asset, asset['id'])
        rev_col = row.b_rev if row is not None and target == 'b' else (row.c_rev if row is not None else None)
        if (row is None or row.revision != asset['revision'] or row.revision != self.revision.get(row.mapping)
                or rev_col != self.target_rev(target, row.mapping) or rev_col != asset[target + '_rev']
                or row.a_fp != encode(asset['a_fp']) or row.a_state != asset['a_state']):
            return False
        for key, value in fields.items():
            if key in JSON_FIELDS:
                value = encode(value)
            setattr(row, key, value)
        fp = getattr(row, target + '_fp')
        if getattr(row, target + '_next') >= PARKED and row.a_state == 'present' and (fp is None or fp != row.a_fp) \
                and (target == 'b' or row.kind == 'meta'):
            setattr(row, target + '_next', 0)  # 已停放的行因本次写入重新需要处理：放回工作队列
        return True

    def drop_finished(self, assets):
        """删除 B/C 均已到终态的已消失路径记录（video 行没有 C）。"""
        by_revision = {}
        for asset in assets:
            by_revision.setdefault(asset['revision'], []).append(asset['id'])
        with self.transaction() as s:
            for revision, ids in by_revision.items():
                for start in range(0, len(ids), 500):
                    s.execute(delete(Asset).where(
                        Asset.id.in_(ids[start:start + 500]), Asset.revision == revision,
                        Asset.a_state == 'missing', Asset.b_state.in_(FINAL),
                        or_(Asset.kind == 'video', Asset.c_state.in_(FINAL))))

    # ---- 状态 --------------------------------------------------------------
    def counts(self):
        """状态统计：B/C 各状态计数及总数、missing 和排队数。"""
        with self.reading() as s:
            out = {}
            for target, col in (('b', Asset.b_state), ('c', Asset.c_state)):
                query = select(col, func.count()).where(self.active(Asset))
                if target == 'c':
                    query = query.where(Asset.kind == 'meta')
                out[target] = {(k or 'todo'): n for k, n in s.execute(query.group_by(col)).all()}
            out['total'], out['missing'] = s.execute(select(
                func.count(), func.count(case((Asset.a_state == 'missing', 1))))
                .select_from(Asset).where(self.active(Asset))).one()
            out['pending'] = s.scalar(select(func.count()).select_from(Pending).where(self.active(Pending)))
            return out

    def note_counts(self):
        """返回计划中行按 note 的计数 {'b': {note: n}, 'c': ...}，即试运行将执行的操作。"""
        with self.reading() as s:
            out = {}
            for target, state, note in (('b', Asset.b_state, Asset.b_note), ('c', Asset.c_state, Asset.c_note)):
                query = select(note, func.count()).where(self.active(Asset), state.in_(('planned', 'delete_planned')))
                if target == 'c':
                    query = query.where(Asset.kind == 'meta')
                out[target] = {k or '': n for k, n in s.execute(query.group_by(note)).all()}
            return out

    def notable(self, limit=100):
        """返回需要关注的行：错误、冲突、未确认上传与删除。"""
        problem_b = ('error', 'conflict', 'verify', 'delete_planned', 'kept')
        problem_c = ('error', 'conflict', 'delete_planned', 'kept')
        with self.reading() as s:
            rows = s.scalars(select(Asset).where(self.active(Asset), or_(
                Asset.b_state.in_(problem_b), Asset.c_state.in_(problem_c),
                Asset.a_state == 'missing')).order_by(Asset.updated_at.desc()).limit(limit)).all()
            return [as_dict(r) for r in rows]
