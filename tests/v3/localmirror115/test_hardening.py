"""加固测试：快照与删除、短暂重载、115 请求量、数据库停放与接口规范。"""
import pytest
from sqlalchemy import text

from conftest import deleting
from app.plugins.localmirror115.config import Config
from app.plugins.localmirror115.engine import SyncEngine, instance_state
from app.plugins.localmirror115.persistence import PARKED, Asset


def test_deleted_file_is_not_adopted_from_the_saved_snapshot_after_restart(harness):
    """插件删除 115 副本后，重启加载的旧快照不能把同名新文件认领为“115已有”。"""
    h = harness(**deleting())
    h.write('S/keep.nfo', b'keep')
    h.write('S/e.nfo', b'old')
    h.settle()
    h.run()
    (h.a / 'S' / 'e.nfo').unlink()
    h.settle()
    h.run()
    assert h.remote.copies('/media/tv/S/e.nfo') == []
    h.engine.index.flush()
    restarted = SyncEngine(h.config, h.repo, h.remote, str(h.q.parent / 'data'), h.clock)
    assert not restarted.index.snapshot(h.config.mappings[0]).has('S/e.nfo')


def test_failed_scan_is_not_reused_after_a_short_reload(harness):
    """全量扫描失败的映射在短暂重载后仍要重新扫描，不能沿用旧基准而恢复删除。"""
    h = harness()
    h.write('S/e.nfo', b'x')
    h.engine.scan_once()
    assert 'tv' in h.engine.scan_record['scans']
    h.engine.fs.scan = lambda *a, **k: (_ for _ in ()).throw(OSError('unreadable'))
    h.engine.scan_once()
    assert 'tv' not in h.engine.scan_record['scans']


def test_only_an_in_process_reload_skips_the_startup_scan(harness):
    """沿用上次扫描只适用于同一进程内的重载；没有进程内停止记录（MP 整体重启）时照常扫描。"""
    h = harness()
    h.write('S/e.nfo', b'x')
    h.engine.scan_once()
    data = str(h.q.parent / 'data')
    state = instance_state(data)
    state.stopped_at = None
    assert SyncEngine(h.config, h.repo, h.remote, data, h.clock)._resumable() == set()
    state.stopped_at = h.clock()
    assert SyncEngine(h.config, h.repo, h.remote, data, h.clock)._resumable() == {'tv'}


def test_files_added_during_a_long_walk_are_not_counted_as_gone(harness):
    """遍历期间新增并已稳定的文件不在清单中，不能被当成消失。"""
    h = harness()
    h.write('S/old.nfo', b'x')
    h.settle()
    real_scan = h.engine.fs.scan

    def scan_while_a_file_settles(*args, **kwargs):
        """模拟遍历期间有新文件被监控发现并已稳定入库。"""
        manifest = real_scan(*args, **kwargs)
        h.write('S/new.nfo', b'y')
        h.engine.hint(str(h.a / 'S' / 'new.nfo'))
        h.clock.advance(2)
        h.engine.tick()
        return manifest
    h.engine.fs.scan = scan_while_a_file_settles
    h.engine.scan_once()
    assert h.engine.status['roots']['tv'] == 'ready'
    assert h.asset('S/new.nfo')['a_state'] == 'present'


def test_changing_video_extensions_reclassifies_known_files(harness, tmp_path):
    """视频后缀设置变化后，已有记录在下次扫描时重新分类。"""
    h = harness()
    h.write('S/a.mka', b'audio')
    h.settle()
    assert h.asset('S/a.mka')['kind'] == 'meta'
    raw = {'mappings': 'tv#%s#/media/tv#%s' % (h.a, h.c), 'api_qps': 20, 'compare_mode': 'detail',
           'video_extensions': 'mkv,mka', 'settle_seconds': 1}
    config = Config.parse(raw)
    engine = SyncEngine(config, h.repo, h.remote, str(tmp_path / 'data2'), h.clock)
    engine.scan_once()
    h.clock.advance(2)
    engine.tick()
    assert h.asset('S/a.mka')['kind'] == 'video'


def test_new_file_upload_costs_few_115_calls(harness):
    """新文件上传：一次实时列目录、一次上传、一次确认列目录。"""
    h = harness()
    h.write('S/keep.nfo', b'k')
    h.settle()
    h.run()
    h.remote.calls.clear()
    h.write('S/e1.mkv', b'video')
    h.settle()
    h.engine.run_b()
    assert h.asset('S/e1.mkv')['b_state'] == 'synced'
    assert len(h.remote.calls) <= 3, h.remote.calls


def test_reupload_after_missing_on_115_is_counted_as_reupload(harness):
    """115 上缺失后的补传单独计数，而不是算作普通上传。"""
    from app.plugins.localmirror115.notify import Notifier
    h = harness()
    h.engine.notifier = Notifier(lambda t, x: None, True, clock=h.clock)
    h.write('S/e.mkv', b'video')
    h.settle()
    h.run()
    asset = h.asset('S/e.mkv')
    h.remote.delete('/media/tv/S/e.mkv', asset['b_fileid'])
    h.repo.mark_missing_on_remote([asset['id']], h.clock())
    h.engine.run_b()
    assert h.engine.notifier.counts.get('reuploaded') == 1


