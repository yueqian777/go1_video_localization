"""Live network smoke test: AUTH/PING/DISARM/RELEASE only, NEVER opens SDK control."""
import argparse
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pc'))
from transport import RobotLink


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=pathlib.Path, default=ROOT / 'config/pc.json')
    parser.add_argument('--duration', type=float, default=3)
    parser.add_argument('--attempts', type=int, default=2)
    args = parser.parse_args()
    if not 3 <= args.duration <= 120 or not 1 <= args.attempts <= 5:
        raise ValueError('Duration must be 3..120 s and attempts 1..5')
    config = json.loads(args.config.read_text(encoding='utf-8'))
    if config.get('simulation') is not False:
        raise RuntimeError('This check expects the paired real receiver, not simulation')
    link = RobotLink(config)
    try:
        for attempt in range(args.attempts):
            link.connect()
            deadline = time.monotonic() + 4
            while not link.snapshot()['connected']:
                if time.monotonic() > deadline:
                    raise RuntimeError('Network connection failed: ' + repr(link.snapshot()['events']))
                time.sleep(.02)
            ticks = set()
            maximum_age = 0
            maximum_rtt = 0
            delayed_samples = 0
            deadline = time.monotonic() + args.duration
            while time.monotonic() < deadline:
                state = link.snapshot()
                robot = state['robot']
                assert state['connected'] and not state['simulation'], 'Live network lost'
                maximum_age = max(maximum_age, state['telemetry_age_ms'])
                maximum_rtt = max(maximum_rtt, state['rtt_ms'] or 0)
                delayed_samples += int(state['telemetry_age_ms'] > 500)
                assert not state['recovery_required'], 'Monitoring must not require a motion rearm'
                if not state['robot_fresh']:
                    assert robot == {}, 'Stale state must not be presented as current telemetry'
                    time.sleep(.02)
                    continue
                assert robot['authenticated'] and not robot['simulated'], 'Live pairing failed'
                assert robot.get('control_timeout_ms') == robot.get('command_echo_ms') == 3000, 'Old receiver network thresholds'
                assert robot.get('hold_to_move') is True and robot.get('continuous_limit_s', 'unknown') is None, 'Old receiver movement duration cap'
                assert robot.get('steady_motion_renewal') is True, 'Receiver still has cross-axis renewal bug'
                assert robot.get('startup_ack_ms') == 1500, 'Old firmware startup ACK window'
                assert not robot['control_active'] and not robot['armed'], 'Unexpected SDK control'
                assert not robot['has_state'] and robot['soc'] is None, 'Unexpected SDK telemetry'
                assert robot['health'] == 'NO_SDK_CONTROL', 'Unexpected SDK state'
                assert all(robot['command_' + axis] == 0 for axis in ('vx', 'vy', 'yaw')), 'Nonzero target'
                ticks.add(robot['tick'])
                time.sleep(.02)
            assert len(ticks) >= args.duration*6, 'State reporting too slow'
            print('MONITOR_ONLY_PASS reconnect={} duration_s={} unique_ticks={} max_observed_age_ms={:.1f} max_rtt_ms={:.1f} delayed_samples={} timeout_ms=3000; SDK unopened, targets zero'.format(
                attempt, args.duration, len(ticks), maximum_age, maximum_rtt, delayed_samples), flush=True)
            link.close()
            time.sleep(.2)
    finally:
        link.close()


if __name__ == '__main__':
    main()
