"""同步引擎边界情况测试：配置变更、上传校验与删除守卫等。"""
import time

from sqlalchemy.orm import sessionmaker

from conftest import deleting, index_first, ops
from app.plugins.localmirror115.adapters import RemoteUnknown
from app.plugins.localmirror115.config import Config
from app.plugins.localmirror115.engine import SyncEngine
from app.plugins.localmirror115.persistence import Repository


def test_mapping_path_change_mirrors_to_new_locations(harness, tmp_path):
    """映射路径变更后，文件被镜像到新的 115 与本地副本位置。"""
    h = harness()
    h.write('S/e1.nfo', b'meta')
    h.settle()
    h.run()
    assert h.remote.copies('/media/tv/S/e1.nfo')
    new_c = tmp_path / 'c2'
    new_c.mkdir()
    raw = {'enabled': True, 'mode': 'sync', 'copy_enabled': True, 'upload_enabled': True, 'settle_seconds': 1,
           'api_qps': 20, 'compare_mode': 'detail', 'mappings': 'tv#%s#/media/tv2#%s' % (h.a, new_c)}
    config = Config.parse(raw)
    repo = Repository(sessionmaker(h.db), config.revision, config.revision_b, config.revision_c)
    engine = SyncEngine(config, repo, h.remote, str(tmp_path / 'data'), h.clock)
    index_first(engine)
    repo.realign()
    (h.a / 'S' / 'e1.nfo').touch()  # 之后任一观察都会让该记录按新版本重新稳定
    engine.scan_once()
    h.clock.advance(5)
    engine.tick()
    engine.run_c()
    engine.run_b()
    assert (new_c / 'S' / 'e1.nfo').read_bytes() == b'meta'
    assert len(h.remote.copies('/media/tv2/S/e1.nfo')) == 1


def test_old_engine_result_is_ignored_after_revision_change(harness, tmp_path):
    """配置版本变更后，旧引擎写回的结果被忽略。"""
    h = harness()
    h.write('S/e1.nfo', b'meta')
    h.settle()
    stale = h.repo.work('b', h.clock())[0]
    other = Repository(sessionmaker(h.db), h.config.revision, {'tv': 'x' * 64}, h.config.revision_c)
    other.realign()
    assert not h.repo.record('b', stale, b_state='synced')


def test_restored_file_is_mirrored_again_after_partial_delete(harness):
    """部分删除后恢复的同内容文件会再次镜像到本地副本。"""
    h = harness(**deleting(delete_b_enabled=False))
    h.write('S/keep.nfo', b'stays')
    src = h.write('S/e1.nfo', b'meta', mtime=1700000000)
    h.settle()
    h.run()
    data = src.read_bytes()
    src.unlink()
    h.settle(scans=2)
    h.run()
    assert not (h.c / 'S' / 'e1.nfo').exists()
    h.write('S/e1.nfo', data, mtime=1700000000)  # 字节与修改时间与之前相同
    h.settle()
    h.engine.run_c()
    assert (h.c / 'S' / 'e1.nfo').read_bytes() == b'meta'


def test_verify_survives_listing_error_without_duplicate_upload(harness):
    """校验期间列目录失败时保持待校验，不重复上传。"""
    h = harness()
    h.write('S/e1.mkv', b'video')
    h.settle()
    h.remote.lag = True
    h.engine.run_b()
    assert h.asset('S/e1.mkv')['b_state'] == 'verify'
    original = h.remote.list_dir
    h.remote.list_dir = lambda path: (_ for _ in ()).throw(RemoteUnknown('folder_list_failed'))
    h.clock.advance(120)
    h.engine.run_b()
    h.remote.list_dir = original
    row = h.asset('S/e1.mkv')
    assert row['b_state'] == 'verify' and row['b_pending']
    h.clock.advance(4000)
    h.remote.reveal()
    h.engine.run_b()
    assert len(ops(h, 'upload')) == 1
    assert len(h.remote.copies('/media/tv/S/e1.mkv')) == 1
    assert h.asset('S/e1.mkv')['b_state'] == 'synced'


