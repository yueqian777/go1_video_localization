"""Local desktop launcher for the existing offline viewer, not robot software."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import time
import traceback
from urllib.request import urlopen
import webbrowser
from uuid import uuid4

from .jobs import atomic_json, ensure_sidecar, read_json


def load_config(path):
    config = read_json(path)
    map_dir = Path(config['map_dir']).resolve()
    if read_json(map_dir/'map_manifest.json').get('frozen') is not True:
        raise ValueError('所选目录尚无冻结地图；首次建图请使用 watch-build。')
    sidecar = ensure_sidecar(map_dir, config['sidecar_dir'])
    port = config.get('port', 8767)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('端口应为 1024 至 65535 的整数。')
    snapshots = config.get('snapshot_dir')
    if snapshots and not Path(snapshots).is_dir():
        raise ValueError('快照目录不存在，请重新配置快捷方式。')
    return dict(map_dir=str(map_dir), sidecar_dir=str(sidecar), port=port,
                snapshot_dir=str(Path(snapshots).resolve()) if snapshots else None)


def instance_key(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


class InstanceLock:
    """OS lock is released even after a crash; stale JSON never owns a service."""
    def __init__(self, path):
        self.path = Path(path)
        self.stream = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open('a+b')
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self.stream = stream
        return True

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None


def http_ready(url):
    try:
        with urlopen(url, timeout=.8) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def existing_url(record, config):
    try:
        state = read_json(record)
        port = state['port']
        if state['config_key'] != instance_key(config) or type(port) is not int or not 1024 <= port <= 65535:
            return None
        url = f'http://127.0.0.1:{port}/'
        return url if http_ready(url) else None
    except (OSError, KeyError, ValueError):
        return None


def available_port(preferred):
    # Do not kill an unrelated service or assume a Viser page belongs to us.
    for port in range(preferred, min(preferred + 30, 65536)):
        with socket.socket() as probe:
            try:
                probe.bind(('127.0.0.1', port))
                return port
            except OSError:
                continue
    raise RuntimeError('附近端口均被占用，请在启动配置中换一个端口。')


def run_viewer(config_path, *, open_browser=True, stop=False, factory=None):
    path = Path(config_path).resolve()
    config = load_config(path)
    lock = InstanceLock(path.with_suffix('.lock'))
    record, stop_file = path.with_suffix('.server.json'), path.with_suffix('.stop.json')
    if not lock.acquire():
        for _ in range(60):
            url = existing_url(record, config)
            if url:
                if stop:
                    atomic_json(stop_file, {'token': read_json(record)['token']})
                elif open_browser:
                    webbrowser.open(url)
                return url
            time.sleep(.25)
        raise RuntimeError('已有查看器正在启动或配置已改变。请查看启动日志，不会启动重复服务。')
    app = None
    try:
        if stop:
            return None
        # Mark startup before creating the service, so a previous registry cannot be reused.
        token = uuid4().hex
        atomic_json(record, {'config_key': instance_key(config), 'port': None, 'token': token})
        if factory is None:
            from .viser_viewer import ViewerApplication
            factory = ViewerApplication
        app = factory(config['map_dir'], config['sidecar_dir'],
                      port=available_port(config['port']), snapshot_dir=config['snapshot_dir'])
        port = app.server.get_port()
        url = f'http://127.0.0.1:{port}/'
        for _ in range(60):
            if http_ready(url):
                break
            time.sleep(.25)
        else:
            raise RuntimeError('页面未能按时启动，请查看日志。')
        atomic_json(record, {'config_key': instance_key(config), 'port': port,
                            'token': token, 'pid': os.getpid(), 'url': url})
        print(f'Offline viewer ready: {url}', flush=True)
        if open_browser:
            webbrowser.open(url)
        while True:
            try:
                if stop_file.exists() and read_json(stop_file).get('token') == token:
                    break
            except (OSError, ValueError):
                pass
            time.sleep(.5)
        return url
    except KeyboardInterrupt:
        return None
    finally:
        try:
            if app is not None:
                app.close()
        finally:
            lock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Go1 中文离线地图工作台：启动、复用或停止本机查看器。')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--configure', action='store_true')
    parser.add_argument('--map-dir')
    parser.add_argument('--sidecar-dir')
    parser.add_argument('--snapshot-dir')
    parser.add_argument('--port', type=int, default=8767)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--stop', action='store_true')
    args = parser.parse_args(argv)
    args.config = args.config.resolve()
    args.config.parent.mkdir(parents=True, exist_ok=True)
    log_path = args.config.with_suffix('.log')
    # pythonw has no stdout/stderr. Give Viser and exceptions durable local streams.
    log = log_path.open('a', encoding='utf-8', buffering=1)
    with log:
        original_out, original_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = log
        try:
            if args.configure:
                if not args.map_dir or not args.sidecar_dir:
                    raise ValueError('配置快捷方式需要地图目录和独立的结果目录。')
                config = dict(map_dir=str(Path(args.map_dir).resolve()),
                              sidecar_dir=str(Path(args.sidecar_dir).resolve()),
                              snapshot_dir=str(Path(args.snapshot_dir).resolve()) if args.snapshot_dir else None,
                              port=args.port)
                # Validate before replacing any working configuration.
                staging = args.config.with_name('desktop-validate-'+uuid4().hex+'.json')
                try:
                    atomic_json(staging, config)
                    load_config(staging)
                finally:
                    staging.unlink(missing_ok=True)
                atomic_json(args.config, config)
            else:
                run_viewer(args.config, open_browser=not args.no_browser, stop=args.stop)
            return 0
        except Exception:
            traceback.print_exc()
            if os.name == 'nt' and original_err is None:
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, '查看器未能启动。原地图不受影响。\n请检查日志：\n'+str(log_path), 'Go1 工作台', 0x10)
            return 1
        finally:
            sys.stdout, sys.stderr = original_out, original_err


if __name__ == '__main__':
    raise SystemExit(main())
