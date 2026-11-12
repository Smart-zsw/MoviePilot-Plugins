"""整目录删除及其安全边界的测试（只调用内存版 115）。"""
import shutil
import threading
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest

from conftest import DirectoryRemote, bench, ops
from app.plugins.localmirror115.adapters import Host115, RemoteUnknown
from app.plugins.localmirror115.directory_delete import process_deleted_directories
from app.plugins.localmirror115.remote_index import RemoteIndex


def setup(harness, names=('Show/a.mkv', 'Show/Season 1/b.nfo'), **options):
    """带目录删除能力的工作台（只完成 115 同步）。"""
    return bench(harness, DirectoryRemote, names, run_c=False, **options)


def vanished(h, relative='Show'):
    """删除源目录并完成事件稳定，不依赖全量扫描确认。"""
    shutil.rmtree(h.a / relative)
    h.engine.scan_subtree(h.config.mappings[0], relative)
    h.clock.advance(2)
    h.engine.tick()  # 依靠事件确认缺失，无需全量扫描确认


def deleted(h):
    """返回已在 115 上删除（进入回收站）的文件数。"""
    return h.engine.notifier.counts.get('deleted_b', 0)


def process(h, limit=1000, files=True):
    """先执行整目录删除，再按需逐个处理剩余的文件记录。"""
    assets = h.repo.work('b', h.clock(), limit)
    remaining = process_deleted_directories(h.engine, assets)
    if files:
        h.engine._delete_files(remaining)
    return remaining


def test_nested_directory_deleted_once_after_one_stability_check(harness):
    """嵌套目录消失后经一次稳定检查即整目录删除一次，计数与通知正确。"""
    h = setup(harness)
    vanished(h)
    assert process(h) == []
    assert len(ops(h, 'delete_dir')) == 1
    assert ops(h, 'delete_dir')[0][1] == '/media/tv/Show'
    assert not ops(h, 'delete')
    assert h.remote.copies('/media/tv/keep.nfo')
    assert h.asset('Show/a.mkv') is None
    meta = h.asset('Show/Season 1/b.nfo')
    assert meta['b_state'] == 'deleted' and meta['b_fp'] is meta['b_pending'] is meta['b_fileid'] is None
    assert meta['c_state'] not in ('deleted', 'absent', 'kept')
    assert h.engine.notifier.counts['deleted_dirs_b'] == 1
    assert h.engine.notifier.counts['deleted_b'] == 2


def test_expands_past_work_batch_and_literal_prefixes(harness):
    """整目录删除越过单批数量限制，且目录名中的 % 和 _ 按字面匹配。"""
    h = setup(harness, ('S%_/a.mkv', 'S%_/b.mkv', 'SXX/keep.mkv'))
    vanished(h, 'S%_')
    assert process(h, limit=1) == []
    assert len(ops(h, 'delete_dir')) == 1
    assert deleted(h) == 2
    assert h.remote.copies('/media/tv/SXX/keep.mkv')


def test_partial_source_directory_uses_single_file_path(harness):
    """源目录只删了部分文件时走单文件删除。"""
    h = setup(harness, ('Show/a.mkv', 'Show/b.mkv'))
    (h.a / 'Show/a.mkv').unlink()
    h.engine.hint(str(h.a / 'Show/a.mkv'))
    h.clock.advance(2)
    h.engine.tick()
    assert len(process(h)) == 1
    assert len(ops(h, 'delete')) == 1 and not ops(h, 'delete_dir')
    assert h.remote.copies('/media/tv/Show/b.mkv')


@pytest.mark.parametrize('extra', ['file', 'empty_dir', 'nested_dir', 'duplicate_file', 'missing_id', 'bad_name'])
def test_unknown_contents_fall_back_without_deleting_them(harness, extra):
    """115 目录含未知内容时回退为逐个删除已知文件，未知内容保留。"""
    h = setup(harness, ('Show/a.mkv', 'Show/b.mkv'))
    if extra == 'file':
        h.remote.put('/media/tv/Show/unknown.txt', 7)
    elif extra == 'empty_dir':
        h.remote.ensure_dir('/media/tv/Show/unknown')
    elif extra == 'nested_dir':
        h.remote.put('/media/tv/Show/unknown/x.txt', 7)
    elif extra == 'duplicate_file':
        h.remote.put('/media/tv/Show/a.mkv', 4)
    else:
        h.remote.dirs['/media/tv/Show']['entries'].append(
            {'name': 'unknown.txt' if extra == 'missing_id' else '../outside',
             'type': 'file', 'size': 7, 'fileid': None if extra == 'missing_id' else '777777'})
    vanished(h)
    process(h)
    assert not ops(h, 'delete_dir') and len(ops(h, 'delete')) == 2
    assert '/media/tv/Show' in h.remote.dirs
    listing = h.remote.list_dir('/media/tv/Show')
    assert listing['entries']
    if extra == 'duplicate_file':
        assert len(h.remote.copies('/media/tv/Show/a.mkv')) == 1


