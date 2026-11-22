"""只读的本地源文件系统观测，以及 C（STRM 目录）的唯一写入器。状态未知绝不会被当作文件不存在。"""
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from pathlib import Path, PurePosixPath
import hashlib
import os
import queue
import shutil
import stat
import threading
import unicodedata

from .config import TEMP_SUFFIX

PROBE_TIMEOUT = 10  # 根目录探测的最长秒数：挂死的 NFS/FUSE 挂载会让 stat 永远不返回
# 文件事件跨主机传不过来、或属性有缓存的挂载类型：自动改用轮询并加长稳定期（与 MoviePilot 的判断一致）
NETWORK_FS = ('nfs', 'nfs4', 'cifs', 'smb3', 'smbfs', 'sshfs', 'fuse', '9p', 'davfs', 'ceph', 'glusterfs')
LOCAL_FUSE = ('fuse.shfs', 'fuseblk')  # Unraid 阵列与 NTFS/exFAT 等本地 FUSE，不算网络盘


def mount_types(paths):
    """返回 {路径: 所在挂载的文件系统类型}（/proc/self/mountinfo 只读一次）；无法确定时为 None。"""
    try:
        lines = open('/proc/self/mountinfo', encoding='utf-8', errors='replace').read().splitlines()
    except OSError:
        return {path: None for path in paths}
    mounts = []
    for line in lines:
        left, _, right = line.partition(' - ')
        fields, tail = left.split(), right.split()
        if len(fields) >= 5 and tail:
            mounts.append((fields[4].replace('\\040', ' '), tail[0]))
    result = {}
    for path in paths:
        try:
            real = probe(path, lambda p: os.path.realpath(str(p)))  # 挂死的挂载上 realpath 也会卡住
        except OSError:
            real = str(path)
        result[path] = max(((len(point), fstype) for point, fstype in mounts
                            if real == point or real.startswith(point.rstrip('/') + '/')), default=(0, None))[1]
    return result


def is_network_fs(fstype):
    """是否为网络盘或远程 FUSE 挂载（极空间 zfuse、Unraid shfs 等本地 FUSE 不算）。"""
    if not fstype or fstype in LOCAL_FUSE or fstype.startswith('zfuse'):
        return False
    return fstype.split('.')[0] in NETWORK_FS


class ProbeTimeout(OSError):
    """探测在限定时间内没有返回（挂载挂死）。"""


class _Prober:
    """某一路径专用的常驻探测线程：依次执行提交的探测，不必每次探测都新起线程。"""

    def __init__(self):
        """启动守护线程；hung 表示有探测超时仍未返回。"""
        self.jobs = queue.SimpleQueue()
        self.hung = False
        threading.Thread(target=self._run, daemon=True, name='localmirror115-probe').start()

    def _run(self):
        """逐个执行探测，记下结果或异常（原样转交给调用方）；挂死的探测返回后解除 hung。"""
        while True:
            func, path, done, result = self.jobs.get()
            try:
                result['value'] = func(path)
            except BaseException as error:
                result['error'] = error
            self.hung = False
            done.set()


_probers = {}
_probers_lock = threading.Lock()


def probe(path, func, timeout=PROBE_TIMEOUT):
    """在该路径的探测线程中执行 func(path)，超时抛出 ProbeTimeout，防止挂死的挂载卡住工作线程。
    挂死期间同一路径的探测立即报错，不再排队等待。"""
    key = str(path)
    with _probers_lock:
        prober = _probers.get(key) or _probers.setdefault(key, _Prober())
    if prober.hung:
        raise ProbeTimeout('mount_probe_timeout')
    done, result = threading.Event(), {}
    prober.jobs.put((func, path, done, result))
    if not done.wait(timeout):
        prober.hung = True
        if done.is_set():
            prober.hung = False  # 恰好在标记前返回
        raise ProbeTimeout('mount_probe_timeout')
    if 'error' in result:
        raise result['error']
    return result['value']


def _is_empty(path):
    """目录是否为空（只读第一个条目）。"""
    with os.scandir(path) as entries:
        return next(entries, None) is None


@dataclass(frozen=True)
class Observation:
    """单个路径的观测结果：状态、指纹（大小/时间/设备/inode）与原因。"""

    status: str
    fingerprint: dict = None
    reason: str = ''

    def to_dict(self):
        """转换为字典。"""
        return asdict(self)


