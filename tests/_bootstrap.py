"""插件仓单测引导薄壳：找到同级 MoviePilot 后端时，引导逻辑委托主程序 ``app.testing.bootstrap``。

与官方插件仓的同名文件一致：后端默认在插件仓的同级目录 ``MoviePilot``，也可用环境变量
``MOVIEPILOT_BACKEND_PATH`` 指定。找不到后端时返回 False，由各插件测试自带的宿主替身兜底。
必须在首次 ``import app.*`` 之前调用。
"""
import os
import sys
from importlib import import_module
from pathlib import Path

PLUGINS_REPO = Path(__file__).resolve().parent.parent


def backend_path():
    """定位 MoviePilot 后端根目录（含 ``app/``）；找不到时返回 None。"""
    candidates = [Path(os.environ['MOVIEPILOT_BACKEND_PATH']).expanduser()] \
        if os.environ.get('MOVIEPILOT_BACKEND_PATH') else []
    candidates.append(PLUGINS_REPO.parent / 'MoviePilot')
    return next((path for path in candidates if (path / 'app').is_dir()), None)


def prepare_v3_backend():
    """找到后端时把它加入 ``sys.path``，并委托主程序完成 V3 插件单测引导；返回是否使用了真实宿主。"""
    path = backend_path()
    if path is None:
        return False
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
    import_module('app.testing.bootstrap').prepare_v3_backend(PLUGINS_REPO)
    return True
