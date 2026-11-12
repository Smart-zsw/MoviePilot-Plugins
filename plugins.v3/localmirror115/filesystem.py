"""只读的本地源文件系统观测，以及 C（STRM 目录）的唯一写入器。状态未知绝不会被当作文件不存在。"""
from dataclasses import dataclass, asdict
from pathlib import Path, PurePosixPath
import hashlib
import os
import shutil
import stat

from .config import TEMP_SUFFIX


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

    def __init__(self):
        """初始化根身份表与空占位根标记。"""
        self.roots = {}
        self.placeholders = {}

    def bind_root(self, root: Path):
        """绑定源根并记录其 (设备号, inode)；已绑定根的身份变化时抛出 OSError。"""
        current = self._root_identity(root)
        previous = self.roots.get(str(root))
        if previous is not None and previous != current:
            raise OSError('本地实体库根目录已被替换，需要重新初始化')
        self.roots[str(root)] = current
        # 绑定时为空的根通常是一个尚在等待磁盘挂载的挂载点。
        self.placeholders[str(root)] = not any(True for _ in os.scandir(root))

    def rebind_placeholder(self, root: Path):
        """仅当已绑定的根是空占位目录时接受新身份（如启动后才挂载的 NAS 卷），返回是否重新绑定。
        有内容的根身份变化仍需显式重新初始化。"""
        if not self.placeholders.get(str(root)):
            return False
        try:
            current = self._root_identity(root)
        except OSError:
            return False
        if current == self.roots.get(str(root)):
            return False
        self.roots[str(root)] = current
        self.placeholders[str(root)] = not any(True for _ in os.scandir(root))
        return True

    @staticmethod
    def _root_identity(root):
        """返回根目录的 (st_dev, st_ino) 身份；路径含符号链接或不是目录时抛出 OSError。"""
        for component in (root,) + tuple(root.parents):
            if component.is_symlink():
                raise OSError('本地实体库路径中有符号链接')
        info = root.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise OSError('本地实体库路径不是目录')
        return (info.st_dev, info.st_ino)

    def healthy(self, root):
        """判断源根当前可访问且身份与绑定时一致。"""
        try:
            return self.roots.get(str(root)) == self._root_identity(root)
        except OSError:
            return False

    def observe(self, root: Path, relative: str, checked: bool = False) -> Observation:
        """观测单个相对路径：返回存在（附指纹）、目录、冲突、疑似缺失或未知；不跟随符号链接。
        checked=True 表示调用方已在这一批前后确认过源根健康，不再逐个检查。"""
        rel = PurePosixPath(relative)
        if rel.is_absolute() or '..' in rel.parts or not rel.parts:
            return Observation('conflict', reason='invalid_relative_path')
        if not checked and not self.healthy(root):
            return Observation('unknown', reason='source_root_unavailable_or_changed')
        target = root.joinpath(*rel.parts)
        current = root
        try:
            for part in rel.parts:
                current = current / part
                if current.is_symlink():
                    return Observation('conflict', reason='symlink_not_followed')
            info = target.stat()
            if stat.S_ISDIR(info.st_mode):
                return Observation('directory')
            if not stat.S_ISREG(info.st_mode):
                return Observation('conflict', reason='special_file_not_supported')
            return Observation('present', {'size': info.st_size, 'mtime_ns': info.st_mtime_ns,
                'ctime_ns': info.st_ctime_ns, 'dev': info.st_dev, 'ino': info.st_ino})
        except FileNotFoundError:
            return Observation('missing_candidate', reason='path_absent_not_authorized_for_deletion')
        except OSError:
            return Observation('unknown', reason='filesystem_query_failed')

    def ancestor_device(self, root, relative):
        """返回已消失路径最近的现存祖先所在设备号，无法确定时返回 None。
        子挂载卸载后会在父设备上留下空挂载点，据此可把其中的文件视为未知而非已删除。"""
        parent = root.joinpath(*PurePosixPath(relative).parts).parent
        try:
            while parent != root and root in parent.parents:
                try:
                    return os.lstat(parent).st_dev
                except FileNotFoundError:
                    parent = parent.parent
            return os.lstat(root).st_dev
        except OSError:
            return None

    def scan(self, root, limit, stop, excluded=lambda relative: False, progress=None, start=''):
        """返回完整扫描清单 {相对路径: ScanEntry}，否则抛出异常；不完整的清单绝不作为基线。
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
            with os.scandir(folder) as entries:  # 读取失败直接抛出：扫描整体失败而不是跳过
                for item in entries:
                    relative = item.path[cut:]
                    if item.is_symlink():
                        if not excluded(relative):
                            result[relative] = ScanEntry('conflict', reason='symlink_not_followed')
                    elif item.is_dir(follow_symlinks=False):
                        if not excluded(relative + '/x'):  # 以子路径判断，名为 *.strm 的目录不被误排除
                            stack.append(item.path)
                        continue
                    elif not excluded(relative):
                        info = item.stat(follow_symlinks=False)
                        result[relative] = (ScanEntry('present', info) if stat.S_ISREG(info.st_mode) else
                                            ScanEntry('conflict', reason='special_file_not_supported'))
                    if len(result) > limit:
                        raise OSError('scan_file_limit_exceeded')
        if not self.healthy(root):
            raise OSError('扫描期间本地实体库根目录发生变化')
        return result


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


class LocalOutput:
    """C 的唯一写入器。绝不写到输出根之外，绝不跟随符号链接，也绝不触碰 STRM 或视频文件。"""

    def __init__(self, root: Path, is_video):
        """绑定输出根目录与视频文件判断函数。"""
        self.root = root
        self.is_video = is_video

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
        temp = target.with_name('.' + target.name + TEMP_SUFFIX)
        try:
            shutil.copyfile(source, temp)
            info = Path(source).stat()
            os.utime(temp, ns=(info.st_atime_ns, info.st_mtime_ns))
            digest = hashlib.sha256()
            if not same_bytes(source, temp, digest=digest):
                raise OutputError('copy_verification_failed')
            os.replace(temp, target)
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
        if not stat.S_ISREG(info.st_mode):
            raise OutputError('output_not_regular_file')
        if (not expected or info.st_size != expected.get('size') or info.st_mtime_ns != expected.get('mtime_ns')
                or sha256(target) != expected.get('sha256')):
            return 'changed_by_others_kept'
        target.unlink()
        parent = target.parent
        while parent != self.root and self.root in parent.parents:
            try:
                parent.rmdir()  # 只有目录为空时才会成功，因此含 STRM 的目录得以保留
            except OSError:
                break
            parent = parent.parent
        return 'deleted'
