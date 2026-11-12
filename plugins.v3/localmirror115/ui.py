"""状态页（及首页仪表盘组件）的 Vuetify JSON（PageRender 格式：component/props/text/content/events）。"""
import time

from .log import MODE_LABEL, TARGET_PLACE, duration_text, note_label

STATE_LABELS = {
    'synced': ('已同步', 'success'), 'planned': ('已有计划', 'info'), 'todo': ('待核对', 'grey'),
    'verify': ('等待115确认', 'warning'), 'error': ('失败重试中', 'error'), 'conflict': ('需人工处理', 'error'),
    'delete_planned': ('待删除', 'warning'), 'kept': ('已保留', 'secondary'), 'deleted': ('已删除', 'grey'),
    'absent': ('无需处理', 'grey'),
}
ROOT_LABELS = {
    'ready': ('正常', 'success'), 'awaiting_scan': ('等待首次扫描', 'info'),
    'scan_incomplete_or_unsafe': ('扫描异常，暂停删除', 'error'),
    'unavailable': ('目录不可用', 'error'),
}
WATCH_LABELS = {
    'native_requested': ('性能模式', 'success'), 'polling': ('兼容模式', 'success'), 'disabled': ('监控已关闭', 'grey'),
    'failed_scan_fallback_only': ('监控失败，仅靠扫描', 'error'),
}

INTRO = '本地文件有变化时实时同步：全部文件上传到115，非视频文件同时复制到STRM目录；比对只读取文件信息，不下载任何文件。'
DASHBOARD_TIP = '本页打开时加载一次，点“刷新状态”更新；在首页仪表盘添加“本地115镜像 实时状态”组件，可每5秒自动刷新。'


def tint(color):
    """色调标签的内联颜色样式：宿主毛玻璃主题会重置标签的颜色类，故直接使用主题变量。"""
    if color in (None, 'grey'):
        return ('color: rgba(var(--v-theme-on-surface), 0.6) !important; '
                'background-color: rgba(var(--v-theme-on-surface), 0.08) !important')
    return ('color: rgb(var(--v-theme-%s)) !important; background-color: rgba(var(--v-theme-%s), 0.14) !important'
            % (color, color))


def chip(text, color, size='small', icon=None):
    """生成色调 VChip 小标签（颜色以内联样式强制生效）。"""
    props = {'size': size, 'variant': 'tonal', 'color': color, 'class': 'me-1 mb-1', 'label': True,
             'style': tint(color)}
    if icon:
        props['prepend-icon'] = icon
    return {'component': 'VChip', 'props': props, 'text': text}


def table(headers, rows):
    """生成紧凑的 VTable 表格。"""
    return {'component': 'VTable', 'props': {'hover': True, 'density': 'compact', 'class': 'text-body-2'}, 'content': [
        {'component': 'thead', 'content': [{'component': 'tr', 'content': [
            {'component': 'th', 'props': {'class': 'text-start text-no-wrap'}, 'text': h} for h in headers]}]},
        {'component': 'tbody', 'content': [{'component': 'tr', 'content': r} for r in rows]},
    ]}


def td(text, cls=''):
    """生成文本单元格。"""
    return {'component': 'td', 'props': {'class': cls} if cls else {}, 'text': str(text)}


def td_state(state, note=''):
    """生成状态单元格：状态标签加可选原因。"""
    label, color = STATE_LABELS.get(state or 'todo', (state or '待核对', 'grey'))
    content = [chip(label, color, 'x-small')]
    if note:
        content.append({'component': 'span', 'props': {'class': 'text-caption text-medium-emphasis'},
                        'text': note_label(note)})
    return {'component': 'td', 'content': content}


WEEKDAYS = '日一二三四五六日'


def cron_text(cron):
    """把 cron 转成中文描述，如 '0 4 * * 3,6' -> '每周三、六 04:00'；不常见的写法原样显示。"""
    try:
        minute, hour, day, month, weekday = cron.split()
        clock = '%02d:%02d' % (int(hour), int(minute))
        if day == '*' and month == '*':
            if weekday == '*':
                return '每天 ' + clock
            return '每周%s %s' % ('、'.join(WEEKDAYS[int(d)] for d in weekday.split(',')), clock)
        if month == '*' and weekday == '*':
            return '每月%s日 %s' % (day, clock)
    except (ValueError, IndexError):
        pass
    return cron


