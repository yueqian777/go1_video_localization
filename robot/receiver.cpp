// Network front end for the stable keyboard policy. Starting the service never opens SDK control.
#include <stdexcept>
#include <cerrno>
#include <cstring>
#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
using Socket=SOCKET;
static constexpr Socket bad_socket=INVALID_SOCKET;
static void close_socket(Socket s) { if (s!=bad_socket) closesocket(s); }
static bool would_block() { return WSAGetLastError()==WSAEWOULDBLOCK; }
static void nonblocking(Socket s) { u_long v=1; if (ioctlsocket(s,FIONBIO,&v)) throw std::runtime_error("nonblocking socket failed"); }
#else
#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>
using Socket=int;
static constexpr Socket bad_socket=-1;
static void close_socket(Socket s) { if (s!=bad_socket) close(s); }
static bool would_block() { return errno==EAGAIN || errno==EWOULDBLOCK || errno==EINTR; }
static void nonblocking(Socket s) { if (fcntl(s,F_SETFL,fcntl(s,F_GETFL)|O_NONBLOCK)<0) throw std::runtime_error("nonblocking socket failed"); }
#endif
#include "control_policy.hpp"
#include "network_timing.hpp"
#ifndef GO1_SIM_ONLY
#include "legacy/go1_highlevel.h"
#endif
#include <algorithm>
#include <atomic>
#include <chrono>
#include <csignal>
#include <deque>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <random>
#include <sstream>
#include <thread>
#include <vector>

