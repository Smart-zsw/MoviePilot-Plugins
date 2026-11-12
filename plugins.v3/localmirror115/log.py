"""插件日志工具：复用宿主日志器，按模块名写入插件页“查看日志”对应的日志文件。"""
import re

from app.sdk.logging import logger

__all__ = ['logger', 'NOTE_LABELS', 'TARGET_LABEL', 'TARGET_PLACE', 'MODE_LABEL', 'where', 'size_text', 'note_label',
           'error_detail', 'duration_text']

TARGET_LABEL = {'b': '115', 'c': 'STRM'}      # 日志前缀
TARGET_PLACE = {'b': '115', 'c': 'STRM目录'}  # 通知中的目标名称
MODE_LABEL = {'tree': '目录树', 'detail': '逐个详细比对'}


def where(asset_or_key, relative=None):
    """生成日志定位文本“[映射] 相对路径”；可传资产字典，或映射名加相对路径。"""
    if relative is None:
        return '[%s] %s' % (asset_or_key['mapping'], asset_or_key['relative'])
    return '[%s] %s' % (asset_or_key, relative)


def size_text(size):
    """把字节数格式化为可读文本（最大单位 GB）。"""
    size = float(size or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return ('%d %s' if unit == 'B' else '%.1f %s') % (size, unit)
        size /= 1024


def duration_text(seconds):
    """把秒数格式化为中文时长，日志、通知和状态页统一使用：45 秒、2 分钟、2 分 05 秒、1 小时 05 分。"""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return '%d 秒' % seconds
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return '%d 分钟' % minutes if not seconds else '%d 分 %02d 秒' % (minutes, seconds)
    hours, minutes = divmod(minutes, 60)
    return '%d 小时' % hours if not minutes else '%d 小时 %02d 分' % (hours, minutes)


def error_detail(error):
    """生成写入日志的异常说明（类型和简要信息），隐去链接和凭据，避免泄露签名 URL、Token 或 Cookie。"""
    if error is None:
        return ''
    text = re.sub(r'https?://\S+', '<链接已隐藏>', str(error))
    text = re.sub(r'(?i)\b(cookie|token|sign|key|uid|cid|seid|kid|password)\b\s*[=:]\s*\S+', r'\1=***', text)
    text = ' '.join(text.split())[:200]
    return '%s：%s' % (type(error).__name__, text) if text else type(error).__name__


NOTE_LABELS = {
    'would_copy': '将从本地复制', 'would_replace': '内容不同，将覆盖', 'would_adopt_identical': '内容一致，无需复制',
    'would_upload': '115缺少，将上传', 'would_adopt_existing': '115已有，无需上传',
    'would_reconfirm_existing': '将复核115已有文件',
    'awaiting_remote_snapshot': '正在与115比对', 'missing_on_115': '115上已不存在，待重传',
    'copied': '已复制', 'identical': '内容一致', 'adopted_identical': '内容一致（已确认）',
    'adopted': '115已有（已确认）', 'adopted_by_tree': '115已有（按目录树确认）',
    'verified': '已上传并确认', 'verified_duplicates_removed': '已上传，已清理重复副本', 'reconfirmed': '已复核',
    'uploaded_unconfirmed': '已上传，等待确认', 'awaiting_remote_index': '等待115索引', 'awaiting_late_copies': '等待迟到副本',
    'delete_disabled': '同步删除未开启', 'delete_b_disabled': '115删除未开启', 'delete_c_disabled': 'STRM目录删除未开启',
    'dry_run': '试运行，不执行', 'delete_delay': '删除延迟中',
    'never_written': '无需处理', 'never_uploaded': '无需处理',
    'deleted_to_recycle_bin': '已删除（115回收站）', 'already_gone': '已不存在', 'deleted': '已删除',
    'changed_by_others_kept': '被他人修改，已保留', 'remote_call_failed': '115请求失败', 'local_io_failed': '本地读写失败',
    'already_absent': '已不存在', 'directory_pending_changes': '目录变化尚未稳定',
    'directory_records_changed': '目录记录已变化，等待复核', 'directory_source_changed': '源目录已变化，等待复核',
    'directory_retry_wait': '整目录删除等待重试', 'directory_stopped': '目录处理已暂停',
    'directory_identity_unconfirmed': '目录内有无法确认身份的副本，已保护',
    'directory_delete_failed': '115目录删除失败', 'directory_listing_unconfirmed': '115目录列表未确认',
    'directory_listing_changed': '115目录列表已变化', 'directory_delete_recheck': '目录删除后源已恢复，重新同步',
    'upload_failed': '上传失败', 'delete_failed': '删除失败', 'folder_list_failed': '读取115目录失败',
    'folder_create_failed': '创建115目录失败', 'delete_old_copy_failed': '删除旧副本失败',
    'delete_extra_copy_failed': '删除重复副本失败',
    'old_copy_still_listed': '旧副本仍未消失', 'upload_not_listed': '上传后未在115中出现',
    'remote_not_directory': '115上同名路径不是目录',
    # 115 访问
    'folder_query_failed': '查询115目录失败', 'folder_query_unconfirmed': '115目录查询无应答',
    'folder_list_unavailable': '115目录列表无应答', 'batch_delete_client_failed': '批量删除通道不可用（检查Cookie）',
    'batch_delete_failed': '批量删除请求失败', 'tree_export_needs_cookie': '目录树导出需要115 Cookie',
    'tree_export_client_failed': '目录树导出不可用（检查Cookie）', 'tree_export_start_failed': '目录树导出启动失败',
    'tree_export_status_failed': '查询目录树导出进度失败', 'tree_export_timeout': '目录树导出超时',
    'tree_export_download_failed': '读取目录树导出结果失败', 'tree_root_has_no_id': '115映射目录缺少ID',
    'snapshot_build_failed': '115快照生成失败', 'snapshot_changed_during_build': '生成期间115目录有变化，稍后重新生成',
    'snapshot_directory_delete_in_progress': '正在删除115目录，稍后再生成快照', 'stopped': '插件正在停止',
    # STRM 目录
    'invalid_output_path': 'STRM目录中的路径无效', 'strm_or_video_not_owned': '不处理.strm和视频文件',
    'output_root_unavailable': 'STRM目录不可用', 'output_symlink': 'STRM目录中有符号链接，已跳过',
    'output_parent_not_directory': 'STRM目录中的上级路径不是目录', 'output_not_regular_file': 'STRM目录中同名路径不是普通文件',
    'copy_verification_failed': '复制后校验不一致',
    # 本地目录与扫描
    'source_root_unhealthy': '本地实体库不可用或已变化', 'root_awaiting_scan': '等待首次扫描完成',
    'root_scan_incomplete_or_unsafe': '扫描异常，暂停删除', 'root_unavailable': '本地实体库不可用',
    'empty_tree_requires_manual_review': '本地目录树意外为空，需要人工确认', 'scan_file_limit_exceeded': '文件数超过上限',
    'scan_stopped': '扫描被中止', 'scan_enqueue_stopped': '扫描被中止', 'placeholder_replaced_by_mount': '挂载已就绪，等待重新扫描',
    # 状态页“最近错误”
    'worker_failed_retrying': '处理线程出错，稍后自动重试', 'index_failed': '115快照处理出错',
    'scan_failed': '扫描出错', 'prepare_failed': '启动准备出错',
}


def note_label(note):
    """把内部状态备注翻译成中文；未知备注原样返回。"""
    if not note:
        return ''
    if note.startswith('would_remove_') and note.endswith('_duplicate_uploads'):
        return '将清理重复副本'
    if note.startswith('would_replace_') and note.endswith('_copies'):
        return '内容不同，将替换'
    number = re.search(r'\d+', note)
    if note.startswith('awaiting_remote_index_') and number:
        return '已上传，等待115索引（目前有 %s 个同名副本）' % number.group()
    if note.startswith('upload_never_confirmed_after_') and number:
        return '已上传 %s 次仍未在115中出现，需人工处理' % number.group()
    return NOTE_LABELS.get(note, note)
