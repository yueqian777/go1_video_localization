"""PC-side authenticated TCP transport. No SSH, no Unitree SDK, no automatic reconnect."""
import collections
import json
import re
import select
import socket
import threading
import time
from timeouts import (
    NETWORK_TIMEOUT_S, UI_LEASE_S, ACTION_QUEUE_S, NAVIGATION_SCHEDULER_PERIOD_S,
)


class RobotLink:
    @staticmethod
    def _blank_navigation_feedback():
        return {
            'command_id': None,
            'command_created_time_s': None,
            'intent_accepted_time_s': None,
            'move_send_sequence': None,
            'move_sent_time_s': None,
            'receiver_ack_sequence': None,
            'receiver_ack_time_s': None,
            'receiver_command_sequence': None,
            'receiver_clock_id': None,
            'receiver_time_s': None,
            'feedback_clock_id': None,
            'time_uncertainty_s': None,
            'clock_offset_s': None,
            'motion_started_time_s': None,
            'motion_stopped_time_s': None,
            'receiver_motion_started_time_s': None,
            'receiver_motion_stopped_time_s': None,
            'status_received_time_s': None,
            'causal_feedback': False,
        }

    def __init__(self, config):
        self.config = config
        self.lock = threading.RLock()
        self.socket = None
        self.thread = None
        self.stopping = threading.Event()
        self.connected = False
        self.connecting = False
        self.robot = {}
        self.received_at = 0.
        self.ui_until = 0.
        self.held_key = ''
        self.events = collections.deque(maxlen=12)
        self.queue = collections.deque(maxlen=8)
        self.force_stop = False
        self.stop_only = False
        self.navigation_intent = None
        self.navigation_hold = False
        self.last_navigation_command_id = None
        self.navigation_intent_expirations = 0
        self.navigation_feedback = self._blank_navigation_feedback()
        self.command_audit = collections.deque(maxlen=256)
        self.receiver_clock_offset_s = None
        self.receiver_clock_uncertainty_s = None
        self.sequence = 0
        self.rtt_ms = None
        self.sent = {}
        self.sent_meta = {}
        self.recovery_required = False
        self.degraded = False
        self.disconnect_reason = ''
        self.connection_stage = 'idle'
        self.last_fault = None
        self.last_disconnect = None
        self.last_network_gap = None
        self.last_ui_heartbeat = 0.
        self.max_ui_gap_ms = 0.
        self.ui_expirations = 0
        self.simulation = bool(config.get('simulation', False))
        if not re.fullmatch('[0-9a-f]{64}', config.get('token', '')):
            raise ValueError('配对 token 必须是 64 位小写十六进制；先运行 tools/create_pairing.py')
        if self.simulation and config['host'] != '127.0.0.1':
            raise ValueError('模拟模式只允许连接 127.0.0.1')

    def log(self, message):
        self.events.append({'time': time.strftime('%H:%M:%S'), 'message': message})

    def connect(self):
        with self.lock:
            if self.connected or self.connecting:
                raise ValueError('已连接或正在连接')
            if self.thread and self.thread.is_alive():
                raise ValueError('上一连接正在释放，请稍后重试')
            self.connecting = True
            self.stopping.clear()
            self.ui_until = 0
            self.held_key = ''
            self.queue.clear()
            self.navigation_intent = None
            self.navigation_hold = False
            self.last_navigation_command_id = None
            self.navigation_feedback = self._blank_navigation_feedback()
            self.command_audit.clear()
            self.receiver_clock_offset_s = None
            self.receiver_clock_uncertainty_s = None
            self.force_stop = True
            self.robot = {}
            self.received_at = 0.
            self.recovery_required = False
            self.degraded = False
            self.disconnect_reason = ''
            self.connection_stage = 'connecting'
            self.stop_only = False
            self.rtt_ms = None
            self.last_ui_heartbeat = self.max_ui_gap_ms = 0.
            self.ui_expirations = 0
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def submit(self, action, key='', speed=.5, standing=False, lateral=.4, turn=.6):
        with self.lock:
            if action == 'connect':
                self.connect()
                return
            if action == 'disconnect':
                self.held_key = ''
                self.ui_until = 0
                self.last_ui_heartbeat = 0.
                self.navigation_intent = None
                self.navigation_hold = False
                self.force_stop = True
                self.stopping.set()
                self.disconnect_reason = '用户已断开；需要恢复后手动连接'
                return
            if action == 'disarm':
                self.held_key = ''
                self.ui_until = 0
                self.last_ui_heartbeat = 0.
                self.navigation_intent = None
                self.navigation_hold = False
                self.force_stop = True
                self.queue.clear()
                return
            if action == 'stop':
                self.held_key = ''
                self.navigation_intent = None
                self.navigation_hold = False
                self.stop_only = True
                self.queue.clear()
                return
            if not self.connected or time.monotonic()-self.received_at > NETWORK_TIMEOUT_S:
                raise ValueError('连接未就绪或状态已过期')
            if action == 'heartbeat':
                if key not in ('', 'w', 's', 'j', 'l', 'z', 'c'):
                    raise ValueError('无效方向键')
                if self.recovery_required:
                    key = ''
                if self.held_key and not key:
                    self.stop_only = True
                now = time.monotonic()
                if self.last_ui_heartbeat and self.ui_until:
                    self.max_ui_gap_ms = max(self.max_ui_gap_ms, (now-self.last_ui_heartbeat)*1000)
                self.last_ui_heartbeat = now
                self.ui_until = now+UI_LEASE_S
                self.held_key = key
                return
            mapping = {'take': 'TAKE', 'arm': 'ARM', 'reset': 'RESET', 'avoid': 'AVOID', 'release': 'RELEASE'}
            if action not in mapping:
                raise ValueError('未知操作')
            if len(self.queue) >= 8:
                raise ValueError('操作队列已满')
            if action == 'take':
                if standing is not True or isinstance(speed, bool) or not isinstance(speed, (int, float)) or not .05 <= speed <= .5:
                    raise ValueError('接管需确认已站立、已拔充电器；前后速度须为 0.05–0.5 m/s')
                if (type(lateral) not in (int, float) or not .1 <= lateral <= .4 or
                        type(turn) not in (int, float) or not .1 <= turn <= .6):
                    raise ValueError('横移须为 0.1–0.4 m/s；转向须为 0.1–0.6 rad/s')
                if not self.robot.get('configurable_speeds'):
                    raise ValueError('接收端仍是旧版本；请先更新机器狗接收端再接管')
                if self.robot.get('control_timeout_ms') != 3000 or self.robot.get('command_echo_ms') != 3000:
                    raise ValueError('接收端仍使用旧网络阈值；请更新为 3 秒超时版本后再接管')
                if self.robot.get('hold_to_move') is not True or self.robot.get('continuous_limit_s', 'unknown') is not None:
                    raise ValueError('接收端尚未移除连续运动时限；请更新为 v0.3.1 后再接管')
                if self.robot.get('steady_motion_renewal') is not True:
                    raise ValueError('接收端仍可能因轻微横摆中断续行，请更新为 v0.3.2 后再接管')
                if self.robot.get('startup_ack_ms') != 1500:
                    raise ValueError('接收端启动确认窗口仍是旧值，请更新为 v0.3.3 后再接管')
                arg = 'STANDING,{},{},{}'.format(round(speed*1000), round(lateral*1000), round(turn*1000))
            else:
                arg = '-'
            self.ui_until = time.monotonic()+UI_LEASE_S
            if action in ('take', 'arm'):
                self.recovery_required = False  # Only a fresh, explicit manual request can clear this latch.
                self.last_ui_heartbeat = 0.
            if action in ('take', 'reset', 'avoid', 'release'):
                self.held_key = ''
            self.queue.append((mapping[action], arg, time.monotonic()+ACTION_QUEUE_S))

    def submit_navigation_intent(self, key, command_id, duration_s):
        """Start one bounded automatic intent; the scheduler renews frames only
        until this fixed deadline.  It never turns a navigation request into an
        unbounded heartbeat stream.
        """
        if key not in ('w', 's', 'j', 'l', 'z', 'c'):
            raise ValueError('无效导航方向')
        if not isinstance(command_id, str) or not command_id:
            raise ValueError('无效导航命令 ID')
        duration_s = float(duration_s)
        if not .05 <= duration_s <= 2.0:
            raise ValueError('导航动作有效期必须在 0.05–2.0 秒内')
        with self.lock:
            now = time.monotonic()
            if not self.connected or now-self.received_at > NETWORK_TIMEOUT_S:
                raise ValueError('连接未就绪或状态已过期')
            if self.held_key and now < self.ui_until:
                raise ValueError('人工输入仍在控制；导航不能抢占')
            self.navigation_intent = {
                'key': key, 'command_id': command_id,
                'expires_at': now + duration_s, 'duration_s': duration_s,
                'created_at_s': now,
            }
            self.last_navigation_command_id = command_id
            self.navigation_hold = True
            self.navigation_feedback = self._blank_navigation_feedback()
            self.navigation_feedback.update({
                'command_id': command_id,
                'command_created_time_s': now,
                'intent_accepted_time_s': now,
            })
            self.command_audit.append({
                'event': 'intent_accepted', 'command_id': command_id,
                'time_s': now,
            })
            self.queue.clear()
            self.stop_only = False

    def clear_navigation_intent(self, reason='navigation_stop', preserve_arm=False):
        with self.lock:
            had_intent = self.navigation_intent is not None
            self.navigation_intent = None
            self.navigation_hold = bool(preserve_arm)
            self.held_key = ''
            self.stop_only = True
            self.queue.clear()
            if had_intent:
                self.log('导航运动意图清除：'+str(reason))
            return had_intent

    def snapshot(self):
        with self.lock:
            age = (time.monotonic()-self.received_at)*1000 if self.received_at else None
            fresh = self.connected and age is not None and age <= NETWORK_TIMEOUT_S*1000
            stage = 'online' if fresh else 'stale' if self.connected else self.connection_stage
            return {'connected': self.connected, 'connecting': self.connecting,
                    'connection_stage': stage, 'robot_fresh': fresh,
                    'address': str(self.config['host'])+':'+str(self.config['port']),
                    'simulation': self.simulation, 'telemetry_age_ms': age, 'rtt_ms': self.rtt_ms,
                    'network_timeout_ms': int(NETWORK_TIMEOUT_S*1000),
                    'degraded': self.connected and (age is None or age > NETWORK_TIMEOUT_S*1000),
                    'recovery_required': self.recovery_required, 'disconnect_reason': self.disconnect_reason,
                    'ui_lease_active': time.monotonic() < self.ui_until,
                    'navigation_intent_active': self.navigation_intent is not None,
                    'navigation_hold': self.navigation_hold,
                    'navigation_command_id': ((self.navigation_intent or {}).get('command_id')
                                              or self.last_navigation_command_id),
                    'navigation_intent_remaining_s': (max(0., self.navigation_intent['expires_at'] - time.monotonic())
                                                      if self.navigation_intent else 0.),
                    'navigation_intent_expirations': self.navigation_intent_expirations,
                    'status_received_time_s': self.received_at or None,
                    'navigation_feedback': dict(self.navigation_feedback),
                    'command_audit': list(self.command_audit),
                    'input_diagnostics': {'heartbeat_age_ms': (time.monotonic()-self.last_ui_heartbeat)*1000 if self.last_ui_heartbeat else None,
                                          'max_heartbeat_gap_ms': self.max_ui_gap_ms, 'ui_expirations': self.ui_expirations},
                    'robot': dict(self.robot) if fresh else {}, 'events': list(self.events),
                    'history': {'last_fault': dict(self.last_fault) if self.last_fault else None,
                                'last_disconnect': dict(self.last_disconnect) if self.last_disconnect else None,
                                'last_network_gap': dict(self.last_network_gap) if self.last_network_gap else None}}

    def _accept_status(self, state):
        """Caller holds lock. Store observations separately from current control authority."""
        previous_reason = self.robot.get('fault_reason') if self.robot.get('fault') else None
        previous_session = self.robot.get('session')
        previous_stops = self.robot.get('motion_stop_count', 0) if previous_session == state.get('session') else 0
        self.robot = state
        self.received_at = time.monotonic()
        self.connected = state.get('authenticated') is True
        self.connecting = not self.connected
        self.connection_stage = 'online' if self.connected else 'authenticating'
        if self.connected and state.get('motion_stop_count', 0) > previous_stops:
            self.log('运动目标归零：{}；指令间隔 {} ms；TTL 中断累计 {}'.format(
                state.get('last_stop_reason', 'UNKNOWN'), state.get('move_gap_ms', '?'), state.get('ttl_expirations', '?')))
        reason = state.get('fault_reason') if state.get('fault') else None
        if self.connected and reason and (not self.last_fault or previous_reason != reason or previous_session != state.get('session')):
            self.last_fault = {'time': time.strftime('%H:%M:%S'), 'reason': reason}
        if self.connected and not state.get('control_active') and not state.get('stopping'):
            self.recovery_required = False  # No SDK session can resume; a new TAKE and ARM remain mandatory.

    def _expire_state(self, now):
        """Stop authority on stale status; an idle monitoring gap must not latch an arm warning."""
        stale = self.connected and now-self.received_at > NETWORK_TIMEOUT_S
        if stale:
            had_authority = bool(self.robot.get('control_active') or self.robot.get('armed') or self.held_key or
                                  self.navigation_intent or any(op in ('TAKE', 'ARM') for op, _, _ in self.queue))
            self.held_key = ''
            self.ui_until = 0
            self.queue.clear()
            self.navigation_intent = None
            self.navigation_hold = False
            self.stop_only = True
            self.recovery_required = self.recovery_required or had_authority
            if not self.degraded:
                self.force_stop = True
                self.last_network_gap = {'time': time.strftime('%H:%M:%S'), 'age_ms': round((now-self.received_at)*1000)}
                self.log('连续 3 秒无状态反馈：停止续发；历史记录不代表当前故障')
        self.degraded = stale

    def _clear_connection(self):
        """Clear all live fields; preserve only explicitly labelled diagnostic history."""
        self.socket = None
        self.connected = self.connecting = False
        self.connection_stage = 'offline'
        self.robot = {}
        self.received_at = 0.
        self.rtt_ms = None
        self.held_key = ''
        self.ui_until = 0
        self.last_ui_heartbeat = 0.
        self.queue.clear()
        self.navigation_intent = None
        self.navigation_hold = False
        self.last_navigation_command_id = None
        self.sent.clear()
        self.sent_meta.clear()
        self.navigation_feedback = self._blank_navigation_feedback()
        self.receiver_clock_offset_s = None
        self.receiver_clock_uncertainty_s = None
        self.force_stop = True
        self.stop_only = False
        self.degraded = self.recovery_required = False
        if self.disconnect_reason:
            self.last_disconnect = {'time': time.strftime('%H:%M:%S'), 'reason': self.disconnect_reason}

    def close(self):
        self.submit('disconnect')
        if self.thread:
            self.thread.join(3)

    def _frame(self, op, arg, ui_active, navigation_command_id=None):
        now = time.monotonic()
        self.sequence += 1
        sequence = self.sequence
        self.sent[sequence] = now
        self.sent = {k: v for k, v in self.sent.items() if now-v < NETWORK_TIMEOUT_S*2}
        self.sent_meta[sequence] = {
            'op': op, 'command_id': navigation_command_id,
            'created_time_s': now,
        }
        self.sent_meta = {k: v for k, v in self.sent_meta.items()
                          if now-v['created_time_s'] < NETWORK_TIMEOUT_S*2}
        self.command_audit.append({
            'event': 'transport_frame_created', 'op': op,
            'send_sequence': sequence, 'command_id': navigation_command_id,
            'time_s': now,
        })
        if op == 'MOVE' and navigation_command_id:
            feedback = self.navigation_feedback
            if feedback.get('command_id') == navigation_command_id:
                feedback['move_send_sequence'] = sequence
                feedback['move_sent_time_s'] = now
                self.command_audit.append({
                    'event': 'move_sent', 'command_id': navigation_command_id,
                    'send_sequence': sequence, 'time_s': now,
                })
        return ('G1R1|{token}|{session}|{seq}|{tick}|{op}|{arg}|{ui}\n'.format(
            token=self.config['token'], session=self.robot['session'], seq=self.sequence,
            tick=self.robot['tick'], op=op, arg=arg, ui=int(ui_active))).encode('ascii')

    def _observe_navigation_feedback(self, state, observed_at_s):
        """Join receiver feedback to the exact MOVE sequence that was sent.

        The HTTP snapshot time is only the PC observation time. It is never
        used as a motion or stop event. Receiver ``motion_command_seq`` and
        the echoed transport sequence establish the causal link first.
        """
        feedback = self.navigation_feedback
        receiver_time_s = (state or {}).get('receiver_time_s')
        if isinstance(receiver_time_s, (int, float)) and receiver_time_s >= 0:
            sample_offset = observed_at_s - float(receiver_time_s)
            if self.receiver_clock_offset_s is None:
                self.receiver_clock_uncertainty_s = .15
            else:
                self.receiver_clock_uncertainty_s = max(
                    .05, float(self.rtt_ms or 0) / 2000,
                    abs(sample_offset - self.receiver_clock_offset_s) + .02)
            self.receiver_clock_offset_s = sample_offset
            feedback['clock_offset_s'] = self.receiver_clock_offset_s
            feedback['time_uncertainty_s'] = self.receiver_clock_uncertainty_s
        send_sequence = feedback.get('move_send_sequence')
        if send_sequence is None:
            return
        ack_seq = state.get('ack_seq')
        robot = state or {}
        receiver_sequence = robot.get('motion_command_seq')
        if ack_seq == send_sequence:
            feedback['receiver_ack_sequence'] = ack_seq
            feedback['receiver_ack_time_s'] = observed_at_s
            self.command_audit.append({
                'event': 'receiver_ack', 'command_id': feedback.get('command_id'),
                'send_sequence': send_sequence, 'time_s': observed_at_s,
            })
        if receiver_sequence != send_sequence:
            return
        feedback['receiver_command_sequence'] = receiver_sequence
        feedback['receiver_clock_id'] = 'receiver:' + str(state.get('session', ''))
        feedback['receiver_time_s'] = robot.get('receiver_time_s')
        feedback['causal_feedback'] = feedback.get('receiver_ack_sequence') == send_sequence
        if (robot.get('motion_executing') is True
                and feedback.get('motion_started_time_s') is None):
            started = robot.get('motion_started_at_s')
            feedback['receiver_motion_started_time_s'] = started
            if (isinstance(started, (int, float)) and started >= 0
                    and self.receiver_clock_offset_s is not None):
                feedback['motion_started_time_s'] = float(started) + self.receiver_clock_offset_s
                feedback['feedback_clock_id'] = 'pc_monotonic'
                feedback['time_uncertainty_s'] = self.receiver_clock_uncertainty_s
            self.command_audit.append({
                'event': 'motion_started', 'command_id': feedback.get('command_id'),
                'send_sequence': send_sequence, 'time_s': observed_at_s,
            })
        if (robot.get('motion_stopped') is True
                and feedback.get('motion_started_time_s') is not None
                and feedback.get('motion_stopped_time_s') is None):
            stopped = robot.get('motion_stopped_at_s')
            feedback['receiver_motion_stopped_time_s'] = stopped
            if (isinstance(stopped, (int, float)) and stopped >= 0
                    and self.receiver_clock_offset_s is not None):
                feedback['motion_stopped_time_s'] = float(stopped) + self.receiver_clock_offset_s
                feedback['feedback_clock_id'] = 'pc_monotonic'
                feedback['time_uncertainty_s'] = self.receiver_clock_uncertainty_s
            self.command_audit.append({
                'event': 'motion_stopped', 'command_id': feedback.get('command_id'),
                'send_sequence': send_sequence, 'time_s': observed_at_s,
            })
        feedback['status_received_time_s'] = observed_at_s

    def _run(self):
        connection = None
        try:
            connection = socket.create_connection((self.config['host'], int(self.config['port'])), timeout=NETWORK_TIMEOUT_S)
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            connection.settimeout(.25)
            with self.lock:
                self.socket = connection
                self.connection_stage = 'authenticating'
                self.sequence = 0
                self.sent.clear()
            buffer = b''
            authed_sent = False
            last_send = 0.
            started = time.monotonic()
            while not self.stopping.is_set():
                if select.select([connection], [], [], .01)[0]:
                    data = connection.recv(8192)
                    if not data:
                        raise ConnectionError('机器狗接收端已断开；若刚连接就断开，请检查是否有另一电脑服务占用')
                    buffer += data
                    if len(buffer) > 16384:
                        raise ConnectionError('状态帧超过长度限制')
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        state = json.loads(line)
                        if state.get('protocol') != 1 or not re.fullmatch('[0-9a-f]{16}', state.get('session', '')):
                            raise ConnectionError('状态协议错误')
                        if bool(state.get('simulated')) != self.simulation:
                            raise ConnectionError('模拟/实机模式不匹配，已拒绝连接')
                        with self.lock:
                            if self.robot and state['session'] != self.robot['session']:
                                raise ConnectionError('接收端会话已改变')
                            self._accept_status(state)
                            self._observe_navigation_feedback(state, time.monotonic())
                            if state.get('ack_seq') in self.sent:
                                self.rtt_ms = (time.monotonic()-self.sent.pop(state['ack_seq']))*1000
                            if not authed_sent:
                                connection.sendall(self._frame('AUTH', '-', False))
                                authed_sent = True
                            if self.connected and not any(e['message'] == '网络已连接；尚未自动接管或解锁' for e in self.events):
                                self.log('网络已连接；尚未自动接管或解锁')
                now = time.monotonic()
                with self.lock:
                    if not self.connected and now-started > NETWORK_TIMEOUT_S:
                        raise ConnectionError('认证超时或配对密钥错误')
                    self._expire_state(now)
                    if self.connected and now-self.received_at > NETWORK_TIMEOUT_S:
                        # Stop is independent of receive freshness; never resend a queued MOVE on exit.
                        connection.sendall(self._frame('DISARM', '-', False))
                        raise ConnectionError('机器狗状态链路连续 3 秒无反馈；连接已关闭，需手动重连')
                    if self.connected and now-last_send >= NAVIGATION_SCHEDULER_PERIOD_S:
                        ui_active = now < self.ui_until or self.navigation_hold
                        navigation_intent = self.navigation_intent
                        if navigation_intent and now >= navigation_intent['expires_at']:
                            self.navigation_intent = None
                            self.navigation_hold = False
                            self.navigation_intent_expirations += 1
                            self.stop_only = True
                            navigation_intent = None
                            self.log('导航动作有效期结束：停止续发')
                        if self.force_stop or (not ui_active and self.held_key):
                            if not ui_active and self.held_key:
                                self.ui_expirations += 1
                                self.log('浏览器按键心跳已失效：请求解除解锁；不是运动时长上限')
                            self.held_key = ''
                            if not ui_active:
                                self.queue.clear()
                            op, arg = 'DISARM', '-'
                            self.force_stop = False
                            self.stop_only = False
                        elif self.stop_only:
                            op, arg = 'STOP', '-'
                            self.stop_only = False
                        elif self.queue:
                            op, arg, deadline = self.queue.popleft()
                            if now > deadline or not ui_active:
                                op, arg = 'DISARM', '-'
                        elif navigation_intent:
                            # Automatic navigation has its own intent deadline,
                            # while the receiver still enforces its 600 ms TTL.
                            op, arg = 'MOVE', '{}@{}'.format(
                                navigation_intent['key'], navigation_intent['command_id'])
                            ui_active = True
                        elif ui_active and self.held_key:
                            op, arg = 'MOVE', self.held_key
                        else:
                            op, arg = 'PING', '-'
                        navigation_command_id = (navigation_intent or {}).get('command_id') \
                            if op == 'MOVE' else None
                        connection.sendall(self._frame(
                            op, arg, ui_active,
                            navigation_command_id=navigation_command_id))
                        last_send = now
            with self.lock:
                if self.connected and self.robot:
                    try:
                        connection.sendall(self._frame('RELEASE', '-', False))
                    except OSError:
                        pass
        except (OSError, ValueError, KeyError, ConnectionError) as error:
            with self.lock:
                if isinstance(error, (socket.timeout, TimeoutError)):
                    self.disconnect_reason = '无法连接接收端：连接超时。请确认机器狗已开机并连接其热点；关机或充电期间不要接管。'
                elif isinstance(error, ConnectionRefusedError):
                    self.disconnect_reason = '接收端拒绝连接：可能未开机、服务未监听或地址不可达。不是已确认的机器人控制故障。'
                elif isinstance(error, ConnectionResetError):
                    self.disconnect_reason = '连接被远端重置：可能关机、链路中断或服务退出；具体原因需恢复连接后检查。'
                else:
                    self.disconnect_reason = str(error)
                self.log('连接结束：'+str(error))
        finally:
            if connection:
                connection.close()
            with self.lock:
                self._clear_connection()
