"""插件日志测试：每个步骤都以中文写入插件自己的日志器。"""
import logging
import re
from pathlib import Path


def test_sync_steps_are_logged_in_chinese(harness, caplog):
    """扫描、稳定、本地副本与上传等同步步骤都写入中文日志。"""
    caplog.set_level(logging.DEBUG, logger='localmirror115')
    h = harness(mode='sync', upload_enabled=True, copy_enabled=True)
    h.write('S/e1.mkv', b'video')
    h.write('S/e1.nfo', b'meta')
    h.settle()
    h.run()
    text = '\n'.join(r.getMessage() for r in caplog.records)
    for expected in ('【扫描】[tv] 开始全量扫描：', '【扫描】[tv] 全量扫描完成：2 个文件，有变化 2 个，消失 0 个',
                     '【STRM】已复制：[tv] S/e1.nfo（4 B）', '【115】已上传：[tv] S/e1.mkv（5 B，用时 '):
        assert expected in text, expected


def test_each_file_gets_one_line_per_target_when_all_goes_well(harness, caplog):
    """正常运行时每个文件在每个目标上只有一条结果日志，没有逐文件的发现、开始或等待确认日志。"""
    caplog.set_level(logging.DEBUG, logger='localmirror115')
    h = harness(mode='sync', upload_enabled=True, copy_enabled=True)
    h.write('S/e1.mkv', b'video')
    h.write('S/e1.nfo', b'meta')
    h.settle()
    h.run()
    lines = [r.getMessage() for r in caplog.records]
    assert [line for line in lines if 'S/e1.mkv' in line] == ['【115】已上传：[tv] S/e1.mkv（5 B，用时 0.0 秒）']
    assert sorted(line.split('：')[0] for line in lines if 'S/e1.nfo' in line) == ['【115】已上传', '【STRM】已复制']


def test_failure_log_has_details_without_links_or_secrets(harness, caplog):
    """出错时日志写明原因和异常详情，便于排查，但隐去链接和凭据。"""
    caplog.set_level(logging.DEBUG, logger='localmirror115')
    h = harness(mode='sync', upload_enabled=True)
    h.write('S/e1.mkv', b'video')
    h.settle()

    def broken(*args, **kwargs):
        """模拟上传时网络出错，异常文本带签名链接和 token。"""
        raise ConnectionError('reset by peer https://cdn.115.com/x?sign=abc token=secret123')
    h.remote.upload = broken
    h.engine.run_b()
    failure = [r.getMessage() for r in caplog.records if '处理失败' in r.getMessage()]
    assert len(failure) == 1 and failure[0].startswith('【115】处理失败：[tv] S/e1.mkv（115请求失败，ConnectionError')
    assert 'reset by peer' in failure[0] and '第 1 次失败，1 分钟后自动重试）' in failure[0]
    assert 'secret123' not in failure[0] and 'sign=abc' not in failure[0] and 'cdn.115.com' not in failure[0]


def test_every_log_call_is_chinese():
    """插件源码中的每条日志调用都使用中文，且引擎不使用调试级别逐文件输出。"""
    source = Path(__file__).parents[3] / 'plugins.v3' / 'localmirror115'
    calls = []
    for path in source.glob('*.py'):
        text = path.read_text(encoding='utf-8')
        calls += re.findall(r"logger\.(?:debug|info|warning|error)\(\s*'([^']*)'", text)
        calls += re.findall(r"\bfmt\(\s*[^,]+,\s*'([^']*)'", text)  # 统一格式日志的“动作”文字
    assert len(calls) > 40
    assert 'logger.debug' not in (source / 'engine.py').read_text(encoding='utf-8')  # 逐文件结果都用 INFO 级别
    assert all(re.search(r'[一-鿿]', c) for c in calls), [c for c in calls if not re.search(r'[一-鿿]', c)]


def test_durations_read_the_same_everywhere():
    """日志、通知和状态页共用同一种时长写法。"""
    from app.plugins.localmirror115.log import duration_text
    assert [duration_text(s) for s in (45, 60, 125, 3600, 3900)] == [
        '45 秒', '1 分钟', '2 分 05 秒', '1 小时', '1 小时 05 分']


def test_every_error_code_has_a_chinese_label():
    """插件抛出的每个错误码都有中文说明，日志、通知和状态页不会出现英文代码。"""
    from app.plugins.localmirror115.log import NOTE_LABELS
    source = Path(__file__).parents[3] / 'plugins.v3' / 'localmirror115'
    codes = set()
    for path in source.glob('*.py'):
        codes |= set(re.findall(r"(?:RemoteUnknown|RemoteConflict|OutputError|OSError|Stopped)\('([a-z0-9_]+)'\)",
                                path.read_text(encoding='utf-8')))
    assert codes and not codes - set(NOTE_LABELS), sorted(codes - set(NOTE_LABELS))


def test_files_needing_no_action_are_summarised_not_listed(harness, caplog):
    """目标上已有相同文件、无需实际操作时，按映射汇总成一条日志，不逐个列出。"""
    caplog.set_level(logging.INFO, logger='localmirror115')
    h = harness(mode='dry_run', upload_enabled=True, copy_enabled=True)
    for i in range(3):
        h.write('S/e%d.mkv' % i, b'video')
        h.remote.put('/media/tv/S/e%d.mkv' % i, 5)
    h.settle()
    h.run()
    lines = [r.getMessage() for r in caplog.records]
    assert '【115】[tv] 无需处理：3 个文件（试运行，目标上已有相同文件）' in lines
    assert not [line for line in lines if 'S/e0.mkv' in line]


