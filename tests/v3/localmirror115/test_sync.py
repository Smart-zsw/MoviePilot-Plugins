"""基本同步测试：配置解析、演练模式、本地副本复制、上传 115 与同步删除。"""
import os
import time

import pytest

from conftest import ops
from app.plugins.localmirror115.config import Config, parse_mappings
from app.plugins.localmirror115.filesystem import LocalOutput, OutputError


# ---- 配置 ----


def test_mapping_lines_and_switch_defaults():
    """映射行解析跳过注释并校验字段数，各开关默认值安全。"""
    rows = parse_mappings('tv#/m/tv#/media/tv#/c/tv\n// comment\nmovies#/m/mv#/media/mv#/c/mv')
    assert [r['id'] for r in rows] == ['tv', 'movies']
    with pytest.raises(ValueError):
        parse_mappings('tv#/m/tv#/media/tv')
    config = Config.parse({'mappings': 'tv#/m/tv#/media/tv#/c/tv'})
    assert config.mode == 'dry_run' and not config.writes
    assert config.watch_enabled and config.transfer_event_enabled
    assert not config.scan_enabled and config.scan_cron == '0 4 * * 3,6'  # 兜底定时扫描需手动开启
    assert config.delete_c_enabled and not config.delete_b_enabled  # 两者仍受 delete_enabled 总开关控制
    assert not (config.copy_enabled or config.upload_enabled or config.delete_enabled)


def test_scan_cron_needs_five_fields():
    """定时扫描 cron 须为 5 段，多余空白会被规整。"""
    base = {'mappings': 'tv#/m/tv#/media/tv#/c/tv'}
    assert Config.parse(dict(base, scan_cron='  30  3 * * 0 ')).scan_cron == '30 3 * * 0'
    with pytest.raises(ValueError):
        Config.parse(dict(base, scan_cron='every night'))


def test_excludes_strm_and_junk_and_custom_list_keeps_temp_suffix():
    """默认排除 strm 与杂项文件；自定义排除列表替换默认值但仍排除临时文件。"""
    config = Config.parse({'mappings': 'tv#/m/tv#/media/tv#/c/tv', 'excludes': 'Thumbs.db'})
    assert config.excluded('show/a.strm')
    assert config.excluded('show/.x.nfo.lm115tmp')
    assert not config.excluded('show/.DS_Store')  # 自定义列表替换了默认值
    assert Config.parse({'mappings': 'tv#/m/tv#/media/tv#/c/tv'}).excluded('show/@eaDir/x.jpg')


# ---- 演练模式 ----


def test_dry_run_plans_but_writes_nothing(harness):
    """演练模式只生成计划，不写入任何文件或 115。"""
    h = harness(mode='dry_run')
    h.write('S/e1.mkv', b'video')
    h.write('S/e1.nfo', b'<x/>')
    h.settle()
    h.run()
    assert list(h.c.iterdir()) == []
    assert not ops(h, 'upload') and not ops(h, 'delete') and not ops(h, 'mkdir')
    assert h.asset('S/e1.nfo')['c_state'] == 'planned'
    assert h.asset('S/e1.mkv')['b_note'] == 'would_upload'


# ---- 本地副本（C）----


def test_copy_meta_only_preserves_bytes_and_mtime(harness):
    """只复制元数据文件，保留内容与修改时间，不碰视频、杂项与已有 strm。"""
    h = harness()
    src = h.write('S/e1.nfo', b'<episode/>', mtime=1700000000)
    h.write('S/e1.mkv', b'video-bytes')
    h.write('S/.DS_Store', b'junk')
    (h.c / 'S').mkdir()
    (h.c / 'S' / 'e1.strm').write_text('http://helper/link')
    h.settle()
    h.engine.run_c()
    out = h.c / 'S' / 'e1.nfo'
    assert out.read_bytes() == b'<episode/>'
    assert out.stat().st_mtime_ns == src.stat().st_mtime_ns
    assert not (h.c / 'S' / 'e1.mkv').exists() and not (h.c / 'S' / '.DS_Store').exists()
    assert (h.c / 'S' / 'e1.strm').read_text() == 'http://helper/link'
    assert h.asset('S/e1.nfo')['c_state'] == 'synced'
    assert not list(h.c.rglob('*.lm115tmp'))


