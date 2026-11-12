"""源目录整体消失时，把对应 115 目录整体删入回收站的保守快速路径。

在 run_b 的 worker 锁内、逐文件规划之前调用。实时树无法完全确认时，只把按 id 归属的行
（或实时名称唯一且大小匹配的树认领行）交回逐文件路径，绝不仅凭名称归属；
暂时性失败让整个分组本轮不进入逐文件路径。
"""
from contextlib import ExitStack
from pathlib import PurePosixPath

from .adapters import RemoteUnknown, Stopped
from .log import logger, where
from .persistence import FINAL, owned_ids, tree_adopted


class _UnsafeTree(Exception):
    """实时列表不是一棵完全已知的树，只允许安全的逐文件删除。"""


def _fileid(value):
    """把值规范化为正整数 fileid 字符串；无效值（包括 bool）返回 None。"""
    text = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ''
    return str(int(text)) if text.isascii() and text.isdigit() and int(text) > 0 else None


def _name(value):
    """判断是否为安全的单级名称：非空、不是 . 或 ..，且不含路径分隔符或 NUL 字符。"""
    return (isinstance(value, str) and value not in ('', '.', '..')
            and not any(char in value for char in ('/', '\\', '\x00')))


def _relative(value):
    """判断相对路径的每一级是否都是安全名称。"""
    return isinstance(value, str) and all(_name(part) for part in value.split('/'))


def _ids(row):
    """返回行中有效的 B fileid 集合（b_fileid 与 pending 上传的 fileid）。"""
    return {_fileid(value) for value in owned_ids(row)} - {None}


def _owned(row):
    """判断行是否按 id 归属（或从未有 B 副本）；无 id 的大小认领不算，不交回逐文件路径。"""
    pending = row['b_pending']
    if (row['b_fileid'] and not _fileid(row['b_fileid'])) or (pending and not _fileid(pending.get('fileid'))):
        return False
    return bool(_ids(row)) or not (row['b_fileid'] or row['b_fp'] or pending)


def _tree_file_fallback(engine, mapping, row, memo):
    """为树认领的行固定唯一实时副本的 id 后交给逐文件路径；无法确认时返回 None。
    只传实时 id，避免之后的列表把删除扩大到未知的同名副本；绝不触发目录删除。"""
    if not tree_adopted(row):
        return None
    path = PurePosixPath(row['relative'])
    listing = _list(engine, memo, mapping.remote / path.parent)  # 本轮已列过的目录不再重复请求
    if listing is None:
        return row  # 文件夹已不存在：逐文件路径会将其记录为已消失
    copies = [item for item in _entries(listing) if item['name'] == path.name]
    if not copies:
        return row  # 没有剩余的同名副本：无需删除，由逐文件路径收尾
    if (len(copies) == 1 and copies[0].get('type') == 'file' and copies[0].get('duplicates', 1) == 1
            and copies[0].get('size') == row['b_fp'].get('size') and _fileid(copies[0].get('fileid'))):
        return dict(row, b_fileid=_fileid(copies[0]['fileid']))
    return None


def _highest_missing(engine, mapping, relative, observations):
    """沿父目录链向上查找源端已消失的最高一级目录（不含映射根）；状态不明确时返回 None。"""
    if not _relative(relative):
        return None
    highest = None
    for parent in PurePosixPath(relative).parents:
        if str(parent) == '.':
            break  # 绝不提议删除映射根目录
        key = (mapping.key, str(parent))
        if key not in observations:
            observations[key] = engine.fs.observe(mapping.source, str(parent)).status
        status = observations[key]
        if status == 'missing_candidate':
            highest = str(parent)
        elif status == 'directory':
            break
        else:
            return None  # 状态未知、符号链接，或父目录链中有文件
    return highest


def _wait(engine, rows, reason, until=None):
    """把仍待删除的 missing 行记为 delete_planned 并延后处理（可指定下次时间）。"""
    fields = engine._later_fields('b', 'delete_planned', reason)
    if until is not None:
        fields['b_next'] = until
    engine.repository.record_many('b', [(row, fields) for row in rows
                                       if row['a_state'] == 'missing' and row['b_state'] not in FINAL])


