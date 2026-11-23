"""115 适配器测试：目录缓存的请求数，以及已删除目录按旧 id 列出为空时的处理。"""
from pathlib import PurePosixPath
from types import SimpleNamespace

from app.plugins.localmirror115 import adapters
from app.plugins.localmirror115.adapters import Host115


class Chain:
    """宿主存储链替身：按路径保存目录（id 与子项），记录每次调用。
    与实测的 115 一致：按已删除目录的 id 列出时不报错，返回空列表。"""
    def __init__(self):
        """初始化空的目录表与调用记录。"""
        self.dirs, self.calls, self.next_id = {}, [], 100

    def mkdir(self, path):
        """创建目录（含上级目录），返回其 id。"""
        for parent in reversed(PurePosixPath(path).parents):
            if str(parent) != '/' and str(parent) not in self.dirs:
                self.mkdir(str(parent))
        self.next_id += 1
        self.dirs[path] = {'fileid': str(self.next_id), 'files': []}
        return self.dirs[path]['fileid']

    def run_module_strict(self, method, **kwargs):
        """按路径查询目录，或按目录 id 列出子项。"""
        self.calls.append(method)
        if method == 'get_file_item_strict':
            folder = self.dirs.get(str(kwargs['path']))
            item = SimpleNamespace(type='dir', fileid=folder['fileid'], path=str(kwargs['path']), name='') \
                if folder else None
            return SimpleNamespace(item=item)
        fileid = kwargs['fileitem'].fileid
        path = next((key for key, folder in self.dirs.items() if folder['fileid'] == fileid), None)
        if path is None:
            return []  # 实测：已删除目录的 id 列出为空，而不是报错
        children = [SimpleNamespace(name=PurePosixPath(key).name, type='dir', fileid=folder['fileid'],
                                    path=key + '/', size=None, modify_time=None)
                    for key, folder in self.dirs.items() if str(PurePosixPath(key).parent) == path]
        files = [SimpleNamespace(name=name, type='file', fileid=name, path=path + '/' + name, size=1,
                                 modify_time=None) for name in self.dirs[path]['files']]
        return children + files

    run_module = run_module_strict


def remote(chain):
    """用替身存储链创建 Host115。"""
    return Host115(chain, SimpleNamespace)


def test_child_directories_are_listed_with_one_request():
    """列过上级目录后，名称唯一的子目录直接按 id 列出，省去按路径查询。"""
    chain = Chain()
    chain.mkdir('/m/Show/Season 1')
    chain.dirs['/m/Show/Season 1']['files'].append('e1.mkv')
    host = remote(chain)
    host.list_dir('/m/Show')
    chain.calls.clear()
    listing = host.list_dir('/m/Show/Season 1')
    assert [e['name'] for e in listing['entries']] == ['e1.mkv']
    assert chain.calls == ['list_files']


def test_deleted_directory_is_not_mistaken_for_an_empty_one():
    """缓存的目录在 115 上被删除后，按旧 id 列出为空：按路径重新确认，得出“不存在”。"""
    chain = Chain()
    chain.mkdir('/m/Show')
    chain.dirs['/m/Show']['files'].append('e1.mkv')
    host = remote(chain)
    assert host.list_dir('/m/Show')['entries']
    del chain.dirs['/m/Show']
    assert host.list_dir('/m/Show') is None
    assert '/m/Show' not in host.folders


def test_directory_recreated_with_a_new_id_is_followed():
    """目录被删除后又以新 id 重建：按路径查到新 id 并改用它。"""
    chain = Chain()
    chain.mkdir('/m/Show')
    chain.dirs['/m/Show']['files'].append('old.mkv')
    host = remote(chain)
    host.list_dir('/m/Show')
    del chain.dirs['/m/Show']
    chain.mkdir('/m/Show')
    chain.dirs['/m/Show']['files'].append('new.mkv')
    assert [e['name'] for e in host.list_dir('/m/Show')['entries']] == ['new.mkv']


def test_genuinely_empty_directory_costs_two_requests():
    """确实为空的缓存目录：列一次为空，按路径确认 id 未变，不再重复列出。"""
    chain = Chain()
    chain.mkdir('/m/Empty')
    host = remote(chain)
    host.list_dir('/m/Empty')
    chain.calls.clear()
    assert host.list_dir('/m/Empty')['entries'] == []
    assert chain.calls == ['list_files', 'get_file_item_strict']


