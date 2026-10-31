"""Open the same current PC service, or start it; never connects to a robot by itself."""
import json
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pc'))
from server import APP_VERSION


def main():
    config_path = ROOT / 'config/pc.json'
    if not config_path.is_file():
        print('缺少配对 config/pc.json，请保留原配对文件，不要随意重新生成。')
        return 1
    config = json.loads(config_path.read_text(encoding='utf-8'))
    url = 'http://127.0.0.1:8099'
    try:
        with urllib.request.urlopen(url+'/api/state', timeout=1) as response:
            state = json.load(response)
    except urllib.error.URLError as error:
        if isinstance(error.reason, ConnectionRefusedError):
            return subprocess.call([sys.executable, str(ROOT / 'pc/server.py'), '--config', str(config_path)], cwd=ROOT)
        print('本机 8099 服务不可用或未响应；请先检查旧服务，不会启动重复实例。', str(error))
        return 1
    except (ValueError, TimeoutError, OSError) as error:
        print('本机端口已有异常响应；请先检查旧服务，不会启动重复实例。', str(error))
        return 1
    expected = str(config['host'])+':'+str(config['port'])
    if state.get('app_name') != 'go1_remote_control' or state.get('app_version') != APP_VERSION:
        print('旧电脑服务或其他程序仍占用 8099。请在原服务窗口按 Ctrl+C 退出，再运行 start_pc.cmd。')
        print('只刷新或重开网页不会更新后台。此入口不会强杀任何进程。')
        return 1
    if state.get('simulation') != bool(config.get('simulation', False)) or state.get('address') != expected:
        print('8099 当前使用另一套模拟/实机配置，请先关闭它再启动本配置。')
        return 1
    print('当前版本 '+APP_VERSION+' 已运行，打开现有控制台；不会重复启动或自动连接机器狗。')
    webbrowser.open(url)
    return 0


if __name__ == '__main__':
    sys.exit(main())