using namespace go1_remote;
using Clock=std::chrono::steady_clock;
static const auto epoch=Clock::now();
static double now_s() { return std::chrono::duration<double>(Clock::now()-epoch).count(); }
static volatile std::sig_atomic_t quitting=0;
static void quit_signal(int) { quitting=1; }
static std::string quoted(const std::string& s) {
    std::string out="\"";
    for (unsigned char c:s) {
        if (c=='\\' || c=='\"') { out+='\\'; out+=static_cast<char>(c); }
        else if (c>=32 && c<127) out+=static_cast<char>(c);
        else out+='?';
    }
    return out+'\"';
}
static std::string number(double n) { if (!std::isfinite(n)) return "null"; std::ostringstream s; s<<std::setprecision(6)<<n; return s.str(); }
static std::string nonce() { std::random_device rd; std::ostringstream s; for (int i=0;i<4;++i) s<<std::hex<<std::setw(4)<<std::setfill('0')<<(rd()&0xffff); return s.str(); }
static bool equal_secret(const std::string& a,const std::string& b) {
    if (a.size()!=b.size()) return false;
    unsigned difference=0; for (size_t i=0;i<a.size();++i) difference|=static_cast<unsigned char>(a[i]^b[i]); return difference==0;
}
static unsigned long long integer(const std::string& s) {
    require(!s.empty() && s.size()<=16,"invalid integer");
    for (char c:s) require(c>='0' && c<='9',"invalid integer");
    size_t used=0; const auto n=std::stoull(s,&used); require(used==s.size(),"invalid integer"); return n;
}
static std::vector<std::string> fields(const std::string& s,char delimiter='|') {
    std::vector<std::string> result; std::istringstream input(s); std::string part;
    while (std::getline(input,part,delimiter)) result.push_back(part);
    return result;
}
struct Sample {
    bool valid=false,remote=false; double stamp=0,vx=0,vy=0,yaw=0,height=.28,roll=0,pitch=0,temp=32;
    int soc=60,bms=1,mode=1,gait=0; std::string health="NO_SDK_CONTROL";
};
struct Backend {
    Sample state;
    virtual void start()=0;
    virtual void read()=0;
    virtual void send(int mode,float vx,float vy,float yaw,bool avoidance)=0;
    virtual void stop()=0;
    virtual ~Backend()=default;
};
class SimBackend:public Backend {
    int soc_; bool no_ack_,cross_drift_; double stale_after_,started_=0;
public:
    SimBackend(int soc,bool no_ack,double stale_after,bool cross_drift):soc_(soc),no_ack_(no_ack),cross_drift_(cross_drift),stale_after_(stale_after) {}
    void start() override { state=Sample{}; state.valid=true; state.soc=soc_; started_=now_s(); read(); }
    void read() override {
        const auto t=now_s();
        if (stale_after_<0 || t-started_<stale_after_) state.stamp=t;
        state.health=t-state.stamp>.15 ? "STALE_STATE" : state.soc<10 || state.soc>100 ? "BATTERY_LIMIT" : "OK";
    }
    void send(int mode,float vx,float vy,float yaw,bool avoidance) override {
        state.mode=no_ack_ ? 1 : mode; state.gait=mode==2 ? (avoidance && (vx!=0 || vy!=0 || yaw!=0) ? 4 : 1) : 0;
        state.vx=vx; state.vy=vy; state.yaw=yaw;
        if (cross_drift_ && (vx!=0 || vy!=0 || yaw!=0)) {
            if (vx==0) state.vx=.08;
            if (vy==0) state.vy=.08;
            if (yaw==0) state.yaw=.08;
        }
    }
    // Preserve the last sample on release; inactive telemetry ages out. No writer races with JSON snapshots.
    void stop() override {}
};
#ifndef GO1_SIM_ONLY
class SdkBackend:public Backend {
    std::unique_ptr<go1_demo::HighLevelSession> sdk_;
public:
    void start() override { sdk_.reset(new go1_demo::HighLevelSession(false,true)); state=Sample{}; }
    void read() override {
        sdk_->receive(); state.health=sdk_->health(); state.valid=sdk_->has_state;
        if (!state.valid) return;
        const auto& s=sdk_->state; state.stamp=now_s()-go1_demo::seconds(sdk_->last_rx);
        state.vx=s.velocity[0]; state.vy=s.velocity[1]; state.yaw=s.yawSpeed;
        state.height=s.bodyHeight; state.roll=s.imu.rpy[0]; state.pitch=s.imu.rpy[1];
        state.soc=s.bms.SOC; state.bms=s.bms.bms_status; state.mode=s.mode; state.gait=s.gaitType;
        state.remote=sdk_->remote_active(); state.temp=-128;
        for (int i=0;i<12;++i) state.temp=std::max(state.temp,static_cast<double>(s.motorState[i].temperature));
    }
    void send(int mode,float vx,float vy,float yaw,bool avoidance) override { sdk_->send(mode,vx,vy,yaw,avoidance); }
    void stop() override { if (sdk_) { sdk_->stop(); sdk_.reset(); } }
};
#endif
class Engine {
    std::unique_ptr<Backend> backend_; Policy policy_; MotionFeedbackGuard motion_; GaitFeedbackGuard gait_;
    bool was_moving_=false; double started_=0,good_since_=-1,stop_since_=-1;
    double last_move_rx_=-1,last_move_gap_=0,max_move_gap_=0;
    char last_move_key_=0;
    unsigned long long last_command_seq_=0,last_motion_command_seq_=0;
    std::string last_command_id_,last_motion_command_id_;
    double last_command_at_=-1,last_motion_started_at_=-1,last_motion_stopped_at_=-1;
    unsigned long long move_rx_count_=0,logged_stops_=0;
    std::atomic<bool> stopping_{false},stop_failed_{false};
    std::thread stop_thread_;
    void report_stop() {
        if (policy_.stop_count==logged_stops_) return;
        logged_stops_=policy_.stop_count;
        std::cout<<"MOTION_STOP reason="<<policy_.last_stop_reason<<" stops="<<policy_.stop_count
            <<" ttl_expirations="<<policy_.ttl_expirations<<" move_gap_ms="<<last_move_gap_*1000
            <<" health="<<backend_->state.health<<" actual_vx="<<backend_->state.vx
            <<" actual_vy="<<backend_->state.vy<<" actual_yaw="<<backend_->state.yaw<<std::endl;
    }
public:
    bool active=false; std::string message="MONITOR_ONLY: confirm STANDING before taking control";
    explicit Engine(std::unique_ptr<Backend> b):backend_(std::move(b)) {}
    void release(const std::string& reason) {
        policy_.stop(reason); report_stop(); policy_.armed=false; policy_.avoidance=false; active=false;
        last_move_rx_=-1; last_move_key_=0;
        message=reason; good_since_=-1; motion_.reset(); gait_.reset(); was_moving_=false;
        if (stopping_) return;
        if (stop_thread_.joinable()) stop_thread_.join();
        stopping_=true; stop_failed_=false;
        // SDK stop sends ~1s of zero frames. Keep the network/status loop responsive while it does so.
        stop_thread_=std::thread([this] { try { backend_->stop(); } catch (...) { stop_failed_=true; } stopping_=false; });
    }
    ~Engine() { if (active) release("SHUTDOWN"); if (stop_thread_.joinable()) stop_thread_.join(); }
    void action(const std::string& op,const std::string& arg,bool ui_active,double t,
                unsigned long long command_seq=0) {
        last_command_seq_=command_seq; last_command_at_=t;
        std::string command_id;
        std::string motion_arg=arg;
        if (op=="MOVE") {
            const auto marker=arg.find('@');
            if (marker!=std::string::npos) {
                require(marker==1 && arg.size()<=130,"Invalid navigation command envelope");
                command_id=arg.substr(marker+1);
                require(!command_id.empty(),"Missing navigation command ID");
                for (const char c:command_id) require((c>='A'&&c<='Z')||(c>='a'&&c<='z')||
                    (c>='0'&&c<='9')||c=='.'||c=='_'||c==':'||c=='-',"Invalid navigation command ID");
                motion_arg=arg.substr(0,marker);
            }
            last_command_id_=command_id;
        }
        if (op=="MOVE") {
            last_motion_command_seq_=command_seq; last_motion_command_id_=command_id;
        }
        if (op=="STOP" || op=="DISARM") {
            policy_.stop(op=="STOP" ? "USER_STOP" : "UI_DISARM"); report_stop();
            last_move_rx_=-1; last_move_key_=0;
            if (op=="DISARM") { policy_.armed=false; policy_.disarm_reason="UI_INACTIVE"; }
            message="STOP_REQUESTED"; return;
        }
        if (op=="RELEASE") { release("CONTROL_RELEASED"); return; }
        if (op=="PING") return;
        if (!ui_active) { message="UI_INACTIVE"; return; }
        if (op=="TAKE") {
            if (stopping_) { message="STOPPING: wait for control release to complete"; return; }
            if (stop_thread_.joinable()) stop_thread_.join();
            require(!active,"Control already active");
            const auto settings=fields(arg,',');
            require((settings.size()==2 || settings.size()==4) && settings[0]=="STANDING","STANDING and speed settings required");
            const double speed=integer(settings[1])/1000.;
            // Legacy two-field clients retain their previous lateral/yaw rates.
            const double lateral=settings.size()==4 ? integer(settings[2])/1000. : .2;
            const double turn=settings.size()==4 ? integer(settings[3])/1000. : .3;
            Policy checked(speed,lateral,turn); backend_->start(); policy_=checked; active=true; started_=t;
            last_move_rx_=-1; last_move_key_=0; last_move_gap_=max_move_gap_=0; move_rx_count_=logged_stops_=0;
            last_command_seq_=last_motion_command_seq_=0; last_command_id_.clear(); last_motion_command_id_.clear();
            last_command_at_=last_motion_started_at_=last_motion_stopped_at_=-1;
            good_since_=-1; stop_since_=-1; was_moving_=false; message="CONTROL_STARTED: wait healthy stationary 500ms, then A"; return;
        }
        if (!active) { message="TAKE_CONTROL_FIRST"; return; }
        char key=op=="ARM" ? 'a' : op=="RESET" ? 'r' : op=="AVOID" ? 'o' : 0;
        if (op=="MOVE") { require(motion_arg.size()==1 && std::string("wsjlzc").find(motion_arg[0])!=std::string::npos,"Invalid motion key"); key=motion_arg[0]; }
        require(key!=0,"Unknown operation");
        if (op=="MOVE") {
            if (last_move_rx_>=0 && last_move_key_==key) {
                last_move_gap_=t-last_move_rx_; max_move_gap_=std::max(max_move_gap_,last_move_gap_);
            }
            last_move_rx_=t; last_move_key_=key; ++move_rx_count_;
        } else { last_move_rx_=-1; last_move_key_=0; }
        const auto& s=backend_->state;
        message=policy_.key(key,t,s.health=="OK",s.vx,good_since_>=0 && t-good_since_>=.5,s.vy,s.yaw);
        report_stop();
        if (message.empty()) message="COMMAND_ACCEPTED";
    }
    void step(double t,bool link_fresh,bool ui_active) {
        if (!active) return;
        backend_->read(); const auto& s=backend_->state; const bool healthy=s.health=="OK";
        if (s.remote || s.mode>2 || s.health=="CHARGING") { policy_.trip(s.health); release("YIELDED:"+s.health); return; }
        if (!link_fresh) policy_.trip("LINK_TIMEOUT");
        if (!ui_active) { policy_.stop("UI_INACTIVE"); policy_.armed=false; policy_.disarm_reason="UI_INACTIVE"; }
        if (healthy && std::hypot(s.vx,s.vy)<.05 && std::abs(s.yaw)<.05) { if (good_since_<0) good_since_=t; }
        else good_since_=-1;
        if (t-started_>.5 || healthy) policy_.advance(t,healthy,s.health);
        if (!policy_.moving()) { if (was_moving_) stop_since_=t; } else stop_since_=-1;
        if (policy_.moving() && !was_moving_) last_motion_started_at_=t;
        if (!policy_.moving() && was_moving_) last_motion_stopped_at_=t;
        const auto motion_fault=motion_.update(t,policy_.vx,s.mode,policy_.vy,policy_.yaw);
        const auto gait_fault=gait_.update(t,policy_.moving(),policy_.avoidance,s.mode,s.gait);
        if (!motion_fault.empty() || !gait_fault.empty()) { policy_.trip(!motion_fault.empty() ? motion_fault : gait_fault); stop_since_=t; motion_.reset(); gait_.reset(); }
        const int mode=policy_.moving() || (stop_since_>=0 && t-stop_since_<.4) ? 2 : 1;
        backend_->send(mode,static_cast<float>(policy_.vx),static_cast<float>(policy_.vy),static_cast<float>(policy_.yaw),
            healthy && policy_.armed && !policy_.fault && policy_.avoidance);
        report_stop();
        was_moving_=policy_.moving();
    }
    std::string json(double t) const {
        const auto& s=backend_->state; std::ostringstream out;
        const bool measured_stopped=s.valid && !policy_.moving() &&
            std::hypot(s.vx,s.vy)<.05 && std::abs(s.yaw)<.05;
        out<<"\"control_active\":"<<(active ? "true" : "false")<<",\"armed\":"<<(policy_.armed ? "true" : "false")
            <<",\"stopping\":"<<(stopping_ ? "true" : "false")<<",\"stop_send_failed\":"<<(stop_failed_ ? "true" : "false")
            <<",\"fault\":"<<(policy_.fault ? "true" : "false")<<",\"fault_reason\":"<<quoted(policy_.fault_reason)
            <<",\"disarm_reason\":"<<quoted(policy_.disarm_reason)<<",\"message\":"<<quoted(message)
            <<",\"health\":"<<quoted(active ? s.health : "NO_SDK_CONTROL")<<",\"has_state\":"<<(s.valid ? "true" : "false")
            <<",\"state_age_ms\":"<<(s.valid ? number(std::max(0.,(t-s.stamp)*1000)) : "null")
            <<",\"soc\":"<<(s.valid ? std::to_string(s.soc) : "null")<<",\"bms\":"<<(s.valid ? std::to_string(s.bms) : "null")
            <<",\"actual_vx\":"<<(s.valid ? number(s.vx) : "null")<<",\"actual_vy\":"<<(s.valid ? number(s.vy) : "null")
            <<",\"actual_yaw\":"<<(s.valid ? number(s.yaw) : "null")
            <<",\"actual_yaw_rate\":"<<(s.valid ? number(s.yaw) : "null")
            <<",\"actual_yaw_is_rate\":true,\"height\":"<<(s.valid ? number(s.height) : "null")
            <<",\"roll\":"<<(s.valid ? number(s.roll) : "null")<<",\"pitch\":"<<(s.valid ? number(s.pitch) : "null")
            <<",\"max_temperature\":"<<(s.valid ? number(s.temp) : "null")<<",\"feedback_mode\":"<<(s.valid ? std::to_string(s.mode) : "null")
            <<",\"feedback_gait\":"<<(s.valid ? std::to_string(s.gait) : "null")
            <<",\"command_vx\":"<<number(policy_.vx)<<",\"command_vy\":"<<number(policy_.vy)<<",\"command_yaw\":"<<number(policy_.yaw)
            <<",\"speed_vx\":"<<number(policy_.speed)<<",\"speed_vy\":"<<number(policy_.lateral_speed)<<",\"speed_yaw\":"<<number(policy_.turn_speed)
            <<",\"remote_active\":"<<(s.valid && s.remote ? "true" : "false")
            <<",\"motion_executing\":"<<(policy_.moving() ? "true" : "false")
            <<",\"motion_stopped\":"<<(measured_stopped ? "true" : "false")
            <<",\"navigation_feedback_version\":1"
            <<",\"receiver_clock_id\":\"receiver_monotonic\""
            <<",\"receiver_time_s\":"<<number(t)
            <<",\"motion_command_seq\":"<<last_motion_command_seq_
            <<",\"motion_command_id\":"<<quoted(last_motion_command_id_)
            <<",\"last_command_seq\":"<<last_command_seq_
            <<",\"last_command_id\":"<<quoted(last_command_id_)
            <<",\"last_command_at_s\":"<<number(last_command_at_)
            <<",\"motion_started_at_s\":"<<number(last_motion_started_at_)
            <<",\"motion_stopped_at_s\":"<<number(last_motion_stopped_at_)
            <<",\"avoidance_selected\":"<<(policy_.avoidance ? "true" : "false")<<",\"motion_feedback\":"<<quoted(motion_.status())
            <<",\"gait_feedback\":"<<quoted(gait_.status())<<",\"ready_ms\":"<<number(good_since_<0 ? 0 : std::max(0.,(t-good_since_)*1000))
            <<",\"ttl_ms\":"<<number(policy_.moving() ? std::max(0.,policy_.expires-t)*1000 : 0)
            <<",\"continuous_limit_s\":null,\"motion_elapsed_s\":"<<number(policy_.moving() ? std::max(0.,t-policy_.motion_since) : 0)
            <<",\"last_stop_reason\":"<<quoted(policy_.last_stop_reason)<<",\"motion_stop_count\":"<<policy_.stop_count
            <<",\"ttl_expirations\":"<<policy_.ttl_expirations<<",\"move_rx_count\":"<<move_rx_count_
            <<",\"move_gap_ms\":"<<number(last_move_gap_*1000)<<",\"max_move_gap_ms\":"<<number(max_move_gap_*1000);
        return out.str();
    }
};