def test_unconfirmed_upload_is_cleaned_up_when_source_disappears(harness):
    """未确认的上传在源文件消失后被清理。"""
    h = harness(**deleting())
    h.write('S/keep.nfo', b'stays')
    path = h.write('S/e1.mkv', b'video')
    h.settle()
    h.remote.lag = True
    h.engine.run_b()
    h.remote.reveal()
    path.unlink()
    h.settle(scans=2)
    h.engine.run_b()
    assert h.remote.copies('/media/tv/S/e1.mkv') == []


def test_deletion_is_real_time_after_a_file_event(harness):
    """文件事件后只需稳定复查即可实时删除，无需全量扫描。"""
    h = harness(**deleting())
    h.write('S/keep.nfo', b'stays')
    h.write('S/e1.mkv', b'video')
    h.write('S/e1.nfo', b'meta')
    h.settle()
    h.run()
    for name in ('e1.mkv', 'e1.nfo'):
        path = h.a / 'S' / name
        path.unlink()
        h.engine.hint(str(path))
    h.clock.advance(2)                    # 只做稳定复查，不做全量扫描
    h.engine.tick()
    h.run()
    assert not (h.c / 'S' / 'e1.nfo').exists()  # STRM目录是本地删除，立即执行
    assert h.remote.copies('/media/tv/S/e1.mkv')  # 115 等本地删除停下后再合并删除
    h.clock.advance(31)
    h.engine.run_b()
    assert h.remote.copies('/media/tv/S/e1.mkv') == []


def test_event_observation_does_not_inherit_scan_evidence(harness):
    """事件观察不沿用扫描得到的存在证据。"""
    h = harness(**deleting())
    h.write('S/keep.nfo', b'stays')
    path = h.write('S/e1.nfo', b'meta')
    h.settle()
    h.write('S/e1.nfo', b'meta2', mtime=time.time() + 5)
    h.engine.scan_once()               # 扫描将变化后的存在状态加入队列
    path.unlink()                      # 稳定前即已消失
    h.engine.hint(str(path))
    h.clock.advance(2)
    h.engine.tick()
    row = h.asset('S/e1.nfo')
    assert row['a_state'] == 'missing'


def test_changing_only_strm_path_does_not_reupload_to_115(harness, tmp_path):
    """只修改本地副本路径时不会重新上传到 115。"""
    h = harness(adopt_existing=False)
    h.write('S/e1.mkv', b'video')
    h.write('S/e1.nfo', b'meta')
    h.settle()
    h.run()
    uploads = len(ops(h, 'upload'))
    new_c = tmp_path / 'c3'
    new_c.mkdir()
    raw = {'enabled': True, 'mode': 'sync', 'copy_enabled': True, 'upload_enabled': True, 'adopt_existing': False,
           'settle_seconds': 1, 'api_qps': 20, 'compare_mode': 'detail',
           'mappings': 'tv#%s#/media/tv#%s' % (h.a, new_c)}
    config = Config.parse(raw)
    repo = Repository(sessionmaker(h.db), config.revision, config.revision_b, config.revision_c)
    engine = SyncEngine(config, repo, h.remote, str(tmp_path / 'data'), h.clock)
    index_first(engine)
    repo.realign()
    engine.run_c()
    engine.run_b()
    assert (new_c / 'S' / 'e1.nfo').read_bytes() == b'meta'
    assert len(ops(h, 'upload')) == uploads
    assert len(h.remote.copies('/media/tv/S/e1.mkv')) == 1


def test_stray_copy_does_not_leave_upload_stuck_forever(harness):
    """迟到的多余副本不会让上传永远卡在校验状态。"""
    h = harness()
    h.write('S/e1.mkv', b'video')
    h.settle()
    original = h.remote.upload
    def upload_without_id(folder, local, name):
        """上传后不返回 fileid，并让一个旧副本迟到出现。"""
        result = original(folder, local, name)
        result['fileid'] = None
        h.remote.put('/media/tv/S/e1.mkv', 3)  # 一个较旧的多余副本迟到出现
        return result
    h.remote.upload = upload_without_id
    h.engine.run_b()
    assert h.asset('S/e1.mkv')['b_state'] == 'verify'
    h.remote.upload = original
    h.clock.advance(h.config.verify_window_minutes * 60 + 1)
    h.engine.run_b()
    copies = h.remote.copies('/media/tv/S/e1.mkv')
    assert len(copies) == 1 and copies[0]['size'] == 5
    assert h.asset('S/e1.mkv')['b_note'] == 'awaiting_late_copies'  # 第二次上传：再等一个校验窗口
    h.clock.advance(h.config.verify_window_minutes * 60 + 1)
    h.engine.run_b()
    assert h.asset('S/e1.mkv')['b_state'] == 'synced'


