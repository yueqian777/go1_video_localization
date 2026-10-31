// Derived SDK adapter from 071f3b2; network demo axis limits updated 2026-10-18.
#pragma once
#include "../motion_limits.hpp"
#include "unitree_legged_sdk/udp.h"
#include <arpa/inet.h>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstring>
#include <fcntl.h>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <sys/file.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <thread>
#include <unistd.h>

namespace go1_demo {
using namespace UNITREE_LEGGED_SDK;
using Clock = std::chrono::steady_clock;
inline double seconds(Clock::time_point start) {
    return std::chrono::duration<double>(Clock::now()-start).count();
}

// Shared by the verified forward demo and keyboard program; Linux/Go1 SDK only.
class HighLevelSession {
    int lock_ = -1;
    int owned_socket_ = -1;
    struct stat socket_identity_{};
    std::unique_ptr<UDP> udp_;
    HighCmd command_{};
    unsigned long long rx_ = 0, crc_ = 0;
    bool yielded_ = false, stopped_ = false, walking_ = false;
    bool quiet_ = false;
    void close_udp() noexcept {
        udp_.reset();
        // This SDK's destructor shutdown()s but does not close() its fd. Verify inode identity
        // after destruction so an SDK version which does close it cannot make us close a reused fd.
        struct stat current{};
        if (owned_socket_>=0 && fstat(owned_socket_,&current)==0 && S_ISSOCK(current.st_mode) &&
            current.st_dev==socket_identity_.st_dev && current.st_ino==socket_identity_.st_ino) close(owned_socket_);
        owned_socket_=-1;
    }
public:
    HighState state{};
    bool has_state = false;
    Clock::time_point last_rx{};
    unsigned sends = 0;
    explicit HighLevelSession(bool fixture=false,bool quiet=false):quiet_(quiet) {
        const int port=fixture ? 24111 : 8111;
        const char* ip=fixture ? "127.0.0.1" : "192.168.123.161";
        const int target=fixture ? 24082 : 8082;
        const std::string path="/tmp/go1_keyboard_port_"+std::to_string(port)+".lock";
        lock_=open(path.c_str(),O_RDWR|O_CREAT|O_NOFOLLOW,0600);
        if (lock_<0 || flock(lock_,LOCK_EX|LOCK_NB)!=0) {
            if (lock_>=0) close(lock_);
            throw std::runtime_error("Another SDK demo owns the selected port");
        }
        try {
            if (sizeof(HighCmd)!=static_cast<size_t>(HIGH_CMD_LENGTH) ||
                sizeof(HighState)!=static_cast<size_t>(HIGH_STATE_LENGTH))
                throw std::runtime_error("SDK packet size mismatch");
            int fd=socket(AF_INET,SOCK_DGRAM,0);
            if (fd<0) throw std::runtime_error("Port-check socket failed");
            sockaddr_in address{}; address.sin_family=AF_INET; address.sin_port=htons(port);
            const int result=bind(fd,reinterpret_cast<sockaddr*>(&address),sizeof(address));
            const int error=errno; close(fd);
            if (result!=0) throw std::runtime_error("Local port unavailable: "+std::string(std::strerror(error)));
            udp_.reset(new UDP(HIGHLEVEL,port,ip,target));
            bool matched=false;
            for (int socket_fd=0;socket_fd<1024;++socket_fd) {
                sockaddr_in local{},peer{}; socklen_t a=sizeof(local),b=sizeof(peer);
                if (getsockname(socket_fd,reinterpret_cast<sockaddr*>(&local),&a)==0 &&
                    getpeername(socket_fd,reinterpret_cast<sockaddr*>(&peer),&b)==0 &&
                    local.sin_family==AF_INET && ntohs(local.sin_port)==port &&
                    peer.sin_addr.s_addr==inet_addr(ip) && ntohs(peer.sin_port)==target) {
                    if (fstat(socket_fd,&socket_identity_)!=0) throw std::runtime_error("SDK socket identity check failed");
                    owned_socket_=socket_fd; matched=true;
                }
            }
            if (!matched) throw std::runtime_error("SDK bind failed; random-port fallback refused");
            udp_->InitCmdData(command_);
            if (!quiet_) std::cout << "TRANSPORT_OK local_port=" << port << " target=" << ip << ':' << target << std::endl;
        } catch (...) { close_udp(); close(lock_); lock_=-1; throw; }
    }
    void receive() {
        const int result=udp_->Recv();
        const auto count=udp_->udpState.RecvCount,errors=udp_->udpState.RecvCRCError;
        if (result>=0 && count!=rx_ && errors==crc_) {
            HighState next{}; udp_->GetRecv(next);
            if (next.head[0]==0xfe && next.head[1]==0xef && next.levelFlag==HIGHLEVEL) {
                state=next; has_state=true; last_rx=Clock::now();
            }
        }
        rx_=count; crc_=errors;
    }
    bool remote_active() const {
        if (!has_state) return false;
        const auto& wire=state.wirelessRemote;
        if (wire[2] || wire[3]) return true;
        for (int i=0;i<5;++i) {
            float value=0; std::memcpy(&value,wire.data()+4+4*i,4);
            if (!std::isfinite(value) || std::abs(value)>.10f) return true;
        }
        return false;
    }
    std::string health() const {
        if (!has_state || seconds(last_rx)>.15) return "STALE_STATE";
        if (remote_active()) return "REMOTE_INPUT";
        if (state.bms.bms_status==2 || state.bms.bms_status==3) return "CHARGING";
        if (state.mode>2) return "UNEXPECTED_MODE";
        if (state.bms.SOC<10 || state.bms.SOC>100) return "BATTERY_LIMIT";
        if (!std::isfinite(state.bodyHeight) || state.bodyHeight<.22f || state.bodyHeight>.36f) return "NOT_UPRIGHT";
        for (float angle:state.imu.rpy) if (!std::isfinite(angle)) return "INVALID_IMU";
        if (std::abs(state.imu.rpy[0])>.20f || std::abs(state.imu.rpy[1])>.20f) return "TILT_LIMIT";
        for (float v:state.velocity) if (!std::isfinite(v)) return "INVALID_SPEED";
        if (std::hypot(state.velocity[0],state.velocity[1])>.80f || !std::isfinite(state.yawSpeed) ||
            std::abs(state.yawSpeed)>go1_remote::limits::actual_yaw_max) return "SPEED_LIMIT";
        for (int i=0;i<12;++i)
            if (state.motorState[i].mode!=10 || state.motorState[i].temperature>=60) return "JOINT_LIMIT";
        return "OK";
    }
    void send(int mode,float vx,float vy=0,float yaw=0,bool avoidance=false) {
        const int axes=(vx!=0)+(vy!=0)+(yaw!=0);
        if (mode<0 || mode>2 || !std::isfinite(vx) || !std::isfinite(vy) || !std::isfinite(yaw) ||
            std::abs(vx)>static_cast<float>(go1_remote::limits::forward_max) ||
            std::abs(vy)>static_cast<float>(go1_remote::limits::lateral_max) ||
            std::abs(yaw)>static_cast<float>(go1_remote::limits::yaw_max) || axes>1 || (mode!=2 && axes!=0))
            throw std::runtime_error("Command outside demo limits");
        if (has_state && (state.bms.bms_status==2 || state.bms.bms_status==3) && mode!=0)
            throw std::runtime_error("CHARGING: stand/walk commands refused; disconnect charger before live use");
        command_.mode=mode;
        // Legged_sport 1.38.0 may generate avoidance velocities even at a zero target.
        // Never retain gait 4 in STOP, TTL expiry, fault, reversal, idle or exit packets.
        command_.gaitType=mode==2 ? (avoidance && axes!=0 ? 4 : 1) : 0;
        command_.speedLevel=0; command_.footRaiseHeight=0; command_.bodyHeight=0;
        command_.euler={{0,0,0}}; command_.velocity={{vx,vy}}; command_.yawSpeed=yaw; command_.reserve=0;
        if (udp_->SetSend(command_)<0 || udp_->Send()!=HIGH_CMD_LENGTH)
            throw std::runtime_error("SDK did not send a complete command packet");
        ++sends; stopped_=false;
        if (mode==2) walking_=true;
    }
    void log(const char* phase,int mode,float vx) const {
        std::cout << "phase=" << phase << " requested_mode=" << mode << " requested_vx=" << vx
                  << " feedback_mode=" << static_cast<int>(state.mode) << " actual_vx=" << state.velocity[0]
                  << " actual_vy=" << state.velocity[1] << " height=" << state.bodyHeight
                  << " SOC=" << static_cast<int>(state.bms.SOC) << " rx=" << rx_ << " sends=" << sends << std::endl;
    }
    void stop() noexcept {
        if (!udp_ || stopped_) return;
        try {
            const auto start=Clock::now();
            while (seconds(start)<1.0) {
                receive();
                if (remote_active() || (has_state && (state.mode>2 || state.bms.bms_status==2 || state.bms.bms_status==3))) {
                    send(0,0); yielded_=true; break;
                }
                send(walking_ && seconds(start)<.4 ? 2 : 1,0);
                std::this_thread::sleep_for(std::chrono::milliseconds(5));
            }
            if (!quiet_) log(yielded_ ? "CONTROL_RELEASE" : "STOP",yielded_ ? 0 : 1,0);
        } catch (...) { std::cerr << "STOP_SEND_FAILED; use the remote controller" << std::endl; }
        stopped_=true;
    }
    ~HighLevelSession() { if (!yielded_) stop(); close_udp(); if (lock_>=0) close(lock_); }
};
}
