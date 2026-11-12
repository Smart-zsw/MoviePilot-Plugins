"""115 访问边界：Open API 经宿主 u115 存储模块调用；可选的 cookie 接口（p115client）用于导出目录树和批量删除。

依赖的宿主行为（MoviePilot v3.2.5，filemanager/storages/u115.py）：
- 向已有同名文件的目录上传会产生第二个同名文件，因此替换前必须先删除所有副本；
- 上传可能返回没有 fileid 的合成条目，因此只有目录列表显示恰好一个大小正确的副本后才信任上传结果；
- 列表失败会经 run_module_strict 抛出异常，从而区分“未知”与“为空”；
- 按已删除目录的 id 列出时不报错、只返回空列表（实测），所以缓存的目录列出为空时要按路径重新确认。"""
import collections
import importlib.util
import threading
import time
from pathlib import Path, PurePosixPath

from app.schemas.file import FileItem
from app.sdk.chain import ChainBase

from .log import error_detail, logger, note_label

FOLDER_TTL = 600  # 目录条目缓存的有效秒数，过期后按路径重新确认
BATCH_DELETE = 1000  # 每次批量删除请求最多包含的文件和目录数
FALLBACK_LIMIT = 20  # 批量删除失败后，一轮中最多逐个重试的条目数
BATCH_GIVE_UP = 2  # 批量删除连续失败这么多次（多半是 Cookie 失效），本次运行改为逐个删除


class RemoteUnknown(Exception):
    """无法确认远端状态；绝不能据此采取任何操作。"""


class Stopped(RemoteUnknown):
    """插件正在停止：中断当前操作，不算任何文件的失败。"""


def describe(error):
    """异常的中文说明：插件自己的错误码译成中文，其他异常给出类型和简要信息（已隐去链接与凭据）。"""
    return note_label(str(error)) if isinstance(error, RemoteUnknown) else error_detail(error)


class RemoteConflict(Exception):
    """远端路径存在，但类型与预期不符。"""


class RateLimiter:
    """所有 115 调用共用的限速器。115 对突发请求只返回风控错误而无明确错误码，因此：
    窗口内失败达到阈值即冷却（夹杂的成功不清零），冷却逐级加长，长时间无失败才复位。"""

    FAILURE_WINDOW = 300  # 统计失败的时间窗口（秒）

    def __init__(self, qps=2.0, failure_threshold=3, cooldown_base=60.0, cooldown_max=1800.0,
                 clock=time.monotonic, sleep=time.sleep, on_cooldown=None):
        """设置速率与冷却参数并初始化计数。"""
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self.next_at = 0.0
        self.failures = []
        self.cooldowns = 0
        self.cooldown_until = 0.0
        self.last_failure_at = None
        self.calls = 0
        self.configure(qps, failure_threshold, cooldown_base, cooldown_max, on_cooldown)

    def configure(self, qps, failure_threshold, cooldown_base, cooldown_max, on_cooldown=None):
        """更新速率与冷却参数；进行中的冷却保留，保存配置不会将其清零。"""
        with self.lock:
            self.interval = 1.0 / qps if qps > 0 else 0.0
            self.failure_threshold = max(1, int(failure_threshold))
            self.cooldown_base, self.cooldown_max = cooldown_base, max(cooldown_base, cooldown_max)
            self.on_cooldown = on_cooldown

    def acquire(self, stop=None):
        """预约下一个调用时间片并等待；等待中收到 stop 时抛出 RemoteUnknown。"""
        with self.lock:
            now = self.clock()
            start = max(now, self.next_at, self.cooldown_until)
            self.next_at = start + self.interval
            self.calls += 1
        delay = start - self.clock()
        if delay > 0:
            if stop is not None:
                if stop.wait(delay):
                    raise Stopped('stopped')
            else:
                self.sleep(delay)

    def success(self):
        """记录成功；连续 cooldown_max 秒无失败时冷却等级归零。"""
        with self.lock:
            if self.last_failure_at is not None and self.clock() - self.last_failure_at >= self.cooldown_max:
                self.cooldowns = 0
                self.failures = []

    def failure(self):
        """记录失败；窗口内失败达到阈值时进入指数增长的冷却。"""
        wait = None
        with self.lock:
            now = self.clock()
            self.last_failure_at = now
            self.failures = [at for at in self.failures if now - at < self.FAILURE_WINDOW] + [now]
            if len(self.failures) >= self.failure_threshold:
                wait = min(self.cooldown_max, self.cooldown_base * (2 ** self.cooldowns))
                self.cooldown_until = now + wait
                self.cooldowns += 1
                self.failures = []
            callback = self.on_cooldown
        if wait is not None and callback is not None:
            try:
                callback(wait)
            except Exception:
                pass

    def recently_failed(self):
        """处于冷却中或窗口内已有失败时返回 True：此时不宜发起大批量请求。"""
        with self.lock:
            now = self.clock()
            return now < self.cooldown_until or any(now - at < self.FAILURE_WINDOW for at in self.failures)

    def state(self):
        """返回累计调用次数与剩余冷却秒数。"""
        with self.lock:
            remaining = self.cooldown_until - self.clock()
            return {'calls': self.calls, 'cooldown_seconds': round(remaining) if remaining > 0 else 0}


