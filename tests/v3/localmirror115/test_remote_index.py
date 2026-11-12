"""115 快照（目录树导出/逐目录遍历）、反向核对与请求限速的测试。"""
import time

import pytest

from conftest import ops
from app.plugins.localmirror115 import remote_index
from app.plugins.localmirror115.adapters import RateLimiter, RemoteUnknown, normalise_tree
from app.plugins.localmirror115.config import Config
from app.plugins.localmirror115.engine import SyncEngine


# ---- 限速器 ----


def test_rate_limiter_paces_and_escalates_cooldown():
    """限速器按 qps 控制间隔，连续失败时冷却时间逐级加倍并有上限。"""
    now = [0.0]
    slept = []
    limiter = RateLimiter(qps=2, failure_threshold=2, cooldown_base=10, cooldown_max=25,
                          clock=lambda: now[0], sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
    limiter.acquire()
    limiter.acquire()
    assert slept == [0.5]
    limiter.failure()
    limiter.failure()                    # 达到阈值：冷却 10 秒
    assert limiter.state()['cooldown_seconds'] == 10
    limiter.failure(); limiter.failure()  # 下次冷却加倍，上限 25 秒
    assert limiter.state()['cooldown_seconds'] == 20
    limiter.failure(); limiter.failure()
    assert limiter.state()['cooldown_seconds'] == 25
    limiter.success()
    assert limiter.cooldowns == 3        # 失败之间的成功不会重置冷却等级……
    now[0] += 25
    limiter.success()
    assert limiter.cooldowns == 0        # ……只有持续 cooldown_max 秒无失败才会重置


def test_rate_limiter_counts_failures_despite_interleaved_successes():
    """失败之间夹杂成功请求时仍累计失败次数并触发冷却。"""
    now = [0.0]
    limiter = RateLimiter(qps=0, failure_threshold=3, cooldown_base=60, cooldown_max=600, clock=lambda: now[0])
    for _ in range(3):
        limiter.success()               # 例如每次上传前列目录成功……
        limiter.failure()               # ……但 115 拒绝了上传本身
        now[0] += 10
    assert limiter.state()['cooldown_seconds'] > 0


# ---- 目录树解析 ----


def test_normalise_tree_strips_header_and_root_and_counts_duplicates():
    """目录树解析去掉表头与根目录，并统计重名数量。"""
    paths = ['/', '根目录', '电视剧', '电视剧/国产剧', '电视剧/国产剧/a.mkv', '电视剧/国产剧/a.mkv', "电视剧/b\\'s.nfo"]
    tree = normalise_tree(paths, '电视剧')
    assert tree == {'国产剧': 1, '国产剧/a.mkv': 2, "b's.nfo": 1}


def test_normalise_tree_strips_paths_written_from_the_account_root():
    """目录树路径从账号根目录写起时能正确去掉前缀。"""
    # 115 实际为 /media/电视剧 导出的内容：父目录在前，每条路径都从账号根目录写起
    paths = ['根目录', 'media', 'media/电视剧', 'media/电视剧/国产剧', 'media/电视剧/国产剧/四喜 (2025)/Season 1/e1.nfo']
    tree = normalise_tree(paths, '/media/电视剧')
    assert tree == {'国产剧': 1, '国产剧/四喜 (2025)/Season 1/e1.nfo': 1}
    assert normalise_tree(['电视剧', '电视剧/a.nfo'], '/media/电视剧') == {'a.nfo': 1}
    assert normalise_tree(['a.nfo', 'b/c.nfo'], '/media/电视剧') == {'a.nfo': 1, 'b/c.nfo': 1}


def test_normalise_tree_keeps_a_subfolder_sharing_the_root_name():
    """与根目录同名的子目录不会被误去掉。"""
    tree = normalise_tree(['电视剧/x.nfo', 'other/y.nfo'], '电视剧')
    assert tree == {'电视剧/x.nfo': 1, 'other/y.nfo': 1}


# ---- 目录树模式 ----


@pytest.fixture
def fake_tree(monkeypatch):
    """替换目录树导出，返回可配置路径与失败开关的状态字典。"""
    state = {'calls': 0, 'fail': False}

    def export(cookie, fileid, name, limiter, timeout=0, stop=None, pending=None, lock=None):
        """记录调用次数，按开关抛出失败（与真实导出一样先计入限速器）或返回当前路径表。"""
        state['calls'] += 1
        if state['fail']:
            limiter.failure()
            raise RemoteUnknown('tree_export_start_failed')
        return state['paths']()
    monkeypatch.setattr(remote_index, 'export_tree', export)
    monkeypatch.setattr(remote_index, 'tree_export_available', lambda: True)
    return state


def tree_harness(harness, fake_tree, **extra):
    """创建目录树模式的工作台，导出结果取自假网盘当前内容。"""
    h = harness(compare_mode='tree', p115_cookie='UID=1', **extra)
    fake_tree['paths'] = lambda: {path[len('/media/tv/'):]: len(h.remote.copies(path))
                                  for folder in list(h.remote.dirs) for path in
                                  [folder + '/' + e['name'] for e in h.remote.dirs[folder]['entries']]
                                  if path.startswith('/media/tv/')}
    return h


def test_tree_mode_adopts_by_name_without_per_file_listing(harness, fake_tree):
    """目录树模式按名称认领已有文件，无需逐目录列表。"""
    h = tree_harness(harness, fake_tree, mode='dry_run')
    for i in range(5):
        h.write('S/e%d.mkv' % i, b'video')
        h.remote.put('/media/tv/S/e%d.mkv' % i, 5)
    h.write('S/new.mkv', b'video')
    h.remote.put('/media/tv/keep.txt', 1)   # 让 /media/tv 存在
    h.settle()
    h.engine.run_b()
    assert fake_tree['calls'] == 1
    assert not ops(h, 'list')                 # 判断都来自快照
    assert h.asset('S/e0.mkv')['b_note'] == 'would_adopt_existing'
    assert h.asset('S/new.mkv')['b_note'] == 'would_upload'


def test_tree_adopted_file_is_deleted_by_name_and_size(harness, fake_tree):
    """目录树认领的文件按名称与大小删除，不影响其他文件。"""
    h = tree_harness(harness, fake_tree, delete_enabled=True, delete_b_enabled=True, delete_delay_hours=0)
    h.write('S/keep.nfo', b'stays')
    h.remote.put('/media/tv/S/keep.nfo', 5)
    path = h.write('S/e1.mkv', b'video')
    h.remote.put('/media/tv/S/e1.mkv', 5)
    h.remote.put('/media/tv/S/other.mkv', 7)
    h.settle()
    h.engine.run_b()
    row = h.asset('S/e1.mkv')
    assert row['b_note'] == 'adopted_by_tree' and row['b_fileid'] is None
    path.unlink()
    h.settle(scans=2)
    h.engine.run_b()
    assert h.remote.copies('/media/tv/S/e1.mkv') == []
    assert len(h.remote.copies('/media/tv/S/other.mkv')) == 1


def test_tree_duplicates_are_not_adopted(harness, fake_tree):
    """目录树中有重名副本时不认领，最终只保留一份。"""
    h = tree_harness(harness, fake_tree)
    h.write('S/e1.nfo', b'meta')
    h.remote.put('/media/tv/S/e1.nfo', 4)
    h.remote.put('/media/tv/S/e1.nfo', 4)
    h.settle()
    h.engine.run_b()
    assert len(h.remote.copies('/media/tv/S/e1.nfo')) == 1


def test_tree_mode_without_cookie_falls_back_to_detail(harness, monkeypatch):
    """目录树模式缺少 Cookie 时回退为逐目录遍历并给出说明。"""
    monkeypatch.setattr(remote_index, 'tree_export_available', lambda: True)
    h = harness(compare_mode='tree', p115_cookie='')
    mode, note = h.engine.index.effective_mode()
    assert mode == 'detail' and 'Cookie' in note


def test_tree_export_failure_falls_back_for_that_build(harness, fake_tree):
    """目录树导出失败时本次构建回退为逐目录遍历。"""
    h = tree_harness(harness, fake_tree)
    fake_tree['fail'] = True
    h.write('S/e1.mkv', b'video')
    h.remote.put('/media/tv/S/e1.mkv', 5, mtime=time.time())
    h.settle()
    h.engine.run_b()
    info = h.engine.index.describe()['tv']
    assert info['snapshot_mode'] == 'detail' and '导出失败' in info['note']
    assert h.asset('S/e1.mkv')['b_state'] == 'synced'


# ---- 反向核对 ----


def test_file_deleted_on_115_is_reuploaded_after_snapshot_refresh(harness):
    """在 115 上被直接删除的文件在快照刷新后重新上传。"""
    h = harness()
    h.write('S/keep.nfo', b'stays')
    h.write('S/e1.mkv', b'video')
    h.settle()
    h.engine.run_b()
    fileid = h.remote.copies('/media/tv/S/e1.mkv')[0]['fileid']
    h.remote.delete('/media/tv/S/e1.mkv', fileid)     # 有人直接在 115 上删除了它
    h.clock.advance(10)
    h.engine.index_once(force=True)
    assert h.asset('S/e1.mkv')['b_note'] == 'missing_on_115'
    h.engine.run_b()
    assert len(h.remote.copies('/media/tv/S/e1.mkv')) == 1


def test_suspect_snapshot_does_not_trigger_mass_reupload(harness, monkeypatch):
    """可疑快照（如整个目录突然为空）不会触发批量重传。"""
    from app.plugins.localmirror115 import engine
    monkeypatch.setattr(engine, 'AUDIT_SUSPECT', 2)
    h = harness()
    for i in range(5):
        h.write('S/e%d.nfo' % i, b'meta')
    h.settle()
    h.engine.run_b()
    h.remote.dirs.clear()                               # 目录看起来是空的（账号错误、服务故障）
    h.clock.advance(10)
    h.engine.index_once(force=True)
    assert h.engine.remote_audit['tv']['state'] == 'suspect'
    assert all(h.asset('S/e%d.nfo' % i)['b_state'] == 'synced' for i in range(5))


def test_snapshot_survives_restart_without_rebuild(harness, tmp_path):
    """快照在重启后可直接使用，无需重建。"""
    h = harness()
    h.write('S/e1.mkv', b'video')
    h.settle()
    h.engine.run_b()
    restarted = SyncEngine(h.config, h.repo, h.remote, str(h.q.parent / 'data'), h.clock)
    assert restarted.index.snapshot(h.config.mappings[0]) is not None
    assert not restarted.index.stale(h.config.mappings[0])


def test_form_has_compare_mode_cookie_and_collapsed_advanced_sections():
    """设置表单含比对模式与 Cookie，高级选项折叠，常用开关在外。"""
    from app.plugins.localmirror115 import build_form, DEFAULTS
    form = str(build_form())
    assert "'compare_mode'" in form and "'p115_cookie'" in form and 'VExpansionPanels' in form
    for title in ('通知', '扫描与监控细节', '115访问与风控', '删除', '文件规则', '变化监测', '同步内容'):
        assert title in form
    # 常用开关位于折叠的高级面板之外
    vform = build_form()[0]['content']
    basic = str([c for c in vform if c.get('component') == 'VCard'])
    for key in ('watch_mode', 'transfer_event_enabled', 'scan_enabled', 'notify_enabled'):
        assert "'%s'" % key in basic
    assert DEFAULTS['compare_mode'] == 'tree' and DEFAULTS['api_qps'] == 2


def test_upload_confirmed_during_snapshot_build_is_not_audited_as_missing(harness):
    """快照构建期间确认上传的文件不会在核对中被判为缺失。"""
    h = harness()
    h.write('S/keep.nfo', b'stays')
    h.settle()
    h.engine.run_b()
    listed_before_upload = h.remote.walk('/media/tv')     # 缓慢的遍历在这里取得了列表……
    h.clock.advance(30)
    h.write('S/new.mkv', b'video')
    h.settle()
    h.engine.run_b()                                       # ……随后 new.mkv 被上传并确认
    h.remote.walk = lambda root, stop=None: listed_before_upload
    started = h.clock() - 60                               # 构建在上传之前就已开始
    real_clock = h.engine.index.clock
    h.engine.index.clock = lambda: started
    try:
        mapping = h.config.mappings[0]
        snap = h.engine.index.build(mapping)
    finally:
        h.engine.index.clock = real_clock
    h.engine._audit(mapping, snap)
    assert h.asset('S/new.mkv')['b_state'] == 'synced'


def test_tree_replace_then_failed_upload_does_not_adopt_deleted_copy(harness, fake_tree):
    """目录树认领的文件替换时上传失败，之后不会认领已删除的旧副本。"""
    h = tree_harness(harness, fake_tree)
    path = h.write('S/e1.nfo', b'v1')
    h.remote.put('/media/tv/S/e1.nfo', 2)
    h.settle()
    h.engine.run_b()
    assert h.asset('S/e1.nfo')['b_note'] == 'adopted_by_tree'
    path.write_bytes(b'version-2')
    h.settle()
    h.remote.fail_upload = True
    h.engine.run_b()                             # 旧副本已删除，上传失败
    h.remote.fail_upload = False
    h.clock.advance(4000)
    h.engine.run_b()
    copies = h.remote.copies('/media/tv/S/e1.nfo')
    assert len(copies) == 1 and copies[0]['size'] == 9


def test_detail_restore_after_remote_delete_is_reuploaded(harness):
    """逐目录模式下删除后恢复的源文件会重新上传。"""
    h = harness(delete_enabled=True, delete_b_enabled=True, delete_delay_hours=0)
    h.write('S/keep.nfo', b'stays')
    path = h.write('S/e1.mkv', b'video', mtime=1700000000)
    h.settle()
    h.engine.run_b()
    data = path.read_bytes()
    path.unlink()
    h.settle(scans=2)
    h.engine.run_b()
    assert h.remote.copies('/media/tv/S/e1.mkv') == []
    h.write('S/e1.mkv', data, mtime=1700000000)
    h.settle()
    h.engine.run_b()
    assert len(h.remote.copies('/media/tv/S/e1.mkv')) == 1


def test_snapshot_miss_adopts_copy_found_in_fresh_listing(harness):
    """快照中没有但实时列表中有的文件会被认领，不重复上传。"""
    h = harness()
    h.write('S/keep.nfo', b'stays')
    h.settle()
    h.engine.run_b()                             # 快照构建时还没有 e1
    h.write('S/e1.mkv', b'video', mtime=time.time() - 60)
    h.remote.put('/media/tv/S/e1.mkv', 5)        # 期间另一个工具把它放到了 115
    h.settle()
    h.engine.run_b()
    assert not [c for c in h.remote.calls if c[0] == 'upload' and c[2] == 'e1.mkv']
    assert h.asset('S/e1.mkv')['b_note'] == 'adopted'


def test_fallback_snapshot_is_not_rebuilt_in_a_loop(harness, fake_tree):
    """回退得到的快照不会被反复重建。"""
    h = tree_harness(harness, fake_tree)
    fake_tree['fail'] = True
    h.remote.put('/media/tv/keep.txt', 1)
    h.write('S/e1.mkv', b'video')
    h.settle()
    for _ in range(5):
        h.engine.run_b()
        h.clock.advance(70)
    assert fake_tree['calls'] == 1


def test_failed_builds_back_off_and_uploads_fall_back_to_listings(harness):
    """快照构建失败后退避，上传改用实时列目录判断。"""
    h = harness()
    h.remote.walk = lambda root, stop=None: (_ for _ in ()).throw(RemoteUnknown('folder_list_failed'))
    h.write('S/e1.mkv', b'video')
    h.settle()
    h.engine.run_b()                             # 构建失败，开始退避
    assert h.engine.index.failing(h.config.mappings[0])
    h.engine.repository.record('b', h.asset('S/e1.mkv'), b_next=0)
    h.engine.run_b()
    assert len(h.remote.copies('/media/tv/S/e1.mkv')) == 1
    assert not h.engine.index.stale(h.config.mappings[0])   # 不会立即重建


def test_truncated_snapshot_file_does_not_stop_the_plugin(harness, tmp_path):
    """快照文件被截断时忽略该快照，插件照常运行。"""
    h = harness()
    h.write('S/e1.mkv', b'video')
    h.settle()
    h.engine.run_b()
    path = h.engine.index._file(h.config.mappings[0])
    path.write_bytes(path.read_bytes()[:20])
    restarted = SyncEngine(h.config, h.repo, h.remote, str(h.q.parent / 'data'), h.clock)
    assert restarted.index.snapshot(h.config.mappings[0]) is None


def test_unexpected_export_error_falls_back_and_status_is_not_stuck(harness, monkeypatch):
    """导出出现意外错误时回退为逐目录遍历，状态不会卡住。"""
    monkeypatch.setattr(remote_index, 'tree_export_available', lambda: True)
    monkeypatch.setattr(remote_index, 'export_tree', lambda *a, **k: (_ for _ in ()).throw(ValueError('boom')))
    h = harness(compare_mode='tree', p115_cookie='UID=1')
    h.remote.put('/media/tv/keep.txt', 1)
    h.engine.index_once()
    info = h.engine.index.describe()['tv']
    assert info['state'] == 'ready' and info['snapshot_mode'] == 'detail' and 'ValueError' in info['note']


def test_undecodable_name_does_not_block_the_batch(harness, monkeypatch):
    """无法解码的文件名被跳过并计数，不阻塞整批处理。"""
    h = harness()
    h.write('S/good.nfo', b'x')
    real_scan = h.engine.fs.scan

    def scan(root, limit, stop, excluded, progress=None):
        """在真实扫描结果中加入一个无法解码的文件名。"""
        manifest = real_scan(root, limit, stop, excluded)
        from app.plugins.localmirror115.filesystem import Observation
        manifest['S/bad\udcff.nfo'] = Observation('present', {'size': 1, 'mtime_ns': 1})
        return manifest
    monkeypatch.setattr(h.engine.fs, 'scan', scan)
    h.settle()
    assert h.asset('S/good.nfo') is not None
    assert h.engine.status['skipped_names']['tv'] == 1
    assert h.engine.status['roots']['tv'] == 'ready'


def test_cookie_is_not_in_config_repr():
    """配置的字符串表示中不包含 Cookie 内容。"""
    config = Config.parse({'mappings': 'tv#/m/tv#/media/tv#/c/tv', 'p115_cookie': 'UID=secret-value'})
    assert 'secret-value' not in repr(config)


def test_abandoned_export_file_is_cleaned_up_on_next_build(harness, monkeypatch):
    """超时遗留的导出文件在下次构建时被清理。"""
    cleaned = []
    monkeypatch.setattr(remote_index, 'tree_export_available', lambda: True)
    monkeypatch.setattr(remote_index, 'cleanup_export',
                        lambda cookie, eid, limiter, stop=None: cleaned.append(eid) or True)

    def export(cookie, fileid, name, limiter, timeout=0, stop=None, pending=None, lock=None):
        """登记一个待清理的导出任务后抛出超时。"""
        pending.append(42)
        raise RemoteUnknown('tree_export_timeout')
    monkeypatch.setattr(remote_index, 'export_tree', export)
    h = harness(compare_mode='tree', p115_cookie='UID=1')
    h.remote.put('/media/tv/keep.txt', 1)
    h.engine.index_once()
    assert h.engine.index.pending_exports == [42]
    h.engine.index.build(h.config.mappings[0])
    assert cleaned == [42]


def test_snapshot_miss_never_adopts_from_a_stale_cached_listing(harness):
    """快照未命中时不会依据超过 FRESH 秒的缓存列表认领文件。"""
    from app.plugins.localmirror115.engine import FRESH
    h = harness()
    h.write('S/keep.nfo', b'stays')
    h.settle()
    h.engine.run_b()                                  # 快照已构建；e1 不在 115 上
    copy = h.remote.put('/media/tv/S/e1.mkv', 5)      # 短暂出现在 115 上……
    h.engine._listing(h.engine.config.mappings[0].remote / 'S', max_age=0)   # ……并被缓存
    h.remote.delete('/media/tv/S/e1.mkv', copy['fileid'])          # ……随后又消失
    h.clock.advance(FRESH)                            # 缓存已超过可复用的时长
    path = h.write('S/e1.mkv', b'video', mtime=time.time() - 60)
    h.engine.hint(str(path))
    h.clock.advance(2)
    h.engine.tick()
    h.engine.run_b()
    assert len(h.remote.copies('/media/tv/S/e1.mkv')) == 1
    assert h.asset('S/e1.mkv')['b_note'] != 'adopted'


def test_failed_snapshot_wakes_files_waiting_for_it(harness):
    """快照生成失败时，正在等快照的新文件立即改用实时列目录，而不是等一小时。"""
    h = harness()
    h.write('S/e1.mkv', b'video')
    h.settle()
    h.repo.record_many('b', [(h.asset('S/e1.mkv'), {'b_note': 'awaiting_remote_snapshot',
                                                   'b_next': h.clock() + 3600})])

    def broken(mapping):
        """模拟快照生成失败。"""
        raise RemoteUnknown('snapshot_build_failed')
    h.engine.index.build = broken
    h.engine.index_once(force=True)
    assert h.asset('S/e1.mkv')['b_next'] == 0