class ScanEntry:
    """扫描清单中的一项：接口同 Observation，用 __slots__ 节省内存（30 万文件时约为其三分之一）。"""

    __slots__ = ('status', 'size', 'mtime_ns', 'ctime_ns', 'dev', 'ino', 'reason')

    def __init__(self, status, info=None, reason=''):
        """由 stat 结果构造；info 为 None 表示没有指纹（冲突项）。"""
        self.status, self.reason = status, reason
        if info is None:
            self.size = self.mtime_ns = self.ctime_ns = self.dev = self.ino = None
        else:
            self.size, self.mtime_ns, self.ctime_ns = info.st_size, info.st_mtime_ns, info.st_ctime_ns
            self.dev, self.ino = info.st_dev, info.st_ino

    @property
    def fingerprint(self):
        """与 Observation 同结构的指纹字典；没有指纹时为 None。"""
        if self.size is None:
            return None
        return {'size': self.size, 'mtime_ns': self.mtime_ns, 'ctime_ns': self.ctime_ns,
                'dev': self.dev, 'ino': self.ino}

    def to_dict(self):
        """转换为与 Observation.to_dict() 相同的字典。"""
        return {'status': self.status, 'fingerprint': self.fingerprint, 'reason': self.reason}


class ReadOnlyFilesystem:
    """只读观测本地源（A）：绑定并校验根目录身份，观测单个路径并生成完整扫描清单。"""

    def __init__(self, follow_symlinks=False):
        """初始化根身份表与空占位根的标记；follow_symlinks 时跟随指向文件的软链接。"""
        self.roots = {}
        self.placeholders = {}
        self.follow_symlinks = follow_symlinks
        self.local = threading.local()  # 当前线程这一批观测共用的目录列表（见 batch）

    @contextmanager
    def batch(self):
        """一批观测共用目录列表：同一目录里多个文件核对名字大小写时只列一次目录。
        批次结束即丢弃，不跨批沿用，不会拿旧的列表判断后来的变化。"""
        listings = {}

        def names(parent):
            """返回目录下的文件名集合（NFC），本批内缓存。"""
            if parent not in listings:
                listings[parent] = {unicodedata.normalize('NFC', n) for n in os.listdir(parent)}
            return listings[parent]
        self.local.names = names
        try:
            yield
        finally:
            self.local.names = None

    def bind_root(self, root: Path):
        """绑定源根并记录其 (设备号, inode)；已绑定根的身份变化时抛出 OSError（由 rebind 处理）。"""
        current = self.identity(root)
        previous = self.roots.get(str(root))
        if previous is not None and previous != current:
            raise OSError('本地实体库根目录已被替换，需要重新初始化')
        self._remember(root, current)

    def _remember(self, root, identity):
        """记下根身份，以及它是否为空占位目录。"""
        empty = probe(root, _is_empty)
        self.roots[str(root)] = identity
        # 绑定时为空的根通常是一个尚在等待磁盘挂载的挂载点。
        self.placeholders[str(root)] = empty

    def rebind(self, root: Path):
        """根目录身份变了（启动后才挂载、NFS/SMB 重连、FUSE 重新挂载）时接受新身份，返回是否重新绑定。
        调用方须把映射置回“等待扫描”，重新扫描并核对已知文件之前不允许删除。"""
        try:
            current = self.identity(root)
        except OSError:
            return False
        if current == self.roots.get(str(root)):
            return False
        self._remember(root, current)
        return True

    @staticmethod
    def identity(root):
        """返回根目录的 (st_dev, st_ino) 身份（带超时探测）；路径含符号链接或不是目录时抛出 OSError。"""
        def read(path):
            """逐级检查符号链接后 stat 根目录。"""
            for component in (path,) + tuple(path.parents):
                if component.is_symlink():
                    raise OSError('本地实体库路径中有符号链接')
            info = path.stat()
            if not stat.S_ISDIR(info.st_mode):
                raise OSError('本地实体库路径不是目录')
            return (info.st_dev, info.st_ino)
        return probe(root, read)

    def healthy(self, root):
        """判断源根当前可访问（探测不超时）且身份与绑定时一致。"""
        try:
            return self.roots.get(str(root)) == self.identity(root)
        except OSError:
            return False

    def observe(self, root: Path, relative: str, checked: bool = False) -> Observation:
        """观测单个相对路径：返回存在（附指纹）、目录、冲突、疑似缺失或未知；只在开启时跟随指向文件的软链接。
        checked=True 表示调用方已在这一批前后确认过源根健康，不再逐个检查。"""
        rel = PurePosixPath(relative)
        if rel.is_absolute() or '..' in rel.parts or not rel.parts:
            return Observation('conflict', reason='invalid_relative_path')
        if not checked and not self.healthy(root):
            return Observation('unknown', reason='source_root_unavailable_or_changed')
        target = root.joinpath(*rel.parts)
        current = root
        try:
            for index, part in enumerate(rel.parts):
                current = current / part
                if current.is_symlink():
                    if index < len(rel.parts) - 1:
                        return Observation('conflict', reason='symlink_not_followed')  # 只跟随指向文件的软链接
                    info, problem = self._follow(True, lambda follow: target.stat())
                    if problem:
                        return Observation(problem[0], reason=problem[1])
                    return Observation('present', ScanEntry('present', info).fingerprint)
            info = target.stat()
            if stat.S_ISDIR(info.st_mode):
                return Observation('directory')
            if not stat.S_ISREG(info.st_mode):
                return Observation('conflict', reason='special_file_not_supported')
            if not exact_name(target, getattr(self.local, 'names', None)):
                # 大小写不敏感的盘上只改了大小写：旧名还能 stat 到，但目录里已经没有这个精确的名字
                return Observation('missing_candidate', reason='renamed_case_only')
            return Observation('present', ScanEntry('present', info).fingerprint)
        except FileNotFoundError:
            return Observation('missing_candidate', reason='path_absent_not_authorized_for_deletion')
        except OSError:
            return Observation('unknown', reason='filesystem_query_failed')

    def _follow(self, linked, read):
        """按软链接设置取文件属性，read(follow) 执行 stat。返回 (stat 结果, None) 或 (None, (状态, 原因))：
        不跟随时软链接为冲突；跟随时悬空链接为未知（不算删除），指向非普通文件的为冲突。"""
        if not linked:
            return read(False), None
        if not self.follow_symlinks:
            return None, ('conflict', 'symlink_not_followed')
        try:
            info = read(True)
        except FileNotFoundError:
            return None, ('unknown', 'symlink_target_missing')
        if not stat.S_ISREG(info.st_mode):
            return None, ('conflict', 'symlink_not_followed')
        return info, None

    def ancestor_device(self, root, relative):
        """返回已消失路径最近的现存上级目录（最多到映射根）的设备号；无法确定时返回 None。"""
        parent = PurePosixPath(relative).parent
        try:
            while str(parent) != '.':
                try:
                    return os.lstat(root.joinpath(*parent.parts)).st_dev
                except FileNotFoundError:
                    parent = parent.parent
            return os.lstat(root).st_dev
        except OSError:
            return None

    def is_file(self, root, relative):
        """路径当前是否为普通文件（开启跟随软链接时也接受指向文件的软链接）。只用来证明所在的盘在线，
        比 observe 轻：不检查上级软链接和名字大小写，悬空链接也算不在。"""
        path = root.joinpath(*PurePosixPath(relative).parts)
        try:
            return stat.S_ISREG((os.stat(path) if self.follow_symlinks else os.lstat(path)).st_mode)
        except OSError:
            return False

    def scan(self, root, limit, stop, excluded=lambda relative: False, progress=None, start='', unreadable=None):
        """返回扫描清单 {相对路径: ScanEntry}；根目录不可用等整体问题抛出异常，不完整的清单绝不作为基线。
        读不了的子目录（权限不足等）不让整个扫描失败：记入 unreadable（相对路径列表），其中已知的文件由调用方视为未知。
        每个目录回调一次 progress；指定 start 时只遍历该子目录（键仍相对 root），start 已不是真实目录时返回空清单。"""
        if not self.healthy(root):
            raise OSError('本地实体库根目录不可用或已变化')
        result = {}
        top = root.joinpath(*PurePosixPath(start).parts) if start else root
        if start and (top.is_symlink() or not top.is_dir()):
            return result
        cut = len(str(root)) + 1
        stack = [str(top)]
        while stack:
            if stop.is_set():
                raise InterruptedError('scan_stopped')
            folder = stack.pop()
            if progress is not None:
                progress(len(result), folder[cut:])
            try:
                entries = os.scandir(folder)
            except (PermissionError, FileNotFoundError):
                if folder == str(top) or unreadable is None:
                    raise  # 扫描起点本身读不了：整体失败
                unreadable.append(folder[cut:])
                continue
            with entries:
                for item in entries:
                    relative = item.path[cut:]
                    if item.is_dir(follow_symlinks=False):
                        if not excluded(relative + '/x'):  # 以子路径判断，名为 *.strm 的目录不被误排除
                            stack.append(item.path)
                        continue
                    if excluded(relative):
                        continue
                    result[relative] = self._entry(item)
                    if len(result) > limit:
                        raise OSError('scan_file_limit_exceeded')
        if not self.healthy(root):
            raise OSError('扫描期间本地实体库根目录发生变化')
        return result


    def _entry(self, item):
        """生成一个扫描项：普通文件带指纹；软链接按设置跟随或记为冲突；读不了的记为未知。"""
        try:
            info, problem = self._follow(item.is_symlink(), lambda follow: item.stat(follow_symlinks=follow))
        except OSError:
            return ScanEntry('unknown', reason='filesystem_query_failed')
        if problem:
            return ScanEntry(problem[0], reason=problem[1])
        if stat.S_ISREG(info.st_mode):
            return ScanEntry('present', info)
        return ScanEntry('conflict', reason='special_file_not_supported')