def test_cached_directory_expires(monkeypatch):
    """目录缓存过期后按路径重新确认（目录可能被移动）。"""
    chain = Chain()
    chain.mkdir('/m/Show')
    chain.dirs['/m/Show']['files'].append('e1.mkv')
    host = remote(chain)
    now = [1000.0]
    monkeypatch.setattr(adapters.time, 'monotonic', lambda: now[0])
    host.list_dir('/m/Show')
    now[0] += adapters.FOLDER_TTL + 1
    chain.calls.clear()
    host.list_dir('/m/Show')
    assert chain.calls == ['get_file_item_strict', 'list_files']


def test_concurrent_uploads_into_a_new_folder_create_it_once():
    """两个上传同时要用同一个新目录：只建一次，第二个直接用第一个建好的（115 索引未生效时不会再建一个同名目录）。"""
    import threading
    import time

    class SlowChain(Chain):
        """get_folder 稍慢，且每次调用都会新建一个目录（模拟 115 索引尚未生效时的重复创建）。"""
        def run_module_strict(self, method, **kwargs):
            """get_folder 每次返回一个新 id，其余照旧。"""
            if method == 'get_folder':
                self.calls.append(method)
                time.sleep(0.05)
                self.next_id += 1
                return SimpleNamespace(type='dir', fileid=str(self.next_id), path=str(kwargs['path']), name='')
            return super().run_module_strict(method, **kwargs)
        run_module = run_module_strict

    chain = SlowChain()
    host = remote(chain)
    results = []
    threads = [threading.Thread(target=lambda: results.append(host.ensure_dir('/m/New/Season 1').fileid))
               for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert chain.calls.count('get_folder') == 1 and len(set(results)) == 1


def test_network_errors_from_the_host_are_remote_failures():
    """宿主调用中的网络中断、超时归为115请求失败（结果不明），并计入风控冷却的失败次数。"""
    import pytest
    from app.plugins.localmirror115.adapters import RateLimiter, RemoteUnknown

    class BrokenChain(Chain):
        """所有调用都因网络中断失败。"""
        def run_module_strict(self, method, **kwargs):
            """模拟连接被重置。"""
            raise ConnectionError('reset by peer')
        run_module = run_module_strict

    host = Host115(BrokenChain(), SimpleNamespace, limiter=RateLimiter(qps=0, failure_threshold=3))
    failures = []
    host.limiter.failure = lambda: failures.append(1)
    with pytest.raises(RemoteUnknown) as caught:
        host.upload(SimpleNamespace(fileid='1'), '/tmp/x.nfo', 'x.nfo')
    assert str(caught.value) == 'remote_call_failed' and isinstance(caught.value.__cause__, ConnectionError)
    assert failures == [1]


def test_new_folder_is_not_created_again_while_the_index_lags():
    """刚建好的目录在115索引生效前按路径查不到、按 id 列出为空：仍按这个目录处理，下一个文件不会再建一个同名目录。"""
    class LaggyChain(Chain):
        """get_folder 每次都会新建目录；按路径查询一律查不到（索引未生效）；按 id 列出为空。"""
        def run_module_strict(self, method, **kwargs):
            """模拟115索引延迟。"""
            self.calls.append(method)
            if method == 'get_folder':
                self.next_id += 1
                return SimpleNamespace(type='dir', fileid=str(self.next_id), path=str(kwargs['path']), name='')
            if method == 'get_file_item_strict':
                return SimpleNamespace(item=None)
            return []
        run_module = run_module_strict

    chain = LaggyChain()
    host = remote(chain)
    first = host.ensure_dir('/m/New Show/Season 1')
    listing = host.list_dir('/m/New Show/Season 1')
    assert listing is not None and listing['folder'].fileid == first.fileid and listing['entries'] == []
    assert host.ensure_dir('/m/New Show/Season 1').fileid == first.fileid
    assert chain.calls.count('get_folder') == 1
    host.forget_folders('/m/New Show')
    assert host.list_dir('/m/New Show/Season 1') is None  # 目录被删除后不再沿用
