"""恢复与安全守卫测试：删除期间恢复、上传竞态、根目录绑定与配置边界。"""
import os
import time

import pytest
from sqlalchemy.orm import sessionmaker

from conftest import deleting, index_first, ops
from app.plugins.localmirror115.config import Config
from app.plugins.localmirror115.engine import SyncEngine
from app.plugins.localmirror115.filesystem import Observation


def event_delete(h, relative):
    """删除源文件并发送对应的文件事件。"""
    path = h.a / relative
    path.unlink()
    h.engine.hint(str(path))


def test_emptying_the_tree_via_events_is_not_a_deletion_baseline(harness):
    """通过事件清空整个目录树不会成为删除基线，根目录被标记为不安全。"""
    h = harness(**deleting())
    h.write('S/e1.nfo', b'a')
    h.write('S/e2.nfo', b'b')
    h.settle()
    h.run()
    event_delete(h, 'S/e1.nfo')
    event_delete(h, 'S/e2.nfo')
    h.clock.advance(2)
    h.engine.tick()
    h.settle(scans=3)
    h.run()
    assert h.engine.status['roots']['tv'] == 'scan_incomplete_or_unsafe'
    assert len(list((h.c / 'S').glob('*.nfo'))) == 2


def test_file_restored_during_remote_delete_is_reuploaded(harness):
    """115 删除期间源文件恢复时会重新上传。"""
    h = harness(**deleting(delete_c_enabled=False))
    h.write('S/keep.nfo', b'stays')
    path = h.write('S/e1.mkv', b'video', mtime=1700000000)
    h.settle()
    h.engine.run_b()
    path.unlink()
    h.settle(scans=2)
    original = h.remote.delete

    def delete_and_restore(remote_path, fileid):
        """删除 115 文件后以相同指纹恢复源文件。"""
        result = original(remote_path, fileid)
        h.write('S/e1.mkv', b'video', mtime=1700000000)  # 以完全相同的指纹恢复
        return result
    h.remote.delete = delete_and_restore
    h.engine.run_b()
    h.remote.delete = original
    h.settle()
    h.engine.run_b()
    assert len(h.remote.copies('/media/tv/S/e1.mkv')) == 1
    assert h.asset('S/e1.mkv')['b_state'] == 'synced'


def test_same_size_change_during_first_upload_is_not_adopted(harness):
    """首次上传期间发生同大小修改时不认领旧上传，而是重新上传。"""
    h = harness()
    path = h.write('S/e1.nfo', b'AAAA', mtime=time.time())
    h.settle()
    original = h.remote.upload

    def upload_then_change(folder, local, name):
        """上传后修改源文件内容并完成稳定。"""
        result = original(folder, local, name)
        path.write_bytes(b'BBBB')
        os.utime(path, (time.time() + 5, time.time() + 5))
        h.engine.hint(str(path))       # 期间由稳定线程确认这次修改
        h.clock.advance(2)
        h.engine.tick()
        return result
    h.remote.upload = upload_then_change
    h.engine.run_b()
    h.remote.upload = original
    h.settle()
    h.engine.run_b()
    assert len(ops(h, 'upload')) == 2
    copies = h.remote.copies('/media/tv/S/e1.nfo')
    assert len(copies) == 1
    assert h.asset('S/e1.nfo')['b_note'] != 'adopted'


def test_file_renamed_during_upload_leaves_no_orphan(harness):
    """上传期间源文件被重命名时，不在 115 上遗留孤立文件。"""
    h = harness(**deleting())
    h.write('S/keep.nfo', b'stays')
    path = h.write('S/old.mkv', b'video')
    h.settle()
    original = h.remote.upload

    def upload_then_rename(folder, local, name):
        """上传后重命名源文件并完成稳定。"""
        result = original(folder, local, name)
        os.rename(path, h.a / 'S' / 'new.mkv')
        h.engine.hint(str(path))       # 期间由稳定线程记录文件缺失
        h.clock.advance(2)
        h.engine.tick()
        return result
    h.remote.upload = upload_then_rename
    h.engine.run_b()
    h.remote.upload = original
    h.settle(scans=2)
    h.engine.run_b()
    h.engine.run_b()
    assert h.remote.copies('/media/tv/S/old.mkv') == []
    assert len(h.remote.copies('/media/tv/S/new.mkv')) == 1