def live_row(icon, color, title, text, right='', percent=None, indeterminate=False, sub=''):
    """实时状态卡片中的一行：某部分正在做什么、进度到哪、还剩多少。"""
    head = {'component': 'div', 'props': {'class': 'd-flex align-center text-body-2'}, 'content': [
        {'component': 'VIcon', 'props': {'size': 18, 'color': color, 'class': 'me-2 flex-shrink-0'}, 'text': icon},
        {'component': 'span', 'props': {'class': 'font-weight-medium me-2 text-no-wrap flex-shrink-0'}, 'text': title},
        {'component': 'span', 'props': {'class': 'text-truncate', 'style': 'min-width:0'}, 'text': text},
        {'component': 'VSpacer'},
        {'component': 'span', 'props': {'class': 'text-caption text-medium-emphasis text-no-wrap ms-2'},
         'text': right}]}
    content = [head]
    if percent is not None or indeterminate:
        content.append({'component': 'VProgressLinear', 'props': {
            'model-value': percent or 0, 'indeterminate': indeterminate, 'color': color, 'height': 6,
            'rounded': True, 'class': 'mt-1'}})
    for line in ([sub] if isinstance(sub, str) else sub):
        if line:
            content.append({'component': 'div',
                            'props': {'class': 'text-caption text-medium-emphasis mt-1 text-truncate'},
                            'text': line})
    return {'component': 'div', 'props': {'class': 'py-2'}, 'content': content}


def build_live(config, status, counts, notes=None):
    """状态页的实时状态卡片：扫描进度、当前文件和各目标的剩余时间。"""
    dry = status.get('mode') == 'dry_run'
    now = status.get('now') or time.time()
    rate = status.get('rate') or {}
    rows = []

    scan = status.get('scan')
    info = status.get('last_scan_info')
    if scan:
        where = '%s %d/%d' % (scan['mapping'], scan['position'], scan['mappings']) if scan['mappings'] > 1 \
            else scan['mapping']
        if scan.get('scope'):
            where = '%s（仅变化的目录）' % scan['mapping']
        if scan['step'] == 'walk':
            expected = scan.get('expected')
            pct = min(99, scan['seen'] * 100 / expected) if expected else None
            rows.append(live_row('mdi-magnify-scan', 'info', '本地扫描', '扫描中 ' + where,
                                 '%d%s' % (scan['seen'], ' / 约%d' % expected if expected else ' 个'),
                                 pct, indeterminate=pct is None,
                                 sub='当前目录：%s' % (scan.get('dir') or '/')))
        else:
            rows.append(live_row('mdi-magnify-scan', 'info', '本地扫描', '比对中 ' + where,
                                 '变化 %d' % scan.get('queued', 0),
                                 indeterminate=True, sub='%d 个文件与上次记录逐个比对大小和修改时间' % scan['seen']))
    elif info:
        summary = '上次扫描 %d 个文件，用时 %s，发现变化 %d 处' % (
            info['files'], duration_text(info['seconds']), info['changes'])
        rows.append(live_row('mdi-magnify-scan', 'success', '本地扫描', '空闲',
                             '上次 %s' % _fmt_time(info['started']), sub=summary))
    else:
        rows.append(live_row('mdi-magnify-scan', 'grey', '本地扫描', '等待首次扫描'))
    if counts.get('pending'):
        rows.append(live_row('mdi-timer-sand', 'info', '等待稳定', '%d 个变化正在等待文件写完' % counts['pending'],
                             '%s 秒后处理' % int(config.settle_seconds)))

    building = _building(status)
    if building:
        rows.append(live_row('mdi-cloud-search-outline', 'info', '115快照', '生成中 ' + '、'.join(building),
                             '', indeterminate=True, sub='读取115目录结构，用于判断哪些文件115已有；不会下载文件'))

    def target_row(key, title, icon, busy_verb):
        """某个目标（115 或 STRM）的进度行：试运行显示核对进度，正式同步显示进度、当前文件与剩余。"""
        done, all_files, _, left, due = _target_progress(key, status, counts)
        busy = status.get(key + '_busy')
        if dry:
            planned = (notes or {}).get(key) or {}
            plan = '、'.join('%s %d' % (note_label(n) or '有计划', v)
                            for n, v in sorted(planned.items(), key=lambda x: -x[1])[:3])
            text = '核对中' if busy else ('已核对完毕' if done >= all_files else '等待核对')
            return live_row(icon, 'warning', title, text, '%d / %d' % (done, all_files),
                            done * 100 / all_files if all_files else 0,
                            sub=['当前：' + busy if busy else '', '试运行，未执行：' + plan if plan else ''])
        speed = rate.get(key) or 0
        if busy:
            text = busy_verb + '中'
        elif due:
            text = '准备处理'
        elif left:
            text = '等待重试或确认'
        else:
            text = '已全部同步'
        sub = ''
        if left:
            sub = '剩余 %d 个' % left
            if speed:
                sub += ' · 每分钟约 %d 个 · 预计还需 %s' % (round(speed * 60), duration_text(due / speed))
            if left > due:
                sub += ' · %d 个稍后重试' % (left - due)
        return live_row(icon, 'primary' if left else 'success', title, text, '%d / %d' % (done, all_files),
                        done * 100 / all_files if all_files else 100, sub=['当前：' + busy if busy else '', sub])

    rows.append(target_row('b', '115', 'mdi-cloud-upload-outline', '处理'))
    rows.append(target_row('c', 'STRM目录', 'mdi-content-copy', '复制'))

    limiter = status.get('limiter') or {}
    if limiter.get('cooldown_seconds'):
        rows.append(live_row('mdi-shield-alert-outline', 'warning', '115风控', '连续请求失败，暂停访问',
                             '%d 秒后恢复' % limiter['cooldown_seconds']))
    err = status.get('last_error')
    if err and now - (err.get('at') or 0) < 3600:
        rows.append(live_row('mdi-alert-circle-outline', 'error', '最近错误', '%s %s' % (
            TARGET_PLACE.get(err.get('target'), err.get('target') or ''), err.get('relative', '')),
            _fmt_time(err.get('at')), sub=note_label(err.get('note'))))

    active = _active(status, counts)
    return section('实时状态', 'mdi-pulse', rows, [
        chip('工作中' if active else '空闲', 'info' if active else 'success', 'x-small'),
        {'component': 'span', 'props': {'class': 'text-caption text-medium-emphasis ms-1'},
         'text': time.strftime('%H:%M:%S', time.localtime(now))}])


