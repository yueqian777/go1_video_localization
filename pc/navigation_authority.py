"""Bounded navigation ownership layered on the existing RobotLink.

The navigation scheduler owns a finite motion intent. RobotLink renews wire
frames at its normal 20 Hz cadence only until that intent deadline; the
receiver still enforces its independent short MOVE TTL. A paused task keeps
its task lease but has no non-zero intent and must submit a new command after
resume.
"""
from collections import deque
import re
import threading
import time

from timeouts import (
    NAVIGATION_INTENT_MAX_S,
    NAVIGATION_FEEDBACK_VERSION,
    NAVIGATION_RESUME_WINDOW_S,
    NAVIGATION_SCHEDULER_PERIOD_S,
    NAVIGATION_TASK_LEASE_S,
    RECEIVER_MOVE_TTL_S,
)


KEYS = {
    'forward': 'w', 'backward': 's', 'strafe_left': 'j',
    'strafe_right': 'l', 'turn_left': 'z', 'turn_right': 'c',
}
SAFE_PRIMITIVES = set(KEYS)
TASK_RE = re.compile(r'[A-Za-z0-9_.:-]{1,96}\Z')
COMMAND_RE = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')


class NavigationAuthority:
    def __init__(self, app, token, *, lease_s=NAVIGATION_TASK_LEASE_S,
                 primitive_ttl_s=NAVIGATION_INTENT_MAX_S,
                 resume_window_s=NAVIGATION_RESUME_WINDOW_S):
        self.app = app
        self.token = token if isinstance(token, str) else ''
        self.lease_s = float(lease_s)
        self.primitive_ttl_s = float(primitive_ttl_s)
        self.resume_window_s = float(resume_window_s)
        self.task_id = ''
        self.lease_until = 0.
        self.paused = False
        self.last_command_id = ''
        self.active_primitive = ''
        self.revocation_reason = ''
        self.used_command_ids = deque(maxlen=512)
        self._closed = threading.Event()
        self._watchdog = threading.Thread(target=self._watchdog_loop,
                                           name='go1-navigation-watchdog', daemon=True)
        self._watchdog.start()

    def close(self):
        self._closed.set()
        self._watchdog.join(max(.2, NAVIGATION_SCHEDULER_PERIOD_S * 4))

    def _watchdog_loop(self):
        while not self._closed.wait(NAVIGATION_SCHEDULER_PERIOD_S):
            with self.app.lock:
                self._expire_locked()

    def _expire_locked(self, now=None):
        now = time.monotonic() if now is None else now
        if self.task_id and now >= self.lease_until:
            self._stop_intent_locked('navigation_lease_expired')
            self.revocation_reason = 'navigation_lease_expired'
            self._clear_task_locked()

    def _require_token(self, token):
        if not self.token or not isinstance(token, str) or token != self.token:
            raise PermissionError('navigation_token_rejected')

    def _require_task_id(self, task_id):
        if not isinstance(task_id, str) or not TASK_RE.fullmatch(task_id):
            raise ValueError('navigation_task_id_invalid')

    def _require_active_task(self, task_id):
        if task_id != self.task_id:
            raise PermissionError('navigation_lease_invalid')
        if self.paused:
            raise PermissionError('navigation_task_paused')

    def _ready_for_lease_renewal(self):
        """Check authority health while a legal primitive may be moving."""
        state = self.app.link.snapshot()
        robot = state.get('robot') or {}
        if (state.get('connected') is not True or state.get('robot_fresh') is not True
                or state.get('recovery_required') is True
                or robot.get('control_active') is not True or robot.get('armed') is not True
                or robot.get('health') not in (None, 'OK')
                or robot.get('fault') is True or robot.get('remote_active') is True
                or robot.get('stopping') is True):
            raise PermissionError('navigation_control_session_not_ready')
        self._require_feedback_capabilities(robot)

    def _require_feedback_capabilities(self, robot):
        capabilities = robot.get('capabilities') or []
        required = {'motion_stopped', 'motion_executing', 'actual_yaw_rate',
                    'motion_command_seq', 'receiver_clock_id'}
        if (robot.get('navigation_feedback_version') != NAVIGATION_FEEDBACK_VERSION
                or not required.issubset(set(capabilities))):
            raise PermissionError('navigation_feedback_capabilities_required')

    def _ready_for_new_motion(self):
        """Starting or switching an intent requires a verified stop."""
        self._ready_for_lease_renewal()
        state = self.app.link.snapshot()
        robot = state.get('robot') or {}
        self._require_feedback_capabilities(robot)
        if robot.get('motion_stopped') is not True:
            raise PermissionError('navigation_motion_not_stopped')
        if state.get('navigation_intent_active'):
            feedback = state.get('navigation_feedback') or {}
            if not (feedback.get('motion_started_time_s') is not None
                    and feedback.get('motion_stopped_time_s') is not None):
                raise PermissionError('navigation_previous_motion_not_confirmed_stopped')

    def _stop_intent_locked(self, reason, preserve_arm=False):
        clear = getattr(self.app.link, 'clear_navigation_intent', None)
        if clear is not None:
            clear(reason, preserve_arm=preserve_arm)
        try:
            self.app.link.submit('stop')
            if preserve_arm and hasattr(self.app.link, 'navigation_hold'):
                self.app.link.navigation_hold = True
        except (ValueError, OSError):
            pass

    def _clear_task_locked(self):
        self.task_id = ''
        self.lease_until = 0.
        self.paused = False
        self.last_command_id = ''
        self.active_primitive = ''
        self.used_command_ids.clear()
        if self.app.owner == '__navigation__':
            self.app.owner = ''
            self.app.owner_until = 0.

    def authorize(self, task_id, token):
        with self.app.lock:
            self._expire_locked()
            self._require_token(token)
            self._require_task_id(task_id)
            if self.task_id:
                if task_id != self.task_id:
                    raise PermissionError('navigation_authority_conflict')
                self._ready_for_lease_renewal()
                self.lease_until = time.monotonic() + (self.resume_window_s if self.paused else self.lease_s)
                self.app.owner_until = self.lease_until
                return self.snapshot()
            if self.app.owner and self.app.owner != '__navigation__' and time.monotonic() < self.app.owner_until:
                raise PermissionError('navigation_authority_conflict')
            self._ready_for_lease_renewal()
            self._require_feedback_capabilities(self.app.link.snapshot().get('robot') or {})
            if (self.app.link.snapshot().get('robot') or {}).get('motion_stopped') is not True:
                raise PermissionError('navigation_motion_not_stopped')
            self.task_id = task_id
            self.lease_until = time.monotonic() + self.lease_s
            self.paused = False
            self.last_command_id = ''
            self.active_primitive = ''
            self.used_command_ids.clear()
            self.revocation_reason = ''
            self.app.owner = '__navigation__'
            self.app.owner_until = self.lease_until
            return self.snapshot()

    def command(self, task_id, token, command_id, primitive, ttl_s=None):
        with self.app.lock:
            now = time.monotonic()
            self._expire_locked(now)
            self._require_token(token)
            self._require_active_task(task_id)
            if not isinstance(command_id, str) or not COMMAND_RE.fullmatch(command_id):
                raise ValueError('navigation_command_id_invalid')
            if command_id in self.used_command_ids:
                raise ValueError('navigation_duplicate_command_id')
            if primitive not in SAFE_PRIMITIVES:
                raise ValueError('navigation_primitive_invalid')
            self._ready_for_new_motion()
            duration = self.primitive_ttl_s if ttl_s is None else float(ttl_s)
            if not .05 <= duration <= self.primitive_ttl_s:
                raise ValueError('navigation_primitive_ttl_invalid')
            submit_intent = getattr(self.app.link, 'submit_navigation_intent', None)
            if submit_intent is None:
                raise PermissionError('navigation_scheduler_required')
            submit_intent(KEYS[primitive], command_id, duration)
            self.used_command_ids.append(command_id)
            self.last_command_id = command_id
            self.active_primitive = primitive
            self.lease_until = now + self.lease_s
            self.app.owner_until = self.lease_until
            return {'accepted': True, 'task_id': task_id, 'command_id': command_id,
                    'primitive': primitive, 'intent_expires_in_s': duration,
                    'receiver_move_ttl_s': RECEIVER_MOVE_TTL_S}

    def keepalive(self, task_id, token):
        with self.app.lock:
            self._expire_locked()
            self._require_token(token)
            if task_id != self.task_id:
                raise PermissionError('navigation_lease_invalid')
            if self.app.owner != '__navigation__':
                raise PermissionError('navigation_authority_revoked')
            self._ready_for_lease_renewal()
            if self.paused:
                self.lease_until = time.monotonic() + self.resume_window_s
            else:
                self.lease_until = time.monotonic() + self.lease_s
            self.app.owner_until = self.lease_until
            return self.snapshot()

    def pause(self, task_id, token, reason='visual_wait'):
        with self.app.lock:
            self._expire_locked()
            self._require_token(token)
            self._require_active_task(task_id)
            self._stop_intent_locked(reason, preserve_arm=True)
            self.paused = True
            self.active_primitive = ''
            self.lease_until = time.monotonic() + self.resume_window_s
            self.app.owner_until = self.lease_until
            return {'paused': True, 'task_id': task_id, 'reason': reason,
                    'resume_window_s': self.resume_window_s}

    def resume(self, task_id, token):
        with self.app.lock:
            self._expire_locked()
            self._require_token(token)
            if task_id != self.task_id:
                raise PermissionError('navigation_lease_invalid')
            if not self.paused:
                return self.snapshot()
            self._ready_for_lease_renewal()
            self.paused = False
            self.active_primitive = ''
            self.lease_until = time.monotonic() + self.lease_s
            self.app.owner_until = self.lease_until
            return self.snapshot()

    def stop(self, task_id, token, reason='navigation_stop'):
        with self.app.lock:
            self._require_token(token)
            if task_id and task_id != self.task_id:
                raise PermissionError('navigation_lease_invalid')
            self._stop_intent_locked(reason)
            self._clear_task_locked()
            self.revocation_reason = reason
            return {'accepted': True, 'primitive': 'stop', 'reason': reason}

    def release(self, task_id, token):
        with self.app.lock:
            self._require_token(token)
            if task_id and task_id != self.task_id:
                raise PermissionError('navigation_lease_invalid')
            self._stop_intent_locked('navigation_release')
            try:
                self.app.link.submit('release')
            finally:
                self._clear_task_locked()
                self.revocation_reason = 'navigation_release'
            return {'accepted': True, 'released': True}

    def revoke_locked(self, reason):
        self._stop_intent_locked(reason)
        self._clear_task_locked()
        self.revocation_reason = reason

    def revoke(self, reason):
        with self.app.lock:
            self.revoke_locked(reason)

    def snapshot(self):
        self._expire_locked()
        remaining = max(0., self.lease_until - time.monotonic()) if self.task_id else 0.
        link = self.app.link.snapshot()
        return {'active': bool(self.task_id), 'task_id': self.task_id or None,
                'paused': self.paused, 'active_primitive': self.active_primitive or None,
                'last_command_id': self.last_command_id or None,
                'lease_remaining_s': remaining,
                'motion_intent_active': link.get('navigation_intent_active', False),
                'motion_intent_command_id': link.get('navigation_command_id'),
                'motion_intent_remaining_s': link.get('navigation_intent_remaining_s', 0.),
                'revocation_reason': self.revocation_reason or None}