def test_newly_excluded_files_are_forgotten_not_deleted_or_blocking(harness):
    """新加入排除规则的文件只被遗忘，不会被删除。"""
    h = harness(**deleting())
    for i in range(4):
        h.write('S/e%d.jpg' % i, b'img')
    h.write('S/gone.nfo', b'x')
    h.write('S/keep.nfo', b'stays')
    h.settle()
    h.run()
    raw = {'enabled': True, 'mode': 'sync', 'copy_enabled': True, 'upload_enabled': True, 'settle_seconds': 1,
           'api_qps': 20, 'compare_mode': 'detail', 'excludes': '*.jpg',
           'mappings': 'tv#%s#/media/tv#%s' % (h.a, h.c)}
    raw.update(deleting())
    h.config = Config.parse(raw)
    h.engine = SyncEngine(h.config, h.repo, h.remote, str(h.q.parent / 'data'), h.clock)
    (h.a / 'S' / 'gone.nfo').unlink()
    h.settle(scans=2)
    h.run()
    assert h.engine.status['roots']['tv'] == 'ready'
    assert not (h.c / 'S' / 'gone.nfo').exists()
    assert len(list((h.c / 'S').glob('*.jpg'))) == 4      # 被排除的文件保持不动
    assert len(h.remote.copies('/media/tv/S/e0.jpg')) == 1
    assert h.asset('S/e0.jpg') is None


def test_uploads_that_never_appear_stop_after_a_cap(harness):
    """上传后始终找不到的文件达到次数上限后停止上传并标记冲突。"""
    h = harness()
    h.write('S/e1.mkv', b'video')
    h.settle()
    original = h.remote.upload

    def renaming_upload(folder, local, name):
        """模拟 115 改名保存上传的文件。"""
        return original(folder, local, name + '_renamed_by_115')
    h.remote.upload = renaming_upload
    for _ in range(6):
        h.engine.run_b()
        h.clock.advance(h.config.verify_window_minutes * 60 + 1)
        row = h.asset('S/e1.mkv')
        h.repo.record('b', row, b_next=0)
    assert len(ops(h, 'upload')) == 3
    assert h.asset('S/e1.mkv')['b_state'] == 'conflict'


def test_root_missing_at_start_is_bound_when_it_appears(harness, tmp_path):
    """启动时不存在的源根目录出现后会被绑定并可用。"""
    h = harness()
    late = tmp_path / 'late'
    raw = {'enabled': True, 'mode': 'sync', 'copy_enabled': True, 'settle_seconds': 1, 'api_qps': 20,
           'compare_mode': 'detail',
           'mappings': 'mv#%s#/media/mv#%s' % (late, h.c / 'mv')}
    config = Config.parse(raw)
    from app.plugins.localmirror115.persistence import Repository
    repo = Repository(sessionmaker(h.db), config.revision, config.revision_b, config.revision_c)
    engine = SyncEngine(config, repo, h.remote, str(tmp_path / 'data'), h.clock)
    index_first(engine)
    assert engine.status['roots']['mv'] == 'unavailable'
    late.mkdir()
    (late / 'a.nfo').write_bytes(b'x')
    engine.scan_once()
    assert engine.status['roots']['mv'] == 'ready'


def test_symlinked_source_is_rejected_at_config_time(tmp_path):
    """源目录为符号链接时在解析配置时拒绝。"""
    real = tmp_path / 'real'
    real.mkdir()
    (tmp_path / 'link').symlink_to(real)
    with pytest.raises(ValueError):
        Config.parse({'mappings': 'tv#%s#/media/tv#%s' % (tmp_path / 'link', tmp_path / 'c')})


def test_config_bounds():
    """配置项被限制在合理范围内，并包含常见视频后缀。"""
    base = {'mappings': 'tv#/m/tv#/media/tv#/c/tv'}
    assert Config.parse(dict(base, scan_seconds=1)).scan_seconds == 60          # 旧配置中的过小值被提升到下限
    assert Config.parse(dict(base, verify_window_minutes=1)).verify_window_minutes == 5
    config = Config.parse(base)
    assert config.is_video('S/a.webm') and config.is_video('S/a.VOB') and config.is_video('S/a.mts')


def test_excludes_are_case_insensitive():
    """排除规则不区分大小写。"""
    config = Config.parse({'mappings': 'tv#/m/tv#/media/tv#/c/tv'})
    assert config.excluded('S/e1.mkv.!qB')
    assert config.excluded('S/THUMBS.DB')


def test_unstable_inode_numbers_do_not_block_settling(harness, monkeypatch):
    """inode 等元数据不稳定时不妨碍文件稳定。"""
    h = harness()
    h.write('S/e1.nfo', b'meta')
    real = h.engine.fs.observe
    counter = iter(range(10**6))

    def flaky(root, relative, checked=False):
        """每次观察返回不同的 inode 与 ctime。"""
        o = real(root, relative, checked)
        if o.status == 'present':
            fp = dict(o.fingerprint, ino=next(counter), ctime_ns=next(counter))
            return Observation('present', fp)
        return o
    monkeypatch.setattr(h.engine.fs, 'observe', flaky)
    h.settle()
    assert h.asset('S/e1.nfo') is not None


