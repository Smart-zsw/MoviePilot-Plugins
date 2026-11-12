"""测试引导与公共夹具：有同级 MoviePilot 后端时用真实宿主，否则安装宿主替身；按生产命名空间导入插件并提供测试工具。"""
import importlib
import itertools
import logging
import os
import sys
import time
import types
from pathlib import Path, PurePosixPath

import pytest
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

PLUGINS_DIR = Path(__file__).resolve().parents[3] / 'plugins.v3'
DataT = TypeVar('DataT')


def _module(name, **attrs):
    """创建一个替身模块并注册到 sys.modules。"""
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class HostPluginBase:
    """宿主 ``_PluginBase`` 的替身：只实现插件用到的方法，行为可由测试替换。"""

    database = None
    data_path = None

    def get_database(self):
        """返回测试提供的数据库句柄（带 session 工厂）。"""
        return type(self).database

    def get_data_path(self):
        """返回测试提供的插件数据目录。"""
        return type(self).data_path

    def post_message(self, **kwargs):
        """通知在测试中不发送。"""


class HostEvents:
    """宿主事件管理器替身：记录插件注册的事件类型。"""

    def __init__(self):
        """初始化事件注册记录。"""
        self.registrations = []

    def register(self, event):
        """记录注册并原样返回被装饰的函数。"""
        self.registrations.append(event)
        return lambda function: function


class HostChain:
    """宿主 ``ChainBase`` 替身：记录调用的存储方法，不返回结果。"""

    calls = []

    def run_module(self, method, **kwargs):
        """记录一次宽松调用。"""
        self.calls.append(method)

    def run_module_strict(self, method, **kwargs):
        """记录一次严格调用。"""
        self.calls.append(method)


class CronTrigger:
    """apscheduler ``CronTrigger`` 替身：保存 cron 表达式供断言。"""

    def __init__(self, expression):
        """保存 cron 表达式。"""
        self.expression = expression

    @classmethod
    def from_crontab(cls, expression):
        """与真实接口一致：表达式不是5段时抛出 ValueError。"""
        if len(expression.split()) != 5:
            raise ValueError('wrong number of fields')
        return cls(expression)


class Response(BaseModel, Generic[DataT]):
    """宿主标准接口响应的替身：只有 success、message、data 三个字段。"""

    model_config = ConfigDict(extra='forbid')
    success: bool
    message: str = ''
    data: Optional[DataT] = None


def install_host():
    """优先用官方引导接入真实宿主；没有 MoviePilot 后端时安装替身模块。"""
    sys.path.insert(0, str(PLUGINS_DIR.parent / 'tests'))
    if importlib.import_module('_bootstrap').prepare_v3_backend():
        return
    try:
        importlib.import_module('app.sdk.plugin')
        return  # 已在 MoviePilot 环境中运行
    except ImportError:
        pass
    _module('app', __path__=[])
    _module('app.plugins', __path__=[str(PLUGINS_DIR)])
    _module('app.sdk', __path__=[])
    _module('app.sdk.plugin', _PluginBase=HostPluginBase)
    _module('app.sdk.events', eventmanager=HostEvents())
    _module('app.sdk.logging', logger=logging.getLogger('localmirror115'))
    _module('app.sdk.database', plugin_declarative_base=declarative_base)
    _module('app.sdk.chain', ChainBase=HostChain)
    _module('app.schemas', __path__=[])
    _module('app.schemas.types',
            EventType=types.SimpleNamespace(TransferComplete='main', AudioTransferComplete='audio',
                                            SubtitleTransferComplete='subtitle'),
            MessageType=types.SimpleNamespace(Plugin='plugin'))
    _module('app.schemas.file', FileItem=lambda **kwargs: types.SimpleNamespace(**kwargs))
    _module('app.schemas.response', Response=Response)
    if 'apscheduler' not in sys.modules:
        _module('apscheduler', __path__=[])
        _module('apscheduler.triggers', __path__=[])
        _module('apscheduler.triggers.cron', CronTrigger=CronTrigger)


install_host()

from app.plugins.localmirror115.adapters import Remote  # noqa: E402
from app.plugins.localmirror115.config import Config  # noqa: E402
from app.plugins.localmirror115.engine import SyncEngine  # noqa: E402
from app.plugins.localmirror115.persistence import Base, Repository  # noqa: E402


