"""One-time SSH maintenance only. Never takes SDK control or issues motion commands.

Uses the workspace's existing SSH connection helper; the PC runtime does not import this file.
"""
import argparse
import hashlib
import pathlib
import json
import re
import shlex
import stat
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / 'tmp'))
from go1_status_cleanup import connect
sys.path.insert(0, str(ROOT / 'pc'))
from transport import RobotLink

TARGET = '/home/pi/go1_remote_control'
OLD = '/home/pi/go1_keyboard_demo'
BACKUPS = '/home/pi/go1_keyboard_backups'
UNIT = '/etc/systemd/system/go1-remote.service'
DOCS = ['README_zh.md', '方案.md', '网络控制使用教程.md', 'DEPLOYMENT_20261016.md', 'FIXES_20261018.md', 'STATUS_FIX_20261018.md', 'TIMEOUT_3S_20261018.md', 'HELD_MOTION_20261018.md', 'SMOOTH_MOTION_20261018.md']
FILES = ['CMakeLists.txt', '.gitignore', *DOCS,
         'robot/receiver.cpp', 'robot/control_policy.hpp', 'robot/motion_limits.hpp', 'robot/network_timing.hpp', 'robot/legacy/go1_highlevel.h',
         'robot/go1-remote.service', 'pc/server.py', 'pc/transport.py', 'pc/timeouts.py',
         'pc/web/index.html', 'pc/web/style.css', 'pc/web/state.js', 'pc/web/app.js',
         'tools/run_demo.py', 'tools/start_pc.py', 'tools/create_pairing.py', 'tools/check_monitor.py', 'tools/check_motion_once.py', 'tests/test_system.py', 'tests/test_start_pc.py', 'tests/test_ui_state.js', 'tests/test_sdk_lifecycle.cpp', 'tests/test_control_policy.cpp']


def remote(client, command, timeout=90):
    print('REMOTE ' + command, flush=True)
    channel = client.get_transport().open_session()
    channel.exec_command(command)
    deadline = time.monotonic() + timeout
    while True:
        for ready, receive in ((channel.recv_ready, channel.recv), (channel.recv_stderr_ready, channel.recv_stderr)):
            while ready():
                print(receive(32768).decode('utf-8', 'replace'), end='', flush=True)
        if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
            break
        if time.monotonic() > deadline:
            channel.close()
            raise RuntimeError('Remote timeout; inspect before retrying')
        time.sleep(.05)
    code = channel.recv_exit_status()
    channel.close()
    if code:
        raise RuntimeError('Remote command exited ' + str(code))


def processes(client):
    _, out, _ = client.exec_command('ps -eo pid=,comm=,args=', timeout=10)
    rows = [line.split(None, 2) for line in out.read().decode('utf-8', 'replace').splitlines()]
    rows = [r for r in rows if len(r) == 3]
    old = [r for r in rows if r[1] == 'go1_keyboard' or r[1].startswith('go1_forward_dem')]
    if old:
        raise RuntimeError('Old demo is running; not stopped automatically: ' + repr(old))
    factory = [(pid, comm) for pid, comm, args in rows
               if comm in ('Legged_sport', 'rosmaster') or 'ros2udp_motion_mode_adv __name:=' in args
               or comm.startswith('node_obstacle_')]
    print('FACTORY ' + repr(factory), flush=True)
    return factory


def directory(sftp, path, create=False):
    try:
        info = sftp.lstat(path)
    except FileNotFoundError:
        if not create:
            raise
        sftp.mkdir(path, mode=0o700)
        info = sftp.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or sftp.normalize(path) != path:
        raise RuntimeError('Unexpected directory or symlink: ' + path)