def test_copy_adopts_identical_and_replaces_changed(harness):
    """本地副本内容相同时认领、不同时替换，源文件更新后副本随之更新。"""
    h = harness()
    h.write('S/a.nfo', b'same')
    h.write('S/b.nfo', b'new')
    (h.c / 'S').mkdir()
    (h.c / 'S' / 'a.nfo').write_bytes(b'same')
    (h.c / 'S' / 'b.nfo').write_bytes(b'old-content')
    h.settle()
    h.engine.run_c()
    assert h.asset('S/a.nfo')['c_note'] == 'adopted_identical'
    assert (h.c / 'S' / 'b.nfo').read_bytes() == b'new'
    # 源文件之后被改写：副本随之更新
    h.write('S/a.nfo', b'rescraped', mtime=time.time() + 5)
    h.settle()
    h.engine.run_c()
    assert (h.c / 'S' / 'a.nfo').read_bytes() == b'rescraped'


def test_copy_switch_off_only_plans(harness):
    """关闭复制时只生成计划。"""
    h = harness(copy_enabled=False)
    h.write('S/a.nfo', b'x')
    h.settle()
    h.engine.run_c()
    assert not (h.c / 'S').exists()
    assert h.asset('S/a.nfo')['c_note'] == 'would_copy'


def test_local_output_refuses_strm_video_and_symlinks(tmp_path):
    """本地输出拒绝 strm、视频、越界路径与经由符号链接的写入。"""
    root = tmp_path / 'c'
    root.mkdir()
    out = LocalOutput(root, lambda r: r.endswith('.mkv'))
    src = tmp_path / 'src.nfo'
    src.write_bytes(b'x')
    for bad in ('a.strm', 'a.mkv', '../x.nfo', '/abs.nfo'):
        with pytest.raises(OutputError):
            out.copy(bad, src)
    (tmp_path / 'elsewhere').mkdir()
    (root / 'link').symlink_to(tmp_path / 'elsewhere')
    with pytest.raises(OutputError):
        out.copy('link/a.nfo', src)
    assert list((tmp_path / 'elsewhere').iterdir()) == []


# ---- 上传 115（B）----


def test_upload_new_files_and_verify_single_copy(harness):
    """上传新文件并确认 115 上只有一份。"""
    h = harness()
    h.write('S/e1.mkv', b'video')
    h.write('S/e1.nfo', b'meta')
    h.settle()
    h.engine.run_b()
    assert len(h.remote.copies('/media/tv/S/e1.mkv')) == 1
    assert len(h.remote.copies('/media/tv/S/e1.nfo')) == 1
    row = h.asset('S/e1.mkv')
    assert row['b_state'] == 'synced' and row['b_note'] == 'verified' and row['b_fileid']


def test_adopts_existing_remote_copy_without_upload(harness):
    """115 上已有相同文件时直接认领，不上传。"""
    h = harness()
    h.write('S/e1.mkv', b'video', mtime=1700000000)
    h.remote.put('/media/tv/S/e1.mkv', 5, mtime=1700000100)
    h.settle()
    h.engine.run_b()
    assert not ops(h, 'upload')
    assert h.asset('S/e1.mkv')['b_note'] == 'adopted'


def test_adopt_switch_off_reuploads(harness):
    """关闭“接管已有文件”时重新上传并替换已有文件。"""
    h = harness(adopt_existing=False)
    h.write('S/e1.nfo', b'meta', mtime=1700000000)
    old = h.remote.put('/media/tv/S/e1.nfo', 4, mtime=1700000100)
    h.settle()
    h.engine.run_b()
    copies = h.remote.copies('/media/tv/S/e1.nfo')
    assert len(copies) == 1 and copies[0]['fileid'] != old['fileid']