class FakeRemote(Remote):
    """内存版 115 网盘：允许同名文件，可模拟索引延迟与上传失败；合并删除流程与生产相同（继承 Remote）。"""
    def __init__(self, clock):
        """初始化空网盘与各项模拟开关。"""
        super().__init__()
        self.clock = clock
        self.dirs = {}          # 路径 -> {fileid, entries}
        self.ids = itertools.count(1000)
        self.lag = False
        self.hidden = []
        self.fail_upload = False
        self.calls = []

    def _dir(self, path, create=False):
        """返回目录记录，需要时逐级创建父目录。"""
        key = str(path)
        if key not in self.dirs and create:
            for parent in reversed(PurePosixPath(key).parents):
                if str(parent) != '/' and str(parent) not in self.dirs:
                    self.dirs[str(parent)] = {'fileid': str(next(self.ids)), 'entries': []}
            self.dirs[key] = {'fileid': str(next(self.ids)), 'entries': []}
        return self.dirs.get(key)

    def put(self, path, size, mtime=None, fileid=None):
        """直接在网盘放入一个文件并返回其条目。"""
        path = PurePosixPath(path)
        folder = self._dir(path.parent, create=True)
        item = {'name': path.name, 'fileid': fileid or str(next(self.ids)), 'size': size,
                'type': 'file', 'mtime': mtime if mtime is not None else time.time()}
        folder['entries'].append(item)
        return item

    def copies(self, path):
        """返回指定路径下所有同名副本。"""
        path = PurePosixPath(path)
        folder = self.dirs.get(str(path.parent))
        return [e for e in folder['entries'] if e['name'] == path.name] if folder else []

    def list_dir(self, path):
        """列出目录内容，目录不存在时返回 None。"""
        self.calls.append(('list', str(path)))
        folder = self.dirs.get(str(path))
        if folder is None:
            return None
        return {'folder': {'fileid': folder['fileid'], 'path': str(path)},
                'entries': [dict(e) for e in folder['entries']]}

    def folder(self, path):
        """返回目录的 fileid 与路径，不存在时返回 None。"""
        folder = self.dirs.get(str(path))
        return {'fileid': folder['fileid'], 'path': str(path)} if folder else None

    def walk(self, root, stop=None):
        """递归遍历目录树，按相对路径汇总条目并统计重名副本数。"""
        out = {}
        prefix = str(root).rstrip('/') + '/'
        for path, folder in self.dirs.items():
            if path == str(root) or path.startswith(prefix):
                rel = path[len(prefix):] if path != str(root) else ''
                for e in folder['entries']:
                    key = (rel + '/' if rel else '') + e['name']
                    if key in out:
                        out[key]['duplicates'] = out[key].get('duplicates', 1) + 1
                    else:
                        out[key] = dict(e)
        return out

    def ensure_dir(self, path):
        """确保目录存在并返回其信息。"""
        self.calls.append(('mkdir', str(path)))
        folder = self._dir(path, create=True)
        return {'fileid': folder['fileid'], 'path': str(path)}

    def upload(self, folder, local_path, name):
        """上传本地文件；可模拟失败或索引延迟（暂不可见）。"""
        self.calls.append(('upload', folder['path'], name))
        if self.fail_upload:
            return None
        size = Path(local_path).stat().st_size
        item = {'name': name, 'fileid': str(next(self.ids)), 'size': size, 'type': 'file', 'mtime': time.time()}
        if self.lag:
            self.hidden.append((folder['path'], item))
        else:
            self.dirs[folder['path']]['entries'].append(item)
        return dict(item)

    def reveal(self):
        """让索引延迟期间上传的文件变为可见。"""
        for path, item in self.hidden:
            self.dirs[path]['entries'].append(item)
        self.hidden = []

    def delete(self, path, fileid):
        """按 fileid 删除文件，返回是否确有删除。"""
        self.calls.append(('delete', str(path), fileid))
        folder = self.dirs.get(str(PurePosixPath(path).parent))
        if not folder:
            return False
        before = len(folder['entries'])
        folder['entries'] = [e for e in folder['entries'] if e['fileid'] != fileid]
        return len(folder['entries']) < before


class DirectoryRemote(FakeRemote):
    """带真实目录条目、支持单次递归删除目录的假网盘。"""
    directory_delete = True

    def list_dir(self, path):
        """列出目录时同时返回直接子目录条目。"""
        listing = super().list_dir(path)
        if listing is not None:
            for key, folder in self.dirs.items():
                child = PurePosixPath(key)
                if child.parent == PurePosixPath(path):
                    listing['entries'].append({'name': child.name, 'fileid': folder['fileid'], 'type': 'dir'})
        return listing

    def delete_dir(self, path, fileid):
        """按 fileid 校验后递归删除整个目录，记录调用。"""
        key = str(path)
        self.calls.append(('delete_dir', key, fileid))
        folder = self.dirs.get(key)
        if not folder or folder['fileid'] != fileid:
            return False
        for name in list(self.dirs):
            if name == key or name.startswith(key + '/'):
                del self.dirs[name]
        return True


def ops(h, kind):
    """返回假网盘中指定类型的调用记录。"""
    return [call for call in h.remote.calls if call[0] == kind]