def upload(sftp, relative, secret=False):
    path = pathlib.PurePosixPath(relative)
    if path.is_absolute() or '..' in path.parts:
        raise RuntimeError('Invalid upload path')
    parent = TARGET
    for part in path.parent.parts:
        parent += '/' + part
        directory(sftp, parent, create=True)
    destination = TARGET + '/' + relative
    try:
        info = sftp.lstat(destination)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError('Not a regular destination: ' + destination)
        if secret:
            with sftp.open(destination, 'rb') as source:
                if source.read() != (ROOT / relative).read_bytes():
                    raise RuntimeError('Remote pairing differs; not overwriting')
            print('PAIRING_ALREADY_MATCHES (secret omitted)', flush=True)
            return
    except FileNotFoundError:
        pass
    sftp.put(str(ROOT / relative), destination)
    sftp.chmod(destination, 0o600 if secret else 0o644)
    with sftp.open(destination, 'rb') as source:
        received = source.read()
    if hashlib.sha256(received).digest() != hashlib.sha256((ROOT / relative).read_bytes()).digest():
        raise RuntimeError('Upload checksum mismatch: ' + relative)
    print('UPLOAD_OK ' + relative, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('inspect', 'backup', 'deploy', 'sync-docs', 'build-test', 'activate', 'install-start', 'archive-old', 'status'))
    parser.add_argument('--build-dir', default='build')
    args = parser.parse_args()
    if not re.fullmatch(r'build[-_a-zA-Z0-9]*', args.build_dir):
        raise ValueError('Build directory must be a simple project-relative build name')
    with connect() as client:
        before = processes(client)
        with client.open_sftp() as sftp:
            directory(sftp, '/home/pi')
            directory(sftp, '/home/pi/unitree_legged_sdk')
            if args.action == 'backup':
                directory(sftp, TARGET)
                archive_root = '/home/pi/go1_remote_control_backups'
                directory(sftp, archive_root, create=True)
                tag = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d_%H%M%S')
                archived = archive_root + '/before_update_' + tag
                try:
                    sftp.lstat(archived)
                except FileNotFoundError:
                    pass
                else:
                    raise RuntimeError('Backup already exists')
                remote(client, 'cp -a ' + shlex.quote(TARGET) + ' ' + shlex.quote(archived))
                directory(sftp, archived)
                print('BACKUP_READY ' + archived, flush=True)
            elif args.action == 'deploy':
                directory(sftp, TARGET, create=True)
                for name in FILES:
                    upload(sftp, name)
                upload(sftp, 'config/robot.token', secret=True)
            elif args.action == 'sync-docs':
                directory(sftp, TARGET)
                for name in DOCS:
                    upload(sftp, name)
            elif args.action == 'archive-old':
                directory(sftp, TARGET)
                directory(sftp, OLD)
                directory(sftp, BACKUPS, create=True)
                # Verify service is running and owns no SDK socket before removing the old deployment.
                remote(client, 'systemctl is-active go1-remote.service && '
                       'test -x /home/pi/go1_remote_control/build/go1_receiver && '
                       'test -z "$(sudo -n ss -uanp | grep :8111)"')
                tag = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d_%H%M%S')
                archived = BACKUPS + '/removed_keyboard_' + tag
                try:
                    sftp.lstat(archived)
                except FileNotFoundError:
                    pass
                else:
                    raise RuntimeError('Archive target already exists')
                sftp.rename(OLD, archived)
                directory(sftp, archived)
                try:
                    sftp.lstat(OLD)
                except FileNotFoundError:
                    print('OLD_DEPLOYMENT_REMOVED_RECOVERABLY ' + archived, flush=True)
                else:
                    raise RuntimeError('Old deployment still exists')
        if args.action == 'inspect':
            remote(client, 'ip -4 -br addr; cmake --version | head -1; g++ --version | head -1; '
                   'python3 --version; sudo -n ss -luntp | grep -E ":8082|:8095|:8111|:8765"; '
                   'ls -ld /home/pi/go1_keyboard_demo /home/pi/unitree_legged_sdk; '
                   'grep -R -l -F /home/pi/go1_keyboard_demo /etc/systemd/system /etc/rc.local /home/pi/.config/autostart 2>/dev/null || true')
        elif args.action == 'build-test':
            build = args.build_dir
            simulated = build + '-sim'
            remote(client, 'cd ' + TARGET + ' && '
                   'cmake -S . -B '+build+' -DUNITREE_SDK_DIR=/home/pi/unitree_legged_sdk -DCMAKE_CXX_FLAGS=-Werror && '
                   'cmake --build '+build+' -j2 && '
                   'cmake -S . -B '+simulated+' -DGO1_SIM_ONLY=ON -DCMAKE_CXX_FLAGS=-Werror && '
                   'cmake --build '+simulated+' -j2 && '
                   'GO1_SIM_BINARY=' + TARGET + '/'+simulated+'/go1_receiver python3 tests/test_system.py && '
                   '(cd '+build+' && ctest --output-on-failure) && '
                   './'+build+'/go1_receiver --help && file '+build+'/go1_receiver && sha256sum '+build+'/go1_receiver', timeout=240)
        elif args.action == 'activate':
            if args.build_dir == 'build':
                raise ValueError('Activation must use a separate staged build directory')
            with client.open_sftp() as checked:
                directory(checked, TARGET + '/' + args.build_dir)
                binary = checked.lstat(TARGET + '/' + args.build_dir + '/go1_receiver')
                if not stat.S_ISREG(binary.st_mode):
                    raise RuntimeError('Staged binary is not a regular file')
            # Reserve the one allowed network client while checking/updating; never take SDK control.
            link = RobotLink(json.loads((ROOT / 'config/pc.json').read_text(encoding='utf-8')))
            try:
                link.connect()
                deadline = time.monotonic() + 4
                while not link.snapshot()['robot_fresh']:
                    if time.monotonic() > deadline:
                        raise RuntimeError('Cannot reserve idle receiver; close other PC clients before update')
                    time.sleep(.02)
                state = link.snapshot()
                if state['robot'].get('control_active') is not False or state['robot'].get('armed') is not False:
                    raise RuntimeError('SDK control is active; refusing service restart')
                deadline = time.monotonic()+3
                while link.snapshot()['robot'].get('stopping') is not False:
                    if time.monotonic()>deadline:
                        raise RuntimeError('Receiver is still stopping; refusing restart')
                    time.sleep(.02)
                # A verified inactive legacy receiver can retain its own leaked UDP fd. Only that
                # service's fd may be reclaimed by restarting; never restart over a foreign owner.
                remote(client, 'owner=$(systemctl show -p MainPID --value go1-remote.service); '
                       'ports=$(sudo -n ss -uanp \'sport = :8111\' | tail -n +2); '
                       'if [ -n "$ports" ]; then printf \'%s\\n\' "$ports" | grep -q "pid=$owner," || exit 42; fi; '
                       'sudo -n systemctl stop go1-remote.service && '
                       'install -m 755 '+TARGET+'/'+args.build_dir+'/go1_receiver '+TARGET+'/build/go1_receiver && '
                       'sudo -n systemctl start go1-remote.service && systemctl is-active go1-remote.service')
            finally:
                link.close()
        elif args.action == 'install-start':
            # Never replace an unknown existing system unit.
            with client.open_sftp() as service_sftp:
                try:
                    with service_sftp.open(UNIT, 'rb') as source:
                        if source.read() != (ROOT / 'robot/go1-remote.service').read_bytes():
                            raise RuntimeError('Different existing service unit; inspect before replacing')
                except FileNotFoundError:
                    pass
            remote(client, 'sudo -n install -m 644 ' + TARGET + '/robot/go1-remote.service ' + UNIT + ' && '
                   'sudo -n systemctl daemon-reload && sudo -n systemctl enable --now go1-remote.service && '
                   'systemctl is-active go1-remote.service && systemctl is-enabled go1-remote.service && '
                   'python3 -c ' + shlex.quote('import socket,time; time.sleep(.5); s=socket.create_connection(("192.168.12.1",8765),2); s.close()') + ' && '
                   'sudo -n ss -lntp | grep :8765 && test -z "$(sudo -n ss -uanp | grep :8111)" && '
                   'sudo -n journalctl -u go1-remote.service -n 8 --no-pager')
        elif args.action == 'status':
            remote(client, 'systemctl is-active go1-remote.service; systemctl is-enabled go1-remote.service; '
                   'sudo -n ss -luntp | grep -E ":8082|:8095|:8111|:8765"; '
                   'sudo -n journalctl -u go1-remote.service -n 8 --no-pager')
        after = processes(client)
        if before != after:
            raise RuntimeError('Factory process set changed; inspect before further actions')
        print('FACTORY_UNCHANGED; NO SDK TAKEOVER OR MOTION WAS REQUESTED', flush=True)


if __name__ == '__main__':
    main()