def entry(item):
    """把存储模块的文件条目转换为字典。"""
    return {'name': getattr(item, 'name', None), 'fileid': getattr(item, 'fileid', None),
            'size': getattr(item, 'size', None), 'type': getattr(item, 'type', None),
            'mtime': getattr(item, 'modify_time', None)}


class Remote:
    """115 远端的公共部分：限速器、停止信号、可选的批量删除通道，以及合并删除的流程。
    子类实现 list_dir、folder、walk、ensure_dir、upload、delete 等具体操作；
    directory_delete 为 True 时支持按目录一次删除（delete_dir）。"""
    directory_delete = False

    def __init__(self, limiter=None, stop=None):
        """绑定限速器与停止信号；批量删除通道由引擎按配置设置。"""
        self.limiter = limiter or RateLimiter(qps=0)
        self.stop = stop
        self.batch_delete = None  # 可选的批量删除通道（CookieDeleter）
        self.batch_failures = 0  # 批量删除连续失败次数

    def delete_many(self, items):
        """删除多个文件或目录 [(路径, fileid, 'file'|'dir')]，返回已删除的 fileid 集合。
        有批量通道时每 BATCH_DELETE 个合并为一次请求；没有批量通道时逐个删除。
        某批失败时只对其中前 FALLBACK_LIMIT 个逐个删除，其余结果不明，由调用方重新列目录确认后稍后重试，
        避免 115 限流时把一批拆成上千次请求。"""
        rest, done, requests, fallback = [item for item in items if item[1]], set(), 0, []
        if self.batch_delete is not None and len(rest) > 1:
            for start in range(0, len(rest), BATCH_DELETE):
                chunk = rest[start:start + BATCH_DELETE]
                if self.batch_delete is None:  # 本轮中已停用批量通道：剩下的逐个删除
                    fallback += chunk
                    continue
                requests += 1
                try:
                    ok = self.batch_delete([fileid for _, fileid, _ in chunk])
                except Stopped:
                    raise
                except RemoteUnknown:
                    ok = False
                if ok:
                    self.batch_failures = 0
                    done.update(str(fileid) for _, fileid, _ in chunk)
                    continue
                self.batch_failures += 1
                if self.batch_failures >= BATCH_GIVE_UP:
                    logger.warning('【115】批量删除连续 %d 次失败，可能是115 Cookie已失效：本次运行改为逐个删除，'
                                   '请检查并更新Cookie', self.batch_failures)
                    self.batch_delete = None
                    fallback += chunk
                else:
                    fallback += chunk[:max(0, FALLBACK_LIMIT - len(fallback))]
            rest = fallback
        for path, fileid, kind in rest:
            requests += 1
            try:
                if (self.delete_dir if kind == 'dir' else self.delete)(path, fileid):
                    done.add(str(fileid))
            except Stopped:
                raise
            except Exception:
                if self.stop is not None and self.stop.is_set():
                    raise Stopped('stopped')
                # 单个失败不影响其余条目；调用方会重新列目录确认这一个
        if len(items) > 1:
            logger.info('【115】本轮删除 %d 项（文件或目录），成功 %d 项，共 %d 次115请求', len(items), len(done), requests)
        return done


