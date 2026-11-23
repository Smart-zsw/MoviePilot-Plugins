"""并发上传测试：同时上传的数量、所有文件一视同仁、单个失败或停止不影响其余、结果与逐个上传一致。"""
import threading
import time

from conftest import ops


def _tracked_upload(h, delay=0.05, before=None):
    """把假网盘的上传换成稍慢的版本，返回每次开始上传时同时在传的文件集合列表（按开始顺序）。
    before(name) 在每次上传开始时调用，可抛出异常模拟这个文件上传出错。"""
    original, lock, active, overlaps = h.remote.upload, threading.Lock(), set(), []

    def upload(folder, local, name):
        """调用 before、登记开始、等一会儿、真正上传、登记结束。"""
        if before is not None:
            before(name)
        with lock:
            active.add(name)
            overlaps.append(frozenset(active))
        time.sleep(delay)
        try:
            return original(folder, local, name)
        finally:
            with lock:
                active.discard(name)
    h.remote.upload = upload
    return overlaps


def _peak(overlaps):
    """同时在传的最多文件数。"""
    return max(map(len, overlaps))


def test_two_files_upload_at_the_same_time_by_default(harness):
    """默认同时上传 2 个；全部上传完成，每个文件在115上只有一份。"""
    h = harness()
    for i in range(6):
        h.write('S/e%d.nfo' % i, b'meta%d' % i)
    h.settle()
    overlaps = _tracked_upload(h)
    h.engine.run_b()
    assert _peak(overlaps) == 2
    for i in range(6):
        assert h.asset('S/e%d.nfo' % i)['b_state'] == 'synced'
        assert len(h.remote.copies('/media/tv/S/e%d.nfo' % i)) == 1


def test_one_worker_uploads_one_by_one(harness):
    """同时上传数设为 1 时逐个上传。"""
    h = harness(upload_workers=1)
    for i in range(3):
        h.write('S/e%d.nfo' % i, b'meta')
    h.settle()
    overlaps = _tracked_upload(h, delay=0.01)
    h.engine.run_b()
    assert _peak(overlaps) == 1 and len(ops(h, 'upload')) == 3


def test_all_files_are_uploaded_alike_up_to_the_configured_count(harness):
    """所有文件一视同仁：大小文件都可以同时上传，同时在传的数量达到设置值。"""
    h = harness(upload_workers=5)
    for i in range(3):
        h.write('S/big%d.mkv' % i, b'x' * 4096)
    for i in range(4):
        h.write('S/s%d.nfo' % i, b'small')
    h.settle()
    overlaps = _tracked_upload(h)
    h.engine.run_b()
    assert _peak(overlaps) == 5
    assert any(len({'big0.mkv', 'big1.mkv', 'big2.mkv'} & active) > 1 for active in overlaps)
    assert all(h.asset('S/big%d.mkv' % i)['b_state'] == 'synced' for i in range(3))


def test_upload_count_is_limited_to_ten():
    """同时上传数最多 10 个，超出时配置报错。"""
    import pytest
    from app.plugins.localmirror115.config import Config, MAX_UPLOAD_WORKERS
    base = {'mappings': 'tv#/a#/b#/c'}
    assert Config.parse(dict(base, upload_workers=MAX_UPLOAD_WORKERS)).upload_workers == 10
    with pytest.raises(ValueError):
        Config.parse(dict(base, upload_workers=MAX_UPLOAD_WORKERS + 1))


def test_one_failing_upload_does_not_stop_the_others(harness):
    """其中一个上传出错只记这个文件的失败，其余照常完成。"""
    h = harness()
    for i in range(4):
        h.write('S/e%d.nfo' % i, b'meta')
    h.settle()

    def broken(name):
        """e1 上传时网络出错。"""
        if name == 'e1.nfo':
            raise ConnectionError('reset by peer')
    _tracked_upload(h, delay=0, before=broken)
    h.engine.run_b()
    assert h.asset('S/e1.nfo')['b_state'] == 'error'
    assert all(h.asset('S/e%d.nfo' % i)['b_state'] == 'synced' for i in (0, 2, 3))


def test_stopping_during_concurrent_uploads_takes_no_new_files(harness):
    """插件停止时正在传的文件传完，其余不再开始，下次启动再处理。"""
    h = harness()
    for i in range(6):
        h.write('S/e%d.nfo' % i, b'meta')
    h.settle()
    overlaps = _tracked_upload(h, delay=0.02, before=lambda name: h.engine.stop_event.set())  # 第一个上传开始后随即停止
    h.engine.run_b()
    assert 1 <= len(overlaps) <= 2
    assert sum(h.asset('S/e%d.nfo' % i)['b_state'] == 'synced' for i in range(6)) <= 2


def test_busy_status_lists_every_file_being_uploaded(harness):
    """状态页“正在处理”列出所有同时在传的文件，传完后清空。"""
    h = harness()
    for i in range(2):
        h.write('S/e%d.nfo' % i, b'meta')
    h.settle()
    seen = []

    def look(name):
        """两个上传都开始后，记下状态页显示的正在处理内容。"""
        time.sleep(0.05)
        seen.append(h.engine.status['b_busy'])
    _tracked_upload(h, delay=0, before=look)
    h.engine.run_b()
    assert any(text and '、' in text for text in seen) and h.engine.status['b_busy'] is None


def test_writing_one_folder_does_not_stop_caching_others(harness):
    """列目录期间别的目录被写入，不影响这个目录的列表进缓存；只有这个目录本身被写入时才不缓存。"""
    h = harness()
    engine, remote = h.engine, h.remote
    folder_a, folder_b = h.config.mappings[0].remote / 'A', h.config.mappings[0].remote / 'B'
    original = remote.list_dir

    def list_dir(path, write=None):
        """列目录时模拟另一个上传写入了某个目录。"""
        result = original(path)
        engine._forget_listing(write)
        return result
    remote.list_dir = lambda path: list_dir(path, folder_a)
    engine._listing(folder_b)
    assert str(folder_b) in engine.listings
    remote.list_dir = lambda path: list_dir(path, folder_b)
    engine.listings.clear()
    engine._listing(folder_b)
    assert str(folder_b) not in engine.listings