def _retry(engine, rows, note):
    """把仍待删除的 missing 行记为错误，以便之后重试。"""
    for row in rows:
        if row['a_state'] == 'missing' and row['b_state'] not in FINAL:
            engine._error('b', row, note)


def _blocker(engine, mapping, relative, rows):
    """检查目录删除的阻止条件；返回阻止原因，可以删除时返回 None。"""
    if engine.stop_event.is_set():
        return 'directory_stopped'
    if engine.fs.observe(mapping.source, relative).status != 'missing_candidate':
        return 'directory_source_changed'
    root = engine._root_blocker('b', mapping)  # 映射级检查（含源根健康）整组只做一次
    for row in rows:
        if (row['a_state'] != 'missing' or not _relative(row['relative'])
                or not row['relative'].startswith(relative + '/')
                or row['b_rev'] != engine.repository.target_rev('b', mapping.key)
                or engine.config.excluded(row['relative'])):
            return 'directory_records_changed'
        blocker = engine._delete_blocker('b', mapping, row, root)
        if blocker:
            return blocker
        if not engine._fresh(mapping, row, checked=True):
            return 'directory_source_changed'
    return None


TRANSIENT = ('directory_source_changed', 'directory_stopped', 'directory_records_changed')

# 只比较源与 B 的标识和调度字段：C 可能并发完成
_GUARD = ('id', 'revision', 'b_rev', 'relative', 'a_state', 'a_fp', 'a_dev', 'missing_since',
          'b_fp', 'b_fileid', 'b_pending', 'b_state', 'b_next', 'b_attempts')


def _unchanged(engine, mapping, relative, rows):
    """确认目录子树记录与开始时一致、没有未稳定的提示，且仍无阻止条件。"""
    current, pending = engine.repository.directory_subtree(mapping.key, relative)
    return (not pending and len(current) == len(rows)
            and all(all(old[key] == new[key] for key in _GUARD) for old, new in zip(rows, current))
            and _blocker(engine, mapping, relative, current) is None)


def _entries(listing):
    """校验并返回列表条目；列表未确认时抛出 RemoteUnknown，名称不安全时抛出 _UnsafeTree。"""
    if not isinstance(listing, dict) or not isinstance(listing.get('entries'), (list, tuple)):
        raise RemoteUnknown('directory_listing_unconfirmed')
    for item in listing['entries']:
        if not isinstance(item, dict) or not _name(item.get('name')):
            raise _UnsafeTree()
    return listing['entries']


def _folder_id(listing):
    """返回列表所属文件夹的有效 fileid；无效时抛出 _UnsafeTree。"""
    folder = listing.get('folder')
    value = folder.get('fileid') if isinstance(folder, dict) else getattr(folder, 'fileid', None)
    fileid = _fileid(value)
    if not fileid:
        raise _UnsafeTree()
    return fileid


def _list(engine, memo, path):
    """本轮内同一 115 目录只列一次（多个待删目录共享祖先目录的列表）。"""
    key = str(path)
    if key not in memo:
        memo[key] = engine.remote.list_dir(path)
    return memo[key]