class Host115(Remote):
    """宿主 u115 存储模块的适配器；不记录提供方错误信息，因其可能含签名 URL 或令牌。"""
    storage = 'u115'
    directory_delete = True

    def __init__(self, chain, file_item_cls, limiter=None, stop=None):
        """绑定存储链、FileItem 类、限速器与停止信号。"""
        super().__init__(limiter, stop)
        self.chain = chain
        self.FileItem = file_item_cls
        self.folders = {}  # 远端目录路径 -> (目录条目, 缓存时间)：列目录时省去一次查询

    def _call(self, strict, method, **kwargs):
        """经限速器调用宿主存储方法，并把结果反馈给限速器。"""
        self.limiter.acquire(self.stop)
        try:
            runner = self.chain.run_module_strict if strict else self.chain.run_module
            result = runner(method, **kwargs)
        except Exception:
            self.limiter.failure()
            raise
        if result is None or result is False:
            self.limiter.failure()  # 无应答或明确失败都计入冷却
        else:
            self.limiter.success()
        return result

    def _item(self, path, kind, fileid=None):
        """为给定路径构造 u115 存储的 FileItem；目录路径以 / 结尾。"""
        text = str(path) + ('/' if kind == 'dir' and str(path) != '/' else '')
        return self.FileItem(storage=self.storage, path=text, type=kind, fileid=fileid,
                             name=PurePosixPath(str(path)).name)

    def folder(self, path):
        """返回远端目录条目；确认不存在返回 None，无法确认抛 RemoteUnknown，非目录抛 RemoteConflict。"""
        try:
            folder = self._call(True, 'get_file_item_strict', storage=self.storage, path=Path(str(path)))
        except Exception as error:
            raise RemoteUnknown('folder_query_failed') from error
        # item=None 的查询结果表示确认不存在；单纯的 None 表示提供方无应答，属于未知。
        if folder is None or not hasattr(folder, 'item'):
            raise RemoteUnknown('folder_query_unconfirmed')
        result = folder.item
        if result is not None and getattr(result, 'type', None) != 'dir':
            raise RemoteConflict('remote_not_directory')
        return result

    def list_folder(self, folder):
        """列出目录的直接子项；请求失败或无结果时抛出 RemoteUnknown。"""
        try:
            items = self._call(True, 'list_files', fileitem=folder, recursion=False)
        except Exception as error:
            raise RemoteUnknown('folder_list_failed') from error
        if items is None:
            raise RemoteUnknown('folder_list_unavailable')
        return items

    def list_dir(self, path):
        """返回目录条目及其子项；确认不存在时返回 None。
        目录按 id 缓存，列目录只需一次请求。115 按已删除目录的 id 列出时不报错、只返回空列表，
        所以缓存的目录列出为空（或失败）时按路径重新确认，id 变了就换成新的目录。"""
        key, items = str(path), None
        cached = self._cached_folder(key)
        if cached is not None:
            try:
                items = self.list_folder(cached)
            except RemoteUnknown:
                items = None
            if items:
                return self._listed(key, cached, items)
            self.folders.pop(key, None)
        folder = self.folder(path)
        if folder is None:
            return None
        if items is not None and getattr(folder, 'fileid', None) == getattr(cached, 'fileid', None):
            return self._listed(key, folder, [])  # 缓存的确实是这个空目录：不必再列一次
        return self._listed(key, folder, self.list_folder(folder))

    def _cached_folder(self, key):
        """返回未过期的缓存目录条目；过期（目录可能已被移动或删除）时丢弃。"""
        hit = self.folders.get(key)
        if hit is not None and time.monotonic() - hit[1] < FOLDER_TTL:
            return hit[0]
        self.folders.pop(key, None)
        return None

    def _remember(self, key, folder):
        """缓存目录条目。"""
        self.folders[key] = (folder, time.monotonic())

    def _listed(self, key, folder, items):
        """缓存这个目录和列表里名称唯一的子目录（之后列子目录省去按路径查询），返回列表结果。"""
        self._remember(key, folder)
        names = collections.Counter(getattr(item, 'name', None) for item in items)
        for item in items:
            name = getattr(item, 'name', None)
            if getattr(item, 'type', None) == 'dir' and name and names[name] == 1 and getattr(item, 'fileid', None):
                self._remember(str(PurePosixPath(key) / name), item)
        return {'folder': folder, 'entries': [entry(item) for item in items]}

    def forget_folders(self, path):
        """目录被删除后清除它及其子目录的缓存条目。"""
        prefix = str(path)
        for key in list(self.folders):
            if key == prefix or key.startswith(prefix + '/'):
                self.folders.pop(key, None)

    def walk(self, root, stop=None):
        """详细模式遍历：以 {相对路径: 条目} 返回 root 下所有文件，并统计同名副本数。"""
        top = self.folder(root)
        if top is None:
            return {}
        files, queue = {}, [(top, '')]
        while queue:
            if stop is not None and stop.is_set():
                raise Stopped('stopped')
            folder, prefix = queue.pop()
            for item in self._list_with_retry(folder, stop):
                name = getattr(item, 'name', None)
                if not name:
                    continue
                relative = prefix + name
                if getattr(item, 'type', None) == 'dir':
                    queue.append((item, relative + '/'))
                elif relative in files:
                    files[relative]['duplicates'] = files[relative].get('duplicates', 1) + 1
                else:
                    files[relative] = entry(item)
        return files

    def _list_with_retry(self, folder, stop, attempts=3):
        """遍历时单个目录列表失败先重试，不让一次偶发失败作废整棵树。"""
        for attempt in range(attempts):
            try:
                return self.list_folder(folder)
            except RemoteUnknown:
                if attempt == attempts - 1 or (stop is not None and stop.wait(5 * (attempt + 1))):
                    raise

    def ensure_dir(self, path):
        """获取或创建远端目录；结果没有 fileid 或不是目录时抛出 RemoteUnknown。"""
        folder = self._call(False, 'get_folder', storage=self.storage, path=Path(str(path)))
        if folder is None or not getattr(folder, 'fileid', None) or getattr(folder, 'type', None) != 'dir':
            raise RemoteUnknown('folder_create_failed')
        self._remember(str(path), folder)  # 之后列这个目录省去一次查询
        return folder

    def upload(self, folder, local_path, name):
        """上传本地文件到远端目录，返回条目或 None；结果须经目录列表确认后才可信。"""
        item = self._call(False, 'upload_file', fileitem=folder, path=Path(local_path), new_name=name)
        return entry(item) if item is not None else None

    def delete(self, path, fileid):
        """按 fileid 删除远端文件；没有 fileid 时返回 False。"""
        if not fileid:
            return False
        return bool(self._call(False, 'delete_file', fileitem=self._item(path, 'file', fileid)))

    def delete_dir(self, path, fileid):
        """按 fileid 删除远端目录（宿主删除接口同样接受目录 id）。"""
        if not fileid:
            return False
        self.forget_folders(path)
        return bool(self._call(False, 'delete_file', fileitem=self._item(path, 'dir', fileid)))

    def delete_many(self, items):
        """删除多个文件或目录，返回已删除的 fileid 集合；删除的目录同时清出目录缓存。"""
        done = super().delete_many(items)
        for path, _, kind in items:
            if kind == 'dir':
                self.forget_folders(path)
        return done


