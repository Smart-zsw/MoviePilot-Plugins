"""子目录扫描测试：目录事件只重新扫描该目录，而不是整个媒体库。"""


def _drain(h):
    """不启动线程，直接执行扫描线程会处理的子目录请求。"""
    full, h.engine.full_scan = h.engine.full_scan, False
    subtrees, h.engine.subtrees = h.engine.subtrees, set()
    for key, relative in sorted(subtrees):
        h.engine.scan_subtree(h.engine.mapping_by_id[key], relative)
    return full, subtrees


def test_new_directory_is_scanned_alone(harness):
    """新增目录只单独扫描该目录，其中文件随后入库。"""
    h = harness()
    h.write('Old/a.nfo', b'x')
    h.settle()
    calls = []
    real = h.engine.fs.scan

    def spy(root, limit, stop, excluded, progress=None, start=''):
        """记录每次扫描的起始子目录后执行真实扫描。"""
        calls.append(start)
        return real(root, limit, stop, excluded, progress=progress, start=start)
    h.engine.fs.scan = spy
    h.write('New/Season 1/e1.nfo', b'y')
    h.write('New/Season 1/e1.mkv', b'video')
    assert h.engine.hint(str(h.a / 'New'), 'filesystem')
    full, subtrees = _drain(h)
    assert not full and subtrees == {('tv', 'New')} and calls == ['New']
    h.clock.advance(10)
    h.engine.tick()
    assert h.asset('New/Season 1/e1.nfo') is not None and h.asset('New/Season 1/e1.mkv') is not None
    assert h.engine.status['scan'] is None


def test_removed_directory_marks_only_its_files_missing(harness):
    """删除目录只把其中文件标记为缺失，不影响同前缀的其他目录。"""
    h = harness()
    h.write('Gone/a.nfo', b'x')
    h.write('Gone/b.nfo', b'x')
    h.write('Gone_but_not_this/c.nfo', b'x')   # 名称前缀相同，但不是同一目录
    h.settle()
    for name in ('a.nfo', 'b.nfo'):
        (h.a / 'Gone' / name).unlink()
    (h.a / 'Gone').rmdir()
    assert h.engine.hint(str(h.a / 'Gone'), 'filesystem')
    full, _ = _drain(h)
    assert not full
    h.clock.advance(10)
    h.engine.tick()
    assert h.asset('Gone/a.nfo')['a_state'] == 'missing' and h.asset('Gone/b.nfo')['a_state'] == 'missing'
    assert h.asset('Gone_but_not_this/c.nfo')['a_state'] == 'present'


def test_removed_file_does_not_schedule_empty_subtree_scan(harness):
    """删除单个文件不会安排子目录扫描。"""
    h = harness()
    path = h.write('Show/e.nfo', b'x')
    h.settle()
    path.unlink()
    h.engine.hint(str(path), 'filesystem')
    assert h.engine.subtrees == set()
    assert h.repo.counts()['pending'] == 1


def test_nested_directory_requests_are_coalesced(harness):
    """嵌套目录的扫描请求合并为最上层目录。"""
    h = harness()
    mapping = h.config.mappings[0]
    h.engine.request_subtree(mapping, 'Show/Season 1')
    h.engine.request_subtree(mapping, 'Show')
    h.engine.request_subtree(mapping, 'Show/Season 2')
    h.engine.request_subtree(mapping, 'Show2')
    assert h.engine.subtrees == {('tv', 'Show'), ('tv', 'Show2')}


def test_other_plugin_already_removed_metadata_is_not_an_error(harness):
    """本地副本已被其他插件删除时不算错误。"""
    import shutil
    h = harness(delete_enabled=True, delete_c_enabled=True)
    path = h.write('Show/e.nfo', b'meta')
    h.write('Keep/other.nfo', b'keep')
    h.settle()
    h.run()
    path.unlink()
    shutil.rmtree(h.c / 'Show')
    h.settle()
    h.engine.run_c()
    asset = h.asset('Show/e.nfo')
    assert asset['c_state'] == 'deleted' and asset['c_note'] == 'already_absent'
    assert h.engine.snapshot()['last_error'] is None


def test_root_change_still_requests_a_full_scan(harness):
    """根目录本身的变化仍请求全量扫描。"""
    h = harness()
    h.engine.hint(str(h.a), 'filesystem')
    assert h.engine.full_scan and not h.engine.subtrees
