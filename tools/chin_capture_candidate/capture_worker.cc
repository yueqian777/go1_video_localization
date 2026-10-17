// OFFLINE CANDIDATE: not a drop-in factory replacement. No motion APIs.
// Run only under supervise.py; SDK constructor/read/cleanup may block forever.
// UDP and shared-memory publishing are deliberately disabled in this candidate.
#include <UnitreeCameraSDK.hpp>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdlib>
#include <iostream>
#include <string>
#include <thread>

namespace {
volatile std::sig_atomic_t stopping = 0;
void request_stop(int) { stopping = 1; }
}

int main(int argc, char* argv[]) {
    if (argc != 4 || std::string(argv[1]) != "--authorize-exclusive-capture")
        return 2;
    char* end = nullptr;
    const double seconds = std::strtod(argv[3], &end);
    if (*end || !std::isfinite(seconds) || seconds < .05 || seconds > 600.)
        return 2;
    std::signal(SIGINT, request_stop);
    std::signal(SIGTERM, request_stop);
    try {
        UnitreeCamera camera{std::string(argv[2])};
        if (!camera.isOpened()) return 10;
        if (!camera.startCapture(false, false)) return 11;
        const auto start = std::chrono::steady_clock::now();
        std::chrono::microseconds last{-1};
        unsigned long long sequence = 0;
        int result = 0;
        while (!stopping && std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count() < seconds) {
            if (!camera.isOpened()) { result = 14; break; }
            cv::Mat frame;
            std::chrono::microseconds timestamp{-1};
            // This call is intentionally isolated in a killable process.
            if (!camera.getRawFrame(frame, timestamp) || frame.empty() || timestamp <= last) {
                std::this_thread::sleep_for(std::chrono::milliseconds(5));
                continue;
            }
            last = timestamp;
            // SDK frame timestamp progression, NOT a hardware exposure guarantee.
            std::cout << "GO1_FRAME " << ++sequence << std::endl;
        }
        if (!camera.stopCapture()) return 12;
        return result;
    } catch (...) {
        return 13;  // No arbitrary SDK details/paths on the machine-readable channel.
    }
}