def test_remote_copy_older_than_local_is_replaced(harness):
    """115 上比本地旧的副本会被替换。"""
    h = harness()
    h.write('S/e1.nfo', b'meta', mtime=time.time())
    old = h.remote.put('/media/tv/S/e1.nfo', 4, mtime=time.time() - 86400)
    h.settle()
    h.engine.run_b()
    copies = h.remote.copies('/media/tv/S/e1.nfo')
    assert len(copies) == 1 and copies[0]['fileid'] != old['fileid']


def test_duplicate_remote_copies_collapse_to_one(harness):
    """115 上的重复副本被合并为一份。"""
    h = harness()
    h.write('S/e1.nfo', b'meta')
    h.remote.put('/media/tv/S/e1.nfo', 4)
    h.remote.put('/media/tv/S/e1.nfo', 4)
    h.settle()
    h.engine.run_b()
    assert len(h.remote.copies('/media/tv/S/e1.nfo')) == 1
    assert len(ops(h, 'delete')) == 2


def test_local_change_replaces_remote_copy(harness):
    """本地文件变化后替换 115 上的副本。"""
    h = harness()
    h.write('S/e1.nfo', b'v1')
    h.settle()
    h.engine.run_b()
    first = h.remote.copies('/media/tv/S/e1.nfo')[0]['fileid']
    h.write('S/e1.nfo', b'version-2', mtime=time.time() + 10)
    h.settle()
    h.engine.run_b()
    copies = h.remote.copies('/media/tv/S/e1.nfo')
    assert len(copies) == 1 and copies[0]['fileid'] != first and copies[0]['size'] == 9


def test_index_lag_waits_for_verification_without_reupload(harness):
    """115 索引延迟时等待确认，不重复上传。"""
    h = harness()
    h.write('S/e1.mkv', b'video')
    h.settle()
    h.remote.lag = True
    h.engine.run_b()
    assert h.asset('S/e1.mkv')['b_state'] == 'verify'
    h.remote.reveal()
    h.clock.advance(120)
    h.engine.run_b()
    assert h.asset('S/e1.mkv')['b_note'] == 'verified'
    assert len(ops(h, 'upload')) == 1


def test_upload_failure_backs_off(harness):
    """上传失败后退避，退避期内不重试。"""
    h = harness()
    h.write('S/e1.mkv', b'video')
    h.settle()
    h.remote.fail_upload = True
    h.engine.run_b()
    row = h.asset('S/e1.mkv')
    assert row['b_state'] == 'error' and row['b_next'] > h.clock.now
    h.engine.run_b()
    assert len(ops(h, 'upload')) == 1  # 退避期内不重试


def test_change_during_upload_is_not_recorded(harness):
    """上传期间源文件发生变化时不记录为已同步的指纹。"""
    h = harness()
    path = h.write('S/e1.nfo', b'v1')
    h.settle()
    original = h.remote.upload

    def upload_and_touch(folder, local, name):
        """上传后立即修改源文件。"""
        result = original(folder, local, name)
        path.write_bytes(b'changed-during-upload')
        return result
    h.remote.upload = upload_and_touch
    h.engine.run_b()
    assert h.asset('S/e1.nfo')['b_fp'] is None


def test_upload_switch_off_only_plans(harness):
    """关闭上传时只生成计划。"""
    h = harness(upload_enabled=False)
    h.write('S/e1.mkv', b'video')
    h.settle()
    h.engine.run_b()
    assert not ops(h, 'upload')
    assert h.asset('S/e1.mkv')['b_note'] == 'would_upload'


# ---- 删除 ----