@pytest.mark.parametrize('idless', ['tree', 'pending'])
def test_idless_rows_never_guess_same_name_size(harness, idless):
    """记录缺少 fileid 时不凭同名同大小认定归属，不整目录删除。"""
    h = setup(harness, ('Show/a.mkv', 'Show/b.mkv'))
    row = h.asset('Show/a.mkv')
    fields = dict(b_fileid=None)
    if idless == 'pending':
        fields['b_pending'] = {'fp': row['a_fp'], 'at': h.clock(), 'fileid': None}
    else:
        h.remote.put('/media/tv/Show/a.mkv', 4)  # 同名同大小的文件不能证明归属
    h.repo.record('b', row, **fields)
    vanished(h)
    process(h)
    assert not ops(h, 'delete_dir')
    assert h.remote.copies('/media/tv/Show/a.mkv')
    assert not h.remote.copies('/media/tv/Show/b.mkv')
    assert h.asset('Show/a.mkv')['b_note'] == 'directory_identity_unconfirmed'


def test_tree_adopted_directory_revalidates_unique_names_and_sizes_before_one_delete(harness):
    """由快照认领的文件经名称唯一、大小一致复核后才整目录删除。"""
    h = setup(harness)
    for name in ('Show/a.mkv', 'Show/Season 1/b.nfo'):
        h.repo.record('b', h.asset(name), b_fileid=None, b_note='adopted_by_tree')
    vanished(h)
    process(h)
    assert len(ops(h, 'delete_dir')) == 1 and not ops(h, 'delete')
    assert deleted(h) == 2


def test_tree_adoption_wrong_remote_size_prevents_directory_delete(harness):
    """认领文件在 115 上大小不符时既不删目录也不删文件。"""
    h = setup(harness, ('Show/a.mkv',))
    h.repo.record('b', h.asset('Show/a.mkv'), b_fileid=None, b_note='adopted_by_tree')
    h.remote.copies('/media/tv/Show/a.mkv')[0]['size'] = 999
    vanished(h)
    process(h)
    assert not ops(h, 'delete_dir') and not ops(h, 'delete')


def test_tree_adoption_keeps_file_fallback_and_preserves_extra_file(harness):
    """目录含额外文件时认领文件回退为逐个删除，额外文件保留。"""
    h = setup(harness, ('Show/a.mkv', 'Show/b.mkv'))
    for name in ('Show/a.mkv', 'Show/b.mkv'):
        h.repo.record('b', h.asset(name), b_fileid=None, b_note='adopted_by_tree')
    h.remote.put('/media/tv/Show/extra.txt', 4)
    vanished(h)
    process(h)
    assert not ops(h, 'delete_dir') and len(ops(h, 'delete')) == 2
    assert h.remote.copies('/media/tv/Show/extra.txt')


def test_tree_fallback_pins_live_id_before_second_listing(harness):
    """回退逐个删除前先固定当时的 fileid，之后出现的同名文件不会被删。"""
    h = setup(harness, ('Show/a.mkv',))
    h.repo.record('b', h.asset('Show/a.mkv'), b_fileid=None, b_note='adopted_by_tree')
    h.remote.put('/media/tv/Show/keep.txt', 1)  # 强制回退为逐个删除
    vanished(h)
    rows = process(h, files=False)
    assert len(rows) == 1 and rows[0]['b_fileid']
    extra = h.remote.put('/media/tv/Show/a.mkv', 4)
    h.engine._delete_files(rows[:1])
    assert h.remote.copies('/media/tv/Show/a.mkv') == [extra]


def test_pending_fileid_is_sufficient_ownership(harness):
    """待确认上传记录中的 fileid 足以证明归属。"""
    h = setup(harness, ('Show/a.mkv',))
    row = h.asset('Show/a.mkv')
    h.repo.record('b', row, b_fileid=None, b_fp=None,
                  b_pending={'fp': row['a_fp'], 'at': h.clock(), 'fileid': row['b_fileid']})
    vanished(h)
    process(h)
    assert len(ops(h, 'delete_dir')) == 1 and deleted(h) == 1


