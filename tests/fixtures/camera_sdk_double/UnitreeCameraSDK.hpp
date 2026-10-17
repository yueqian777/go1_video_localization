// TEST DOUBLE ONLY. Never opens USB/V4L2, networks or shared memory.
#pragma once
#include <chrono>
#include <stdexcept>
#include <string>
#include <thread>
namespace cv {
struct Mat { bool is_empty = false; bool empty() const { return is_empty; } };
}
class UnitreeCamera {
    std::string mode;
    int reads = 0;
    static void hang() { for (;;) std::this_thread::sleep_for(std::chrono::seconds(1)); }
public:
    explicit UnitreeCamera(std::string name) : mode(name) { if (mode == "constructor_hang") hang(); }
    bool isOpened() { return mode != "open_fail" && !(mode == "closed" && reads > 2); }
    bool startCapture(bool udp, bool shm) {
        if (udp || shm) throw std::runtime_error("candidate must not publish");
        return mode != "start_fail";
    }
    bool getRawFrame(cv::Mat& frame, std::chrono::microseconds& timestamp) {
        if (mode == "read_hang") hang();
        if (mode == "throw") throw std::runtime_error("test exception");
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
        ++reads;
        frame.is_empty = mode == "empty";
        timestamp = std::chrono::microseconds(mode == "timestamp_stuck" ? 1 : reads);
        return mode != "read_false";
    }
    bool stopCapture() { if (mode == "stop_hang") hang(); return mode != "stop_fail"; }
};