def _live_tree(engine, mapping, relative, rows, memo):
    """校验实时 115 子树，返回 (目录 id, 文件数)；(None, 0) 表示已确认不存在。
    逐级列出祖先链（get_file_item 可能在同名目录中任选其一）；名称和 id 必须唯一，
    目录只能是已记录文件的祖先。"""
    expected = {row['relative']: row for row in rows}
    allowed_dirs = {str(parent) for row in rows for parent in PurePosixPath(row['relative']).parents}
    known_ids = [fileid for row in rows for fileid in _ids(row)]
    if len(known_ids) != len(set(known_ids)):
        raise _UnsafeTree()
    for row in rows:
        # 树认领的行没有 id：下面用完整实时列表按记录的大小重新校验
        if not _owned(row) and not tree_adopted(row):
            raise _UnsafeTree()
    path = mapping.remote
    listing = _list(engine, memo, path)
    seen_ids = set()
    if listing is None:
        return None, 0
    for part in PurePosixPath(relative).parts:
        parent_id = _folder_id(listing)
        if parent_id in seen_ids:
            raise _UnsafeTree()
        seen_ids.add(parent_id)
        matches = [item for item in _entries(listing) if item['name'] == part]
        if not matches:
            return None, 0
        if (len(matches) != 1 or matches[0].get('type') != 'dir'
                or matches[0].get('duplicates', 1) != 1 or not _fileid(matches[0].get('fileid'))):
            raise _UnsafeTree()
        path = path / part
        listing = _list(engine, memo, path)
        if listing is None:
            raise RemoteUnknown('directory_listing_changed')
        if _folder_id(listing) != _fileid(matches[0]['fileid']):
            raise _UnsafeTree()
    top_id = _folder_id(listing)
    queue, count = [(relative, listing)], 0
    while queue:
        if engine.stop_event.is_set():
            raise Stopped('stopped')
        prefix, listing = queue.pop()
        folder_id = _folder_id(listing)
        if folder_id in seen_ids:
            raise _UnsafeTree()
        seen_ids.add(folder_id)
        names = set()
        for item in _entries(listing):
            name, fileid = item['name'], _fileid(item.get('fileid'))
            if name in names or not fileid or fileid in seen_ids or item.get('duplicates', 1) != 1:
                raise _UnsafeTree()
            names.add(name)
            child = prefix + '/' + name
            if item.get('type') == 'dir':
                if child not in allowed_dirs:
                    raise _UnsafeTree()  # 包括未知的空目录
                sub = _list(engine, memo, mapping.remote / child)
                if sub is None:
                    raise RemoteUnknown('directory_listing_changed')
                if _folder_id(sub) != fileid:
                    raise _UnsafeTree()
                queue.append((child, sub))
            elif item.get('type') == 'file' and child in expected:
                row = expected[child]
                tree_match = (tree_adopted(row) and item.get('size') is not None
                              and item['size'] == row['b_fp'].get('size'))
                if fileid not in _ids(row) and not tree_match:
                    raise _UnsafeTree()
                seen_ids.add(fileid)
                count += 1
            else:
                raise _UnsafeTree()
    return top_id, count


def _prepare(engine, mapping, relative, rows, fences, memo):
    """核验一个待删目录（本地记录未变，115 上实时目录树完全是已知文件），返回删除计划；
    已处理（已不存在、需等待）时返回 None。快照栅栏在 fences 中保持到本轮删除结束，期间快照不会重新发布这些路径。"""
    remote_path = mapping.remote / relative
    if engine.index is not None:
        fences.enter_context(engine.index.deleting_prefix(mapping, relative))
    engine._forget_listing(remote_path, subtree=True)
    if not _unchanged(engine, mapping, relative, rows):
        _wait(engine, rows, 'directory_source_changed', engine.clock() + 30)
        return None
    fileid, count = _live_tree(engine, mapping, relative, rows, memo)
    if fileid is None:
        _finish(engine, rows, 'already_gone')
        return None
    return {'mapping': mapping, 'relative': relative, 'rows': rows, 'fileid': fileid, 'count': count,
            'path': remote_path}


def _finish(engine, rows, note):
    """记录目录删除结果并清理已收尾的行；note=None 表示结果不明，行保持可重试。"""
    done = engine.repository.finish_directory_delete(rows, engine.clock(), note)
    for row in done:
        engine._resolve_notice('b', row)
    engine.repository.drop_finished(done)


def _execute(engine, plans):
    """把本轮核验通过的目录合并为一次批量删除（无批量通道时逐个删除），再分别记录结果。"""
    if not plans:
        return
    try:
        deleted = engine.remote.delete_many([(plan['path'], plan['fileid'], 'dir') for plan in plans])
    except Stopped:
        raise
    except Exception:
        deleted = set()
    for plan in plans:
        engine._forget_listing(plan['path'], subtree=True)
        if plan['fileid'] in deleted:
            engine.notifier.count('deleted_dirs_b')
            engine.notifier.count('deleted_b', plan['count'])
            logger.info('【115】已删除目录（进入回收站）：%s/（含 %d 个文件，按目录一次删除）',
                        where(plan['mapping'].key, plan['relative']), plan['count'])
            for row in plan['rows']:
                if row['b_state'] not in FINAL:
                    logger.info('【115】已删除（随目录）：%s', where(row))
            _finish(engine, plan['rows'], 'deleted_to_recycle_bin')
        else:
            # 请求失败或结果不明：115 可能已删除只是响应丢失，先按不明处理再安排重试
            _finish(engine, plan['rows'], None)
            _retry(engine, plan['rows'], 'directory_delete_failed')


