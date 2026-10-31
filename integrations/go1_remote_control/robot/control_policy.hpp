#pragma once
// Derived from stable keyboard commit 071f3b2; legacy project remains unchanged.
#include "motion_limits.hpp"
#include <cmath>
#include <stdexcept>
#include <string>
namespace go1_remote {
inline void require(bool ok,const char* why) { if (!ok) throw std::runtime_error(why); }
struct Policy {
    bool armed=false,fault=false,avoidance=false;
    double vx=0,vy=0,yaw=0,expires=0,motion_since=-1,reverse_until=0;
    double speed=.5;
    double lateral_speed=.4,turn_speed=.6;
    double last_obstacle_key=-2;
    unsigned long long stop_count=0,ttl_expirations=0;
    std::string last_stop_reason="STARTUP";
    std::string fault_reason,disarm_reason="STARTUP";
    explicit Policy(double value=.5,double lateral=.4,double turn=.6):speed(value),lateral_speed(lateral),turn_speed(turn) {
        require(std::isfinite(speed) && speed>=.05 && speed<=limits::forward_max,"Forward speed outside 0.05..0.5 m/s");
        require(std::isfinite(lateral_speed) && lateral_speed>=.1 && lateral_speed<=limits::lateral_max,"Lateral speed outside 0.1..0.4 m/s");
        require(std::isfinite(turn_speed) && turn_speed>=.1 && turn_speed<=limits::yaw_max,"Yaw speed outside 0.1..0.6 rad/s");
    }
    bool moving() const { return vx!=0 || vy!=0 || yaw!=0; }
    void stop(const std::string& reason="USER_STOP") {
        if (moving()) { ++stop_count; last_stop_reason=reason; }
        vx=vy=yaw=0; expires=0; motion_since=-1;
    }
    void trip(const std::string& reason="UNHEALTHY_STATE") {
        if (!fault) fault_reason=reason;
        stop("FAULT:"+reason); armed=false; fault=true; avoidance=false; disarm_reason="FAULT";
    }
    void advance(double now,bool healthy,const std::string& reason="UNHEALTHY_STATE") {
        if (!healthy) { trip(reason); return; }
        // Held-key MOVE frames renew a short lease; there is no total-duration cap.
        if (moving() && now>=expires) { ++ttl_expirations; stop("COMMAND_TTL_EXPIRED"); }
    }
    std::string key(char key,double now,bool healthy,double actual_vx=0,bool stationary_ready=true,
                    double actual_vy=0,double actual_yaw=0) {
        advance(now,healthy);
        const bool valid=std::isfinite(actual_vx) && std::isfinite(actual_vy) && std::isfinite(actual_yaw);
        const bool settled=valid && std::hypot(actual_vx,actual_vy)<.05 && std::abs(actual_yaw)<.05;
        if (key==' ' || key=='x' || key=='X') { stop(); return "STOP_REQUESTED"; }
        if (key=='o' || key=='O') {
            // SSH has no key-up. Require a 1 s O-input gap so autorepeat cannot flap the switch.
            const bool repeated=now-last_obstacle_key<1.0;
            last_obstacle_key=now;
            if (repeated) return "";
            if (!healthy || fault || !stationary_ready || !settled || moving())
                return "AVOIDANCE_BLOCKED: stop, wait healthy stationary 500ms; clear fault first";
            stop(); armed=false; avoidance=!avoidance; disarm_reason="AVOIDANCE_CHANGED";
            return avoidance ? "AVOIDANCE_SELECTED: ON; idle gait remains normal; press A, then move to request gait 4" :
                               "AVOIDANCE_SELECTED: OFF; press A before moving";
        }
        if (key=='r' || key=='R') {
            if (!healthy || !stationary_ready || !settled || moving())
                return "RESET_BLOCKED: wait for healthy state and 500ms stationary";
            stop(); armed=false; fault=false; fault_reason.clear(); disarm_reason="RESET";
            return "RESET_OK: press A to arm; no motion resumes automatically";
        }
        if (key=='a' || key=='A') {
            if (fault) return "ARM_BLOCKED: fault="+fault_reason+"; stop, then R followed by A";
            if (!healthy || !stationary_ready || !settled || moving())
                return "ARM_BLOCKED: wait for healthy state and 500ms stationary";
            armed=true; disarm_reason.clear(); return "ARMED";
        }
        double request_x=0,request_y=0,request_yaw=0;
        switch (key) {
            case 'w': case 'W': request_x=speed; break;
            case 's': case 'S': request_x=-speed; break;
            case 'j': case 'J': request_y=lateral_speed; break;
            case 'l': case 'L': request_y=-lateral_speed; break;
            case 'z': case 'Z': request_yaw=turn_speed; break;
            case 'c': case 'C': request_yaw=-turn_speed; break;
            default: return "";
        }
        if (fault) return "MOVE_BLOCKED: fault latched; stop, then R followed by A";
        if (!healthy || !armed) return "MOVE_BLOCKED: press A when ready";
        if (now<reverse_until) return "REVERSAL_WAIT: stopping before direction change";
        // One body-frame axis per key, never accumulate stale perpendicular targets.
        const bool different_axis=(vx!=0 && request_x==0) || (vy!=0 && request_y==0) || (yaw!=0 && request_yaw==0);
        if (different_axis || vx*request_x<0 || vy*request_y<0 || yaw*request_yaw<0) {
            stop("DIRECTION_CHANGE"); reverse_until=now+.4; return "REVERSAL_WAIT: stopping before direction change";
        }
        if (!valid) { trip("INVALID_SPEED"); return "MOVE_BLOCKED: invalid feedback"; }
        const bool continuing=moving() && vx==request_x && vy==request_y && yaw==request_yaw;
        // Gait drift is not a new direction request. Ignore small cross-axis
        // residuals only while renewing the identical, already-active intent.
        // Requested-axis wrong-way motion and independent SDK health limits remain.
        const auto incompatible=[continuing](double actual,double request,double cross_limit) {
            if (request==0) return std::abs(actual)>=(continuing ? cross_limit : .05);
            return std::abs(actual)>=.05 && actual*request<0;
        };
        if (incompatible(actual_vx,request_x,limits::steady_cross_linear_max) ||
            incompatible(actual_vy,request_y,limits::steady_cross_linear_max) ||
            incompatible(actual_yaw,request_yaw,limits::steady_cross_yaw_max)) {
            stop("FEEDBACK_NOT_SETTLED"); reverse_until=now+.1;
            return "REVERSAL_WAIT: actual speed has not settled";
        }
        if (!moving()) motion_since=now;
        vx=request_x; vy=request_y; yaw=request_yaw; expires=now+.6;
        return "";
    }
};

// Runs on command intent, not console output cadence. Sensor freshness remains an immediate gate.
class MotionFeedbackGuard {
    bool active_=false,confirmed_=false;
    double start_=0,bad_since_=-1,direction_=0;
public:
    // Firmware mode transition is independent of the short MOVE renewal TTL.
    static constexpr double startup_timeout=1.50;
    static constexpr double loss_timeout=.30;
    void reset() { active_=false; confirmed_=false; bad_since_=-1; direction_=0; }
    std::string update(double now,double requested_vx,int feedback_mode,double requested_vy=0,double requested_yaw=0) {
        if (requested_vx==0 && requested_vy==0 && requested_yaw==0) { reset(); return ""; }
        const double direction=requested_vx!=0 ? (requested_vx>0 ? 1 : -1) :
            requested_vy!=0 ? (requested_vy>0 ? 2 : -2) : (requested_yaw>0 ? 3 : -3);
        if (!active_ || direction!=direction_) {
            active_=true; confirmed_=false; start_=now; bad_since_=-1; direction_=direction;
        }
        if (feedback_mode==2) { confirmed_=true; bad_since_=-1; return ""; }
        if (!confirmed_)
            return now-start_>=startup_timeout ? "MOTION_START_TIMEOUT" : "";
        if (bad_since_<0) bad_since_=now;
        return now-bad_since_>=loss_timeout ? "MOTION_MODE_LOST" : "";
    }
    const char* status() const {
        if (!active_) return "IDLE";
        if (!confirmed_) return "WAITING_ACK";
        return bad_since_<0 ? "ACKED" : "TRANSIENT_MODE";
    }
    double gap_ms(double now) const { return bad_since_<0 ? 0 : (now-bad_since_)*1000; }
    double waiting_ms(double now) const { return active_ && !confirmed_ ? (now-start_)*1000 : 0; }
};

// Gait feedback confirms firmware selection only, NOT obstacle perception or safe clearance.
class GaitFeedbackGuard {
    MotionFeedbackGuard feedback_;
    int expected_=0;
public:
    void reset() { feedback_.reset(); expected_=0; }
    std::string update(double now,bool moving,bool avoidance,int mode,int gait) {
        if (!moving) { reset(); return ""; }
        const int expected=avoidance ? 4 : 1;
        if (expected!=expected_) { feedback_.reset(); expected_=expected; }
        const auto reason=feedback_.update(now,1,mode==2 && gait==expected ? 2 : 1);
        if (reason=="MOTION_START_TIMEOUT") return "GAIT_START_TIMEOUT";
        if (reason=="MOTION_MODE_LOST") return "GAIT_MODE_LOST";
        return "";
    }
    const char* status() const { return feedback_.status(); }
};


}
