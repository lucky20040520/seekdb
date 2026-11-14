#pragma once
#include <array>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <cstdio>
#include <string>
#include <nlohmann/json.hpp>

namespace mv::profile {
enum class Phase : size_t {
    protocol_parse, sql_parse, vector_literal_parse, stored_row_decode, filter, filter_lists,
    distance_exact, distance_screen, heap, file_sync, directory_sync, collect, count
};
inline constexpr std::array<const char*,static_cast<size_t>(Phase::count)> names = {
    "protocol_parse", "sql_parse", "vector_literal_parse", "stored_row_decode", "filter", "filter_lists",
    "distance_exact", "distance_screen", "heap", "file_sync", "directory_sync", "collect"
};
struct Counter { uint64_t ns=0, calls=0; };
inline thread_local std::array<Counter,static_cast<size_t>(Phase::count)> counters{};
inline thread_local size_t stored_decode_depth=0;
using Clock=std::chrono::steady_clock;
inline bool enabled() {
    static const bool value=[] { const char* env=std::getenv("MINIVECTOR_PROFILE"); return env && std::string(env)=="1"; }();
    return value;
}
inline void reset() { if(enabled()) counters={}; }
class Scope {
    Phase phase_;
    bool running_;
    Clock::time_point start_;
public:
    explicit Scope(Phase phase,bool active=true):phase_(phase),running_(active&&enabled()) {
        if(running_) { if(phase_==Phase::stored_row_decode) ++stored_decode_depth; start_=Clock::now(); }
    }
    Scope(const Scope&)=delete;
    Scope& operator=(const Scope&)=delete;
    void stop() noexcept {
        if(!running_) return;
        const auto end=Clock::now();
        auto& counter=counters[static_cast<size_t>(phase_)];
        counter.ns+=static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(end-start_).count());
        ++counter.calls;
        if(phase_==Phase::stored_row_decode) --stored_decode_depth;
        running_=false;
    }
    ~Scope() { stop(); }
};

// The protocol remains on stdout. This diagnostic is intentionally opt-in.
inline void report(const char* kind,uint64_t sequence,Clock::time_point start) noexcept {
    if(!enabled()) return;
    try {
        const auto elapsed=std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now()-start).count();
        nlohmann::json phases=nlohmann::json::object();
        for(size_t i=0;i<names.size();++i) phases[names[i]]={{"ns",counters[i].ns},{"calls",counters[i].calls}};
        auto line=nlohmann::json({{"kind",kind},{"sequence",sequence},{"wall_ns",elapsed},{"phases",phases}}).dump();
        std::fputs("MINIVECTOR_PROFILE ",stderr); std::fputs(line.c_str(),stderr); std::fputc('\n',stderr); std::fflush(stderr);
    } catch (...) { /* Diagnostics must not change request results. */ }
}
class Request {
    const char* kind_;
    uint64_t sequence_;
    Clock::time_point start_;
public:
    Request(const char* kind,uint64_t sequence):kind_(kind),sequence_(sequence) { if(enabled()) {reset();start_=Clock::now();} }
    ~Request() { if(enabled()) report(kind_,sequence_,start_); }
};
}
