"""Single-instance launcher for the local recording dashboard; no auto-connect."""
import argparse
import io
import json
import logging
import os
import sys
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from .capture_web import CaptureWeb
from .desktop import InstanceLock, available_port, instance_key
from .jobs import atomic_json, read_json


class _LogStream(io.TextIOBase):
    """Supply pythonw's missing stdio without retaining a rotated file handle."""
    def __init__(self, handler):
        self.handler = handler

    @property
    def encoding(self):
        return 'utf-8'

    def write(self, text):
        if text.strip():
            # Direct handler dispatch avoids logging -> stderr -> logging loops.
            self.handler.handle(logging.LogRecord('library.stdio', logging.INFO,
                                '', 0, text.rstrip(), (), None))
        return len(text)

    def flush(self):
        self.handler.flush()


def load_settings(path):
    config = read_json(path)
    port = config.get('port', 8772)
    if type(port) is not int or not 1024 <= port <= 65505:
        raise ValueError('invalid_local_port')
    connection = Path(config['connection_config']).resolve()
    if not connection.is_file():
        raise ValueError('connection_config_missing')
    output = Path(config['output']).resolve()
    result = {'connection_config': str(connection), 'output': str(output), 'port': port}
    if config.get('workbench') is True:
        viewer = config.get('viewer_config')
        if viewer and not Path(viewer).is_file():
            raise ValueError('viewer_config_missing')
        result.update(workbench=True, viewer_config=str(Path(viewer).resolve()) if viewer else None,
                      workspace_dir=str(Path(config.get('workspace_dir',path.parent/'workspace')).resolve()))
    return result


def existing_capture(record, settings):
    try:
        saved = read_json(record)
        port = saved['port']
        if saved['config_key'] != instance_key(settings) or type(port) is not int or not 1024 <= port <= 65535:
            return None
        url = f'http://127.0.0.1:{port}/'
        # Ignore proxy environment settings even for readiness probes.
        with build_opener(ProxyHandler({})).open(url + 'health', timeout=.6) as response:
            health = json.loads(response.read(4096))
        return url if health == {'app': 'go1-capture', 'instance_id': saved['instance_id']} else None
    except (OSError, ValueError, KeyError, URLError):
        return None


def run_capture(config_path, *, open_browser=True, stop=False):
    path = Path(config_path).resolve()
    settings = load_settings(path)
    lock = InstanceLock(path.with_suffix('.lock'))
    record, stop_file = path.with_suffix('.server.json'), path.with_suffix('.stop.json')
    if not lock.acquire():
        for _ in range(40):
            url = existing_capture(record, settings)
            if url:
                if stop:
                    atomic_json(stop_file, {'instance_id': read_json(record)['instance_id']})
                elif open_browser:
                    webbrowser.open(url)
                return url
            time.sleep(.25)
        raise RuntimeError('已有页面正在启动或配置已改变，请查看启动日志。')
    app = None
    try:
        if stop:
            return None
        atomic_json(record, {'config_key': instance_key(settings), 'port': None})
        if settings.get('workbench'):
            from .workbench import Workbench
            app = Workbench(settings['connection_config'], settings['output'], port=available_port(settings['port']),
                            viewer_config=settings['viewer_config'], workspace_dir=settings['workspace_dir'])
        else:
            app = CaptureWeb(settings['connection_config'], settings['output'], port=available_port(settings['port']))
        app.start()
        atomic_json(record, {'config_key': instance_key(settings), 'port': app.server.server_port,
                            'instance_id': app.instance_id, 'pid': os.getpid()})
        logging.info('Local capture dashboard ready: %s', app.url)
        if open_browser:
            webbrowser.open(app.url)
        while not app.stop.wait(.25):
            try:
                if stop_file.is_file() and read_json(stop_file).get('instance_id') == app.instance_id:
                    break
            except (OSError, ValueError):
                pass
        return app.url
    except KeyboardInterrupt:
        return None
    finally:
        try:
            if app:
                app.close()
        finally:
            lock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Go1 相机录制台：仅本机页面，不自动连接机器狗。')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--stop', action='store_true')
    args = parser.parse_args(argv)
    args.config.parent.mkdir(parents=True, exist_ok=True)
    log_path = args.config.with_suffix('.log')
    handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=2, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    original_stdout, original_stderr = sys.stdout, sys.stderr
    stream = _LogStream(handler)
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    try:
        run_capture(args.config, open_browser=not args.no_browser, stop=args.stop)
        return 0
    except Exception:  # noqa: BLE001 - show a recoverable launch failure under pythonw
        logging.exception('Unable to start capture dashboard')
        if os.name == 'nt' and original_stderr is None:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, '相机录制台未能启动，请查看日志：\n' + str(log_path), 'Go1 相机录制台', 0x10)
        return 1
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr
        stream.flush()
        logging.getLogger().removeHandler(handler)
        handler.close()


if __name__ == '__main__':
    raise SystemExit(main())