# ---- 目录树导出（cookie 网页接口） ----


def tree_export_available():
    """判断是否安装了支持目录树导出的 p115client。"""
    try:
        return importlib.util.find_spec('p115client.tool.export_dir') is not None
    except ImportError:  # 连 p115client 本身都未安装
        return False


def _client(cookie):
    """用 cookie 创建 P115Client；cookie 为空时抛出 RemoteUnknown。"""
    from p115client import P115Client
    if not cookie or not str(cookie).strip():
        raise RemoteUnknown('tree_export_needs_cookie')
    # 非空 cookie 绝不会触发 p115client 的交互式登录（只有 cookies=None 才会）。
    return P115Client(str(cookie).strip(), console_qrcode=False)


class CookieDeleter:
    """用 115 网页接口一次删除多个文件或目录（进入回收站），每次请求都经过限速器。
    需要 Cookie 和 p115client；整批成功返回 True，否则返回 False。"""

    def __init__(self, cookie, limiter, stop=None):
        """保存 Cookie、限速器和停止信号；客户端在第一次删除时才创建。"""
        self.cookie, self.limiter, self.stop = cookie, limiter, stop
        self.client = None

    def __call__(self, fileids):
        """删除这一批 fileid。"""
        try:
            if self.client is None:
                self.client = _client(self.cookie)
        except Exception as error:
            raise RemoteUnknown('batch_delete_client_failed') from error
        self.limiter.acquire(self.stop)
        try:
            response = self.client.fs_delete([int(fileid) for fileid in fileids])
        except Exception as error:
            self.limiter.failure()
            raise RemoteUnknown('batch_delete_failed') from error
        ok = isinstance(response, dict) and bool(response.get('state'))
        (self.limiter.success if ok else self.limiter.failure)()
        return ok


