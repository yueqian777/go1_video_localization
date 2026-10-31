// Real SDK lifecycle regression, but ALL packets go to 127.0.0.1:24082, never Go1.
#include "legacy/go1_highlevel.h"
#include <atomic>

static void check(bool value,const char* message) { if (!value) throw std::runtime_error(message); }

int main() {
    int sink=-1;
    std::atomic<bool> done{false};
    std::thread worker;
    try {
        sink=socket(AF_INET,SOCK_DGRAM,0); check(sink>=0,"Fixture socket failed");
        sockaddr_in destination{}; destination.sin_family=AF_INET;
        destination.sin_addr.s_addr=htonl(INADDR_LOOPBACK); destination.sin_port=htons(24082);
        check(bind(sink,reinterpret_cast<sockaddr*>(&destination),sizeof(destination))==0,"Loopback fixture port busy");
        check(fcntl(sink,F_SETFL,fcntl(sink,F_GETFL)|O_NONBLOCK)==0,"Fixture nonblock failed");
        worker=std::thread([&] {
            char bytes[2048];
            while (!done) { (void)recv(sink,bytes,sizeof(bytes),0); std::this_thread::sleep_for(std::chrono::milliseconds(1)); }
        });
        for (int attempt=0;attempt<3;++attempt) {
            {
                go1_demo::HighLevelSession session(true,true);  // fixture=true is mandatory.
                session.send(2,0,.4f,0);
                session.send(2,0,0,.6f);
                bool rejected=false;
                try { session.send(2,0,0,.6001f); } catch (const std::runtime_error&) { rejected=true; }
                check(rejected,"Yaw limit was not enforced");
            }
            const int probe=socket(AF_INET,SOCK_DGRAM,0); check(probe>=0,"Probe failed");
            destination.sin_port=htons(24111);
            const int result=bind(probe,reinterpret_cast<sockaddr*>(&destination),sizeof(destination));
            close(probe);
            check(result==0,"SDK destructor left its UDP port bound");
        }
        done=true; worker.join(); close(sink);
        std::cout<<"SDK_LOOPBACK_LIFECYCLE_PASS cycles=3; released port each time; new rate limits verified"<<std::endl;
        return 0;
    } catch (const std::exception& e) {
        done=true; if (worker.joinable()) worker.join(); if (sink>=0) close(sink);
        std::cerr<<"SDK_LOOPBACK_LIFECYCLE_FAIL: "<<e.what()<<std::endl; return 1;
    }
}
