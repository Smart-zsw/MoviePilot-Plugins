"""本地115镜像插件入口：配置表单、生命周期、事件、API、定时服务和页面。"""
import time

from apscheduler.triggers.cron import CronTrigger

from app.schemas.response import Response
from app.schemas.types import EventType, MessageType
from app.sdk.events import eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

from .adapters import build_host_remote, transfer_target
from .config import Config, DEFAULT_EXCLUDES, DEFAULT_SCAN_CRON, DEFAULT_VIDEO_EXTENSIONS
from .engine import SyncEngine, notification_state
from .notify import Notifier
from .persistence import Asset, Pending, Repository

STATS_TTL = 10  # 状态页与仪表盘数据库统计的缓存秒数

MAPPING_EXAMPLE = ('电影#/media/movies#/115/movies#/strm/movies\n'
                   '剧集#/media/tv#/115/tv#/strm/tv')
DEFAULT_MAPPINGS = ''

DEFAULTS = {
    # 运行
    'enabled': False, 'mode': 'dry_run', 'notify_enabled': True,
    # 同步内容
    'upload_enabled': False, 'copy_enabled': False, 'adopt_existing': True,
    # 变化监测
    'watch_mode': 'fast', 'transfer_event_enabled': True, 'scan_enabled': False, 'scan_cron': DEFAULT_SCAN_CRON,
    # 115比对
    'compare_mode': 'tree', 'p115_cookie': '',
    'mappings': DEFAULT_MAPPINGS,
    # 同步删除
    'delete_enabled': False, 'delete_b_enabled': False, 'delete_c_enabled': True,
    # 高级：扫描与监控
    'poll_seconds': 10, 'settle_seconds': 5, 'max_files': 2000000,
    # 高级：115访问与风控
    'api_qps': 2, 'failure_threshold': 3, 'cooldown_base_seconds': 60, 'cooldown_max_seconds': 1800,
    'remote_refresh_hours': 24, 'tree_timeout_minutes': 60, 'verify_window_minutes': 30,
    # 高级：删除
    'delete_delay_hours': 0,
    # 高级：文件规则
    'video_extensions': ','.join(e.lstrip('.') for e in DEFAULT_VIDEO_EXTENSIONS),
    'excludes': '\n'.join(DEFAULT_EXCLUDES),
}


def _col(md, *content, sm=None, offset=0):
    """生成 VCol 栅格列：手机端占整行，桌面端占 md 列。"""
    props = {'cols': 12, 'md': md}
    if sm:
        props['sm'] = sm
    if offset:
        props['offset-md'] = offset  # 与上一行的某一列对齐（仅桌面端）
    return {'component': 'VCol', 'props': props, 'content': list(content)}


def _row(*cols):
    """生成紧凑的 VRow 行。"""
    return {'component': 'VRow', 'props': {'dense': True, 'class': 'ma-0'}, 'content': list(cols)}


def _switch(model, label, hint='', md=4, offset=0):
    """生成栅格列中的 VSwitch 开关。"""
    props = {'model': model, 'label': label, 'color': 'primary', 'inset': True, 'density': 'compact'}
    if hint:
        props.update({'hint': hint, 'persistent-hint': True})
    else:
        props['hide-details'] = True
    col = _col(md, {'component': 'VSwitch', 'props': props}, sm=6)
    if offset:
        col['props']['offset-md'] = offset
    return col


def _field(model, label, hint='', md=4, kind='number', **extra):
    """生成栅格列中的 VTextField 输入框，额外参数写入 props。"""
    props = {'model': model, 'label': label, 'type': kind, 'variant': 'outlined', 'density': 'compact'}
    if hint:
        props.update({'hint': hint, 'persistent-hint': True})
    props.update(extra)
    return _col(md, {'component': 'VTextField', 'props': props}, sm=6)


def _area(model, label, hint, rows=3, placeholder=None):
    """生成占整行的 VTextarea 多行输入框。"""
    props = {'model': model, 'label': label, 'hint': hint, 'persistent-hint': True, 'rows': rows,
             'auto-grow': False, 'variant': 'outlined', 'density': 'compact'}
    if placeholder:
        props.update({'placeholder': placeholder, 'persistent-placeholder': True})
    return _col(12, {'component': 'VTextarea', 'props': props})


