"""经过校验的插件配置。任何写入都需要 mode='sync' 且对应目标的开关已开启。"""
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path, PurePosixPath
import fnmatch
import hashlib
import json
import re

DEFAULT_VIDEO_EXTENSIONS = ('.mp4', '.mkv', '.ts', '.iso', '.rmvb', '.avi', '.mov', '.mpeg', '.mpg',
                            '.wmv', '.3gp', '.asf', '.m4v', '.flv', '.m2ts', '.tp', '.f4v',
                            '.vob', '.webm', '.mts', '.m2t', '.rm', '.divx', '.ogm')
VIDEO_EXTENSIONS = frozenset(DEFAULT_VIDEO_EXTENSIONS)
TEMP_SUFFIX = '.lm115tmp'  # 复制到 STRM 目录时的临时文件后缀
# 系统垃圾文件与插件自身的临时文件，绝不镜像。
DEFAULT_EXCLUDES = ('.DS_Store', 'Thumbs.db', 'desktop.ini', '._*', '*.tmp', '*.part', '*.!qb',
                    '*' + TEMP_SUFFIX, '@eaDir', '#recycle', '#snapshot', '.snapshot', 'lost+found')
MODES = ('dry_run', 'sync')


def parse_mappings(value):
    """解析映射配置：每行一条 id#local_source#115_remote#cloud_output（也接受 JSON 列表）。"""
    if isinstance(value, list):
        return value
    text = (value or '').strip()
    if not text:
        return []
    if text.startswith('['):
        return json.loads(text)
    rows = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('//'):
            continue
        parts = [p.strip() for p in line.split('#')]
        if len(parts) != 4 or not all(parts):
            raise ValueError('映射第 %d 行格式应为：ID#本地实体库路径#115路径#STRM目录路径' % number)
        rows.append({'id': parts[0], 'source': parts[1], 'remote': parts[2], 'output': parts[3]})
    return rows


WATCH_MODES = ('fast', 'compatibility', 'off')  # 表单只提供前两种；'off' 由 watch_enabled=False 推导
DEFAULT_SCAN_CRON = '0 4 * * 3,6'   # 每周三、六 04:00


@dataclass(frozen=True)
class Mapping:
    """一条目录映射：A（本地源）、B（115 远端）与 C（STRM 输出）的根路径及映射 id。"""

    key: str
    source: Path
    remote: PurePosixPath
    output: Path


