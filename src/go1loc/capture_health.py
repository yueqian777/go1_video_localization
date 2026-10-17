"""Conservative PC-side content observations, NEVER sensor exposure claims."""
import hashlib
import math
import time


class FrameHealth:
    def __init__(self, freeze_after=3.):
        if not math.isfinite(freeze_after) or freeze_after <= 0:
            raise ValueError('invalid_freeze_warning_budget')
        self.freeze_after = freeze_after
        self.reset()

    def reset(self):
        self.digest = self.changed_at = None
        self.seen_change = False
        self.status = 'waiting'
        self.unchanged_s = 0.

    def observe(self, frame, *, now=None):
        now = time.monotonic() if now is None else now
        digest = hashlib.sha256(frame.payload).digest()
        if digest != self.digest:
            self.seen_change = self.digest is not None
            self.digest, self.changed_at = digest, now
        self.unchanged_s = max(0., now - self.changed_at)
        self.status = ('suspected_frozen' if self.unchanged_s >= self.freeze_after else
                       'changing' if self.seen_change else 'unverified')
        return self.view()

    def view(self):
        return {'content_status': self.status, 'unchanged_s': self.unchanged_s,
                'freeze_after_s': self.freeze_after, 'sensor_freshness': 'unverified',
                'method': 'pc_monotonic_payload_sha256',
                'limitation': 'identical_pixels_may_be_a_static_scene_not_proof_of_sensor_failure'}
