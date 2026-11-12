"""通知测试：只有所有生产者、队列与目标都处理完毕，一轮才算完成并发送汇总。"""
from conftest import Clock
from app.plugins.localmirror115.notify import Notifier, PROBE_INTERVAL, QUIET_SECONDS, new_state


def test_alerts_are_throttled_per_kind():
    """告警按类型限频，超过间隔后可再次发送。"""
    sent, clock = [], Clock(1000.0)
    n = Notifier(lambda t, x: sent.append(t), True, clock=clock)
    assert n.alert('snapshot', 'A', 'x')
    assert not n.alert('snapshot', 'A', 'x')
    assert n.alert('conflict', 'B', 'y')
    clock.now += 601
    assert n.alert('snapshot', 'A', 'x')
    assert sent == ['A', 'B', 'A']


def test_summary_waits_for_quiet_and_does_not_list_successful_files():
    """汇总在静默期后才发送，且不列出成功的文件名。"""
    sent, clock = [], Clock(1000.0)
    n = Notifier(lambda t, x: sent.append((t, x)), True, clock=clock)
    n.record('uploaded', 'S/e1.mkv')
    clock.now += 20
    n.record('copied', 'S/e1.nfo')
    clock.now += 20
    assert not n.flush()
    clock.now += 11
    assert n.flush()
    title, text = sent[0]
    assert '本轮完成' in title and '上传115  1' in text and '复制到STRM目录  1' in text
    assert 'e1.mkv' not in text and 'e1.nfo' not in text
    assert n.items == {} and not n.flush()


def test_continuous_activity_never_sends_a_five_minute_summary():
    """持续有活动时不发送汇总，活动停止并静默后才发送。"""
    sent, clock = [], Clock(1000.0)
    n = Notifier(lambda t, x: sent.append(t), True, clock=clock)
    for _ in range(100):
        n.record('uploaded', 'x')
        clock.now += 10
        assert not n.flush()
    assert sent == [] and n.counts['uploaded'] == 100
    clock.now += 31
    assert n.flush()


def test_busy_probe_blocks_even_forced_summary_and_starts_quiet_after_drain():
    """探测到忙碌时即使强制也不发送汇总，处理完毕后重新开始计算静默期。"""
    sent, clock = [], Clock(1000.0)
    n = Notifier(lambda t, x: sent.append(t), True, clock=clock)
    n.record('copied')
    clock.now += 700
    assert not n.flush(force=True, probe=lambda: {'busy': True})
    assert not n.flush(probe=lambda: {'busy': False})  # 刚查过还在忙：几秒内不再检查
    clock.now += PROBE_INTERVAL
    assert not n.flush(probe=lambda: {'busy': False})  # 处理完毕，从这里开始计算静默期
    clock.now += QUIET_SECONDS
    assert n.flush(probe=lambda: {'busy': False})
    assert len(sent) == 1


def test_empty_worker_polls_do_not_reset_quiet_time():
    """工作线程的空轮询不会重置静默计时。"""
    sent, clock = [], Clock(1000.0)
    n = Notifier(lambda t, x: sent.append(t), True, clock=clock)
    n.count('copied')
    n.flush(probe=lambda: {})
    for _ in range(61):
        clock.now += 0.5
        with n.activity():
            assert not n.flush(probe=lambda: {})
    assert n.flush(probe=lambda: {})


def test_new_event_during_completion_probe_cancels_claim():
    """完成探测期间出现新事件时取消本次汇总。"""
    sent, clock = [], Clock(1000.0)
    n = Notifier(lambda t, x: sent.append(t), True, clock=clock)
    n.count('copied')
    clock.now += 60

    def new_event():
        """探测时触发一次新活动。"""
        n.touch()
        return {}
    assert not n.flush(force=True, probe=new_event)
    assert n.counts == {'copied': 1} and not sent


def test_long_upload_and_batch_activity_blocks_even_force():
    """长时间上传或批处理进行中时即使强制也不发送汇总。"""
    n = Notifier(lambda t, x: None, True, clock=Clock(1000.0))
    n.count('copied')
    with n.activity():
        assert not n.flush(force=True)
    assert n.flush(force=True)


def test_failures_are_deduplicated_and_only_errors_list_files():
    """失败项按文件去重，只有错误会列出文件，超出部分折叠显示。"""
    n = Notifier(lambda t, x: None, True)
    n.record('uploaded', 'private/success.mkv', n=20)
    for i in range(15):
        n.record('failed', 'S/f%d.mkv（115）' % i, '上传失败')
    n.record('failed', 'S/f0.mkv（115）', '读取目录失败')
    title, text = n.compose(n.items, n.counts)
    assert '需关注' in title and '需处理  15' in text
    assert 'S/f0.mkv（115）：读取目录失败' in text
    assert '其余 5 项' in text and 'success.mkv' not in text
    n.resolve('S/f0.mkv（115）')
    assert n.counts['failed'] == 14