def _building(status):
    """正在生成 115 快照的映射。"""
    mappings = (status.get('remote_index') or {}).get('mappings') or {}
    return [key for key, value in mappings.items() if value.get('state') == 'building']


def _active(status, counts):
    """是否有扫描、快照、115/STRM 处理或待稳定的变化正在进行。"""
    return bool(status.get('scan') or _building(status) or status.get('b_busy') or status.get('c_busy')
                or counts.get('pending'))


def _target_progress(key, status, counts):
    """按实时卡片口径返回目标的 (已完成, 总数, 标签, 剩余, 当前可处理)。"""
    states = counts.get(key, {})
    total = counts.get('total', 0) if key == 'b' else sum(states.values())
    left, due = (status.get('remaining') or {}).get(key, (0, 0))
    if status.get('mode') == 'dry_run':
        return total - states.get('todo', 0), total, '已核对', left, due
    return total - left, total, '已同步', left, due


def _ring(title, icon, color, value, center, caption, indeterminate=False):
    """仪表盘的一列：中间带数字的进度环，下方是说明文字。"""
    return {'component': 'VCol', 'props': {'cols': 4, 'class': 'd-flex flex-column align-center pa-1'}, 'content': [
        {'component': 'VProgressCircular', 'props': {
            'model-value': value, 'indeterminate': indeterminate, 'size': 76, 'width': 7, 'color': color,
            'bg-color': 'rgba(var(--v-theme-on-surface), 0.08)'}, 'content': [
            {'component': 'div', 'props': {'class': 'd-flex flex-column align-center'}, 'content': [
                {'component': 'VIcon', 'props': {'size': 16, 'color': color}, 'text': icon},
                {'component': 'span', 'props': {'class': 'text-caption font-weight-bold', 'style': 'line-height:1.2'},
                 'text': center}]}]},
        {'component': 'div', 'props': {'class': 'text-body-2 font-weight-medium mt-2'}, 'text': title}] + [
        {'component': 'div', 'props': {'class': 'text-caption text-medium-emphasis text-center',
                                       'style': 'line-height:1.3'}, 'text': part}
        for part in caption.split('\n')]}


