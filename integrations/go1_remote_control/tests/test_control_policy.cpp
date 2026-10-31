// Pure logical-time tests. No sockets, SDK or real-time robot commands.
#include "control_policy.hpp"
#include <iostream>

int main() {
    try {
        struct Direction { char key; double vx,vy,yaw; };
        const Direction cases[]={{'w',.5,0,0},{'s',-.5,0,0},{'j',0,.4,0},
                                 {'l',0,-.4,0},{'z',0,0,.6},{'c',0,0,-.6}};
        for (const auto& c:cases) {
            go1_remote::Policy policy;
            policy.key('a',100,true);
            double t=100;
            for (int i=1;i<=2400;++i) {
                t=100+i*.05;  // 120 seconds of held input, without waiting in wall time.
                policy.key(c.key,t,true);
                policy.advance(t+.025,true);
                go1_remote::require(policy.armed && !policy.fault,"Held input unexpectedly disarmed");
                go1_remote::require(policy.vx==c.vx && policy.vy==c.vy && policy.yaw==c.yaw,"Held axis changed");
                go1_remote::require(policy.motion_since==100.05,"Motion timer unexpectedly reset");
            }
            policy.advance(t+.61,true);
            go1_remote::require(!policy.moving() && policy.armed,"TTL must still stop a missing MOVE renewal");
            policy.key(c.key,t+1,true);
            policy.key('x',t+1.1,true);
            go1_remote::require(!policy.moving(),"STOP must still work after a long hold");
            policy.key(c.key,t+2,true);
            policy.advance(t+2.1,false,"STALE_STATE");
            go1_remote::require(!policy.moving() && !policy.armed && policy.fault,"Health fault must still stop/disarm");
        }
        // Normal gait tracking is not perfectly axis-aligned. Renewing the SAME
        // intent must not mistake small cross-axis drift for a direction change.
        for (const auto& c:cases) {
            go1_remote::Policy policy;
            policy.key('a',0,true);
            policy.key(c.key,.05,true);
            const auto message=policy.key(c.key,.1,true,c.vx!=0 ? c.vx*.9 : .08,false,
                c.vy!=0 ? c.vy*.9 : .08,c.yaw!=0 ? c.yaw*.9 : .08);
            if (!policy.moving()) std::cerr<<"SAME_INTENT_STUTTER key="<<c.key<<" reason="<<message<<'\n';
            go1_remote::require(policy.moving() && policy.armed,"Cross-axis gait drift wrongly stopped an unchanged held command");
        }
        {
            go1_remote::Policy wrong_way;
            wrong_way.key('a',0,true); wrong_way.key('w',.05,true);
            wrong_way.key('w',.1,true,-.1,false,0,0);
            go1_remote::require(!wrong_way.moving(),"Wrong-way motion must still stop");
            go1_remote::Policy large_drift;
            large_drift.key('a',0,true); large_drift.key('w',.05,true);
            large_drift.key('w',.1,true,.4,false,.2,0);
            go1_remote::require(!large_drift.moving(),"Large cross-axis drift must still stop");
            go1_remote::Policy change;
            change.key('a',0,true); change.key('w',.05,true);
            change.key('j',.1,true,.4,false,0,0);
            go1_remote::require(!change.moving() && change.last_stop_reason=="DIRECTION_CHANGE","Real axis change must still brake");
            change.key('j',.6,true,.2,false,0,0);
            go1_remote::require(!change.moving(),"Residual-speed guard must remain strict on a new axis");
        }
        {
            go1_remote::MotionFeedbackGuard guard;
            go1_remote::require(guard.update(0,.1,1).empty(),"Initial ACK must be pending");
            go1_remote::require(guard.update(.8,.1,1).empty(),"600 ms must not prematurely fail firmware startup");
            go1_remote::require(guard.update(1.1,.1,2).empty(),"Delayed firmware ACK must be accepted");
            go1_remote::require(guard.update(1.2,.1,1).empty(),"Brief mode glitch must remain tolerated");
            go1_remote::require(guard.update(1.6,.1,1)=="MOTION_MODE_LOST","Persistent mode loss must still fail");
            guard.reset(); guard.update(0,.1,1);
            go1_remote::require(guard.update(1.51,.1,1)=="MOTION_START_TIMEOUT","Missing firmware ACK must still fail at 1.5s");
        }
        std::cout<<"POLICY_HOLD_PASS six directions x 120 logical seconds; TTL, STOP, health, drift and delayed ACK guards verified\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr<<error.what()<<'\n'; return 1;
    }
}