int main(int argc,char** argv) {
    Socket listener=bad_socket,client=bad_socket;
    try {
#ifdef _WIN32
        WSADATA ws{}; require(WSAStartup(MAKEWORD(2,2),&ws)==0,"WSAStartup failed");
#else
        std::signal(SIGPIPE,SIG_IGN);
#endif
        std::signal(SIGINT,quit_signal); std::signal(SIGTERM,quit_signal);
        bool simulate=false,live=false,no_ack=false,cross_drift=false; int port=8765,soc=60; double stale_after=-1;
        std::string bind_ip,token_path;
        for (int i=1;i<argc;++i) {
            const std::string arg=argv[i];
            if (arg=="--simulate") simulate=true;
            else if (arg=="--live") live=true;
            else if (arg=="--mock-no-ack") no_ack=true;
            else if (arg=="--mock-cross-drift") cross_drift=true;
            else if (arg=="--help") { std::cout<<"go1_receiver --simulate|--live --token-file FILE [--bind IP] [--port 8765]\nService starts monitor-only; TAKE/STANDING and A are required before movement.\n"; return 0; }
            else {
                require(i+1<argc,"Missing argument value"); const std::string value=argv[++i];
                if (arg=="--bind") bind_ip=value;
                else if (arg=="--token-file") token_path=value;
                else if (arg=="--port") port=static_cast<int>(integer(value));
                else if (arg=="--mock-soc") soc=static_cast<int>(integer(value));
                else if (arg=="--mock-stale-after") stale_after=std::stod(value);
                else throw std::runtime_error("Unknown argument: "+arg);
            }
        }
        require(simulate!=live,"Choose exactly one of --simulate / --live");
        require(simulate || (!no_ack && !cross_drift && soc==60 && stale_after==-1),"Mock options refused in live mode");
        require(port>=1024 && port<=65535 && soc>=0 && soc<=255,"Invalid port/SOC");
        if (bind_ip.empty()) bind_ip=simulate ? "127.0.0.1" : "192.168.12.1";
        require(!simulate || bind_ip=="127.0.0.1","Simulation must bind loopback");
        std::ifstream token_file(token_path); std::string token; token_file>>token;
        require(token.size()==64 && token.find_first_not_of("0123456789abcdef")==std::string::npos,"Token must contain 64 lowercase hex characters");
        std::unique_ptr<Backend> backend;
        if (simulate) backend.reset(new SimBackend(soc,no_ack,stale_after,cross_drift));
#ifdef GO1_SIM_ONLY
        else throw std::runtime_error("This binary is SIMULATION ONLY; live SDK is not compiled");
#else
        else backend.reset(new SdkBackend());
#endif
        Engine engine(std::move(backend));
        listener=socket(AF_INET,SOCK_STREAM,IPPROTO_TCP); require(listener!=bad_socket,"Socket creation failed");
#ifndef _WIN32
        int reuse=1; require(setsockopt(listener,SOL_SOCKET,SO_REUSEADDR,&reuse,sizeof(reuse))==0,"Socket reuse setup failed");
#endif
        sockaddr_in address{}; address.sin_family=AF_INET; address.sin_port=htons(static_cast<unsigned short>(port));
        require(inet_pton(AF_INET,bind_ip.c_str(),&address.sin_addr)==1,"Invalid bind IPv4");
        if (bind(listener,reinterpret_cast<sockaddr*>(&address),sizeof(address))!=0) {
#ifdef _WIN32
            throw std::runtime_error("Bind failed, Windows socket error="+std::to_string(WSAGetLastError()));
#else
            throw std::runtime_error("Bind "+bind_ip+":"+std::to_string(port)+" failed: "+std::string(std::strerror(errno)));
#endif
        }
        require(listen(listener,2)==0,"Listen failed"); nonblocking(listener);
        std::cout<<(simulate ? "SIMULATION_ONLY" : "LIVE_RECEIVER_MONITOR_ONLY")<<" listening="<<bind_ip<<":"<<port<<std::endl;
        std::string incoming,pending,session; bool authenticated=false,ui_active=false;
        unsigned long long seq=0,tick=0; double connected_at=0,last_rx=0,last_fresh_rx=0,last_ui=0,next_status=0,pending_since=0;
        std::deque<std::pair<unsigned long long,double>> ticks;
        const auto drop=[&](const std::string& reason) {
            std::cout<<"CLIENT_CLOSED reason="<<reason<<" authenticated="<<authenticated<<" controlled="<<engine.active<<std::endl;
            close_socket(client); client=bad_socket; authenticated=false; ui_active=false; incoming.clear(); pending.clear(); ticks.clear();
            if (engine.active) engine.release(reason);
        };
        while (!quitting) {
            const double t=now_s();
            const Socket candidate=accept(listener,nullptr,nullptr);
            if (candidate!=bad_socket) {
                if (client!=bad_socket) { close_socket(candidate); std::cout<<"CLIENT_REJECTED reason=CLIENT_BUSY"<<std::endl; }
                else { client=candidate; nonblocking(client); int yes=1; setsockopt(client,IPPROTO_TCP,TCP_NODELAY,reinterpret_cast<const char*>(&yes),sizeof(yes));
                    session=nonce(); seq=0; authenticated=false; ui_active=false; connected_at=last_rx=last_fresh_rx=t; next_status=t; ticks.clear(); incoming.clear(); pending.clear();
                    std::cout<<"CLIENT_CONNECTED"<<std::endl; }
            }
            if (client!=bad_socket) {
                char bytes[2048]; const int n=recv(client,bytes,sizeof(bytes),0);
                if (n==0 || (n<0 && !would_block())) drop("CLIENT_DISCONNECTED");
                else if (n>0) incoming.append(bytes,static_cast<size_t>(n));
                if (incoming.size()>4096) drop("INPUT_OVERFLOW");
                for (int count=0;client!=bad_socket && count<16;++count) {
                    const auto end=incoming.find('\n'); if (end==std::string::npos) { if (incoming.size()>256) drop("INVALID_FRAME"); break; }
                    const auto line=incoming.substr(0,end); incoming.erase(0,end+1);
                    try {
                        const auto f=fields(line); require(line.size()<=256 && f.size()==8 && f[0]=="G1R1","Invalid protocol");
                        require(equal_secret(f[1],token) && f[2]==session,"Authentication/session rejected");
                        const auto next_seq=integer(f[3]),echo=integer(f[4]); require(next_seq>seq && next_seq<=9007199254740991ULL,"Replay/out-of-order command");
                        require(f[7]=="0" || f[7]=="1","Invalid UI lease");
                        const bool safe=f[5]=="STOP" || f[5]=="DISARM" || f[5]=="RELEASE";
                        const bool liveness=f[5]=="AUTH" || f[5]=="PING";
                        bool fresh=false; for (const auto& item:ticks) if (item.first==echo && t-item.second<=timing::echo_timeout_s) fresh=true;
                        seq=next_seq;
                        if (!fresh && !safe && !liveness) { engine.action("DISARM","-",false,t,next_seq); engine.message="STALE_COMMAND_REJECTED"; continue; }
                        if (!authenticated) { require(f[5]=="AUTH","Authenticate first"); authenticated=true; }
                        else { require(f[5]!="AUTH","Duplicate AUTH"); engine.action(f[5],f[6],f[7]=="1",t,next_seq); }
                        last_rx=t;
                        // Old PING/AUTH can keep TCP alive, but can never renew motion freshness or UI authority.
                        if (fresh) last_fresh_rx=t;
                        ui_active=f[7]=="1" && fresh; if (ui_active) last_ui=t;
                    } catch (const std::exception& e) { std::cout<<"COMMAND_REJECTED detail="<<e.what()<<std::endl; drop("PROTOCOL_OR_COMMAND_REJECTED"); }
                }
                if (client!=bad_socket && !authenticated && t-connected_at>timing::network_timeout_s) drop("AUTH_TIMEOUT");
                if (client!=bad_socket && authenticated && t-last_rx>timing::network_timeout_s) drop("NETWORK_IDLE_TIMEOUT");
            }
            engine.step(now_s(),client!=bad_socket && authenticated && t-last_fresh_rx<=timing::network_timeout_s,
                client!=bad_socket && authenticated && ui_active && t-last_ui<=timing::ui_lease_s);
            if (client!=bad_socket) {
                if (pending.empty() && t>=next_status) {
                    ++tick; ticks.push_back({tick,t}); if (ticks.size()>timing::tick_history) ticks.pop_front();
                    pending="{\"protocol\":1,\"receiver_version\":\"0.4.0-20261030\",\"navigation_feedback_version\":1,\"capabilities\":[\"motion_stopped\",\"motion_executing\",\"actual_yaw_rate\",\"motion_command_seq\",\"receiver_clock_id\"],\"startup_ack_ms\":1500,\"steady_motion_renewal\":true,\"command_ttl_ms\":600,\"hold_to_move\":true,\"configurable_speeds\":true,\"network_grace_ms\":3000,\"control_timeout_ms\":3000,\"command_echo_ms\":3000,\"ui_lease_ms\":750,\"session\":"+::quoted(session)+",\"tick\":"+std::to_string(tick)+
                        ",\"authenticated\":"+(authenticated ? "true" : "false")+",\"simulated\":"+(simulate ? "true" : "false")+
                        ",\"ack_seq\":"+std::to_string(seq)+(authenticated ? ","+engine.json(t) : "")+"}\n";
                    next_status=t+.1; pending_since=t;
                }
                if (!pending.empty()) {
                    const int n=send(client,pending.data(),static_cast<int>(pending.size()),0);
                    if (n>0) pending.erase(0,static_cast<size_t>(n));
                    else if (n==0 || !would_block()) drop("SEND_FAILED");
                    if (client!=bad_socket && !pending.empty() && t-pending_since>timing::network_timeout_s) drop("SLOW_CLIENT");
                }
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        drop("SERVER_SHUTDOWN"); close_socket(listener); listener=bad_socket; return 0;
    } catch (const std::exception& e) { close_socket(client); close_socket(listener); std::cerr<<"RECEIVER_ERROR: "<<e.what()<<std::endl; return 1; }
}
