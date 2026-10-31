#pragma once
// Do not turn the 3 s network tolerance into 3 s of blind motion.
namespace go1_remote { namespace timing {
constexpr double network_timeout_s=3.0;
constexpr double echo_timeout_s=3.0;
constexpr double ui_lease_s=.75;
// 10 Hz status: retain more than the entire echo-validity window.
constexpr unsigned int tick_history=64;
} }
