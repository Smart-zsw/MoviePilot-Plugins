"""各种安装环境的适配：重新挂载、网络盘、读不了的目录、挂死的挂载、时钟跳变、软链接与不区分大小写的盘。"""
import logging
import os
import threading

import pytest

from conftest import Clock, deleting


def _real_dev(h):
    """测试目录所在盘的真实设备号。"""
    return os.stat(h.a).st_dev


def _remount(h, monkeypatch):
    """模拟根目录重新挂载：之后读到的根目录身份与绑定时不同。"""
    real = h.engine.fs.identity
    monkeypatch.setattr(h.engine.fs, 'identity', lambda root: (real(root)[0], -7))


def _fake_watch(monkeypatch, fail_native=False):
    """替换 watchfiles.watch：记录每次是否用轮询；fail_native 时性能模式报 inotify 上限错误。返回调用记录。"""
    calls = []

    def watch(*args, force_polling=None, **kwargs):
        """记录调用后随即结束（或按设置报错）。"""
        calls.append(force_polling)
        if fail_native and not force_polling:
            raise OSError('OS file watch limit reached (inotify)')
        return iter(())
    monkeypatch.setattr('watchfiles.watch', watch)
    return calls


def _link(h, relative):
    """在映射根外放一个文件，并在 relative 处放指向它的软链接；返回目标文件路径。"""
    target = h.a.parent / 'outside.nfo'
    target.write_bytes(b'linked')
    (h.a / relative).parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, h.a / relative)
    return target


def test_remount_renumbers_devices_so_earlier_deletions_still_sync(harness):
    """重新挂载后整盘设备号变了：全量扫描把同一设备号下的全部记录（含已删除的）改到新号，已删除的文件照常同步删除。"""
    h = harness(**deleting())
    for name in ('a', 'b', 'c'):
        h.write('S/%s.nfo' % name, b'meta')
    h.settle()
    h.run()
    h.repo.refresh_devices('tv', [('S/%s.nfo' % name, 999) for name in 'abc'])  # 记录的是挂载前的设备号
    (h.a / 'S' / 'c.nfo').unlink()
    h.settle(scans=2)
    h.run()
    assert h.asset('S/c.nfo') is None
    assert not (h.c / 'S' / 'c.nfo').exists() and h.remote.copies('/media/tv/S/c.nfo') == []
    assert h.asset('S/a.nfo')['a_dev'] == _real_dev(h)
    assert h.repo.counts()['postponed'] == 0


def test_subtree_scan_leaves_devices_for_the_full_scan(harness):
    """重新挂载后先发生的目录扫描不改设备号（它只看到一部分文件），随后的全量扫描仍能认出整盘重新编号。"""
    h = harness(**deleting())
    for name in ('a', 'b', 'c'):
        h.write('S/%s.nfo' % name, b'meta')
    h.write('T/d.nfo', b'meta')
    h.settle()
    h.run()
    h.repo.refresh_devices('tv', [(name, 999) for name in ('S/a.nfo', 'S/b.nfo', 'S/c.nfo', 'T/d.nfo')])
    h.engine.scan_subtree(h.engine.mapping_by_id['tv'], 'S')
    assert h.asset('S/a.nfo')['a_dev'] == 999
    (h.a / 'S' / 'c.nfo').unlink()
    h.settle(scans=2)
    h.run()
    assert h.asset('S/c.nfo') is None and h.asset('T/d.nfo')['a_dev'] == _real_dev(h)


def test_offline_disk_is_not_mistaken_for_a_renumbering(harness):
    """某块盘掉线：它上面的文件全都不在清单里，不会被当成重新编号，消失的文件暂缓删除；其余盘上的文件只刷新设备号。"""
    h = harness(**deleting())
    h.write('keep.nfo', b'stays')
    h.write('Disk2/e1.nfo', b'meta')
    h.write('Disk2/e2.nfo', b'meta')
    h.settle()
    h.run()
    h.repo.refresh_devices('tv', [('keep.nfo', 111), ('Disk2/e1.nfo', 222), ('Disk2/e2.nfo', 222)])
    for name in ('e1.nfo', 'e2.nfo'):
        (h.a / 'Disk2' / name).unlink()
    h.settle(scans=2)
    h.run()
    assert h.asset('Disk2/e1.nfo')['a_dev'] == 222 and (h.c / 'Disk2' / 'e1.nfo').exists()
    assert h.asset('keep.nfo')['a_dev'] == _real_dev(h)
    assert h.repo.counts()['postponed'] == 2


