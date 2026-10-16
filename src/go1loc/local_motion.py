"""Capability contract. No sockets, SSH, standing constructor or direct UDP.

The currently audited go1_hri profile is straight-only. A verified adapter can
be injected in a separately authorized test; this package supplies none.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class MotionCapabilities:
    adapter_id: str = 'go1_hri_audited_not_connected'
    velocity_interface: str = 'existing_go1_hri_gateway_not_direct_UDP'
    yaw_rate_range_rad_s: tuple = (0., 0.)
    lateral_speed_range_m_s: tuple = (0., 0.)
    current_gait_verified: int | None = None
    verified_velocity: bool = False
    verified_turning: bool = False
    accepts_local_goal: bool = False
    state_feedback_fresh: bool = False
    single_command_arbiter_verified: bool = False
    avoidance_requested: bool = False
    avoidance_verified: bool = False
    avoidance_evidence: tuple = ()
    stops: bool = False
    slows: bool = False
    detours: bool = False
    front_range_verified: bool = False
    side_range_verified: bool = False


def execution_gate(capabilities, *, mode, live, metric_scale, camera_mount_verified,
                   up_verified, current_authorization, fresh_observation, local_approach_verified,
                   task_state=None):
    conditions=dict(supervised_mode=mode=='supervised_execution', live=live,
        metric_scale=metric_scale,camera_mount_verified=camera_mount_verified,up_verified=up_verified,
        explicit_current_authorization=current_authorization,fresh_observation=fresh_observation,
        local_approach_verified=local_approach_verified,velocity=capabilities.verified_velocity,
        turning=capabilities.verified_turning,feedback=capabilities.state_feedback_fresh,
        single_arbiter=capabilities.single_command_arbiter_verified,
        avoidance=capabilities.avoidance_verified and bool(capabilities.avoidance_evidence),
        stop=capabilities.stops,front_range=capabilities.front_range_verified,
        side_range=capabilities.side_range_verified, active_task=task_state in ('following','turning'))
    return dict(execution_permitted=all(conditions.values()),
                missing=[k for k,v in conditions.items() if not v],conditions=conditions)


class LocalMotionAdapter:
    def __init__(self):
        self.capabilities=MotionCapabilities()

    def submit(self, subgoal, gate):
        raise RuntimeError('No verified turning/avoidance adapter installed; shadow guidance only')

    def invalidate(self):
        # No transport exists, hence no old motion command can be refreshed.
        return {'command_sent':False,'reason':'shadow_adapter_has_no_transport'}