def _select(model, label, items, hint='', md=4, narrow=False):
    """生成栅格列中的 VSelect 下拉框；narrow 时在桌面端收窄。"""
    props = {'model': model, 'label': label, 'items': items, 'variant': 'outlined', 'density': 'compact'}
    if narrow:
        # 用响应式右边距收窄：桌面端约与带标签的开关同宽，手机端仍占整行。
        props['class'] = 'me-md-8'
    if hint:
        props.update({'hint': hint, 'persistent-hint': True})
    return _col(md, {'component': 'VSelect', 'props': props}, sm=6)


def _section(title, icon, rows, subtitle=''):
    """生成配置页的分组卡片：带图标的标题、可选副标题和表单行。"""
    head = [{'component': 'VIcon', 'props': {'color': 'primary', 'size': 18, 'class': 'me-2'}, 'text': icon},
            {'component': 'span', 'props': {'class': 'text-subtitle-2 font-weight-bold'}, 'text': title}]
    if subtitle:
        head.append({'component': 'span', 'props': {'class': 'text-caption text-medium-emphasis ms-2'},
                     'text': subtitle})
    return {'component': 'VCard', 'props': {'variant': 'outlined', 'class': 'mb-3', 'rounded': 'lg'}, 'content': [
        {'component': 'div', 'props': {'class': 'd-flex align-center px-4 pt-3 pb-1'}, 'content': head},
        {'component': 'VCardText', 'props': {'class': 'px-2 pt-1 pb-2'}, 'content': rows},
    ]}


def _panel(title, icon, rows):
    """生成高级设置中的折叠面板。"""
    return {'component': 'VExpansionPanel', 'content': [
        {'component': 'VExpansionPanelTitle', 'content': [
            {'component': 'VIcon', 'props': {'size': 'small', 'color': 'primary', 'class': 'me-2'}, 'text': icon},
            {'component': 'span', 'text': title}]},
        {'component': 'VExpansionPanelText', 'content': rows},
    ]}