def test_single_file_moved_to_another_pool_disk_only_updates_that_file(harness):
    """存储池里个别文件换了盘：其余文件仍报告旧设备号，不算整盘重新编号，只更新这个文件的记录。"""
    h = harness(**deleting())
    h.write('S/a.nfo', b'meta')
    h.write('S/b.nfo', b'meta')
    h.settle()
    h.run()
    real = _real_dev(h)
    h.repo.refresh_devices('tv', [('S/a.nfo', 333)])
    h.settle()
    assert h.asset('S/a.nfo')['a_dev'] == real and h.asset('S/b.nfo')['a_dev'] == real


@pytest.mark.skipif(os.name != 'posix' or os.geteuid() == 0, reason='需要非 root 的 POSIX 权限')
def test_unreadable_directory_is_neither_synced_nor_deleted(harness, caplog):
    """读不了的子目录（权限不足）：其中的已知文件既不算消失也不删除，其他目录照常同步，并提醒。"""
    h = harness(**deleting())
    h.write('Locked/e1.nfo', b'meta')
    h.write('Open/e2.nfo', b'meta')
    h.settle()
    h.run()
    h.write('Open/e3.nfo', b'new')
    caplog.set_level(logging.WARNING, logger='localmirror115')
    os.chmod(h.a / 'Locked', 0)
    try:
        h.settle(scans=2)
        h.run()
    finally:
        os.chmod(h.a / 'Locked', 0o755)
    assert h.asset('Locked/e1.nfo')['a_state'] == 'present' and (h.c / 'Locked' / 'e1.nfo').exists()
    assert (h.c / 'Open' / 'e3.nfo').read_bytes() == b'new'
    assert '无法读取' in caplog.text and 'Locked' in caplog.text


def test_remounted_root_is_rebound_and_checked_before_deleting(harness, monkeypatch):
    """根目录身份变了（重新挂载）：扫描时接受新身份，核对已知文件大半仍在才恢复为可删除。"""
    h = harness(**deleting())
    h.write('S/e1.nfo', b'meta')
    h.write('S/e2.nfo', b'meta')
    h.settle()
    h.run()
    fs = h.engine.fs
    _remount(h, monkeypatch)
    assert not fs.healthy(h.a)
    h.engine.scan_once()
    assert fs.healthy(h.a)
    assert h.engine.status['roots']['tv'] == 'ready' and 'tv' not in h.engine.remounted


def test_remount_with_different_content_keeps_deletions_paused(harness, monkeypatch):
    """重新挂载后已知文件大半不见了（挂上了别的盘或空盘）：不作为删除依据，保持暂停删除。"""
    h = harness(**deleting())
    for name in ('a', 'b', 'c'):
        h.write('S/%s.nfo' % name, b'meta')
    h.settle()
    h.run()
    for name in ('a', 'b'):
        (h.a / 'S' / ('%s.nfo' % name)).unlink()
    _remount(h, monkeypatch)
    h.engine.scan_once()
    assert h.engine.status['roots']['tv'] == 'scan_incomplete_or_unsafe'
    assert h.engine.status['root_detail']['tv'] == '重新挂载后内容与记录不符，暂停删除'
    h.clock.advance(120)
    h.engine.tick()
    h.run()
    assert (h.c / 'S' / 'a.nfo').exists() and h.remote.copies('/media/tv/S/a.nfo')


def test_heartbeat_accepts_a_remount_and_pauses_deletes_until_checked(harness, monkeypatch):
    """心跳发现根目录身份变了：立即接受新身份、暂停删除并安排重新扫描；扫描核对通过后恢复。"""
    h = harness(**deleting())
    h.write('S/e1.nfo', b'meta')
    h.settle()
    _remount(h, monkeypatch)
    h.engine._detect_remounts()
    assert h.engine.fs.healthy(h.a) and h.engine.scan_requested.is_set()
    assert h.engine.status['roots']['tv'] == 'awaiting_scan' and 'tv' in h.engine.remounted
    h.engine._detect_remounts()  # 已接受：不再重复安排
    h.engine.scan_once()
    assert h.engine.status['roots']['tv'] == 'ready' and 'tv' not in h.engine.remounted