def build_dashboard(status, counts):
    """首页仪表盘组件：三个进度环、正在处理的内容和剩余工作。"""
    if not status.get('running'):
        return [{'component': 'div', 'props': {'class': 'd-flex align-center text-medium-emphasis pa-2'}, 'content': [
            {'component': 'VIcon', 'props': {'size': 18, 'class': 'me-2'}, 'text': 'mdi-power'},
            {'component': 'span', 'props': {'class': 'text-body-2'}, 'text': '插件未启用'}]}]
    dry = status.get('mode') == 'dry_run'
    now = status.get('now') or time.time()
    rate = status.get('rate') or {}
    scan = status.get('scan')
    info = status.get('last_scan_info')

    if scan and scan['step'] == 'walk' and scan.get('expected'):
        pct = min(99, scan['seen'] * 100 / scan['expected'])
        scan_ring = _ring('本地扫描', 'mdi-magnify-scan', 'info', pct, '%d%%' % pct,
                          '扫描中\n%d / %d' % (scan['seen'], scan['expected']))
    elif scan:
        scan_ring = _ring('本地扫描', 'mdi-magnify-scan', 'info', 0, '扫描中',
                          ('仅变化的目录' if scan.get('scope') else '比对中') + '\n%d 个文件' % scan['seen'],
                          indeterminate=True)
    elif info:
        scan_ring = _ring('本地扫描', 'mdi-check', 'success', 100, '完成',
                          '上次全量\n' + time.strftime('%m-%d %H:%M', time.localtime(info['started'])))
    else:
        scan_ring = _ring('本地扫描', 'mdi-timer-sand', 'grey', 0, '等待', '尚未扫描\n—')
    rings = [scan_ring]
    for key, title, icon in (('b', '115', 'mdi-cloud-upload-outline'), ('c', 'STRM目录', 'mdi-content-copy')):
        done, total, label, left, due = _target_progress(key, status, counts)
        pct = done * 100 / total if total else 100
        finished = done >= total
        color = 'warning' if dry else ('success' if finished else 'primary')
        rings.append(_ring(title, 'mdi-check' if finished else icon, color, pct, '%d%%' % int(pct),
                           '%s\n%d / %d' % (label, done, total)))

    # ---- 当前正在进行的工作 ----
    lines = []

    def line(icon, color, label, text):
        """向“当前正在进行”列表追加一行。"""
        lines.append({'component': 'div', 'props': {'class': 'd-flex align-center py-1 text-body-2'}, 'content': [
            {'component': 'VAvatar', 'props': {'size': 26, 'color': color, 'variant': 'tonal', 'class': 'me-2'},
             'content': [{'component': 'VIcon', 'props': {'size': 15}, 'text': icon}]},
            {'component': 'span', 'props': {'class': 'text-medium-emphasis me-2 text-no-wrap'}, 'text': label},
            {'component': 'span', 'props': {'class': 'text-truncate', 'style': 'min-width:0'}, 'text': text}]})
    if scan:
        line('mdi-folder-search-outline', 'info', '扫描', '%s %s' % (scan['mapping'], scan.get('dir') or ''))
    building = _building(status)
    if building:
        line('mdi-cloud-search-outline', 'info', '115快照', '正在生成 ' + '、'.join(building))
    for key, label, color in (('b', '115', 'primary'), ('c', 'STRM', 'success')):
        if status.get(key + '_busy'):
            line('mdi-file-sync-outline', color, label, status[key + '_busy'])
    if not lines:
        line('mdi-check-circle-outline', 'success', '空闲',
             '试运行，未执行任何操作' if dry else '全部完成，等待新的变化')

    # ---- 底部：剩余工作、速度与问题 ----
    facts = []
    for key, label in (('b', '115'), ('c', 'STRM')):
        _, _, _, left, due = _target_progress(key, status, counts)
        if left and not dry:
            text = '%s 剩余 %d' % (label, left)
            if rate.get(key) and due:
                text += ' · 约 %s' % duration_text(due / rate[key])
            facts.append(chip(text, 'primary', 'x-small', 'mdi-timer-outline'))
    if counts.get('pending'):
        facts.append(chip('等待稳定 %d' % counts['pending'], 'info', 'x-small', 'mdi-timer-sand'))
    problems = sum(counts.get(t, {}).get(st, 0) for t in ('b', 'c') for st in ('error', 'conflict'))
    if problems:
        facts.append(chip('需要关注 %d' % problems, 'error', 'x-small', 'mdi-alert-circle-outline'))
    if (status.get('limiter') or {}).get('cooldown_seconds'):
        facts.append(chip('115风控冷却中', 'warning', 'x-small', 'mdi-shield-alert-outline'))

    active = _active(status, counts)
    head = {'component': 'div', 'props': {'class': 'd-flex align-center mb-1'}, 'content': [
        {'component': 'VBadge', 'props': {'dot': True, 'inline': True, 'color': 'info' if active else 'success',
                                          'class': 'me-1'}},
        {'component': 'span', 'props': {'class': 'text-caption font-weight-medium me-2'},
         'text': '工作中' if active else '空闲'},
        chip('试运行', 'warning', 'x-small') if dry else chip('同步中', 'success', 'x-small'),
        {'component': 'VSpacer'},
        {'component': 'span', 'props': {'class': 'text-caption text-medium-emphasis'},
         'text': '更新于 ' + time.strftime('%H:%M:%S', time.localtime(now))}]}
    body = [head,
            {'component': 'VRow', 'props': {'dense': True, 'class': 'my-1'}, 'content': rings},
            {'component': 'div', 'props': {'class': 'rounded-lg px-2 py-1',
                                           'style': 'background: rgba(var(--v-theme-on-surface), 0.04)'},
             'content': lines}]
    if facts:
        body.append({'component': 'div', 'props': {'class': 'd-flex flex-wrap mt-2'}, 'content': facts})
    return [{'component': 'div', 'props': {'class': 'px-1 pb-1'}, 'content': body}]