def test_extra_copies_are_not_removed_when_upload_is_disabled(harness):
    """关闭上传时不删除 115 上的多余副本。"""
    h = harness()
    h.write('S/e1.mkv', b'video')
    h.settle()
    h.remote.lag = True
    h.engine.run_b()
    h.remote.reveal()
    h.remote.put('/media/tv/S/e1.mkv', 99)
    raw = {'enabled': True, 'mode': 'sync', 'copy_enabled': True, 'upload_enabled': False, 'settle_seconds': 1,
           'api_qps': 20, 'compare_mode': 'detail', 'mappings': 'tv#%s#/media/tv#%s' % (h.a, h.c)}
    engine = SyncEngine(Config.parse(raw), h.repo, h.remote, str(h.q.parent / 'data'), h.clock)
    index_first(engine)
    h.clock.advance(120)
    engine.run_b()
    assert not ops(h, 'delete')
    assert len(h.remote.copies('/media/tv/S/e1.mkv')) == 2


def test_switching_to_sync_wakes_planned_rows(harness):
    """从演练模式切换到同步模式后，计划中的记录被唤醒并执行。"""
    h = harness(mode='dry_run')
    h.write('S/e1.nfo', b'meta')
    h.settle()
    h.engine.run_c()
    assert h.asset('S/e1.nfo')['c_next'] > h.clock.now
    raw = {'enabled': True, 'mode': 'sync', 'copy_enabled': True, 'settle_seconds': 1, 'api_qps': 20,
           'compare_mode': 'detail',
           'mappings': 'tv#%s#/media/tv#%s' % (h.a, h.c)}
    h.repo.wake_planned()
    engine = SyncEngine(Config.parse(raw), h.repo, h.remote, str(h.q.parent / 'data'), h.clock)
    index_first(engine)
    engine.run_c()
    assert (h.c / 'S' / 'e1.nfo').exists()


def test_source_reverting_during_replace_does_not_lose_the_remote_copy(harness):
    """替换期间源文件回退到旧版本时，115 上仍保留一份正确副本。"""
    h = harness()
    h.write('S/e1.nfo', b'v1', mtime=1700000000)
    h.settle()
    h.engine.run_b()
    h.write('S/e1.nfo', b'v2-longer', mtime=1700000500)
    h.settle()
    original = h.remote.delete

    def delete_then_revert(remote_path, fileid):
        """删除 115 旧副本后把源文件改回 115 原先保存的版本。"""
        result = original(remote_path, fileid)
        h.write('S/e1.nfo', b'v1', mtime=1700000000)  # 改回 115 原先保存的版本
        return result
    h.remote.delete = delete_then_revert
    h.engine.run_b()
    h.remote.delete = original
    h.settle()
    h.engine.run_b()
    copies = h.remote.copies('/media/tv/S/e1.nfo')
    assert len(copies) == 1 and copies[0]['size'] == 2


def test_duplicates_from_repeated_lagged_uploads_are_collapsed(harness):
    """索引延迟导致的重复上传副本最终合并为一份。"""
    h = harness()
    h.write('S/e1.mkv', b'video')
    h.settle()
    h.remote.lag = True
    original = h.remote.upload

    def no_id(folder, local, name):
        """上传后不返回 fileid。"""
        result = original(folder, local, name)
        result['fileid'] = None
        return result
    h.remote.upload = no_id
    for _ in range(4):
        h.engine.run_b()
        h.clock.advance(h.config.verify_window_minutes * 60 + 1)
    assert len(ops(h, 'upload')) == 3
    assert h.asset('S/e1.mkv')['b_state'] == 'conflict'
    h.remote.reveal()                       # 索引终于追上：出现 3 个相同副本
    h.repo.record('b', h.asset('S/e1.mkv'), b_next=0)
    h.engine.run_b()
    h.clock.advance(h.config.verify_window_minutes * 60 + 1)
    h.engine.run_b()
    assert len(h.remote.copies('/media/tv/S/e1.mkv')) == 1
    assert h.asset('S/e1.mkv')['b_state'] == 'synced'