@dataclass(frozen=True)
class Config:
    """不可变的插件配置，提供派生阈值、修订标识与文件过滤。"""

    mappings: tuple
    enabled: bool = False
    mode: str = 'dry_run'
    watch_enabled: bool = True
    transfer_event_enabled: bool = True
    scan_enabled: bool = False        # 仅作兜底：变化主要由监控和整理完成事件捕获
    adopt_existing: bool = True
    copy_enabled: bool = False
    upload_enabled: bool = False
    delete_enabled: bool = False
    delete_b_enabled: bool = False
    delete_c_enabled: bool = True
    delete_delay_hours: float = 0.0
    settle_seconds: float = 5.0
    scan_seconds: float = 900.0
    force_polling: bool = False
    max_files: int = 2000000
    video_extensions: frozenset = VIDEO_EXTENSIONS
    excludes: tuple = DEFAULT_EXCLUDES
    verify_window_minutes: float = 30.0
    poll_seconds: float = 10.0
    compare_mode: str = 'tree'           # tree：导出目录树（cookie）；detail：逐目录 Open API 列表
    p115_cookie: str = field(default='', repr=False)  # 不进入 repr，避免泄露到日志
    remote_refresh_hours: float = 24.0
    tree_timeout_minutes: float = 60.0
    api_qps: float = 2.0
    failure_threshold: int = 3
    cooldown_base_seconds: float = 60.0
    cooldown_max_seconds: float = 1800.0
    notify_enabled: bool = True
    scan_cron: str = DEFAULT_SCAN_CRON
    watch_mode: str = 'fast'          # fast：文件系统事件；compatibility：轮询；off：关闭

    @staticmethod
    def _hash(*parts):
        """对各部分的字符串形式计算 SHA-256 摘要。"""
        return hashlib.sha256(json.dumps([str(p) for p in parts], ensure_ascii=False).encode()).hexdigest()

    @property
    def revision(self):
        """每个映射 A 根的身份标识（映射 id -> 摘要）。"""
        # B、C 各有修订标识，修改一个目标路径不会重置另一个。
        return {m.key: self._hash(m.key, m.source) for m in self.mappings}

    @property
    def revision_b(self):
        """每个映射 A 根加 B 根的修订标识。"""
        return {m.key: self._hash(m.key, m.source, m.remote) for m in self.mappings}

    @property
    def revision_c(self):
        """每个映射 A 根加 C 根的修订标识。"""
        return {m.key: self._hash(m.key, m.source, m.output) for m in self.mappings}

    @property
    def writes(self):
        """是否为允许写入的 sync 模式。"""
        return self.mode == 'sync'

    def is_video(self, relative):
        """按扩展名判断是否为视频文件。"""
        return PurePosixPath(relative).suffix.lower() in self.video_extensions

    @cached_property
    def _exclude_pattern(self):
        """把全部排除规则编译成一个不区分大小写的正则，扫描时每个路径段只需匹配一次。"""
        return re.compile('|'.join(fnmatch.translate(pattern.lower()) for pattern in self.excludes) or r'(?!)')

    def excluded(self, relative):
        """判断相对路径是否应排除：空路径、.strm 文件或任一路径段匹配排除规则。"""
        parts = relative.lower().split('/')
        if not relative or parts[-1].endswith('.strm'):
            return True
        match = self._exclude_pattern.match
        return any(match(part) for part in parts)

    @classmethod
    def parse(cls, raw):
        """把原始设置解析并校验为 Config；不合法时抛出 ValueError。"""
        raw = {key: value for key, value in (raw or {}).items() if value is not None and value != ''}  # 清空的项用默认值
        mode = raw.get('mode') or 'dry_run'
        if mode not in MODES:
            raise ValueError('运行模式只能是试运行或同步')
        rows = parse_mappings(raw.get('mappings', []))
        mappings = []
        for row in rows:
            key = str(row['id'])
            source, output = Path(row['source']), Path(row['output'])
            remote = PurePosixPath(row['remote'])
            if not key or len(key) > 64 or not all(c.isalnum() or c in '-_' for c in key):
                raise ValueError('映射 ID 只能包含字母、数字、中文、横线或下划线')
            if not source.is_absolute() or not output.is_absolute() or not remote.is_absolute():
                raise ValueError('映射中的路径都必须是绝对路径')
            if '..' in source.parts + output.parts + remote.parts:
                raise ValueError('映射路径不能包含 ..')
            if source == Path('/') or output == Path('/') or remote == PurePosixPath('/'):
                raise ValueError('不能把根目录作为映射路径')
            if source.exists() and source.resolve() != source:
                raise ValueError('本地实体库路径不能包含符号链接，请填写实际路径：%s' % source.resolve())
            mappings.append(Mapping(key, source, remote, output))
        if len({m.key for m in mappings}) != len(mappings):
            raise ValueError('映射 ID 重复')
        # 拒绝符号链接别名及所有相互嵌套的本地根（包括跨映射的根）。
        local = [p.resolve() for m in mappings for p in (m.source, m.output)]
        for i, root in enumerate(local):
            for other in local[i + 1:]:
                if root == other or root in other.parents or other in root.parents:
                    raise ValueError('各映射的本地实体库与STRM目录不能相同或互相包含')
        remote_roots = [m.remote for m in mappings]
        for i, root in enumerate(remote_roots):
            for other in remote_roots[i + 1:]:
                if root == other or root in other.parents or other in root.parents:
                    raise ValueError('各映射的115路径不能相同或互相包含')
        settle = float(raw.get('settle_seconds', 5))
        # 低于 60 秒的值钳制到下限，而不是拒绝启动。
        scan = max(60.0, float(raw.get('scan_seconds', 900)))
        limit = int(raw.get('max_files', 2000000))
        delay = float(raw.get('delete_delay_hours', 0))
        if not 0.05 <= settle <= 300 or scan > 86400 or not 1 <= limit <= 10000000:
            raise ValueError('稳定观察间隔、扫描间隔或文件数上限超出范围')
        if not 0 <= delay <= 24 * 90:
            raise ValueError('删除延迟超出范围（0～2160 小时）')
        verify_window = max(5.0, float(raw.get('verify_window_minutes', 30)))
        if verify_window > 24 * 60 * 7:
            raise ValueError('上传确认等待超出范围（5 分钟～7 天）')
        poll = float(raw.get('poll_seconds', 10))
        if not 1 <= poll <= 3600:
            raise ValueError('轮询间隔超出范围（1～3600 秒）')
        compare = raw.get('compare_mode') or 'tree'
        if compare not in ('tree', 'detail'):
            raise ValueError('115比对方式只能是目录树或逐个详细比对')
        refresh = max(1.0, float(raw.get('remote_refresh_hours', 24)))
        tree_timeout = min(max(5.0, float(raw.get('tree_timeout_minutes', 60))), 24 * 60)
        qps = float(raw.get('api_qps', 2))
        threshold = int(raw.get('failure_threshold', 3))
        cool_base = float(raw.get('cooldown_base_seconds', 60))
        cool_max = float(raw.get('cooldown_max_seconds', 1800))
        if (not 0.1 <= qps <= 20 or not 1 <= threshold <= 100 or not 1 <= cool_base <= 86400
                or not cool_base <= cool_max <= 7 * 86400):
            raise ValueError('115访问与风控设置超出范围')

        videos = raw.get('video_extensions') or ','.join(DEFAULT_VIDEO_EXTENSIONS)
        video_set = frozenset('.' + e.strip().lower().lstrip('.') for e in str(videos).split(',') if e.strip())
        excludes = raw.get('excludes')
        if not excludes:
            exclude_list = DEFAULT_EXCLUDES
        else:
            exclude_list = tuple(e.strip() for e in str(excludes).replace(',', '\n').splitlines() if e.strip())
            # 无论用户如何配置，插件自身的临时文件都必须保持排除。
            exclude_list = tuple(dict.fromkeys(exclude_list + ('*' + TEMP_SUFFIX,)))
        cron = ' '.join(str(raw.get('scan_cron') or DEFAULT_SCAN_CRON).split())
        if len(cron.split()) != 5:
            raise ValueError('定时扫描周期须为5段cron表达式，例如 %s' % DEFAULT_SCAN_CRON)
        watch_mode = raw.get('watch_mode') or None
        if watch_mode is None:  # 未设置监控模式时由 watch_enabled / force_polling 推导
            watch_mode = ('off' if not raw.get('watch_enabled', True)
                          else 'compatibility' if raw.get('force_polling') else 'fast')
        if watch_mode not in WATCH_MODES:
            raise ValueError('监控模式只能是性能模式或兼容模式')
        flag = lambda key, default: bool(raw.get(key, default))
        return cls(
            mappings=tuple(mappings), enabled=flag('enabled', False), mode=mode,
            watch_enabled=watch_mode != 'off', transfer_event_enabled=flag('transfer_event_enabled', True),
            scan_enabled=flag('scan_enabled', False), adopt_existing=flag('adopt_existing', True),
            copy_enabled=flag('copy_enabled', False), upload_enabled=flag('upload_enabled', False),
            delete_enabled=flag('delete_enabled', False), delete_b_enabled=flag('delete_b_enabled', False),
            delete_c_enabled=flag('delete_c_enabled', True), delete_delay_hours=delay,
            settle_seconds=settle, scan_seconds=scan, force_polling=watch_mode == 'compatibility',
            max_files=limit, video_extensions=video_set, excludes=exclude_list,
            verify_window_minutes=verify_window, poll_seconds=poll, compare_mode=compare,
            p115_cookie=str(raw.get('p115_cookie') or '').strip(), remote_refresh_hours=refresh,
            tree_timeout_minutes=tree_timeout, api_qps=qps, failure_threshold=threshold,
            cooldown_base_seconds=cool_base, cooldown_max_seconds=cool_max,
            notify_enabled=flag('notify_enabled', True), scan_cron=cron, watch_mode=watch_mode)