def test_file_events_check_the_root_once_per_batch(harness, monkeypatch):
    """一批文件事件每个映射只确认一次源根，不逐个文件探测。"""
    h = harness()
    paths = [str(h.write('S/e%d.nfo' % i, b'x')) for i in range(20)]
    calls = []
    real = h.engine.fs.healthy
    monkeypatch.setattr(h.engine.fs, 'healthy', lambda root: calls.append(root) or real(root))
    assert all(h.engine.hint_many(paths, 'filesystem'))
    assert len(calls) == 1 and len(h.repo.due(h.clock() + 60, limit=100)) == 20


def test_device_sampling_uses_spread_out_present_files(harness):
    """按设备号取样：只取仍为 present 的、不含排除项，从多个位置取，结果不超过上限。"""
    h = harness()
    for i in range(30):
        h.write('S/e%02d.nfo' % i, b'x')
    h.settle()
    dev = _real_dev(h)
    sample = h.repo.present_on_device('tv', dev, exclude={'S/e00.nfo'}, limit=8)
    assert 0 < len(sample) <= 8 and 'S/e00.nfo' not in sample and len(set(sample)) == len(sample)
    assert h.repo.present_on_device('tv', 12345) == []


def test_network_mount_uses_polling_and_longer_settle(harness, monkeypatch):
    """本地目录在网络盘上：稳定观察至少 60 秒，监控自动改用轮询；本地 FUSE 存储池（shfs）不算网络盘。"""
    from app.plugins.localmirror115 import engine as engine_module
    from app.plugins.localmirror115.filesystem import is_network_fs
    assert is_network_fs('nfs4') and is_network_fs('cifs') and is_network_fs('fuse.rclone')
    assert not is_network_fs('fuse.shfs') and not is_network_fs('ext4') and not is_network_fs('fuseblk')
    monkeypatch.setattr(engine_module, 'mount_types', lambda paths: {path: 'nfs4' for path in paths})
    h = harness()
    assert h.engine.settle['tv'] == 60
    h.write('S/e1.nfo', b'meta')
    h.engine.hint(str(h.a / 'S' / 'e1.nfo'), 'filesystem')
    assert not h.repo.due(h.clock() + 59) and h.repo.due(h.clock() + 60)
    calls = _fake_watch(monkeypatch)
    h.engine._watch(h.engine.mapping_by_id['tv'])
    assert calls == [True]


def test_watcher_error_falls_back_to_polling_and_rescans(harness, monkeypatch, caplog):
    """性能模式出错（例如 inotify 上限）：提示调整方法、改用轮询继续监控，并补一次扫描。"""
    h = harness()
    calls = _fake_watch(monkeypatch, fail_native=True)
    caplog.set_level(logging.WARNING, logger='localmirror115')
    h.engine._watch(h.engine.mapping_by_id['tv'])
    assert calls == [False, True]
    assert 'max_user_watches' in caplog.text and '改用兼容模式' in caplog.text
    assert h.engine.scan_requested.is_set()


def test_hung_mount_probe_times_out_and_recovers(tmp_path):
    """挂死的挂载：探测超时报错，挂死期间不再为同一路径起新线程；探测返回后恢复正常。"""
    from app.plugins.localmirror115.filesystem import ProbeTimeout, probe
    release = threading.Event()
    with pytest.raises(ProbeTimeout):
        probe(tmp_path, lambda path: release.wait(5), timeout=0.05)
    with pytest.raises(ProbeTimeout):
        probe(tmp_path, lambda path: 'never called', timeout=5)  # 仍挂死：立即报错
    release.set()
    for _ in range(100):
        try:
            assert probe(tmp_path, lambda path: 'ok', timeout=1) == 'ok'
            break
        except ProbeTimeout:
            threading.Event().wait(0.01)
    else:
        pytest.fail('探测返回后仍被当作挂死')