def build_form():
    """构建插件配置表单。"""
    intro = {'component': 'VAlert', 'props': {
        'type': 'info', 'variant': 'tonal', 'density': 'compact', 'class': 'mb-3 text-body-2',
        'prepend-icon': 'mdi-information-outline',
        'text': '以本地实体库为准：上传115，并把元数据复制到STRM目录；不写STRM、不改本地实体库。'}}
    sections = [
        _section('运行', 'mdi-power', [_row(
            _switch('enabled', '启用插件'),
            # 偏移让后面的开关对齐第三列。
            _select('mode', '运行模式', [{'title': '试运行', 'value': 'dry_run'}, {'title': '同步', 'value': 'sync'}],
                    '试运行只记录', md=3, narrow=True),
            _switch('notify_enabled', '发送通知', '整轮结束汇总，异常单独提醒', offset=1))]),
        _section('同步内容', 'mdi-sync', [_row(
            _switch('upload_enabled', '上传到115', '视频与元数据全部上传'),
            _switch('copy_enabled', '复制到STRM目录', '非视频文件从本地直接复制'),
            _switch('adopt_existing', '接管已有文件', '已存在的相同文件直接记为已同步'))]),
        _section('变化监测', 'mdi-radar', [_row(
            _select('watch_mode', '监控模式', [
                {'title': '性能模式', 'value': 'fast'},
                {'title': '兼容模式', 'value': 'compatibility'}],
                '网络挂载选兼容模式', md=3, narrow=True),
            _switch('transfer_event_enabled', 'MP整理完成事件', '整理完成后立即处理', offset=1),
            _switch('scan_enabled', '定时兜底扫描', '万一漏掉变化时补齐，正常用不到；周期在高级设置'))]),
        _section('115比对', 'mdi-cloud-search-outline', [_row(
            _select('compare_mode', '比对方式', [
                {'title': '目录树（推荐）', 'value': 'tree'},
                {'title': '逐个详细比对', 'value': 'detail'}],
                '目录树需115 Cookie，请求极少；详细比对无需Cookie，可比对大小，库越大越慢', md=6),
            _col(6, {'component': 'VTextField', 'props': {
                'model': 'p115_cookie', 'label': '115 Cookie', 'type': 'password', 'variant': 'outlined',
                'density': 'compact', 'hint': '用于导出目录树和批量删除；留空时改用详细比对、逐个删除',
                'persistent-hint': True}}, sm=6))]),
        _section('路径映射', 'mdi-folder-sync-outline', [_row(
            _area('mappings', '映射（每行一条）',
                  '格式：ID#本地实体库路径#115路径#STRM目录路径。ID用来区分各条映射，可用中文，设好后不要改',
                  2, placeholder=MAPPING_EXAMPLE))]),
        _section('同步删除', 'mdi-delete-sweep-outline', [_row(
            _switch('delete_enabled', '允许同步删除', '总开关；关闭时只列出待删除项'),
            _switch('delete_b_enabled', '同步删除115文件', '进入115回收站，可恢复'),
            _switch('delete_c_enabled', '同步删除STRM文件', '删除STRM目录中对应的非视频文件'))], '本地删除后实时同步'),
    ]
    advanced = {'component': 'VExpansionPanels', 'props': {'variant': 'accordion', 'multiple': True}, 'content': [
        _panel('扫描与监控细节', 'mdi-tune-variant', [
            _row(_field('poll_seconds', '轮询间隔（秒）', '仅兼容模式；文件越多建议越大'),
                 _field('settle_seconds', '稳定观察间隔（秒）', '文件这么久不变才处理')),
            _row(_col(4, {'component': 'VCronField', 'props': {
                     'model': 'scan_cron', 'label': '兜底扫描周期', 'placeholder': DEFAULT_SCAN_CRON,
                     'hint': 'cron表达式，默认每周三、六凌晨4点；需打开上方的定时兜底扫描', 'persistent-hint': True,
                     'density': 'compact', 'variant': 'outlined'}}, sm=6),
                 _field('max_files', '单个映射最多文件数', '防止误配到超大目录')),
        ]),
        _panel('115访问与风控', 'mdi-shield-half-full', [
            {'component': 'VAlert', 'props': {'type': 'warning', 'variant': 'tonal', 'density': 'compact',
                                              'class': 'mb-3',
                'text': '115访问过于频繁会触发风控。默认值适用于几千到几十万文件的库，一般不需要修改。'}},
            _row(_field('api_qps', '每秒最多请求数', '所有115请求共用'),
                 _field('failure_threshold', '连续失败几次进入冷却'),
                 _field('cooldown_base_seconds', '首次冷却（秒）', '之后每次翻倍')),
            _row(_field('cooldown_max_seconds', '冷却上限（秒）'),
                 _field('remote_refresh_hours', '快照刷新间隔（小时）', '用于首次判断和反向核对'),
                 _field('tree_timeout_minutes', '目录树导出超时（分钟）')),
            _row(_field('verify_window_minutes', '上传确认等待（分钟）', '索引未出现时等待多久再重传，同一版本最多3次')),
        ]),
        _panel('删除', 'mdi-delete-clock-outline', [
            _row(_field('delete_delay_hours', '删除延迟（小时）',
                        '默认0即本地删除后同步删除；115上删除的文件进入回收站，可在回收站找回')),
        ]),
        _panel('文件规则', 'mdi-file-cog-outline', [
            _row(_area('video_extensions', '视频后缀', '逗号分隔；视频只上传115，不复制到STRM目录', 2)),
            _row(_area('excludes', '排除规则', '每行一个文件或目录名通配，不区分大小写', 3)),
        ]),
    ]}
    advanced_section = {'component': 'div', 'content': [
        {'component': 'div', 'props': {'class': 'd-flex align-center mb-2'}, 'content': [
            {'component': 'VIcon', 'props': {'color': 'primary', 'size': 18, 'class': 'me-2'},
             'text': 'mdi-cog-outline'},
            {'component': 'span', 'props': {'class': 'text-subtitle-2 font-weight-bold'}, 'text': '高级设置'},
            {'component': 'span', 'props': {'class': 'text-caption text-medium-emphasis ms-2'}, 'text': '一般无需修改'}]},
        advanced]}
    return [{'component': 'VForm', 'content': [intro] + sections + [advanced_section]}]


