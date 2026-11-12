"""实时状态测试：扫描进度、剩余工作量、速度与预计剩余时间。"""
from app.plugins.localmirror115.ui import build_live, build_page


def test_scan_reports_progress_while_walking_and_a_summary_after(harness):
    """扫描遍历时实时报告进度，结束后给出汇总。"""
    h = harness()
    for i in range(3):
        h.write('S%d/e.nfo' % i, b'x')
    seen = []
    real = h.engine.fs.scan

    def spy(root, limit, stop, excluded, progress=None):
        """包装真实扫描，在每次进度回调时记录扫描状态。"""
        def watch(count, folder):
            """转发进度并保存当前扫描状态副本。"""
            progress(count, folder)
            seen.append(dict(h.engine.status['scan']))
        return real(root, limit, stop, excluded, progress=watch)
    h.engine.fs.scan = spy
    h.engine.scan_once()
    assert seen and all(s['step'] == 'walk' and s['mapping'] == 'tv' for s in seen)
    assert max(s['seen'] for s in seen) >= 2 and any(s['dir'].startswith('S') for s in seen)
    status = h.engine.snapshot()
    assert status['scan'] is None
    assert status['last_scan_info']['files'] == 3 and status['last_scan_info']['changes'] == 3


def test_remaining_speed_and_time_left_in_the_live_card(harness):
    """实时卡片显示剩余数量、处理速度与预计剩余时间。"""
    h = harness(mode='sync', copy_enabled=True)
    for i in range(4):
        h.write('S/e%d.nfo' % i, b'x')
    h.settle()
    left = h.repo.remaining(h.clock())
    assert left['c'] == (4, 4) and left['b'][0] == 4
    h.engine.run_c()
    assert h.repo.remaining(h.clock())['c'] == (0, 0)
    status = h.engine.snapshot()
    status['remaining'] = h.repo.remaining(status['now'])
    assert status['rate']['c'] > 0
    live = str(build_live(h.config, status, h.repo.counts(), h.repo.note_counts()))
    assert '4 / 4' in live and '已全部同步' in live and '剩余 4 个' in live  # 115 尚未执行
    assert '预计还需' not in live or '每分钟' in live


def test_page_shows_live_scan_and_explanation(harness):
    """状态页显示正在进行的扫描与说明文字。"""
    h = harness()
    h.write('S/e.nfo', b'x')
    h.settle()
    status = h.engine.snapshot()
    status['running'] = True
    status['scan'] = {'mapping': 'tv', 'position': 1, 'mappings': 1, 'step': 'walk', 'seen': 50,
                      'expected': 100, 'dir': 'S/Season 1', 'queued': 0, 'started': status['now']}
    page = str(build_page('LocalMirror115', h.config, status, h.repo.counts(), [], h.repo.note_counts()))
    assert '扫描中 tv' in page and '50 / 约100' in page and 'S/Season 1' in page
    assert '不下载任何文件' in page and '同步任务' not in page


def test_cron_is_described_in_words():
    """cron 表达式转换为中文描述，无法描述时原样显示。"""
    from app.plugins.localmirror115.ui import cron_text
    assert cron_text('0 4 * * 3,6') == '每周三、六 04:00'
    assert cron_text('30 2 * * *') == '每天 02:30'
    assert cron_text('0 3 1 * *') == '每月1日 03:00'
    assert cron_text('*/5 * * * *') == '*/5 * * * *'


def test_dashboard_shows_rings_current_file_and_time_left(harness):
    """仪表板显示三个进度环、当前文件与剩余数量，未启用时给出提示。"""
    from app.plugins.localmirror115.ui import build_dashboard
    h = harness(mode='sync', copy_enabled=True)
    for i in range(4):
        h.write('S/e%d.nfo' % i, b'x')
    h.settle()
    h.engine.run_c()
    status = h.engine.snapshot()
    status['remaining'] = h.repo.remaining(status['now'])
    status['b_busy'], status['running'] = 'S/e0.nfo', True
    widget = str(build_dashboard(status, h.repo.counts()))
    assert widget.count("'VProgressCircular'") == 3
    assert '4 / 4' in widget and '0 / 4' in widget  # 本地副本已完成，115 尚未开始
    assert 'S/e0.nfo' in widget and '115 剩余 4' in widget
    status['running'] = False
    assert '插件未启用' in str(build_dashboard(status, {}))


def test_unchanged_plans_are_not_rechecked_every_scan_interval(harness):
    """未变化的计划结果不会每个扫描周期都复查，配置变更时立即复查。"""
    h = harness(mode='dry_run')         # 演练模式：所有结果都停留在计划状态
    h.write('S/e.nfo', b'x')
    h.settle()
    h.run()
    row = h.asset('S/e.nfo')
    assert row['c_state'] == 'planned' and row['c_next'] - h.clock() > 3600 * 23
    h.repo.wake_planned()               # 配置变更时仍会立即复查
    assert h.asset('S/e.nfo')['c_next'] == 0


def test_snapshot_comparison_is_done_in_bulk(harness):
    """与快照的比对批量进行，大量文件也能在限定时间内完成。"""
    import time as _time
    h = harness(mode='dry_run')
    for i in range(2000):
        path = 'S%02d/e%04d.nfo' % (i % 40, i)
        h.write(path, b'x')
        if i % 2 == 0:
            h.remote.put('/media/tv/' + path, 1)       # 其中一半已在 115 上
    h.settle()
    began = _time.monotonic()
    for _ in range(5):                                 # 与工作循环一致，每次调用处理一批 500 个
        h.run()
    elapsed = _time.monotonic() - began
    notes = h.repo.note_counts()
    assert notes['b'] == {'would_adopt_existing': 1000, 'would_upload': 1000}
    assert notes['c'] == {'would_copy': 2000}
    assert elapsed < 10, elapsed


def test_worker_takes_the_next_full_batch_without_idling(harness):
    """工作线程处理满批后立即取下一批，不空等。"""
    import threading
    import time as _time
    h = harness()
    calls = []

    def step():
        """记录调用时间，前三次报告满批，第四次后停止。"""
        calls.append(_time.monotonic())
        if len(calls) >= 4:
            h.engine.stop_event.set()
        return len(calls) < 4          # 三个满批，之后队列为空
    h.engine.prepared.set()             # 启动准备已完成，相当于 _stability 发出的信号
    worker = threading.Thread(target=h.engine._loop, args=(step, threading.Event(), 'b', 5.0))
    worker.start()
    worker.join(3)
    assert len(calls) == 4 and calls[-1] - calls[0] < 2  # 满批之间没有 5 秒空等


def test_bulk_record_skips_rows_changed_meanwhile(harness):
    """批量写回时跳过期间已变化的记录。"""
    h = harness(mode='dry_run')
    h.write('S/a.nfo', b'x')
    h.write('S/b.nfo', b'x')
    h.settle()
    a, b = h.asset('S/a.nfo'), h.asset('S/b.nfo')
    h.write('S/b.nfo', b'changed!')          # b 的源文件版本在批量写回前已变化
    h.clock.advance(10)
    h.engine.scan_once()
    h.clock.advance(10)
    h.engine.tick()
    assert h.asset('S/b.nfo')['a_fp'] != b['a_fp']
    ok = h.repo.record_many('c', [(a, {'c_note': 'batch'}), (b, {'c_note': 'batch'})])
    assert ok == [True, False]
    assert h.asset('S/a.nfo')['c_note'] == 'batch' and h.asset('S/b.nfo')['c_note'] != 'batch'