class OutputError(Exception):
    """C（STRM 目录）写入被拒绝或失败时抛出的异常。"""


def sha256(path, chunk=1024 * 1024):
    """返回文件的 SHA-256 十六进制摘要。"""
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def same_bytes(a, b, chunk=1024 * 1024, digest=None):
    """逐块比较两个文件内容是否完全一致；给出 digest 时顺带把内容计入摘要，省去再读一遍算 SHA-256。"""
    with open(a, 'rb') as fa, open(b, 'rb') as fb:
        while True:
            x, y = fa.read(chunk), fb.read(chunk)
            if x != y:
                return False
            if not x:
                return True
            if digest is not None:
                digest.update(y)


def exact_name(path, listing=None):
    """已存在的 path 在目录里是否就是这个大小写的名字（不区分大小写的盘上换了大小写也能 stat 到）。
    文件名按 NFC 比较（macOS 等可能以分解形式保存）；换大小写后找不到同名项的大小写敏感目录不必列目录。"""
    swapped = path.name.swapcase()
    if swapped == path.name or not os.path.lexists(path.with_name(swapped)):
        return True
    names = listing(path.parent) if listing else {unicodedata.normalize('NFC', n) for n in os.listdir(path.parent)}
    return unicodedata.normalize('NFC', path.name) in names