def synced(h):
    """同步一组文件后删除其中两个源文件，保留一个文件避免目录树为空。"""
    h.write('S/keep.nfo', b'stays')  # 目录树不能变为空
    h.write('S/e1.mkv', b'video')
    h.write('S/e1.nfo', b'meta')
    h.settle()
    h.run()
    (h.a / 'S' / 'e1.mkv').unlink()
    (h.a / 'S' / 'e1.nfo').unlink()


def test_delete_disabled_only_reports(harness):
    """关闭删除时只报告，不删除任何副本。"""
    h = harness(delete_delay_hours=0)
    synced(h)
    h.settle(scans=3)
    h.run()
    assert h.asset('S/e1.nfo')['c_note'] == 'delete_disabled'
    assert (h.c / 'S' / 'e1.nfo').exists()
    assert len(h.remote.copies('/media/tv/S/e1.mkv')) == 1


def test_deletion_is_real_time_by_default_and_delay_is_optional(harness):
    """默认实时删除且不保留隔离副本，延迟删除为可选项。"""
    h = harness(delete_enabled=True, delete_b_enabled=True, delete_c_enabled=True)
    assert h.config.delete_delay_hours == 0
    synced(h)
    h.settle()
    h.run()
    assert h.remote.copies('/media/tv/S/e1.mkv') == []
    assert not (h.c / 'S' / 'e1.nfo').exists()  # 直接删除，不另行保留
    assert not h.q.exists()
    assert h.asset('S/e1.mkv') is None and h.asset('S/e1.nfo') is None


def test_optional_delete_delay(harness):
    """设置删除延迟后，到期才删除 115 副本。"""
    h = harness(delete_enabled=True, delete_b_enabled=True, delete_c_enabled=True, delete_delay_hours=1)
    synced(h)
    h.settle()
    h.run()
    assert h.asset('S/e1.mkv')['b_note'] == 'delete_delay'
    h.clock.advance(3600)
    h.settle()
    h.run()
    assert h.remote.copies('/media/tv/S/e1.mkv') == []


def test_per_target_delete_switches(harness):
    """115 与本地副本的删除开关分别生效。"""
    h = harness(delete_enabled=True, delete_b_enabled=False, delete_c_enabled=True, delete_delay_hours=0)
    synced(h)
    h.settle(scans=2)
    h.run()
    assert not (h.c / 'S' / 'e1.nfo').exists()
    assert len(h.remote.copies('/media/tv/S/e1.mkv')) == 1
    assert h.asset('S/e1.mkv')['b_note'] == 'delete_b_disabled'


def test_c_file_changed_by_others_is_kept(harness):
    """被他人修改过的本地副本文件予以保留。"""
    h = harness(delete_enabled=True, delete_c_enabled=True, delete_delay_hours=0)
    synced(h)
    (h.c / 'S' / 'e1.nfo').write_bytes(b'edited elsewhere')
    h.settle(scans=2)
    h.engine.run_c()
    assert (h.c / 'S' / 'e1.nfo').read_bytes() == b'edited elsewhere'
    assert h.asset('S/e1.nfo')['c_state'] == 'kept'


def test_source_root_loss_blocks_deletion(harness):
    """源根目录丢失或被替换时阻止删除。"""
    h = harness(delete_enabled=True, delete_b_enabled=True, delete_c_enabled=True, delete_delay_hours=0)
    synced(h)
    h.settle(scans=2)
    moved = h.a.with_name('a-offline')
    os.rename(h.a, moved)
    h.a.mkdir()  # 同一路径换成了另一个目录：如同在空目录上重新挂载
    h.run()
    assert len(h.remote.copies('/media/tv/S/e1.mkv')) == 1
    assert (h.c / 'S' / 'e1.nfo').exists()


def test_empty_tree_is_not_a_deletion_baseline(harness):
    """目录树整体为空时不作为删除基线。"""
    h = harness(delete_enabled=True, delete_b_enabled=True, delete_c_enabled=True, delete_delay_hours=0)
    synced(h)
    (h.a / 'S' / 'keep.nfo').unlink()
    h.settle(scans=3)  # 整个目录树此时为空
    assert h.engine.status['roots']['tv'] == 'scan_incomplete_or_unsafe'
    h.run()
    assert (h.c / 'S' / 'e1.nfo').exists()