def test_rows_waiting_for_the_first_snapshot_are_parked_in_one_write(harness):
    """快照尚未生成时，首次见到的文件整批停放，快照生成后统一唤醒，而不是每 5 分钟逐个检查。"""
    h = harness()
    for i in range(5):
        h.write('S/e%d.nfo' % i, b'x')
    h.settle()
    h.engine.index.failures.clear()
    h.engine.index.snapshots.clear()
    h.engine.index.loaded = True
    type(h.engine).run_b(h.engine)  # 不经 index_first：模拟快照线程还没完成
    rows = [h.asset('S/e%d.nfo' % i) for i in range(5)]
    assert all(row['b_note'] == 'awaiting_remote_snapshot' and row['b_next'] > h.clock() for row in rows)
    h.engine.index_once(force=True)
    assert all(h.asset('S/e%d.nfo' % i)['b_next'] == 0 for i in range(5))


def test_synced_rows_are_parked_out_of_the_work_queue(harness):
    """已同步的行停放到 PARKED，工作队列查询只扫描真正待处理的行；文件变化后重新进入队列。"""
    h = harness()
    h.write('S/e.nfo', b'x')
    h.settle()
    h.run()
    row = h.asset('S/e.nfo')
    assert row['b_next'] == PARKED and row['c_next'] == PARKED
    h.write('S/e.nfo', b'changed', mtime=1700000999)
    h.settle()
    assert h.asset('S/e.nfo')['b_next'] < PARKED
    with h.repo.transaction() as s:
        plan = ' '.join(str(r) for r in s.execute(text(
            'EXPLAIN QUERY PLAN SELECT id FROM lm115_asset_v1 WHERE b_next <= 1 ORDER BY b_next, id')))
    assert 'ix_lm115_asset_v1_b_next' in plan


def test_cleared_numeric_settings_fall_back_to_defaults():
    """设置页清空数值项时使用默认值，而不是让插件无法启动。"""
    config = Config.parse({'mappings': 'tv#/m/tv#/media/tv#/c/tv', 'settle_seconds': '', 'api_qps': None})
    assert config.settle_seconds == 5 and config.api_qps == 2
    with pytest.raises(ValueError):
        Config.parse({'mappings': 'tv#/m/tv#/media/tv#/c/tv', 'api_qps': 0})


def test_transaction_rolls_back_on_error(harness):
    """事务内出错时整体回滚，不留下部分写入。"""
    h = harness()
    h.write('S/e.nfo', b'x')
    h.settle()
    asset = h.asset('S/e.nfo')
    with pytest.raises(RuntimeError):
        with h.repo.transaction() as s:
            s.get(Asset, asset['id']).b_note = 'half-written'
            raise RuntimeError('boom')
    assert h.asset('S/e.nfo')['b_note'] != 'half-written'


def test_page_buttons_return_the_standard_response(harness):
    """状态页按钮接口返回宿主标准的 success/message/data 三字段响应。"""
    from app.plugins.localmirror115 import LocalMirror115
    plugin = LocalMirror115()
    plugin._engine = None
    for endpoint in (plugin.api_scan, plugin.api_ping, plugin.api_index):
        body = endpoint().model_dump()
        assert set(body) == {'success', 'message', 'data'}
    assert all(api['response_model'] is not None for api in plugin.get_api())


def test_consecutive_new_files_in_one_folder_reuse_the_confirmation_listing(harness):
    """同一目录连续上传两个新文件：第二个直接复用第一个上传后确认用的列表，少一次列目录。"""
    h = harness()
    h.write('S/keep.nfo', b'k')
    h.settle()
    h.run()
    h.write('S/e1.mkv', b'video1')
    h.write('S/e2.mkv', b'video2')
    h.settle()
    h.remote.calls.clear()
    h.engine.run_b()
    assert h.asset('S/e1.mkv')['b_state'] == h.asset('S/e2.mkv')['b_state'] == 'synced'
    assert [call[0] for call in h.remote.calls].count('list') == 3, h.remote.calls  # 列、传、确认、传、确认


def test_expired_listings_are_cleaned_up_without_touching_fresh_ones(harness):
    """定期清理只丢弃超过有效期的目录列表缓存。"""
    from app.plugins.localmirror115.engine import LISTING_TTL
    h = harness()
    h.engine.listings = {'/media/tv/old': (h.clock() - LISTING_TTL - 1, None),
                         '/media/tv/new': (h.clock(), None)}
    h.engine.listing_cleanup_at = 0
    h.engine._listing(h.config.mappings[0].remote / 'S')
    assert '/media/tv/old' not in h.engine.listings and '/media/tv/new' in h.engine.listings


def test_strm_copy_records_the_digest_computed_while_verifying(harness):
    """STRM 复制时校验顺带算出的 SHA-256 与文件实际内容一致（之后据此判断文件是否被他人改过）。"""
    from app.plugins.localmirror115.filesystem import sha256
    h = harness()
    h.write('S/e.nfo', b'metadata')
    h.settle()
    h.run()
    assert h.asset('S/e.nfo')['c_written']['sha256'] == sha256(h.c / 'S' / 'e.nfo')


def test_short_reload_with_an_emptied_database_scans_again(harness):
    """短暂重载时数据库已被清空（如重置插件数据），不能沿用上次扫描，必须重新扫描。"""
    from sqlalchemy import delete
    from app.plugins.localmirror115.persistence import Asset, Pending
    h = harness()
    h.write('S/e.nfo', b'x')
    h.engine.scan_once()
    data = str(h.q.parent / 'data')
    instance_state(data).stopped_at = h.clock()
    assert SyncEngine(h.config, h.repo, h.remote, data, h.clock)._resumable() == {'tv'}
    with h.repo.transaction() as s:
        s.execute(delete(Asset))
        s.execute(delete(Pending))
    assert SyncEngine(h.config, h.repo, h.remote, data, h.clock)._resumable() == set()