def deleting(**extra):
    """开启全部同步删除的配置项，可用参数覆盖。"""
    return dict(dict(delete_enabled=True, delete_b_enabled=True, delete_c_enabled=True), **extra)


def bench(harness, remote_cls, names, run_c=True, **options):
    """创建开启删除的工作台：换上指定假网盘、启用通知计数，写入并同步给定文件后清空调用记录。
    run_c=False 时只完成 115 同步，不复制到 STRM 目录。"""
    from app.plugins.localmirror115.notify import Notifier
    h = harness(**deleting(**options))
    h.remote = remote_cls(h.clock)
    h.engine.remote = h.engine.index.remote = h.remote
    h.engine.notifier = Notifier(lambda *args: None, enabled=True, clock=h.clock)
    h.write('keep.nfo', b'keep')  # 避免空映射成为删除基线
    for name in names:
        h.write(name, b'data', mtime=1700000000)
    h.settle()
    h.run() if run_c else h.engine.run_b()
    h.remote.calls.clear()
    return h


class Clock:
    """可手动推进的测试时钟。"""
    def __init__(self, start=None):
        """以 start（默认当前真实时间）作为起点。"""
        self.now = time.time() if start is None else start

    def __call__(self):
        """返回当前时间。"""
        return self.now

    def advance(self, seconds):
        """将时间向前推进指定秒数。"""
        self.now += seconds


def index_first(engine):
    """让每次 115 处理前先执行快照步骤（生产中由独立线程执行）。"""
    original = engine.run_b

    def run_b():
        """先建立快照索引，再执行原来的 115 处理。"""
        engine.index_once()
        original()
    engine.run_b = run_b
    return engine


class Harness:
    """测试工作台：组装临时目录、数据库、假网盘、配置与同步引擎。"""
    def __init__(self, tmp_path, **options):
        """创建源目录、本地副本目录、数据库与引擎；options 覆盖默认配置。"""
        self.a = tmp_path / 'a'
        self.c = tmp_path / 'c'
        self.q = tmp_path / 'q'
        self.a.mkdir()
        self.c.mkdir()
        self.clock = Clock()
        self.remote = FakeRemote(self.clock)
        self.db = create_engine('sqlite:///' + str(tmp_path / 'p.db'), connect_args={'check_same_thread': False})
        Base.metadata.create_all(self.db)
        raw = {'enabled': True, 'mode': 'sync', 'copy_enabled': True, 'upload_enabled': True,
               'settle_seconds': 1, 'api_qps': 20, 'compare_mode': 'detail',
               'mappings': 'tv#%s#/media/tv#%s' % (self.a, self.c)}
        raw.update(options)
        self.config = Config.parse(raw)
        self.repo = Repository(sessionmaker(self.db), self.config.revision, self.config.revision_b,
                               self.config.revision_c)
        self.engine = SyncEngine(self.config, self.repo, self.remote, str(tmp_path / 'data'), self.clock)
        index_first(self.engine)

    def write(self, relative, data, mtime=None):
        """在源目录写入文件，可指定修改时间。"""
        path = self.a / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def settle(self, scans=1):
        """执行若干轮扫描并推进时钟，使变更度过稳定期。"""
        for _ in range(scans):
            self.engine.scan_once()
            self.clock.advance(2)
            self.engine.tick()
            self.clock.advance(60)

    def run(self):
        """依次执行本地副本处理与 115 处理。"""
        self.engine.run_c()
        self.engine.run_b()

    def asset(self, relative):
        """按相对路径查询资产记录，不存在时返回 None。"""
        from sqlalchemy import select
        from app.plugins.localmirror115.persistence import Asset
        with self.repo.transaction() as s:
            row = s.scalar(select(Asset.id).where(Asset.relative == relative))
        return self.repo.get(row) if row else None


@pytest.fixture
def harness(tmp_path):
    """提供创建工作台的工厂，测试结束后释放数据库连接。"""
    made = []

    def make(**options):
        """用给定配置项在独立子目录中创建一个工作台。"""
        root = tmp_path / str(len(made))
        root.mkdir()
        h = Harness(root, **options)
        made.append(h)
        return h
    yield make
    for h in made:
        h.db.dispose()


@pytest.fixture(autouse=True)
def no_request_pacing(monkeypatch):
    """测试中的引擎不按每秒请求数等待（风控冷却逻辑照常生效）；限速器本身的测试直接构造 RateLimiter。"""
    from app.plugins.localmirror115 import engine as engine_module

    class Unpaced(engine_module.RateLimiter):
        """只去掉请求间隔的限速器。"""

        def configure(self, qps, *args, **kwargs):
            """忽略 qps，其余参数照常设置。"""
            super().configure(0, *args, **kwargs)
    monkeypatch.setattr(engine_module, 'RateLimiter', Unpaced)
