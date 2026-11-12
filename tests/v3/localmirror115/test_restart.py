"""短暂重启（保存配置、更新插件）沿用上次全量扫描的测试。"""
from app.plugins.localmirror115.engine import SyncEngine, SHORT_RESTART


def _restart(h, config=None, advance=5):
    """停止当前引擎并推进时间后启动新引擎，返回新引擎与其扫描请求记录。"""
    h.engine.stop()
    h.clock.advance(advance)
    engine = SyncEngine(config or h.config, h.repo, h.remote, str(h.q.parent / 'data'), h.clock)
    requested = []
    engine.request_scan = lambda keys=None, reason='': requested.append(keys)
    engine.start()
    return engine, requested


def test_short_restart_skips_the_startup_scan(harness):
    """短暂重启时跳过启动扫描，沿用上次扫描结果。"""
    h = harness()
    h.write('S/e.nfo', b'x')
    h.engine.scan_once()            # 与启动时一样执行引擎的全量扫描
    engine, requested = _restart(h)
    try:
        assert requested == []
        status = engine.snapshot()
        assert status['roots']['tv'] == 'ready' and status['resumed'] == ['tv']
        assert status['last_scan_info']['files'] == 1
    finally:
        engine.stop()


def test_short_restart_replays_unfinished_scan_requests(harness):
    """短暂重启后重新发起未完成的扫描请求。"""
    h = harness()
    h.write('S/e.nfo', b'x')
    h.engine.scan_once()
    h.engine.request_subtree(h.config.mappings[0], 'New')
    engine, requested = _restart(h)
    try:
        assert requested == [None]
    finally:
        engine.stop()


def test_long_downtime_or_changed_rules_scan_again(harness):
    """停机过久或文件规则变化时重启后重新扫描。"""
    from app.plugins.localmirror115.config import Config
    h = harness()
    h.write('S/e.nfo', b'x')
    h.engine.scan_once()
    engine, requested = _restart(h, advance=SHORT_RESTART + 60)
    engine.stop()
    assert requested == [None]
    h.engine = SyncEngine(h.config, h.repo, h.remote, str(h.q.parent / 'data'), h.clock)
    h.engine.scan_once()
    raw = {'mappings': 'tv#%s#/media/tv#%s' % (h.a, h.c), 'excludes': '*.jpg', 'api_qps': 20, 'compare_mode': 'detail'}
    engine2, requested = _restart(h, Config.parse(raw))
    engine2.stop()
    assert requested == [None]      # 文件规则不同：旧扫描不算数


def test_no_record_means_a_normal_scan(harness):
    """没有扫描记录时照常扫描。"""
    h = harness()
    engine, requested = _restart(h)
    engine.stop()
    assert requested == [None] and engine.snapshot()['roots']['tv'] == 'awaiting_scan'


def test_plans_are_rechecked_at_start_only_when_settings_changed(harness):
    """只有设置变化时启动才复查计划结果。"""
    from app.plugins.localmirror115.config import Config
    h = harness(mode='dry_run')
    h.write('S/e.nfo', b'x')
    h.settle()
    h.run()
    h.engine.prepare()                       # 以这些设置首次启动：复查一次
    assert h.asset('S/e.nfo')['c_next'] == 0
    h.run()
    assert h.asset('S/e.nfo')['c_next'] > h.clock()
    same = SyncEngine(h.config, h.repo, h.remote, str(h.q.parent / 'data'), h.clock)
    same.prepare()
    assert h.asset('S/e.nfo')['c_next'] > h.clock()      # 设置未变：不复查所有计划
    raw = {'mappings': 'tv#%s#/media/tv#%s' % (h.a, h.c), 'api_qps': 20, 'compare_mode': 'detail',
           'mode': 'dry_run', 'notify_enabled': False}
    changed = SyncEngine(Config.parse(raw), h.repo, h.remote, str(h.q.parent / 'data'), h.clock)
    changed.prepare()
    assert h.asset('S/e.nfo')['c_next'] == 0              # 设置有变：立即复查


def test_crash_during_scan_is_not_reused_after_restart(harness):
    """扫描中途崩溃的记录在重启后不被沿用。"""
    h = harness()
    h.write('S/e.nfo', b'x')
    h.engine.scan_once()
    h.engine._save_record(scan_pending=True)       # 扫描已开始，随后进程退出
    engine = SyncEngine(h.config, h.repo, h.remote, str(h.q.parent / 'data'), h.clock)
    requested = []
    engine.request_scan = lambda keys=None, reason='': requested.append(keys)
    engine.start()
    try:
        assert requested == [None]
    finally:
        engine.stop()
