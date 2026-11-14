#include "minivector.hpp"
#include "distance_screen.hpp"
#include <chrono>
#include <iostream>
#include <random>

using namespace mv;
namespace {
void require(bool ok,const char* message) { if(!ok) throw std::runtime_error(message); }
float arbitrary(std::mt19937& rng) {
    uint32_t bits=rng();
    if((bits&0x7f800000)==0x7f800000) bits^=0x00800000;
    float value; std::memcpy(&value,&bits,4); return value;
}

Json correctness() {
    std::mt19937 rng(30871);
    size_t checks=0,early=0;
    for(size_t dimension:{1,2,3,4,7,15,16,17,31,32,33,63,64,65,127,128,129,255,256,257,1024,4097}) {
        // Offset pointers also exercise unaligned input and vector-width tails.
        std::vector<float> left(dimension+1),right(dimension+1);
        float* a=left.data()+1; float* b=right.data()+1;
        for(size_t trial=0;trial<96;++trial) {
            for(size_t i=0;i<dimension;++i) {
                if(trial%4==0) {a[i]=arbitrary(rng);b[i]=arbitrary(rng);}
                else if(trial%4==1) {a[i]=float(int(rng()%2001)-1000)/1024;b[i]=float(int(rng()%2001)-1000)/1024;}
                else if(trial%4==2) {a[i]=std::ldexp(1.0f,int(rng()%277)-149);b[i]=0;}
                else {a[i]=i==0 ? float(trial) : 0;b[i]=0;}
            }
            const double exact=l2_distance(a,b,dimension);
            for(double boundary:{exact,std::nextafter(exact,0.0),std::nextafter(exact,std::numeric_limits<double>::infinity()),exact*0.95,exact*1.05,0.0}) {
                const double upper=screen::cutoff(boundary,dimension,false),lower=screen::cutoff(boundary,dimension,true);
                for(bool vectorized:{false,true}) {
                    if(vectorized && !screen::has_avx2()) continue;
                    for(size_t block:{0,32,64,128}) {
                        const auto sum=screen::select(block,vectorized)(a,b,dimension,upper);
                        require(sum.dimensions<=dimension,"screen read beyond dimensions");
                        if(sum.squared>upper) require(exact>boundary,"ascending screen discarded a possible winner/tie");
                        if(sum.dimensions<dimension) {require(sum.squared>upper,"early exit without safe rejection");++early;}
                        const auto full=screen::select(0,vectorized)(a,b,dimension,upper);
                        if(full.squared<lower) require(exact<boundary,"descending screen discarded a possible winner/tie");
                        ++checks;
                    }
                }
            }
        }
    }
    require(early>0,"blocked kernels never exited early");
    return {{"passed",true},{"checks",checks},{"early_exits",early},{"avx2_available",screen::has_avx2()}};
}

Json benchmark() {
    constexpr size_t rows=10000,dimension=128,queries=32,rounds=5;
    std::mt19937 rng(19017); std::uniform_real_distribution<float> uniform(-1,1);
    std::vector<float> data(rows*dimension),query(queries*dimension);
    for(auto& value:data) value=uniform(rng);
    for(auto& value:query) value=uniform(rng);
    std::vector<double> boundaries(queries),cutoffs(queries);
    for(size_t q=0;q<queries;++q) {
        std::vector<double> distances; distances.reserve(rows);
        for(size_t i=0;i<rows;++i) distances.push_back(l2_distance(data.data()+i*dimension,query.data()+q*dimension,dimension));
        std::nth_element(distances.begin(),distances.begin()+9,distances.end());
        boundaries[q]=distances[9]; cutoffs[q]=screen::cutoff(boundaries[q],dimension,false);
    }
    Json results=Json::array();
    for(bool vectorized:{false,true}) {
        if(vectorized && !screen::has_avx2()) continue;
        for(size_t block:{0,32,64,128}) {
            std::vector<double> times;
            uint64_t rejected=0,dimensions=0;
            const auto kernel=screen::select(block,vectorized);
            for(size_t repeat=0;repeat<rounds;++repeat) {
                uint64_t reject_count=0,dimension_count=0;
                const auto begin=std::chrono::steady_clock::now();
                for(size_t q=0;q<queries;++q) for(size_t i=0;i<rows;++i) {
                    const auto sum=kernel(data.data()+i*dimension,query.data()+q*dimension,dimension,cutoffs[q]);
                    reject_count+=sum.squared>cutoffs[q]; dimension_count+=sum.dimensions;
                }
                times.push_back(std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-begin).count());
                rejected=reject_count;dimensions=dimension_count;
            }
            auto sorted=times; std::sort(sorted.begin(),sorted.end());
            require(rejected==queries*(rows-10),"microbenchmark screening changed exact Top10 admission");
            results.push_back({{"avx2",vectorized},{"block",block},{"times_ms",times},{"median_ms",sorted[rounds/2]},
                               {"rejected",rejected},{"mean_dimensions",double(dimensions)/(queries*rows)}});
        }
    }
    return {{"scope","kernel-only; fixed exact Top10 boundary, excludes heap/SQL/IPC"},
            {"rows",rows},{"dimensions",dimension},{"queries",queries},{"rounds",rounds},{"kernels",results}};
}
}
int main(int argc,char** argv) {
    try {
        std::cout<<correctness().dump()<<'\n';
        if(argc>1 && std::string(argv[1])=="--bench") std::cout<<benchmark().dump()<<'\n';
    } catch(const std::exception& error) {std::cerr<<error.what()<<'\n';return 1;}
}