BORDER = 'border: thin solid rgba(var(--v-border-color), var(--v-border-opacity))'
SECTION_BAR = ('background: linear-gradient(90deg, rgba(var(--v-theme-primary), 0.14), '
               'rgba(var(--v-theme-primary), 0.02))')
DIVIDER = 'border-bottom: thin solid rgba(var(--v-border-color), var(--v-border-opacity))'


def section(title, icon, body, right=None):
    """带色调标题栏的卡片，页面每个区块都使用同样的外观。"""
    bar = [{'component': 'VIcon', 'props': {'size': 18, 'color': 'primary', 'class': 'me-2'}, 'text': icon},
           {'component': 'span', 'props': {'class': 'text-subtitle-2 font-weight-bold'}, 'text': title},
           {'component': 'VSpacer'}] + (right or [])
    return {'component': 'VCard', 'props': {'variant': 'outlined', 'rounded': 'lg', 'class': 'mb-3'}, 'content': [
        {'component': 'div', 'props': {'class': 'd-flex align-center px-3 py-2', 'style': SECTION_BAR}, 'content': bar},
        {'component': 'div', 'props': {'class': 'px-3 py-1'}, 'content': body}]}


def list_item(icon, label, value, color='grey', last=False):
    """列表行：左侧图标加名称，右侧一个小状态标签。"""
    return {'component': 'div', 'props': {'class': 'd-flex align-center py-2 text-body-2',
                                          'style': '' if last else DIVIDER}, 'content': [
        {'component': 'VIcon', 'props': {'size': 18, 'color': color if color != 'grey' else None,
                                         'class': 'me-3 text-medium-emphasis' if color == 'grey' else 'me-3'},
         'text': icon},
        {'component': 'span', 'props': {'class': 'text-no-wrap'}, 'text': label},
        {'component': 'VSpacer'},
        {'component': 'VChip', 'props': {'size': 'x-small', 'variant': 'tonal', 'label': True, 'color': color,
                                         'class': 'ms-2', 'style': tint(color)}, 'text': str(value)}]}


def _fmt_time(value):
    """把时间戳格式化为“月-日 时:分:秒”，为空时显示“尚未完成”。"""
    return time.strftime('%m-%d %H:%M:%S', time.localtime(value)) if value else '尚未完成'