class NameCache:
    """目录下文件名集合（NFC）的缓存，按目录修改时间失效；只用于插件自己写入的 STRM 目录，写入后由写入方清除。"""

    def __init__(self):
        """初始化缓存：目录 -> (修改时间, 文件名集合)。"""
        self.listings = {}

    def __call__(self, parent):
        """返回目录下的文件名集合。"""
        mtime = parent.stat().st_mtime_ns
        cached = self.listings.get(parent)
        if cached is None or cached[0] != mtime:
            if len(self.listings) > 256:
                self.listings.clear()
            cached = self.listings[parent] = (mtime, {unicodedata.normalize('NFC', n) for n in os.listdir(parent)})
        return cached[1]

    def drop(self, parent):
        """目录内容被本插件改过：丢弃缓存（目录修改时间精度可能只有 1 秒）。"""
        self.listings.pop(parent, None)


class LocalOutput:
    """C 的唯一写入器。绝不写到输出根之外，绝不跟随符号链接，也绝不触碰 STRM 或视频文件。"""

    def __init__(self, root: Path, is_video):
        """绑定输出根目录与视频文件判断函数。"""
        self.root = root
        self.is_video = is_video
        self.names = NameCache()  # 不区分大小写的 STRM 目录上核对名字大小写用

    def _exact(self, path):
        """目标已存在时，确认它的名字大小写与要求的一致；无法确认时按一致处理。"""
        try:
            return exact_name(path, self.names)
        except OSError:
            return True

    def _target(self, relative):
        """校验相对路径并返回输出根下的目标路径；不允许写入时抛出 OutputError。"""
        rel = PurePosixPath(relative)
        if rel.is_absolute() or '..' in rel.parts or not rel.parts:
            raise OutputError('invalid_output_path')
        if rel.suffix.lower() == '.strm' or self.is_video(relative):
            raise OutputError('strm_or_video_not_owned')
        if not self.root.is_dir() or self.root.is_symlink():
            raise OutputError('output_root_unavailable')
        path = self.root.joinpath(*rel.parts)
        current = self.root
        for part in rel.parts:
            current = current / part
            if current.is_symlink():
                raise OutputError('output_symlink')
        return path

    def status(self, relative):
        """返回目标文件状态字典：present（含大小与修改时间）、missing、conflict 或 unknown。"""
        try:
            path = self._target(relative)
            info = path.stat()
            if not self._exact(path):
                return {'status': 'missing'}  # 只是大小写不同的另一个名字：按缺失处理，复制时改成正确的大小写
        except FileNotFoundError:
            return {'status': 'missing'}
        except OutputError as error:
            return {'status': 'conflict', 'reason': str(error)}
        except OSError:
            return {'status': 'unknown'}
        if not stat.S_ISREG(info.st_mode):
            return {'status': 'conflict', 'reason': 'output_not_regular_file'}
        return {'status': 'present', 'size': info.st_size, 'mtime_ns': info.st_mtime_ns}

    def matches(self, relative, source):
        """目标文件存在且与源文件字节完全一致时返回目标的 SHA-256（比对时顺带算出），否则返回 None。"""
        if self.status(relative)['status'] != 'present':
            return None
        digest = hashlib.sha256()
        return digest.hexdigest() if same_bytes(source, self._target(relative), digest=digest) else None

    def copy(self, relative, source):
        """经临时文件写入并校验后原子替换，返回写入后的指纹。"""
        target = self._target(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        current = self.root
        for part in PurePosixPath(relative).parts[:-1]:
            current = current / part
            if current.is_symlink() or not current.is_dir():
                raise OutputError('output_parent_not_directory')
        if target.exists() and not target.is_file():
            raise OutputError('output_not_regular_file')
        # 不区分大小写的盘上覆盖只差大小写的旧文件会保留旧名字：复制后经中间名改成源文件的大小写
        fix_case = target.exists() and not self._exact(target)
        temp = target.with_name('.' + target.name + TEMP_SUFFIX)
        try:
            shutil.copyfile(source, temp)
            info = Path(source).stat()
            os.utime(temp, ns=(info.st_atime_ns, info.st_mtime_ns))
            digest = hashlib.sha256()
            if not same_bytes(source, temp, digest=digest):
                raise OutputError('copy_verification_failed')
            os.replace(temp, target)
            if fix_case:
                os.rename(target, temp)
                os.rename(temp, target)
            self.names.drop(target.parent)
        finally:
            if temp.exists():
                temp.unlink()
        return self.identity(relative, digest.hexdigest())

    def identity(self, relative, sha=None):
        """返回目标文件的指纹：大小、修改时间与 SHA-256（sha 为比对时已算出的摘要，省去再读一遍）。"""
        target = self._target(relative)
        info = target.stat()
        return {'size': info.st_size, 'mtime_ns': info.st_mtime_ns, 'sha256': sha or sha256(target)}

    def remove(self, relative, expected):
        """仅当副本仍与记录的指纹一致时删除，并清理随之变空的父目录；被他人修改的文件保留。返回结果标记。"""
        target = self._target(relative)
        try:
            info = target.stat()
        except FileNotFoundError:
            return 'already_absent'
        if not self._exact(target):
            return 'already_absent'  # 只剩大小写不同的另一个文件（只改了大小写的重命名），它不是这个记录的
        if not stat.S_ISREG(info.st_mode):
            raise OutputError('output_not_regular_file')
        if (not expected or info.st_size != expected.get('size') or info.st_mtime_ns != expected.get('mtime_ns')
                or sha256(target) != expected.get('sha256')):
            return 'changed_by_others_kept'
        target.unlink()
        self.names.drop(target.parent)
        parent = target.parent
        while parent != self.root and self.root in parent.parents:
            try:
                parent.rmdir()  # 只有目录为空时才会成功，因此含 STRM 的目录得以保留
            except OSError:
                break
            parent = parent.parent
        return 'deleted'