def test_directory_counts_and_deferred_title():
    """汇总显示整目录删除数量，存在暂缓项时标题提示需关注。"""
    title, text = Notifier.compose({}, {'deleted_b': 92, 'deleted_dirs_b': 1}, deferred=2)
    assert '需关注' in title
    assert '92 个（1 个目录）' in text and '暂缓处理  2' in text


def test_disabled_notifier_sends_nothing():
    """关闭通知时不发送任何告警或汇总。"""
    sent = []
    n = Notifier(lambda t, x: sent.append(t), False)
    n.count('uploaded')
    assert not n.alert('conflict', 'A', 'x')
    assert not n.flush(force=True)
    assert sent == []


def test_failed_delivery_keeps_counts_and_merges_new_results():
    """发送失败时保留计数并合并新结果，且不会立即反复重试。"""
    clock = Clock(1000.0)
    sent = []

    def boom(title, text):
        """发送期间产生新结果后抛出发送失败。"""
        n.count('copied', 2)
        raise RuntimeError('channel down')
    n = Notifier(boom, True, clock=clock)
    n.count('uploaded', 3)
    assert not n.flush(force=True)
    assert n.counts == {'copied': 2, 'uploaded': 3}
    n.send = lambda t, x: sent.append(x)
    assert not n.flush(force=True)  # 不会立即反复重试
    clock.now += 61
    assert n.flush(force=True)
    assert '上传115  3' in sent[0] and '复制到STRM目录  2' in sent[0]


def test_send_runs_without_notification_lock_and_new_records_survive():
    """发送时不持有通知锁，发送期间的新记录得以保留。"""
    import threading
    sent = []

    def send(title, text):
        """在另一线程中记录新结果，确认不会被锁阻塞。"""
        worker = threading.Thread(target=lambda: n.count('copied'))
        worker.start()
        worker.join(2)
        assert not worker.is_alive()
        sent.append(text)
    n = Notifier(send, True)
    n.count('uploaded')
    assert n.flush(force=True)
    assert n.counts == {'copied': 1}


def test_reload_keeps_late_old_results_but_only_current_owner_sends():
    """重载后保留旧实例迟到的结果，但只有当前实例负责发送。"""
    sent, clock, state = [], Clock(1000.0), new_state()
    old = Notifier(lambda t, x: sent.append(x), True, clock=clock, state=state)
    old.count('uploaded', 3)
    old.suspend()
    current = Notifier(lambda t, x: sent.append(x), True, clock=clock, state=state)
    with old.activity():
        old.count('uploaded', 2)
        assert not current.flush(force=True)
    assert not old.flush(force=True)
    assert current.flush(force=True)
    assert '上传115  5' in sent[0] and len(sent) == 1


def test_alerts_inside_worker_are_sent_by_scanner_not_under_worker_lock():
    """工作线程内产生的告警延后到工作线程之外发送。"""
    sent = []
    n = Notifier(lambda t, x: sent.append((t, x)), True)
    with n.activity():
        n.warning('risk', '115访问冷却', '115同步暂停', '稍后重试')
        assert not sent
    n.flush_alerts()
    assert len(sent) == 1 and '状态  115访问冷却' in sent[0][1]


def attach(h):
    """为引擎换上记录发送内容的通知器，返回发送记录列表。"""
    sent = []
    h.engine.notifier = Notifier(lambda t, x: sent.append((t, x)), True, clock=h.clock)
    return sent


def test_engine_waits_for_both_targets_and_pending_changes(harness):
    """引擎等待两个目标与待处理变更全部完成后才发送汇总。"""
    h = harness()
    sent = attach(h)
    h.write('S/e.nfo', b'meta')
    h.engine.scan_once()
    assert not h.engine.flush_notifications(force=True)
    h.clock.advance(2)
    h.engine.tick()
    h.engine.run_c()
    h.clock.advance(1000)
    assert not h.engine.flush_notifications(force=True)
    h.engine.run_b()
    h.clock.advance(PROBE_INTERVAL)
    assert not h.engine.flush_notifications()
    h.clock.advance(31)
    assert h.engine.flush_notifications()
    assert len(sent) == 1 and '本轮完成' in sent[0][0]


def test_engine_unconfirmed_upload_blocks_summary(harness):
    """未确认的上传会阻止发送汇总。"""
    h = harness()
    attach(h)
    h.write('S/e.nfo', b'x')
    h.settle()
    h.remote.lag = True
    h.run()
    assert h.asset('S/e.nfo')['b_state'] == 'verify'
    h.clock.advance(40)
    assert not h.engine.flush_notifications(force=True)
    h.remote.reveal()
    h.clock.advance(60)
    h.engine.run_b()
    assert h.engine.flush_notifications(force=True)