def test_unknown_list_is_not_an_absent_directory(harness):
    """子目录列表失败视为未知而非不存在，整组记录推迟重试。"""
    h = setup(harness)
    vanished(h)
    original = h.remote.list_dir
    def failed(path):
        """列出指定子目录时抛出未知状态异常。"""
        if str(path) == '/media/tv/Show/Season 1':
            raise RemoteUnknown('folder_list_failed')
        return original(path)
    h.remote.list_dir = failed
    assert process(h) == []
    assert not ops(h, 'delete_dir') and not ops(h, 'delete')
    assert h.asset('Show/a.mkv')['b_next'] > h.clock()
    assert h.asset('Show/Season 1/b.nfo')['b_next'] > h.clock()


def test_absent_remote_directory_clears_records_without_delete(harness):
    """115 上目录已不存在时只清理记录，不发起删除也不计数。"""
    h = setup(harness)
    vanished(h)
    for key in list(h.remote.dirs):
        if key == '/media/tv/Show' or key.startswith('/media/tv/Show/'):
            del h.remote.dirs[key]
    process(h)
    assert not ops(h, 'delete') and not ops(h, 'delete_dir')
    assert h.asset('Show/a.mkv') is None
    assert h.asset('Show/Season 1/b.nfo')['b_note'] == 'already_gone'
    assert deleted(h) == 0


@pytest.mark.parametrize('guard', ['root', 'device', 'pending', 'future', 'delay', 'stopped'])
def test_guards_keep_entire_group_out_of_file_deletion(harness, guard):
    """任一安全守卫生效时整组都不进入删除。"""
    h = setup(harness, **({'delete_delay_hours': 1} if guard == 'delay' else {}))
    vanished(h)
    if guard == 'root':
        h.engine.status['roots']['tv'] = 'awaiting_scan'
    elif guard == 'device':
        h.engine.fs.ancestor_device = lambda *args: -1
    elif guard == 'pending':
        h.repo.enqueue_many([('tv', 'Show/new.mkv', {'status': 'missing_candidate'}, h.clock() + 50, 'filesystem')])
    elif guard == 'future':
        h.repo.record('b', h.asset('Show/Season 1/b.nfo'), b_next=h.clock() + 50, b_state='error')
    elif guard == 'stopped':
        h.engine.stop_event.set()
    assert process(h) == []
    assert not ops(h, 'delete') and not ops(h, 'delete_dir')
    assert h.remote.copies('/media/tv/Show/a.mkv')


def test_mapping_root_cannot_be_deleted(harness):
    """映射根目录永远不会被整目录删除。"""
    h = setup(harness, ('root.mkv',))
    shutil.rmtree(h.a)
    h.repo.enqueue_many([('tv', 'root.mkv', {'status': 'missing_candidate'}, h.clock(), 'filesystem')])
    # 直接传入过期的缺失记录，模拟工作线程已取到它
    row = h.asset('root.mkv')
    row['a_state'] = 'missing'
    assert process_deleted_directories(h.engine, [row]) == [row]
    h.engine._delete_files([row])
    assert not ops(h, 'delete_dir') and not ops(h, 'delete')


def test_disabled_policy_keeps_existing_path(harness):
    """关闭 115 删除时原样返回记录，不发起任何目录操作。"""
    h = setup(harness, delete_b_enabled=False)
    vanished(h)
    assets = h.repo.work('b', h.clock())
    assert process_deleted_directories(h.engine, assets) is assets
    assert not ops(h, 'list') and not ops(h, 'delete_dir')


