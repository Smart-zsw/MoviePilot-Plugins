"""批量删除测试：一轮中的文件和目录删除合并为少量 115 请求，失败时逐个确认。"""
import shutil
import threading
from types import SimpleNamespace

from conftest import DirectoryRemote, FakeRemote, bench, ops
from app.plugins.localmirror115 import adapters
from app.plugins.localmirror115.adapters import Host115, RemoteUnknown


class Batch:
    """给假网盘加上批量删除：一次调用删除多个文件或目录，可指定删除失败的 fileid。"""
    def delete_many(self, items):
        """删除一批条目，返回实际删除的 fileid 集合；只记一次 delete_many 调用。"""
        self.calls.append(('delete_many', len(items)))
        mark = len(self.calls)
        done = {str(fileid) for path, fileid, kind in items if fileid not in self.refuse
                and (self.delete_dir if kind == 'dir' else self.delete)(path, fileid)}
        del self.calls[mark:]  # 逐项删除是批量请求的内部实现，不算单独的调用
        return done


class BatchRemote(Batch, FakeRemote):
    """支持批量删除的文件级假网盘。"""
    refuse = ()


class BatchDirectoryRemote(Batch, DirectoryRemote):
    """支持批量删除、带目录条目的假网盘。"""
    refuse = ()


def test_files_deleted_across_folders_cost_one_delete_request(harness):
    """不同目录中消失的多个文件合并为一次删除请求，每个目录只列一次。"""
    names = ['%s/e%d.nfo' % (folder, i) for folder in 'ABC' for i in range(3)]
    h = bench(harness, BatchRemote, names)
    for folder in 'ABC':
        for i in range(2):
            (h.a / folder / ('e%d.nfo' % i)).unlink()
    h.settle()
    h.remote.calls.clear()
    h.engine.run_b()
    assert ops(h, 'delete_many') == [('delete_many', 6)]
    assert not ops(h, 'delete')
    assert len(ops(h, 'list')) <= 3
    for folder in 'ABC':
        assert not h.remote.copies('/media/tv/%s/e0.nfo' % folder)
        assert h.remote.copies('/media/tv/%s/e2.nfo' % folder)
        assert h.asset('%s/e0.nfo' % folder) is None or h.asset('%s/e0.nfo' % folder)['b_state'] == 'deleted'


def test_several_deleted_folders_cost_one_delete_request(harness):
    """同一轮消失的多个目录核验通过后合并为一次删除请求，不逐个删除文件。"""
    names = ['Show%d/Season 1/e%d.mkv' % (n, i) for n in range(3) for i in range(2)]
    h = bench(harness, BatchDirectoryRemote, names)
    for n in range(3):
        shutil.rmtree(h.a / ('Show%d' % n))
    h.settle()
    h.remote.calls.clear()
    h.engine.run_b()
    assert ops(h, 'delete_many') == [('delete_many', 3)]
    assert not ops(h, 'delete') and not ops(h, 'delete_dir')
    assert not any(key.startswith('/media/tv/Show') for key in h.remote.dirs)
    assert h.remote.copies('/media/tv/keep.nfo')
    # 只核验一遍：共用的父目录 1 次，每个剧集目录 2 级
    assert len(ops(h, 'list')) == 1 + 2 * 3


def test_failed_item_in_a_batch_is_confirmed_by_one_listing(harness):
    """批量中未删除的文件：重新列一次目录，仍在 115 上的记为失败，其余照常完成。"""
    h = bench(harness, BatchRemote, ['S/a.nfo', 'S/b.nfo', 'S/c.nfo'])
    stuck = h.asset('S/b.nfo')['b_fileid']
    h.remote.refuse = (stuck,)
    for name in ('a', 'b', 'c'):
        (h.a / 'S' / ('%s.nfo' % name)).unlink()
    h.settle()
    h.remote.calls.clear()
    h.engine.run_b()
    assert ops(h, 'delete_many') == [('delete_many', 3)]
    assert h.asset('S/b.nfo')['b_state'] == 'error' and h.asset('S/b.nfo')['b_note'] == 'delete_failed'
    assert h.remote.copies('/media/tv/S/b.nfo') and not h.remote.copies('/media/tv/S/a.nfo')


def test_without_batch_channel_files_are_deleted_one_by_one(harness):
    """没有批量删除通道时逐个删除，结果与之前一致。"""
    h = bench(harness, FakeRemote, ['S/a.nfo', 'S/b.nfo', 'S/c.nfo'])
    for name in ('a', 'b'):
        (h.a / 'S' / ('%s.nfo' % name)).unlink()
    h.settle()
    h.remote.calls.clear()
    h.engine.run_b()
    assert len(ops(h, 'delete')) == 2
    assert not h.remote.copies('/media/tv/S/a.nfo') and h.remote.copies('/media/tv/S/c.nfo')


def host(batch):
    """创建一个只记录删除调用的 Host115，可选配批量通道。"""
    remote = Host115(chain=None, file_item_cls=SimpleNamespace, stop=threading.Event())
    remote.single = []
    remote.delete = lambda path, fileid: remote.single.append(fileid) or True
    remote.delete_dir = lambda path, fileid: remote.single.append(fileid) or True
    remote.batch_delete = batch
    return remote


def test_host_batches_in_chunks_and_falls_back_per_item(monkeypatch):
    """批量通道按上限分批；某批失败时这一批改为逐个删除，其余批次不受影响。"""
    monkeypatch.setattr(adapters, 'BATCH_DELETE', 2)
    sent = []

    def batch(fileids):
        """第二批返回失败，其余成功。"""
        sent.append(list(fileids))
        return len(sent) != 2
    remote = host(batch)
    items = [('/m/%d' % i, str(i), 'file') for i in range(1, 6)]
    assert remote.delete_many(items) == {'1', '2', '3', '4', '5'}
    assert sent == [['1', '2'], ['3', '4'], ['5']]
    assert remote.single == ['3', '4']