def process_deleted_directories(engine, assets):
    """处理可安全整体删除的目录分组，返回仍交给现有逐文件路径处理的资产。"""
    config = engine.config
    if not (config.writes and config.delete_enabled and config.delete_b_enabled
            and engine.remote.directory_delete):
        return assets
    candidates, observations = set(), {}
    for asset in assets:
        mapping = engine.mapping_by_id.get(asset['mapping'])
        if mapping is not None and asset['a_state'] == 'missing':
            relative = _highest_missing(engine, mapping, asset['relative'], observations)
            if relative:
                candidates.add((mapping.key, relative))
    by_mapping = {}
    for asset in assets:
        by_mapping.setdefault(asset['mapping'], []).append(asset)
    removed, covered, replacements = set(), [], {}
    with ExitStack() as fences:
        plans, memo = [], {}
        # 从浅到深处理：已被上级目录覆盖的候选直接跳过
        for key, relative in sorted(candidates, key=lambda item: (item[0], len(PurePosixPath(item[1]).parts))):
            if any(key == parent_key and relative.startswith(parent + '/') for parent_key, parent in covered):
                continue
            covered.append((key, relative))
            group = [asset for asset in by_mapping[key] if asset['relative'].startswith(relative + '/')]
            removed.update(asset['id'] for asset in group)
            plan, fallbacks = _group(engine, engine.mapping_by_id[key], relative, group, fences, memo)
            if plan is not None:
                plans.append(plan)
            removed.difference_update(fallbacks)
            replacements.update(fallbacks)
        _execute(engine, plans)
    return [replacements.get(asset['id'], asset) for asset in assets if asset['id'] not in removed]


def _group(engine, mapping, relative, group, fences, memo):
    """处理一个消失的源目录。返回 (删除计划或 None, {资产 id: 交回逐文件路径的行})；
    不能整体删除时只把按 id 归属或能实时固定 id 的行交回逐文件路径，其余安排重试。"""
    rows, pending = engine.repository.directory_subtree(mapping.key, relative)
    try:
        if not rows:
            return None, {}
        if pending:
            _wait(engine, rows, 'directory_pending_changes', engine.clock() + 30)
            return None, {}
        blocker = _blocker(engine, mapping, relative, rows)
        if blocker:
            # 仅暂时状态 30 秒后重试；延迟、开关等按常规间隔复查
            _wait(engine, rows, blocker, engine.clock() + 30 if blocker in TRANSIENT else None)
            return None, {}
        active = [row for row in rows if row['b_state'] not in FINAL]
        due = max((row['b_next'] for row in active), default=0)
        if due > engine.clock():
            _wait(engine, rows, 'directory_retry_wait', due)
            return None, {}
        if any(row['b_state'] == 'kept' for row in rows):
            raise _UnsafeTree()
        return _prepare(engine, mapping, relative, rows, fences, memo), {}
    except _UnsafeTree:
        # 未知的额外内容不构成删除授权
        current, fallbacks = {row['id']: row for row in rows}, {}
        for asset in group:
            row = current.get(asset['id'])
            if row is None or row['a_state'] != 'missing' or row['b_state'] in FINAL:
                continue
            try:
                fallback = row if _owned(row) else _tree_file_fallback(engine, mapping, row, memo)
            except Exception:
                if engine.stop_event.is_set():
                    break  # 插件正在停止，不算失败
                engine._error('b', row, 'directory_listing_unconfirmed')
                continue
            if fallback is not None:
                fallbacks[asset['id']] = fallback
            else:
                engine._error('b', row, 'directory_identity_unconfirmed', 'conflict')
        return None, fallbacks
    except Exception as error:
        if not engine.stop_event.is_set():
            note = str(error) if isinstance(error, RemoteUnknown) else 'directory_delete_failed'
            _retry(engine, rows, note)
        return None, {}