@pytest.mark.parametrize('result', ['false', 'exception', 'lost_success'])
def test_failed_or_unknown_result_backs_off_whole_group(harness, result):
    """目录删除失败或结果未知时整组退避重试。"""
    h = setup(harness, ('Show/a.mkv', 'Show/b.mkv'))
    vanished(h)
    original = h.remote.delete_dir
    def fail(path, fileid):
        """按参数模拟删除返回失败、抛异常或成功后丢失响应。"""
        if result == 'lost_success':
            original(path, fileid)
        else:
            h.remote.calls.append(('delete_dir', str(path), fileid))
        if result == 'false':
            return False
        raise OSError('remote response lost')
    h.remote.delete_dir = fail
    process(h, limit=1)
    assert len(ops(h, 'delete_dir')) == 1 and not ops(h, 'delete')
    assert h.asset('Show/a.mkv')['b_next'] > h.clock()
    assert h.asset('Show/b.mkv')['b_next'] > h.clock()
    # 扫描重置其中一条记录不应破坏整组退避。
    h.repo.record('b', h.asset('Show/a.mkv'), b_next=0)
    process(h)
    assert len(ops(h, 'delete_dir')) == 1 and not ops(h, 'delete')
    h.remote.delete_dir = original
    h.clock.advance(61)
    process(h)
    assert not h.remote.copies('/media/tv/Show/a.mkv')


@pytest.mark.parametrize('during', ['listing', 'delete'])
def test_source_recovery_cancels_or_resynchronizes_without_stale_result(harness, during):
    """核验或删除过程中源文件恢复时重新同步，不留下过期结果。"""
    h = setup(harness, ('Show/a.mkv', 'Show/b.mkv'))
    vanished(h)
    restored = []
    def restore():
        """恢复源文件（只执行一次）并完成稳定。"""
        if restored:
            return
        restored.append(True)
        h.write('Show/a.mkv', b'new version', mtime=1800000000)
        h.engine.hint(str(h.a / 'Show/a.mkv'))
        h.clock.advance(2)
        h.engine.tick()
    if during == 'listing':
        original = h.remote.list_dir
        def listing(path):
            """列出目标目录时触发源文件恢复。"""
            answer = original(path)
            if str(path) == '/media/tv/Show':
                restore()
            return answer
        h.remote.list_dir = listing
    else:
        original = h.remote.delete_dir
        def delete(path, fileid):
            """删除目录前触发源文件恢复。"""
            restore()
            return original(path, fileid)
        h.remote.delete_dir = delete
    process(h)
    row = h.asset('Show/a.mkv')
    assert row['a_state'] == 'present' and row['a_fp']['size'] == 11
    assert row['b_state'] != 'deleted' and row['b_fp'] is None
    h.engine.run_b()
    assert h.remote.copies('/media/tv/Show/a.mkv')[0]['size'] == 11
    assert h.asset('Show/a.mkv')['b_fp'] == row['a_fp']


def test_recovery_after_successful_delete_and_restart_never_adopts_old_snapshot(harness):
    """整目录删除成功并重启后，不会从旧快照认领已删文件。"""
    h = setup(harness, ('Show/a.mkv',))
    mapping = h.config.mappings[0]
    h.engine.index.build(mapping)  # 持久化即将被删除的条目
    h.engine._listing(mapping.remote / 'Show', max_age=0)
    vanished(h)
    process(h)
    assert str(mapping.remote / 'Show') not in h.engine.listings
    restarted = RemoteIndex(h.config, h.remote, h.q.parent / 'data', h.engine.limiter, h.clock)
    assert not restarted.snapshot(mapping).has('Show/a.mkv')
    h.engine.index = restarted
    h.write('Show/a.mkv', b'data', mtime=1700000000)
    h.settle()
    h.engine.run_b()
    assert h.remote.copies('/media/tv/Show/a.mkv')


def test_inflight_snapshot_cannot_publish_predelete_tree(harness):
    """删除期间进行中的快照构建不能发布删除前的目录树。"""
    h = setup(harness, ('Show/a.mkv',))
    mapping = h.config.mappings[0]
    index = h.engine.index
    index.build(mapping)
    started, resume = threading.Event(), threading.Event()
    original = h.remote.walk
    def slow(root, stop=None):
        """遍历完成后等待放行，模拟耗时的快照构建。"""
        result = original(root, stop)
        started.set()
        assert resume.wait(5)
        return result
    h.remote.walk = slow
    errors = []
    def build():
        """在后台构建快照并记录未知状态错误。"""
        try:
            index.build(mapping)
        except RemoteUnknown as error:
            errors.append(str(error))
    worker = threading.Thread(target=build)
    worker.start()
    assert started.wait(5)
    try:
        vanished(h)
        process(h)
    finally:
        resume.set()
        worker.join(5)
    assert errors == ['snapshot_changed_during_build']
    assert not index.snapshot(mapping).has('Show/a.mkv')
    restarted = RemoteIndex(h.config, h.remote, h.q.parent / 'data', h.engine.limiter, h.clock)
    assert not restarted.snapshot(mapping).has('Show/a.mkv')


