"""Offline demo: compile/start a loopback-only native simulator and the PC web UI."""
import argparse
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from create_pairing import create

ROOT = pathlib.Path(__file__).resolve().parents[1]


def build_simulator():
    compiler = shutil.which('g++')
    if not compiler:
        raise RuntimeError('需要 g++ 编译离线接收端（当前电脑已有 MSYS2 g++）；实机 SDK 不参与模拟编译')
    build = ROOT / 'build'
    build.mkdir(exist_ok=True)
    binary = build / ('go1_receiver_sim.exe' if os.name == 'nt' else 'go1_receiver_sim')
    command = [compiler, '-std=c++14', '-Wall', '-Wextra', '-Wpedantic', '-Werror', '-DGO1_SIM_ONLY',
               'robot/receiver.cpp', '-o', 'build/'+binary.name]
    if os.name == 'nt':
        command.append('-lws2_32')
    else:
        command.append('-pthread')
    # Relative compiler paths avoid MinGW linker issues with the Chinese workspace path on Windows.
    subprocess.run(command, check=True, cwd=ROOT)
    return binary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--port', type=int, default=8099)
    parser.add_argument('--mock-cross-drift', action='store_true', help='Loopback-only gait drift regression fixture')
    args = parser.parse_args()
    binary = build_simulator()
    with tempfile.TemporaryDirectory(prefix='go1_offline_demo_') as temporary:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            receiver_port = probe.getsockname()[1]
        config, token = create(pathlib.Path(temporary), '127.0.0.1', receiver_port, True)
        hidden = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        receiver_args = [str(binary), '--simulate', '--port', str(receiver_port), '--token-file', str(token)]
        if args.mock_cross_drift:
            receiver_args.append('--mock-cross-drift')
        receiver = subprocess.Popen(receiver_args, creationflags=hidden)
        pc = None
        try:
            time.sleep(.15)
            if receiver.poll() is not None:
                raise RuntimeError('模拟接收端启动失败')
            command = [sys.executable, str(ROOT / 'pc/server.py'), '--config', str(config), '--port', str(args.port)]
            if args.no_browser:
                command.append('--no-browser')
            pc = subprocess.Popen(command)
            print('OFFLINE DEMO: 不连接机器人、不调用 SSH。按 Ctrl+C 退出。', flush=True)
            pc.wait()
        except KeyboardInterrupt:
            pass
        finally:
            if pc and pc.poll() is None:
                pc.terminate()
                pc.wait(3)
            if receiver.poll() is None:
                receiver.terminate()  # This child is compiled simulation-only and binds loopback.
                receiver.wait(3)


if __name__ == '__main__':
    main()