class LocalMirror115(_PluginBase):
    """插件主类：按配置管理同步引擎，并向宿主提供页面、API、定时服务与事件处理。"""
    plugin_name = '本地115镜像'
    plugin_desc = '本地媒体库实时同步到115，并把非视频文件复制到STRM目录'
    plugin_icon = 'https://raw.githubusercontent.com/Smart-zsw/MoviePilot-Plugins/main/icons/localmirror115.png'
    plugin_version = '1.0.0'
    plugin_author = 'Smart-zsw'
    author_url = 'https://github.com/Smart-zsw'
    plugin_config_prefix = 'localmirror115_'
    plugin_order = 99
    auth_level = 1

    def init_plugin(self, config=None):
        """宿主加载插件或保存配置时调用：解析配置并重建同步引擎。"""
        old = getattr(self, '_engine', None)
        if old is not None and not old.stop():
            # 上传无法中断；模块锁会让下一个引擎等它完成。
            logger.warning('【停止】上一个工作线程还在完成上传，新的引擎会等它结束后再处理')
        self._engine = None
        self._repository = None
        merged = dict(DEFAULTS)
        merged.update(config or {})
        if config and 'watch_mode' not in config:
            merged.pop('watch_mode')  # 交由 Config.parse 从 watch_enabled / force_polling 推导
        try:
            self._config = Config.parse(merged)
        except ValueError as error:
            logger.error('【启动】配置有误，插件未启动：%s', error)
            raise
        if not self._config.enabled:
            logger.info('【启动】插件未启用')
            return
        handle = self.get_database()
        self._repository = Repository(handle.session, self._config.revision, self._config.revision_b,
                                      self._config.revision_c, engine=handle.engine)
        remote = build_host_remote() if self._config.upload_enabled or self._config.mode == 'dry_run' \
            or self._config.delete_b_enabled else None
        self._engine = SyncEngine(self._config, self._repository, remote, self.get_data_path(),
                                  notifier=self._notifier())
        batch = remote is not None and remote.batch_delete is not None
        c = self._config
        on = lambda flag: '开' if flag else '关'
        logger.info('【启动】本地115镜像启动：模式=%s，上传115=%s，复制到STRM目录=%s，同步删除=%s'
                    '（115=%s，STRM=%s，115批量删除=%s），监控模式=%s，MP整理事件=%s，定时兜底扫描=%s，115比对=%s，通知=%s',
                    '试运行（只记录不执行）' if c.mode == 'dry_run' else '正式同步', on(c.upload_enabled),
                    on(c.copy_enabled), on(c.delete_enabled), on(c.delete_b_enabled), on(c.delete_c_enabled),
                    '开' if batch else '关（未填Cookie或未装p115client，逐个删除）',
                    {'fast': '性能模式', 'compatibility': '兼容模式', 'off': '关'}[c.watch_mode],
                    on(c.transfer_event_enabled),
                    c.scan_cron if c.scan_enabled else '关', '目录树' if c.compare_mode == 'tree' else '逐个详细比对',
                    on(c.notify_enabled))
        for m in c.mappings:
            logger.info('【启动】[%s] 本地 %s → 115 %s，非视频文件 → %s', m.key, m.source, m.remote, m.output)
        self._engine.start()

    def _notifier(self):
        """创建通知器：以插件消息发送，通知状态按数据目录在进程内共享。"""
        config = self._config

        def send(title, text):
            """以插件消息发送一条通知。"""
            self.post_message(mtype=MessageType.Plugin, title=title, text=text)
        return Notifier(send, config.notify_enabled, state=notification_state(self.get_data_path()))

    def get_database_models(self):
        """宿主获取插件的数据库模型。"""
        return [Asset, Pending]

    def get_state(self):
        """宿主查询插件是否启用：引擎已创建即为启用。"""
        return bool(getattr(self, '_engine', None))

    @staticmethod
    def get_command():
        """宿主获取远程命令；本插件没有。"""
        return []

    def get_api(self):
        """宿主注册插件 API：全量扫描、刷新状态页、刷新115快照。"""
        # 仅供页面按钮使用；'bear' 复用已登录管理员的会话令牌。
        return [{'path': '/scan', 'endpoint': self.api_scan, 'methods': ['POST'], 'auth': 'bear',
                 'response_model': Response[None], 'summary': '立即全量扫描'},
                {'path': '/ping', 'endpoint': self.api_ping, 'methods': ['GET'], 'auth': 'bear',
                 'response_model': Response[None], 'summary': '刷新状态页'},
                {'path': '/index', 'endpoint': self.api_index, 'methods': ['POST'], 'auth': 'bear',
                 'response_model': Response[None], 'summary': '刷新115目录快照'}]

    def get_service(self):
        """宿主注册定时服务：开启定时兜底扫描且 cron 有效时注册扫描任务。"""
        if not getattr(self, '_engine', None) or not self._config.scan_enabled:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._config.scan_cron)
        except ValueError:
            logger.error('【扫描】定时兜底扫描周期 %r 不是有效的cron表达式，本次未设置定时扫描',
                         self._config.scan_cron)
            return []
        return [{'id': '%s.Reconcile' % self.__class__.__name__, 'name': '本地115镜像兜底扫描',
                 'trigger': trigger, 'func': self.request_scan, 'kwargs': {}}]

    def request_scan(self):
        """定时服务回调：请求一次兜底扫描。"""
        if self._engine:
            self._engine.request_scan(reason='定时兜底扫描')

    @eventmanager.register(EventType.TransferComplete)
    @eventmanager.register(EventType.AudioTransferComplete)
    @eventmanager.register(EventType.SubtitleTransferComplete)
    def on_transfer(self, event):
        """MP 整理完成事件：把整理后的本地路径交给同步引擎尽快处理。"""
        engine = getattr(self, '_engine', None)
        if engine is not None and self._config.transfer_event_enabled:
            path = transfer_target(event)
            if path:
                if not engine.hint(path, 'moviepilot'):
                    logger.info('【监控】该路径不在任何映射的本地目录下，忽略：%s', path)

    def get_form(self):
        """宿主获取配置表单与默认值。"""
        return build_form(), dict(DEFAULTS)

    def _live_inputs(self):
        """汇总状态页和仪表盘共用的实时数据；数据库统计缓存 STATS_TTL 秒，避免大库下频繁全表统计。"""
        engine = getattr(self, '_engine', None)
        repository = getattr(self, '_repository', None)
        status = engine.snapshot() if engine else {'running': False}
        if not (engine and repository):
            return status, {}
        cached = getattr(self, '_stats', None)
        if cached is None or cached[0] is not repository or time.monotonic() - cached[1] >= STATS_TTL:
            cached = (repository, time.monotonic(), repository.remaining(status['now']), repository.counts())
            self._stats = cached
        status['remaining'] = cached[2]
        return status, cached[3]

    def get_page(self):
        """宿主获取插件详情页（状态页）。"""
        from .ui import build_page
        status, counts = self._live_inputs()
        repository = getattr(self, '_repository', None)
        notes = repository.note_counts() if repository else {}
        rows = repository.notable(limit=100) if repository else []
        return build_page(self.__class__.__name__, self._config, status, counts, rows, notes)

    def get_dashboard_meta(self):
        """宿主获取可添加的仪表盘组件列表。"""
        return [{'key': 'live', 'name': '本地115镜像 实时状态'}]

    def get_dashboard(self, key='live', **kwargs):
        """宿主获取仪表盘组件（每 5 秒自动刷新）。"""
        from .ui import build_dashboard
        status, counts = self._live_inputs()
        attrs = {'refresh': 5, 'title': '本地115镜像', 'subtitle': '实时同步状态', 'border': True}
        return {'cols': 12, 'md': 4}, attrs, build_dashboard(status, counts)

    def api_scan(self):
        """状态页“立即全量扫描”按钮：请求一次全量扫描。"""
        if not getattr(self, '_engine', None):
            return Response(success=False, message='插件未启用')
        self._engine.request_scan(reason='在状态页手动点击“立即全量扫描”')
        return Response(success=True, message='已开始全量扫描')

    def api_ping(self):
        """状态页“刷新状态”按钮：只返回成功，由页面自行刷新。"""
        return Response(success=True)

    def api_index(self):
        """状态页“刷新115快照”按钮：强制重建115目录快照。"""
        if not getattr(self, '_engine', None):
            return Response(success=False, message='插件未启用')
        logger.info('【快照】在状态页手动点击“刷新115快照”')
        self._engine.request_index(force=True)
        return Response(success=True, message='已开始刷新115目录快照')

    def stop_service(self):
        """宿主停用或重载插件时调用：停止同步引擎。"""
        engine = getattr(self, '_engine', None)
        if engine is not None:
            # 不在此发送汇总：本轮通知状态在进程内保留给重载后的引擎，且停止超时不代表已完成。
            engine.stop()  # 是否立即停止由 stop() 记录日志
            self._engine = None