def test_build_started_inside_delete_is_fenced(harness):
    """目录删除进行中启动的快照构建会被拒绝。"""
    h = setup(harness, ('Show/a.mkv',))
    vanished(h)
    original = h.remote.delete_dir
    def delete(path, fileid):
        """删除目录前断言快照构建被拒绝。"""
        with pytest.raises(RemoteUnknown, match='snapshot_directory_delete_in_progress'):
            h.engine.index.build(h.config.mappings[0])
        return original(path, fileid)
    h.remote.delete_dir = delete
    process(h)
    assert len(ops(h, 'delete_dir')) == 1


def test_pending_only_recovery_rechecks_without_index_wait(harness):
    """仅有待确认记录的文件恢复后立即复查，无需等待索引。"""
    h = setup(harness, ('Show/a.mkv',))
    row = h.asset('Show/a.mkv')
    h.repo.record('b', row, b_fileid=None, b_fp=None,
                  b_pending={'fp': row['a_fp'], 'at': h.clock(), 'fileid': row['b_fileid']})
    vanished(h)
    original = h.remote.delete_dir
    def delete(path, fileid):
        """删除目录前恢复源文件并完成稳定。"""
        h.write('Show/a.mkv', b'data', mtime=1700000000)
        h.engine.hint(str(h.a / 'Show/a.mkv'))
        h.clock.advance(2)
        h.engine.tick()
        return original(path, fileid)
    h.remote.delete_dir = delete
    process(h)
    current = h.asset('Show/a.mkv')
    assert current['a_state'] == 'present'
    assert current['b_pending'] is None and current['b_fp'] is None
    assert current['b_fileid'] == row['b_fileid'] and current['b_next'] == 0
    h.engine.run_b()
    assert h.remote.copies('/media/tv/Show/a.mkv')
    assert h.asset('Show/a.mkv')['b_state'] == 'synced'


def test_directory_success_resolves_previous_error_notice(harness, monkeypatch):
    """整目录删除成功后解除此前的错误通知。"""
    h = setup(harness, ('Show/a.mkv',))
    vanished(h)
    h.repo.record('b', h.asset('Show/a.mkv'), b_attempts=1, b_state='error')
    resolved = []
    monkeypatch.setattr(h.engine.notifier, 'resolve', lambda name: resolved.append(name))
    process(h)
    assert len(resolved) == 1 and 'Show/a.mkv' in resolved[0]


def test_host_directory_delete_uses_explicit_directory_item():
    """宿主适配器删除目录时传入目录类型条目，缺少 fileid 时拒绝。"""
    calls = []
    chain = SimpleNamespace(run_module=lambda method, **kwargs: calls.append((method, kwargs)) or True)
    remote = Host115(chain, lambda **kwargs: SimpleNamespace(**kwargs))
    assert remote.delete_dir(PurePosixPath('/media/tv/Show'), '123')
    method, kwargs = calls[0]
    assert method == 'delete_file'
    assert kwargs['fileitem'].type == 'dir' and kwargs['fileitem'].path == '/media/tv/Show/'
    assert kwargs['fileitem'].fileid == '123'
    assert not remote.delete_dir('/media/tv/Show', None)
    assert len(calls) == 1


def test_tree_adopted_file_already_gone_on_115_is_not_a_conflict(harness):
    """认领文件已在 115 上被手动删除时不算冲突。"""
    h = setup(harness, ('Show/a.mkv', 'Show/b.mkv'))
    for name in ('Show/a.mkv', 'Show/b.mkv'):
        h.repo.record('b', h.asset(name), b_fileid=None, b_note='adopted_by_tree')
    h.remote.put('/media/tv/Show/extra.txt', 4)          # 强制回退为逐个删除
    copy = h.remote.copies('/media/tv/Show/a.mkv')[0]
    h.remote.delete('/media/tv/Show/a.mkv', copy['fileid'])   # 在 115 上手动删除
    vanished(h)
    process(h)
    assert h.asset('Show/a.mkv') is None or h.asset('Show/a.mkv')['b_note'] == 'already_gone'
    assert h.asset('Show/a.mkv') is None or h.asset('Show/a.mkv')['b_state'] != 'conflict'
    assert not h.remote.copies('/media/tv/Show/b.mkv')
    assert h.remote.copies('/media/tv/Show/extra.txt')