def test_no_class_defines_a_method_twice():
    """同一个类里不能有同名方法（后定义的会悄悄覆盖前一个）。"""
    import ast
    source = Path(__file__).parents[3] / 'plugins.v3' / 'localmirror115'
    for path in source.glob('*.py'):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.ClassDef):
                names = [item.name for item in node.body if isinstance(item, ast.FunctionDef)]
                assert len(names) == len(set(names)), (path.name, node.name)


def test_log_lines_share_one_layout():
    """日志统一为“【分类】动作：对象（细节，细节）”，没有对象时“【分类】动作：细节”；空的细节略去；耗时一分钟内精确到 0.1 秒。"""
    from app.plugins.localmirror115.log import fmt, took_text
    assert fmt('115', '已上传', '7.7 GB', '', '用时 62.5 秒', subject='[tv] 剧 (2023)/S01E01.mkv') == \
        '【115】已上传：[tv] 剧 (2023)/S01E01.mkv（7.7 GB，用时 62.5 秒）'
    assert fmt('扫描', '[tv] 全量扫描完成', '10 个文件', '用时 1.0 秒') == '【扫描】[tv] 全量扫描完成：10 个文件，用时 1.0 秒'
    assert fmt('扫描', '安排全量扫描', subject='全部映射') == '【扫描】安排全量扫描：全部映射'
    assert took_text(2.94) == '用时 2.9 秒' and took_text(125) == '用时 2 分 05 秒'
    from app.plugins.localmirror115.log import mappings_text
    assert mappings_text({'tv', 'movies'}) == '[movies]、[tv]'


def test_round_summary_is_written_as_one_log_line(caplog):
    """一轮结束时把各项数量和用时写成一行日志（失败明细已逐条记录，不重复）；单独的告警不算本轮汇总。"""
    from conftest import Clock
    from app.plugins.localmirror115.notify import Notifier
    caplog.set_level(logging.INFO, logger='localmirror115')
    clock, sent = Clock(), []
    notifier = Notifier(lambda title, text: sent.append(title), enabled=True, clock=clock)
    notifier.count('uploaded', 3)
    notifier.count('copied', 2)
    notifier.record('failed', 'S/e1.mkv', '上传失败')
    clock.advance(75)
    notifier.count('copied')
    assert notifier.flush(force=True)
    notifier.warning('cooldown', '115访问冷却', '115同步暂停', '60 秒后自动重试')
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith('【汇总】')]
    assert lines == ['【汇总】本轮需关注：上传115 3 个，复制到STRM目录 3 个，处理用时 1 分 15 秒，需处理 1 项']
    assert len(sent) == 2


def test_a_vanished_file_is_counted_once_while_waiting_for_deletion(harness, caplog):
    """已记为消失、等待删除的文件在之后的扫描里再次确认时，不会再记一次“消失”。"""
    from conftest import deleting
    h = harness(**deleting(delete_delay_hours=1))
    h.write('S/keep.nfo', b'keep')
    h.write('S/e1.nfo', b'meta')
    h.settle()
    h.run()
    caplog.set_level(logging.INFO, logger='localmirror115')
    (h.a / 'S' / 'e1.nfo').unlink()
    h.settle(scans=3)
    found = [r.getMessage() for r in caplog.records if r.getMessage().startswith('【发现】')]
    assert found == ['【发现】[tv] 1 个文件有变化并已稳定，开始处理：消失 1 个']


def test_files_only_touched_are_not_reported_as_changes(harness, caplog):
    """已同步的文件只是属性被碰了一下（改权限、媒体服务器刷新等），大小和修改时间都没变：不记“发现”，也没有要做的事。"""
    import os
    h = harness()
    paths = [h.write('S/e%02d.nfo' % i, b'meta') for i in range(25)]
    h.settle()
    h.run()
    h.remote.calls.clear()
    caplog.set_level(logging.INFO, logger='localmirror115')
    for path in paths:
        os.chmod(path, 0o600)
    h.engine.hint_many([str(path) for path in paths], 'filesystem')
    h.clock.advance(2)
    h.engine.tick()
    h.run()
    assert not [r for r in caplog.records if r.getMessage().startswith('【发现】')]
    assert not [call for call in h.remote.calls if call[0] in ('upload', 'delete')]


def test_a_big_batch_of_real_changes_is_reported_with_counts(harness, caplog):
    """真正的大批入库（20 个以上）记一条“发现”，分别写明新增、修改的数量。"""
    h = harness()
    for i in range(3):
        h.write('S/old%d.nfo' % i, b'old')
    h.settle()
    caplog.set_level(logging.INFO, logger='localmirror115')
    for i in range(3):
        h.write('S/old%d.nfo' % i, b'changed!')
    for i in range(20):
        h.write('S/new%02d.nfo' % i, b'new')
    h.settle()
    found = [r.getMessage() for r in caplog.records if r.getMessage().startswith('【发现】')]
    assert found == ['【发现】[tv] 23 个文件有变化并已稳定，开始处理：新增 20 个，修改 3 个']