def export_tree(cookie, root_fileid, root_path, limiter, timeout=1800, poll=2.0, stop=None, pending=None,
                lock=None):
    """通过“导出目录树”获取 115 目录下的所有路径，无论规模都只需少量请求。

    返回文件和非空目录的 {相对路径: 出现次数}，不含大小或 id；导出文件随后从云端删除。
    导出开始后若超时或失败，其 id 留在 `pending` 中，供之后清理。"""
    from p115client.tool.export_dir import export_dir_start, export_dir_status, export_dir_parse_iter
    pending = pending if pending is not None else []
    with lock or threading.Lock():  # 115 每个账号同一时间只运行一个导出：调用方传入共享锁
        try:
            client = _client(cookie)
            root = int(root_fileid)
        except RemoteUnknown:
            raise
        except Exception as error:
            raise RemoteUnknown('tree_export_client_failed') from error
        limiter.acquire(stop)
        try:
            export_id = export_dir_start(client, file_ids=root, target='U_1_0')
        except Exception as error:
            limiter.failure()
            raise RemoteUnknown('tree_export_start_failed') from error
        pending.append(export_id)
        deadline = time.monotonic() + timeout
        while True:
            if stop is not None and stop.wait(poll):
                raise Stopped('stopped')
            poll = min(poll * 1.5, 30.0)  # 大目录导出要几分钟：逐步放慢查询状态
            limiter.acquire(stop)
            try:
                status = export_dir_status(client, export_id)
            except Exception as error:
                limiter.failure()
                raise RemoteUnknown('tree_export_status_failed') from error
            if status.get('file_id'):
                break
            if time.monotonic() > deadline:
                raise RemoteUnknown('tree_export_timeout')
        limiter.acquire(stop)
        try:
            # delete=True 会在 finally 块中删除导出文件，即使解析失败也是如此。
            raw = list(export_dir_parse_iter(client, int(status['file_id']), parse_iter='path', delete=True))
        except Exception as error:
            limiter.failure()
            raise RemoteUnknown('tree_export_download_failed') from error
        if export_id in pending:
            pending.remove(export_id)
        limiter.success()
    return normalise_tree(raw, root_path)


def cleanup_export(cookie, export_id, limiter, stop=None):
    """删除在停止等待之后才完成的导出所生成的文件；删除完成时返回 True。"""
    from p115client.tool.export_dir import export_dir_status
    client = _client(cookie)
    limiter.acquire(stop)
    status = export_dir_status(client, export_id)
    if not status.get('file_id'):
        return False
    limiter.acquire(stop)
    client.fs_delete(int(status['file_id']))
    return True


def normalise_tree(paths, root_path):
    """把导出路径转为相对被导出目录的路径，并统计各路径出现次数。

    导出带“根目录”表头，路径可能从账号根、被导出目录名或其内部开始，父目录本身也会成为条目；
    只去掉所有条目共有的、取自 root_path 末尾的最长前缀及这些父目录条目，仅同名的子目录不受影响。"""
    cleaned = []
    for path in paths:
        path = path.replace("\\'", "'").strip('/')
        if path.startswith('根目录/'):
            path = path[len('根目录/'):]
        if path and path != '根目录':
            cleaned.append(path)
    parts = [p for p in PurePosixPath(str(root_path)).parts if p != '/']
    for start in range(len(parts)):              # '/media/电视剧'：先尝试 'media/电视剧'，再尝试 '电视剧'
        prefix = '/'.join(parts[start:])
        parents = {'/'.join(parts[start:end]) for end in range(start + 1, len(parts) + 1)}
        if cleaned and all(p in parents or p.startswith(prefix + '/') for p in cleaned):
            cleaned = [p[len(prefix) + 1:] for p in cleaned if p not in parents]
            break
    counts = {}
    for p in cleaned:
        if p:
            counts[p] = counts.get(p, 0) + 1
    return counts  # 次数大于 1 表示 115 上有同名重复文件


class _MirrorStorageChain(ChainBase):
    """插件专用的存储处理链，仅用于调用宿主存储模块方法。"""


def build_host_remote(limiter=None, stop=None):
    """创建基于宿主存储链的 Host115 适配器。"""
    return Host115(_MirrorStorageChain(), FileItem, limiter, stop)


def transfer_target(event):
    """从整理完成事件中提取本地目标路径；调用方仍须核对其属于 A。"""
    data = getattr(event, 'event_data', event)
    if not isinstance(data, dict):
        return None
    info = data.get('transferinfo')
    target = info.get('target_item') if isinstance(info, dict) else getattr(info, 'target_item', None)
    if target is None:
        return None
    storage = target.get('storage') if isinstance(target, dict) else getattr(target, 'storage', None)
    path = target.get('path') if isinstance(target, dict) else getattr(target, 'path', None)
    return path if storage == 'local' and isinstance(path, str) else None
