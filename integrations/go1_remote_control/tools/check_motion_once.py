"""One operator-authorized short probe, NEVER run automatically or on startup.

Real hardware requires both --live and --operator-ready. Only +0.1 m/s forward
input is generated, for 1.2 seconds; stop/release follows even on errors. This
software timer is not a hardware emergency stop or a guaranteed braking time.
"""
import argparse
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pc'))
from transport import RobotLink


def probe(config):
    link = RobotLink(config)
    observations = []
    last_tick = None
    owns_control = False
    started = time.monotonic()

    def snapshot(phase):
        nonlocal last_tick
        state = link.snapshot()
        r = state['robot']
        if state['robot_fresh'] and r.get('tick') != last_tick:
            last_tick = r.get('tick')
            observations.append(dict(phase=phase, elapsed_s=round(time.monotonic()-started, 3),
                **{key:r.get(key) for key in ('health','armed','fault','fault_reason','command_vx','actual_vx',
                    'actual_vy','actual_yaw','feedback_mode','feedback_gait','motion_elapsed_s','motion_stop_count','last_stop_reason',
                    'ttl_expirations','move_gap_ms','max_move_gap_ms')}))
        return state

    def wait(predicate, seconds, phase, heartbeat=False):
        deadline = time.monotonic()+seconds
        while time.monotonic() < deadline:
            state = snapshot(phase)
            if state['robot_fresh'] and predicate(state['robot']):
                return state['robot']
            if heartbeat and state['connected']:
                link.submit('heartbeat')
            time.sleep(.02)
        raise RuntimeError('Timed out in '+phase+'; last diagnostic: '+repr(observations[-1:]))

    try:
        link.connect()
        ready = wait(lambda r:r.get('authenticated') and not r.get('stopping'), 5, 'connect')
        if ready.get('control_active') is not False or ready.get('armed') is not False:
            raise RuntimeError('Refusing probe: an SDK session was already active')
        link.submit('take', standing=True, speed=.1, lateral=.1, turn=.1)
        owns_control = True  # Also clean up if TAKE is accepted but status is delayed.
        ready = wait(lambda r:r.get('control_active') and r.get('health') == 'OK' and
                     r.get('ready_ms', 0) >= 500 and r.get('state_age_ms', 9999) <= 150,
                     8, 'healthy_stationary', heartbeat=True)
        if ready.get('soc', 0) < 15 or ready.get('fault'):
            raise RuntimeError('Probe requires SOC >=15 and no fault')
        link.submit('arm')
        before = wait(lambda r:r.get('armed') is True, 3, 'arm', heartbeat=True)
        deadline = time.monotonic()+1.2
        try:
            while time.monotonic() < deadline:
                state = snapshot('forward_input')
                r = state['robot']
                if not state['robot_fresh'] or not r.get('armed') or r.get('fault'):
                    raise RuntimeError('Probe interrupted by connection/health/arming gate; '+json.dumps(observations[-4:]))
                link.submit('heartbeat', 'w')
                time.sleep(.02)
        finally:
            link.submit('stop')
            link.submit('disarm')
        after = wait(lambda r:not r.get('armed') and r.get('command_vx') == 0 and
                     abs(r.get('actual_vx', 999)) < .04 and abs(r.get('actual_vy', 999)) < .04 and
                     abs(r.get('actual_yaw', 999)) < .05,
                     5, 'stop_feedback')
        motion_rows = [row for row in observations if row['phase'] in ('forward_input','stop_feedback')]
        peak = max((row.get('actual_vx') or 0 for row in motion_rows), default=0)
        acknowledged = any(row.get('feedback_mode') == 2 and (row.get('command_vx') or 0) > 0 for row in motion_rows)
        result = dict(simulated=link.simulation, requested_vx=.1, input_duration_s=1.2,
                      peak_observed_vx=peak, movement_acknowledged=acknowledged, ttl_expirations=after.get('ttl_expirations'),
                      motion_stops=int(after.get('motion_stop_count',0))-int(before.get('motion_stop_count',0)),
                      last_stop_reason=after.get('last_stop_reason'), max_move_gap_ms=after.get('max_move_gap_ms'),
                      observations=observations)
        if not acknowledged or peak < .02 or result['ttl_expirations'] or result['motion_stops'] > 1:
            raise RuntimeError('Motion not smooth/acknowledged: '+json.dumps(result))
        return result
    finally:
        try:
            if owns_control and link.snapshot()['connected']:
                link.submit('stop')
                link.submit('disarm')
                link.submit('release')
                wait(lambda r:not r.get('control_active') and not r.get('stopping'), 5, 'release')
        finally:
            link.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=pathlib.Path, default=ROOT / 'config/pc.json')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--operator-ready', action='store_true')
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding='utf-8'))
    if not config.get('simulation') and not (args.live and args.operator_ready):
        parser.error('Real motion requires --live --operator-ready after current on-site human confirmation')
    print(json.dumps(probe(config), indent=2, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