def test_vanished_file_on_other_device_than_its_ancestor_is_unknown(harness, monkeypatch):
    """文件消失但所在设备与上级目录不同时视为未知，不删除。"""
    h = harness(**deleting())
    h.write('S/keep.nfo', b'stays')
    h.write('S/e1.nfo', b'meta')
    h.settle()
    h.run()
    (h.a / 'S' / 'e1.nfo').unlink()
    monkeypatch.setattr(h.engine.fs, 'ancestor_device', lambda root, rel: -1)  # 如同空的挂载点
    h.settle(scans=2)
    h.run()
    row = h.asset('S/e1.nfo')
    assert row['a_state'] == 'present'
    assert (h.c / 'S' / 'e1.nfo').exists()


def test_unexpected_scan_error_marks_root_unsafe(harness, monkeypatch):
    """扫描中出现意外错误时将根目录标记为不安全。"""
    h = harness()
    h.write('S/e1.nfo', b'x')
    h.settle()
    assert h.engine.status['roots']['tv'] == 'ready'
    monkeypatch.setattr(h.repo, 'fingerprints', lambda key: (_ for _ in ()).throw(ValueError('db')))
    h.engine.scan_once()
    assert h.engine.status['roots']['tv'] == 'scan_incomplete_or_unsafe'


def test_same_size_change_after_sync_is_reuploaded(harness):
    """同步后大小不变但内容变化的文件会重新上传。"""
    h = harness()
    path = h.write('S/e1.nfo', b'aaaa', mtime=time.time())
    h.settle()
    h.engine.run_b()
    first = h.remote.copies('/media/tv/S/e1.nfo')[0]['fileid']
    path.write_bytes(b'bbbb')
    h.settle()
    h.engine.run_b()
    copies = h.remote.copies('/media/tv/S/e1.nfo')
    assert len(copies) == 1 and copies[0]['fileid'] != first


def test_adopted_c_file_is_deleted_like_any_other(harness):
    """认领的本地副本文件与其他文件一样会被删除。"""
    h = harness(**deleting())
    h.write('S/keep.nfo', b'stays')
    h.write('S/e1.nfo', b'meta')
    (h.c / 'S').mkdir()
    (h.c / 'S' / 'e1.nfo').write_bytes(b'meta')
    h.settle()
    h.engine.run_c()
    assert h.asset('S/e1.nfo')['c_owner'] == 'adopted'  # 文件已存在且内容相同
    (h.a / 'S' / 'e1.nfo').unlink()
    h.settle(scans=2)
    h.engine.run_c()
    assert not (h.c / 'S' / 'e1.nfo').exists()


def test_no_deletion_before_first_scan_of_this_engine(harness, tmp_path):
    """本引擎完成首次扫描前不执行删除。"""
    h = harness(**deleting())
    h.write('S/keep.nfo', b'stays')
    h.write('S/e1.nfo', b'meta')
    h.settle()
    h.run()
    (h.a / 'S' / 'e1.nfo').unlink()
    h.settle(scans=2)
    fresh = SyncEngine(h.config, h.repo, h.remote, str(tmp_path / 'data'), h.clock)
    index_first(fresh)
    assert fresh.status['roots']['tv'] == 'awaiting_scan'
    fresh.run_c()
    assert (h.c / 'S' / 'e1.nfo').exists()
    assert h.asset('S/e1.nfo')['c_note'] == 'root_awaiting_scan'


def test_unknown_observation_is_postponed_not_spun(harness):
    """状态未知的观察被推迟，不会反复空转。"""
    h = harness()
    h.write('S/e1.nfo', b'meta')
    h.engine.scan_once()
    import os
    os.rename(h.a, h.a.with_name('gone'))
    h.a.mkdir()
    h.clock.advance(2)
    h.engine.tick()
    assert h.repo.due(h.clock()) == []


def test_scan_only_queues_differences(harness):
    """扫描只将有变化的文件加入队列。"""
    h = harness()
    for i in range(5):
        h.write('S/e%d.nfo' % i, b'meta')
    h.settle()
    h.engine.scan_once()
    assert h.repo.counts()['pending'] == 0