def test_never_synced_missing_rows_are_dropped(harness):
    """从未同步过就消失的文件记录被直接移除。"""
    h = harness(mode='dry_run')
    h.write('S/keep.nfo', b'x')
    h.write('S/e1.nfo', b'meta')
    h.settle()
    (h.a / 'S' / 'e1.nfo').unlink()
    h.settle()
    h.clock.advance(10000)
    h.run()
    assert h.asset('S/e1.nfo') is None


def test_hint_from_transfer_event_settles_without_scan(harness):
    """整理完成事件提示的文件无需扫描即可稳定并上传。"""
    h = harness()
    path = h.write('S/e2.mkv', b'video')
    assert h.engine.hint(str(path), 'moviepilot')
    h.clock.advance(2)
    h.engine.tick()
    h.engine.run_b()
    assert h.asset('S/e2.mkv')['b_state'] == 'synced'


def test_chinese_mapping_ids_work_end_to_end(harness, tmp_path):
    """中文映射名称可完整使用，含空格或特殊字符的名称被拒绝。"""
    config = Config.parse({'mappings': '电视剧#/m/tv#/media/tv#/c/tv\n电影#/m/mv#/media/mv#/c/mv'})
    assert [m.key for m in config.mappings] == ['电视剧', '电影']
    for bad in ('电 视剧#/m/a#/media/a#/c/a', 'a!b#/m/a#/media/a#/c/a'):
        with pytest.raises(ValueError):
            Config.parse({'mappings': bad})
    h = harness(mappings='电视剧#%s#/media/tv#%s' % (tmp_path / '0' / 'a', tmp_path / '0' / 'c'))
    h.write('S/e1.nfo', b'meta')
    h.settle()
    h.run()
    assert (h.c / 'S' / 'e1.nfo').exists()
    assert len(h.remote.copies('/media/tv/S/e1.nfo')) == 1
    assert h.engine.index._file(h.config.mappings[0]).name == '电视剧.json.gz'


def test_deleting_many_files_at_once_is_real_time(harness):
    """同时删除大量文件也照常同步删除（115 上进入回收站，可找回）。"""
    count = 520
    h = harness(delete_enabled=True, delete_b_enabled=True, delete_c_enabled=True)
    h.write('keep.nfo', b'stays')
    for i in range(count):
        h.write('Show/S01/e%03d.nfo' % i, b'm')
    h.settle()
    for _ in range(count // 100 + 2):
        h.run()
    assert len(list((h.c / 'Show' / 'S01').glob('*.nfo'))) == count
    for i in range(count):
        (h.a / 'Show' / 'S01' / ('e%03d.nfo' % i)).unlink()
    h.settle()
    for _ in range(count // 100 + 2):
        h.run()
    assert not list((h.c / 'Show').rglob('*.nfo'))


def test_monitoring_mode_and_older_switches():
    """监控模式取值正确，旧版开关保存的配置保持原意。"""
    base = {'mappings': 'tv#/m/tv#/media/tv#/c/tv'}
    fast = Config.parse(base)
    assert fast.watch_mode == 'fast' and fast.watch_enabled and not fast.force_polling
    compat = Config.parse(dict(base, watch_mode='compatibility'))
    assert compat.watch_enabled and compat.force_polling
    assert not Config.parse(dict(base, watch_mode='off')).watch_enabled
    # 新增该模式之前保存的设置保持原意
    assert Config.parse(dict(base, force_polling=True)).watch_mode == 'compatibility'
    assert Config.parse(dict(base, watch_enabled=False)).watch_mode == 'off'
    assert Config.parse(dict(base, watch_mode='')).watch_mode == 'fast'
    with pytest.raises(ValueError):
        Config.parse(dict(base, watch_mode='turbo'))
