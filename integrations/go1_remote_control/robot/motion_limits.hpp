#pragma once
// Project limits, not the robot manufacturer's full capability envelope.
namespace go1_remote { namespace limits {
constexpr double forward_max=.5;
constexpr double lateral_max=.4;
constexpr double yaw_max=.6;
constexpr double actual_yaw_max=.9;
// Tracking tolerance for an already-active, unchanged body-axis request only.
constexpr double steady_cross_linear_max=.15;
constexpr double steady_cross_yaw_max=.25;
} }