def _sum(counts, *states):
    """累加计数中指定几个状态的数量。"""
    return sum(counts.get(s, 0) for s in states)


def build_page(plugin_id, config, status, counts, rows, notes=None):
    """构建插件状态页；插件未启用时只返回一条提示。"""
    if not status.get('running'):
        return [{'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal', 'prepend-icon': 'mdi-power',
                 'text': '插件未启用。在设置中打开“启用插件”并保存后，这里会显示同步状态。'}}]
    b, c = counts.get('b', {}), counts.get('c', {})
    total = counts.get('total', 0)
    meta_total = sum(c.values())
    dry = status.get('mode') == 'dry_run'
    sw = status.get('switches', {})
    index = status.get('remote_index') or {}
    limiter = status.get('limiter') or {}
    watch = set(status.get('watchers', {}).values())
    watch_label = WATCH_LABELS.get(next(iter(watch)) if len(watch) == 1 else 'native_requested', ('监控', 'grey'))
    problems = _sum(b, 'error', 'conflict') + _sum(c, 'error', 'conflict')
    on = lambda key: ('已开启', 'success') if sw.get(key) else ('已关闭', 'grey')

    # ---- 运行状态：模式、开关和总数 ----
    items = [
        ('mdi-flask-outline' if dry else 'mdi-sync', '运行模式',
         *(('试运行 · 只记录不执行', 'warning') if dry else ('正式同步', 'success'))),
        ('mdi-cloud-upload-outline', '上传到115', *on('upload_enabled')),
        ('mdi-content-copy', '复制到STRM目录', *on('copy_enabled')),
        ('mdi-delete-sweep-outline', '同步删除', *on('delete_enabled')),
        ('mdi-radar', '监控模式', *watch_label),
        ('mdi-calendar-sync-outline', '定时兜底扫描',
         *((cron_text(config.scan_cron), 'success') if sw.get('scan_enabled') else ('已关闭', 'grey'))),
        ('mdi-cloud-search-outline', '115比对方式',
         *((MODE_LABEL[index['mode']], 'success') if index.get('mode') in MODE_LABEL else ('未启用', 'grey'))),
        ('mdi-file-multiple-outline', '跟踪文件', '%d · 非视频 %d' % (total, meta_total),
         'success' if total else 'grey'),
        ('mdi-swap-vertical', '115请求', '%d 次%s' % (limiter.get('calls', 0),
                                                 ' · 冷却中' if limiter.get('cooldown_seconds') else ''),
         'warning' if limiter.get('cooldown_seconds') else 'success'),
        ('mdi-alert-circle-outline', '需要关注', problems or '无', 'error' if problems else 'success'),
    ]
    run_status = section('运行状态', 'mdi-information-outline',
                         [list_item(*item, last=i == len(items) - 1) for i, item in enumerate(items)])

    # ---- 路径映射 ----
    snap_state = {'building': ('快照生成中', 'info'), 'ready': ('快照就绪', 'success'), 'failed': ('快照失败', 'error')}
    mapping_body = []
    for m in config.mappings:
        r_label, r_color = ROOT_LABELS.get(status['roots'].get(m.key), (status['roots'].get(m.key) or '未知', 'grey'))
        w_label, w_color = WATCH_LABELS.get(status['watchers'].get(m.key), ('监控未启动', 'grey'))
        info = (index.get('mappings') or {}).get(m.key, {})
        audit = (index.get('audit') or {}).get(m.key, {})
        chips = [chip(r_label, r_color, 'x-small'), chip(w_label, w_color, 'x-small')]
        if index:
            s_label, s_color = snap_state.get(info.get('state'), ('快照未生成', 'grey'))
            if info.get('entries') is not None and info.get('built_at'):
                s_label += ' · %d 条 · %s' % (info['entries'], _fmt_time(info['built_at']))
            chips.append(chip(s_label, s_color, 'x-small'))
        warnings = []
        if (status.get('root_detail') or {}).get(m.key):
            warnings.append(status['root_detail'][m.key])
        if info.get('note'):
            warnings.append(info['note'])
        if audit.get('state') == 'suspect':
            warnings.append('快照疑似异常：%d/%d 个已同步文件不在115中，已忽略本次核对' % (audit['missing'], audit['synced']))
        elif audit.get('missing'):
            warnings.append('%d 个文件已不在115中%s' % (audit['missing'], '，已安排重传' if audit.get('applied') else ''))
        targets = [{'component': 'div', 'props': {'class': 'd-flex align-center text-body-2 py-1'}, 'content': [
            {'component': 'VIcon', 'props': {'size': 16, 'color': color, 'class': 'me-2'}, 'text': icon},
            {'component': 'span', 'props': {'class': 'text-caption text-medium-emphasis me-2 text-no-wrap'},
             'text': label},
            {'component': 'span', 'props': {'class': 'text-truncate', 'style': 'min-width:0'}, 'text': str(path)}]}
            for icon, color, label, path in (('mdi-harddisk', 'primary', '本地', m.source),
                                             ('mdi-cloud-outline', 'success', '115', m.remote),
                                             ('mdi-folder-play-outline', 'info', 'STRM', m.output))]
        mapping_body.append({'component': 'div', 'props': {'class': 'py-2'}, 'content': [
            {'component': 'div', 'props': {'class': 'd-flex flex-wrap align-center mb-1'}, 'content': [
                {'component': 'span', 'props': {'class': 'text-body-2 font-weight-bold me-2'}, 'text': m.key}] + chips},
            {'component': 'div', 'props': {'class': 'rounded-lg px-3 py-1',
                                           'style': BORDER},
             'content': targets}] + [
            {'component': 'div', 'props': {'class': 'text-caption text-warning mt-1'}, 'text': w} for w in warnings]})
    mappings = section('路径映射', 'mdi-folder-sync-outline', mapping_body)

    blocks = [{'component': 'VRow', 'props': {'dense': True}, 'content': [
        {'component': 'VCol', 'props': {'cols': 12, 'md': 7}, 'content': [build_live(config, status, counts, notes),
                                                                         mappings]},
        {'component': 'VCol', 'props': {'cols': 12, 'md': 5}, 'content': [run_status]}]}]

    # ---- 需要关注：仅在有需人工处理的文件时显示 ----
    if rows:
        attention = [[td(row['mapping']), td(row['relative'], 'text-truncate'), td_state(row['b_state'], row['b_note']),
                      td_state(row['c_state'], row['c_note']) if row['kind'] == 'meta'
                      else td('—', 'text-medium-emphasis')]
                     for row in rows[:100]]
        blocks.append(section('需要关注（%d）' % len(attention), 'mdi-alert-outline',
                              [table(['映射', '文件', '115', 'STRM目录'], attention)]))

    # ---- 说明与操作按钮 ----
    def tip(text):
        """生成一行带信息图标的说明文字。"""
        return {'component': 'div', 'props': {'class': 'd-flex align-start text-caption text-medium-emphasis mb-1'},
                'content': [{'component': 'VIcon',
                             'props': {'size': 14, 'color': 'info', 'class': 'me-2', 'style': 'margin-top:1px'},
                             'text': 'mdi-information'},
                            {'component': 'span', 'text': text}]}

    def action(text, icon, api, method='post', variant='text'):
        """生成点击后调用本插件 API 的操作按钮。"""
        return {'component': 'VBtn', 'props': {'color': 'primary', 'variant': variant, 'size': 'small',
                                               'prepend-icon': icon, 'class': 'me-1 mb-1'},
                'text': text, 'events': {'click': {'api': 'plugin/%s/%s' % (plugin_id, api), 'method': method,
                                                   'params': {}}}}
    blocks += [
        {'component': 'div', 'props': {'class': 'px-1 mt-1'}, 'content': [tip(INTRO), tip(DASHBOARD_TIP)]},
        {'component': 'VDivider', 'props': {'class': 'my-2'}},
        {'component': 'div', 'props': {'class': 'd-flex flex-wrap align-center'}, 'content': [
            # 左对齐：MoviePilot 的设置按钮悬浮在对话框右下角
            action('立即全量扫描', 'mdi-magnify-scan', 'scan', variant='tonal'),
            action('刷新115快照', 'mdi-cloud-search-outline', 'index'),
            action('刷新状态', 'mdi-refresh', 'ping', 'get')]},
    ]
    return blocks
