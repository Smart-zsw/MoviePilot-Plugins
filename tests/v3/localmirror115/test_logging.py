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
    for expected in ('【扫描】[tv] 开始全量扫描', '全量扫描完成：2 个文件', '【发现】[tv] 2 个文件有变化并已稳定',
                     '【STRM】已复制：[tv] S/e1.nfo', '【115】已上传：[tv] S/e1.mkv'):
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
    assert len(failure) == 1 and failure[0].startswith('【115】处理失败：[tv] S/e1.mkv（')
    assert 'ConnectionError' in failure[0] and 'reset by peer' in failure[0] and '自动重试' in failure[0]
    assert 'secret123' not in failure[0] and 'sign=abc' not in failure[0] and 'cdn.115.com' not in failure[0]


def test_every_log_call_is_chinese():
    """插件源码中的每条日志调用都使用中文，且引擎不使用调试级别逐文件输出。"""
    source = Path(__file__).parents[3] / 'plugins.v3' / 'localmirror115'
    calls = []
    for path in source.glob('*.py'):
        calls += re.findall(r"logger\.(?:debug|info|warning|error)\(\s*'([^']*)'", path.read_text(encoding='utf-8'))
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
    assert '【115】[tv] 3 个文件试运行：目标上已有相同文件，无需处理' in lines
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