def test_clock_rollback_shifts_schedule_and_pauses_deletes(harness):
    """系统时间回退（NAS 开机后才校时）：已排好的任务整体提前同样的时间，并暂停删除一段时间。"""
    h = harness(**deleting())
    mono = Clock(0)
    h.engine.monotonic = mono
    h.engine.clock_mark = (h.clock(), mono())
    h.write('S/e1.nfo', b'meta')
    h.engine.scan_once()  # 入队，1 秒后到期
    h.clock.advance(-3600)
    h.engine.tick()  # 检测到回退，到期时间同样提前
    h.clock.advance(2)
    mono.advance(2)
    assert h.repo.due(h.clock())
    mapping = h.engine.mapping_by_id['tv']
    assert h.engine._root_blocker('c', mapping) == 'clock_jumped'
    mono.advance(601)
    assert h.engine._root_blocker('c', mapping) != 'clock_jumped'


def test_symlinks_are_skipped_by_default(harness):
    """默认不跟随软链接：指向文件的软链接不同步。"""
    h = harness()
    _link(h, 'S/link.nfo')
    h.settle()
    h.run()
    assert not (h.c / 'S' / 'link.nfo').exists() and not h.remote.copies('/media/tv/S/link.nfo')


def test_followed_symlink_syncs_target_and_dangling_link_is_not_a_delete(harness):
    """开启跟随软链接：按目标文件同步；目标不见了（悬空链接）视为未知，不删除。"""
    h = harness(follow_symlinks=True, **deleting())
    target = _link(h, 'S/link.nfo')
    h.write('S/keep.nfo', b'keep')
    h.settle()
    h.run()
    assert (h.c / 'S' / 'link.nfo').read_bytes() == b'linked'
    assert h.remote.copies('/media/tv/S/link.nfo')
    target.unlink()
    h.settle(scans=2)
    h.run()
    assert (h.c / 'S' / 'link.nfo').exists() and h.remote.copies('/media/tv/S/link.nfo')
    assert h.asset('S/link.nfo')['a_state'] == 'present'


def test_case_only_rename_is_synced(harness):
    """只改了文件名大小写：旧名删除、新名同步；不区分大小写的盘上也不会把新名的副本一起删掉。"""
    h = harness(**deleting())
    h.write('S/keep.nfo', b'keep')
    h.write('S/e1.nfo', b'meta')
    h.settle()
    h.run()
    os.rename(h.a / 'S' / 'e1.nfo', h.a / 'S' / 'E1.nfo')
    h.settle(scans=2)
    h.run()
    assert h.asset('S/e1.nfo') is None and h.asset('S/E1.nfo')['c_state'] == 'synced'
    assert 'E1.nfo' in os.listdir(h.c / 'S') and 'e1.nfo' not in os.listdir(h.c / 'S')
    assert (h.c / 'S' / 'E1.nfo').read_bytes() == b'meta'
    assert h.remote.copies('/media/tv/S/E1.nfo') and not h.remote.copies('/media/tv/S/e1.nfo')


def test_system_folders_are_always_excluded(harness):
    """NAS 回收站、缩略图等系统目录即使自定义了排除规则也始终排除。"""
    h = harness(excludes='*.bak')
    for name in ('@Recycle/x.nfo', '$RECYCLE.BIN/x.nfo', '.@__thumb/x.jpg', '.Trash-1000/x.nfo', 'S/e1.nfo'):
        h.write(name, b'x')
    h.settle()
    h.run()
    assert sorted(os.listdir(h.c)) == ['S']


def test_case_checks_list_each_directory_once_per_batch(harness, monkeypatch):
    """不区分大小写的盘上，一批观测里同一目录的名字核对只列一次目录；批次之外不沿用。"""
    h = harness()
    for i in range(5):
        h.write('S/e%d.nfo' % i, b'x')
    if not (h.a / 'S' / 'E0.NFO').exists():
        pytest.skip('测试目录所在的盘区分大小写')
    calls = []
    real = os.listdir
    monkeypatch.setattr(os, 'listdir', lambda path: calls.append(path) or real(path))
    fs = h.engine.fs
    with fs.batch():
        assert all(fs.observe(h.a, 'S/e%d.nfo' % i, checked=True).status == 'present' for i in range(5))
    assert len(calls) == 1
    fs.observe(h.a, 'S/e0.nfo', checked=True)
    assert len(calls) == 2
