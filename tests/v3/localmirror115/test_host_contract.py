"""宿主契约测试：表单、API、服务、事件、页面和生命周期（在替身宿主上运行，不能代替真实 MP 加载检查）。"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.plugins import localmirror115
from app.plugins.localmirror115.persistence import Base
from app.sdk.events import eventmanager

# 这里替换宿主替身的数据库句柄并读取事件注册记录；在真实 MoviePilot 环境中跳过。
pytestmark = pytest.mark.skipif(not hasattr(eventmanager, 'registrations'), reason='需要测试替身宿主')


def test_plugin_host_shape_and_switches(monkeypatch, tmp_path):
    """插件对宿主暴露的接口形状正确，各开关、定时服务与生命周期按预期工作。"""
    from app.sdk.plugin import _PluginBase
    db = create_engine('sqlite:///' + str(tmp_path / 'plugin.db'), connect_args={'check_same_thread': False})
    Base.metadata.create_all(db)

    class Handle:
        """宿主数据库句柄替身，提供 session 工厂与引擎。"""
        session = sessionmaker(db)
        engine = db

    monkeypatch.setattr(_PluginBase, 'database', Handle)
    monkeypatch.setattr(_PluginBase, 'data_path', tmp_path / 'data')
    try:
        plugin = localmirror115.LocalMirror115()
        plugin.init_plugin({'enabled': False})
        assert {a['path'] for a in plugin.get_api()} == {'/scan', '/ping', '/index'}
        assert all(a['auth'] == 'bear' for a in plugin.get_api())
        assert plugin.get_service() == []
        assert plugin.get_state() is False
        assert len(plugin.get_database_models()) == 2
        form, defaults = plugin.get_form()
        assert defaults['mode'] == 'dry_run'
        assert not defaults['copy_enabled'] and not defaults['upload_enabled'] and not defaults['delete_enabled']
        models = str(form)
        for key in ('watch_mode', 'transfer_event_enabled', 'scan_enabled', 'adopt_existing',
                    'copy_enabled', 'upload_enabled', 'delete_enabled', 'delete_b_enabled',
                    'delete_c_enabled', 'mappings', 'scan_cron', 'excludes', 'video_extensions',
                    'verify_window_minutes', 'delete_delay_hours'):
            assert "'%s'" % key in models
        a, c = tmp_path / 'a', tmp_path / 'c'
        a.mkdir(); c.mkdir()
        (a / 'x.nfo').write_bytes(b'x')
        plugin.init_plugin({'enabled': True, 'force_polling': True, 'mode': 'dry_run',
                            'mappings': 'tv#%s#/media/tv#%s' % (a, c)})
        assert plugin.get_service() == []  # 兜底定时扫描默认关闭
        assert set(eventmanager.registrations) == {'main', 'audio', 'subtitle'}
        page = str(plugin.get_page())
        assert '跟踪文件' in page and '立即全量扫描' in page and "'pre'" not in page
        assert '实时状态' in page and '本地扫描' in page
        assert plugin.get_dashboard_meta()[0]['key'] == 'live'
        cols, attrs, body = plugin.get_dashboard('live')
        assert attrs['refresh'] == 5 and '本地扫描' in str(body)
        plugin.stop_service()
        assert not plugin.get_state()
        assert list(c.iterdir()) == []  # 演练模式从不写本地副本
        plugin.init_plugin({'enabled': True, 'scan_enabled': True, 'scan_cron': '30 3 * * 6',
                            'watch_enabled': False, 'mappings': 'tv#%s#/media/tv#%s' % (a, c)})
        service = plugin.get_service()[0]
        assert service['trigger'].expression == '30 3 * * 6'
        assert plugin._engine.snapshot()['watchers'] == {'tv': 'disabled'}
        plugin.stop_service()
    finally:
        db.dispose()