def test_engine_error_retry_blocks_summary_and_recovery_clears_error(harness):
    """出错重试期间不发送汇总，恢复后汇总中不再包含该错误。"""
    h = harness()
    sent = attach(h)
    h.write('S/v.mkv', b'video')
    h.settle()
    h.remote.fail_upload = True
    h.engine.run_b()
    assert not h.engine.flush_notifications(force=True)
    h.remote.fail_upload = False
    h.clock.advance(4000)
    h.engine.run_b()
    assert h.engine.flush_notifications(force=True)
    assert '本轮完成' in sent[-1][0] and '上传失败' not in sent[-1][1]


def test_synced_state_with_fingerprint_difference_is_still_busy(harness):
    """状态为已同步但指纹不一致时仍视为忙碌。"""
    h = harness()
    attach(h)
    h.write('S/e.nfo', b'x')
    h.settle()
    h.run()
    asset = h.asset('S/e.nfo')
    h.repo.record('b', asset, b_fp=None, b_state='synced')
    assert not h.engine.flush_notifications(force=True)


def test_deleted_source_with_old_synced_state_is_still_busy(harness):
    """源文件已删除但仍是旧的已同步状态时视为忙碌。"""
    h = harness(delete_enabled=True, delete_b_enabled=True, delete_c_enabled=True)
    attach(h)
    h.write('S/e.nfo', b'x')
    h.write('Keep/e.nfo', b'keep')
    h.settle()
    h.run()
    (h.a / 'S/e.nfo').unlink()
    h.settle()
    assert h.asset('S/e.nfo')['b_state'] == 'synced'
    assert not h.engine.flush_notifications(force=True)


def test_disabled_target_is_deferred_not_success(harness):
    """关闭的目标计为暂缓处理而不是成功。"""
    h = harness(copy_enabled=False)
    sent = attach(h)
    h.write('S/e.nfo', b'x')
    h.settle()
    h.run()
    assert h.engine.flush_notifications(force=True)
    assert '需关注' in sent[-1][0] and '暂缓处理' in sent[-1][1]


def test_engine_scan_request_blocks_summary(harness):
    """有待执行的扫描请求时不发送汇总。"""
    h = harness()
    attach(h)
    h.engine.notifier.count('copied')
    h.engine.request_subtree(h.config.mappings[0], 'New')
    assert not h.engine.flush_notifications(force=True)
    h.engine._scan_pending()
    assert h.engine.flush_notifications(force=True)


def test_engine_stop_does_not_flush_partial_results(harness):
    """引擎停止时不发送不完整的结果。"""
    h = harness()
    sent = attach(h)
    h.engine.notifier.count('copied')
    h.engine.stop()
    assert not h.engine.flush_notifications(force=True)
    assert sent == [] and h.engine.notifier.counts['copied'] == 1


def test_file_failing_repeatedly_no_longer_holds_the_round_summary(harness):
    """反复失败的文件达到次数后不再阻塞本轮汇总，并在汇总中列出。"""
    h = harness()
    sent = attach(h)
    h.write('S/v.mkv', b'video')
    h.write('S/ok.nfo', b'meta')
    h.settle()
    h.remote.fail_upload = True
    h.engine.run_c()
    for _ in range(2):
        h.engine.run_b()
        assert not h.engine.flush_notifications(force=True)   # 仍在正常重试
        h.clock.advance(4000)
    h.engine.run_b()                                           # 第三次失败
    assert h.asset('S/v.mkv')['b_attempts'] >= 3
    assert h.engine.flush_notifications(force=True)
    title, text = sent[-1]
    assert '需关注' in title and 'S/v.mkv' in text and '上传失败' in text


def test_failed_send_does_not_bring_back_a_file_resolved_meanwhile():
    """发送失败时不会恢复期间已解决的失败项。"""
    clock = Clock(1000.0)

    def boom(title, text):
        """发送期间解决一个失败项后抛出发送失败。"""
        n.resolve('S/a.mkv（115）')
        raise RuntimeError('channel down')
    n = Notifier(boom, True, clock=clock)
    n.record('failed', 'S/a.mkv（115）', '上传失败')
    n.record('failed', 'S/b.mkv（115）', '上传失败')
    assert not n.flush(force=True)
    assert set(n.items['failed']) == {'S/b.mkv（115）'} and n.counts['failed'] == 1


def test_summary_counts_files_already_present_in_strm(harness):
    """STRM 目录中已有相同文件时，本轮总结也列出“STRM目录已存在，无需复制”。"""
    h = harness(mode='sync', copy_enabled=True)
    sent = attach(h)
    h.write('S/e.nfo', b'meta')
    (h.c / 'S').mkdir(parents=True)
    (h.c / 'S' / 'e.nfo').write_bytes(b'meta')
    h.settle()
    h.engine.run_c()
    h.engine.run_b()
    h.clock.advance(60)
    h.engine.flush_notifications(force=True)
    assert sent and 'STRM目录已存在，无需复制  1' in sent[-1][1]