def test_host_without_batch_channel_or_single_item_uses_host_delete():
    """未配置批量通道或只有一个条目时，走 MP 自带的逐个删除。"""
    remote = host(None)
    assert remote.delete_many([('/m/a', '1', 'file'), ('/m/d', '2', 'dir')]) == {'1', '2'}
    assert remote.single == ['1', '2']
    remote = host(lambda fileids: (_ for _ in ()).throw(AssertionError('不应调用批量接口')))
    assert remote.delete_many([('/m/a', '7', 'file')]) == {'7'}


def test_cookie_deleter_reports_state_and_feeds_the_limiter():
    """批量删除按 115 返回的 state 判断成败，并把结果计入限速器。"""
    class Limiter:
        """记录限速器调用。"""
        def __init__(self):
            """初始化计数。"""
            self.events = []

        def acquire(self, stop=None):
            """记录一次请求。"""
            self.events.append('acquire')

        def success(self):
            """记录成功。"""
            self.events.append('success')

        def failure(self):
            """记录失败。"""
            self.events.append('failure')

    limiter = Limiter()
    deleter = adapters.CookieDeleter('cookie', limiter)
    deleter.client = SimpleNamespace(fs_delete=lambda ids: {'state': ids == [1, 2]})
    assert deleter(['1', '2']) is True
    assert deleter(['3']) is False
    deleter.client = SimpleNamespace(fs_delete=lambda ids: (_ for _ in ()).throw(OSError('network')))
    try:
        deleter(['4'])
    except RemoteUnknown as error:
        assert str(error) == 'batch_delete_failed'
    else:
        raise AssertionError('应抛出 RemoteUnknown')
    assert limiter.events == ['acquire', 'success', 'acquire', 'failure', 'acquire', 'failure']


def test_moviepilot_deleting_a_whole_series_costs_one_delete_request(harness):
    """模拟 MP 删除整部剧：逐集删除文件，最后删除只剩元数据的季目录和剧目录。
    插件等本地删除停下后整体处理，115 上只按剧目录删除一次。"""
    names = ['Show/tvshow.nfo'] + ['Show/Season 1/e%d.%s' % (i, ext) for i in range(1, 6) for ext in ('mkv', 'nfo')]
    h = bench(harness, BatchDirectoryRemote, names)
    mapping = h.config.mappings[0]
    for i in range(1, 6):  # MP 一个请求删一集，worker 线程同时在运行
        path = h.a / 'Show' / 'Season 1' / ('e%d.mkv' % i)
        path.unlink()
        h.engine.hint(str(path))
        h.clock.advance(6)
        h.engine.tick()
        h.engine.run_b()
    shutil.rmtree(h.a / 'Show')  # 最后一集删完，MP 删除不再含媒体文件的目录
    h.engine.scan_subtree(mapping, 'Show')
    h.clock.advance(6)
    h.engine.tick()
    h.engine.run_b()
    assert not ops(h, 'delete_many')  # 删除还在进行：一个都没删
    h.clock.advance(31)
    h.engine.run_b()
    assert ops(h, 'delete_many') == [('delete_many', 1)]
    assert not ops(h, 'delete') and not ops(h, 'delete_dir')
    assert not any(key.startswith('/media/tv/Show') for key in h.remote.dirs)
    assert h.remote.copies('/media/tv/keep.nfo')


def test_continuous_deletions_do_not_postpone_forever(harness):
    """本地一直有文件在消失时，较早消失的文件最多等待 DELETE_QUIET_MAX 秒就会删除。"""
    from app.plugins.localmirror115.engine import DELETE_QUIET_MAX
    names = ['S%d/e.nfo' % i for i in range(40)]
    h = bench(harness, BatchRemote, names)
    first = None
    for i in range(40):
        path = h.a / ('S%d' % i) / 'e.nfo'
        path.unlink()
        h.engine.hint(str(path))
        h.clock.advance(10)
        h.engine.tick()
        h.engine.run_b()
        if first is None and not h.remote.copies('/media/tv/S0/e.nfo'):
            first = i
    assert first is not None and first * 10 <= DELETE_QUIET_MAX + 20


def test_failed_batch_falls_back_one_by_one_only_for_a_few_items(monkeypatch):
    """批量删除失败（例如 115 限流）时只逐个重试少量条目，不会把一批拆成大量单个请求。"""
    monkeypatch.setattr(adapters, 'FALLBACK_LIMIT', 3)
    remote = host(lambda fileids: False)
    items = [('/m/%d' % i, str(i), 'file') for i in range(1, 11)]
    assert remote.delete_many(items) == {'1', '2', '3'}
    assert remote.single == ['1', '2', '3']


def test_batch_channel_is_dropped_after_repeated_failures(monkeypatch):
    """批量删除连续失败（多半是 Cookie 失效）后，本次运行改为逐个删除，不再每轮只重试少量条目。"""
    monkeypatch.setattr(adapters, 'FALLBACK_LIMIT', 1)
    remote = host(lambda fileids: False)
    first = [('/m/a%d' % i, 'a%d' % i, 'file') for i in range(3)]
    assert remote.delete_many(first) == {'a0'}            # 第一次失败：只逐个重试一个
    second = [('/m/b%d' % i, 'b%d' % i, 'file') for i in range(3)]
    assert remote.delete_many(second) == {'b0', 'b1', 'b2'}  # 第二次失败：停用批量通道，全部逐个删除
    assert remote.batch_delete is None