def test_earlier_upload_surfacing_late_is_removed(harness):
    """较早一次上传迟到出现时被删除，只保留一份。"""
    h = harness()
    h.write('S/e1.mkv', b'video')
    h.settle()
    h.remote.lag = True
    h.engine.run_b()                        # 第一次上传暂不可见
    h.clock.advance(h.config.verify_window_minutes * 60 + 1)
    hidden = list(h.remote.hidden)
    h.remote.hidden = []
    h.remote.lag = False
    h.engine.run_b()                        # 第二次上传可见
    assert h.asset('S/e1.mkv')['b_note'] == 'awaiting_late_copies'
    h.remote.hidden = hidden
    h.remote.reveal()                       # 第一次上传迟到出现
    h.clock.advance(h.config.verify_window_minutes * 60 + 1)
    h.engine.run_b()
    assert len(h.remote.copies('/media/tv/S/e1.mkv')) == 1
    assert h.asset('S/e1.mkv')['b_state'] == 'synced'


def test_empty_placeholder_root_is_rebound_after_mount(harness, tmp_path):
    """启动时为空的挂载点在挂载后重新绑定为新目录。"""
    h = harness()
    root = tmp_path / 'mnt'
    root.mkdir()                            # 启动时是空的挂载点
    raw = {'enabled': True, 'mode': 'sync', 'copy_enabled': True, 'settle_seconds': 1, 'api_qps': 20,
           'compare_mode': 'detail',
           'mappings': 'mv#%s#/media/mv#%s' % (root, tmp_path / 'cmv')}
    config = Config.parse(raw)
    from app.plugins.localmirror115.persistence import Repository
    repo = Repository(sessionmaker(h.db), config.revision, config.revision_b, config.revision_c)
    engine = SyncEngine(config, repo, h.remote, str(tmp_path / 'data'), h.clock)
    index_first(engine)
    disk = tmp_path / 'disk'
    disk.mkdir()
    (disk / 'a.nfo').write_bytes(b'x')
    os.rmdir(root)
    os.rename(disk, root)                   # “挂载”：该路径下换成了另一个目录
    engine.scan_once()
    assert engine.status['roots']['mv'] == 'ready'


def test_populated_root_identity_change_still_needs_reinit(harness):
    """已有文件的根目录被替换后仍需重新初始化，标记为不安全。"""
    h = harness()
    h.write('S/e1.nfo', b'x')
    h.settle()
    moved = h.a.with_name('a-old')
    os.rename(h.a, moved)
    os.rename(moved, h.a.with_name('a-tmp'))
    h.a.mkdir()
    (h.a / 'S').mkdir()
    h.engine.scan_once()
    assert h.engine.status['roots']['tv'] == 'scan_incomplete_or_unsafe'


def test_source_root_lost_during_a_batch_voids_the_whole_batch(harness):
    """一批观测进行中源根掉线（结束时确认失败）：整批视为未知并延后，绝不当成删除。"""
    h = harness(**deleting())
    for i in range(3):
        h.write('S/e%d.nfo' % i, b'x')
    h.settle()
    h.run()
    for i in range(3):
        (h.a / 'S' / ('e%d.nfo' % i)).unlink()
        h.engine.hint(str(h.a / 'S' / ('e%d.nfo' % i)))
    checks = iter([True, False])  # 开始时健康，结束时已掉线
    h.engine.fs.healthy = lambda root: next(checks, False)
    h.clock.advance(2)
    h.engine.tick()
    assert all(h.asset('S/e%d.nfo' % i)['a_state'] == 'present' for i in range(3))


def test_failed_full_scan_is_retried_automatically(harness):
    """全量扫描未完成时安排自动重扫，不必等重启或手动点扫描。"""
    from app.plugins.localmirror115.engine import SCAN_RETRY
    h = harness()
    h.write('S/e.nfo', b'x')
    h.engine.fs.scan = lambda *args, **kwargs: (_ for _ in ()).throw(OSError('scan_file_limit_exceeded'))
    h.engine.scan_once()
    assert h.engine.status['roots']['tv'] == 'scan_incomplete_or_unsafe'
    assert h.engine.scan_retry['tv'] == h.clock() + SCAN_RETRY


def test_holds_left_by_the_old_version_are_cleared_at_startup(harness, caplog):
    """旧版本暂扣的删除在启动时清除标记并记一条日志，之后按正常流程删除。"""
    import logging
    from sqlalchemy import update
    from app.plugins.localmirror115.persistence import Asset
    h = harness(**deleting())
    h.write('S/keep.nfo', b'k')
    h.write('S/e.nfo', b'x')
    h.settle()
    h.run()
    (h.a / 'S' / 'e.nfo').unlink()
    h.settle()
    with h.repo.transaction() as s:
        s.execute(update(Asset).values(held=1))
    caplog.set_level(logging.INFO, logger='localmirror115')
    h.engine.prepare()
    assert '旧版本暂扣的 1 个删除' in caplog.text
    assert h.repo.clear_holds() == 0
