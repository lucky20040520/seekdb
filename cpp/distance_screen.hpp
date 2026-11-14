#pragma once
#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdlib>
#include <cstring>
#include <limits>
#if defined(__GNUC__) && (defined(__x86_64__) || defined(__i386__))
#include <immintrin.h>
#define MV_HAS_AVX2_SCREEN 1
#else
#define MV_HAS_AVX2_SCREEN 0
#endif

namespace mv::screen {
struct Sum { double squared; size_t dimensions; };
using Kernel = Sum (*)(const float*, const float*, size_t, double);

// The accurate distance sums rounded binary64 squared differences and then
// rounds sqrt. Inflate the squared heap boundary for both summation error and
// sqrt rounding. Nonnegative partial sums may only reject above the upper
// bound; descending queries must inspect all dimensions against the lower one.
// Float32 inputs keep squared terms normal in binary64, including subnormals.
inline double cutoff(double boundary,size_t dimension,bool descending) {
    const double error=32*std::numeric_limits<double>::epsilon()*std::max<size_t>(1,dimension);
    const double infinity=std::numeric_limits<double>::infinity();
    if(!std::isfinite(boundary) || boundary<0 || error>=0.25) return descending ? 0 : infinity;
    const double direction=descending ? 0 : infinity;
    const double outer=std::nextafter(boundary,direction);
    return std::nextafter((outer*outer)*(descending ? 1-error : 1+error),direction);
}

template<size_t Block>
inline Sum scalar(const float* a,const float* b,size_t dimension,double upper) {
    double s0=0,s1=0,s2=0,s3=0,tail=0,sum=0;
    size_t i=0;
    while(i<dimension) {
        const size_t end=Block ? i+std::min(Block,dimension-i) : dimension;
        for(;i+3<end;i+=4) {
            const double d0=double(a[i])-double(b[i]),d1=double(a[i+1])-double(b[i+1]);
            const double d2=double(a[i+2])-double(b[i+2]),d3=double(a[i+3])-double(b[i+3]);
            s0+=d0*d0; s1+=d1*d1; s2+=d2*d2; s3+=d3*d3;
        }
        for(;i<end;++i) {const double delta=double(a[i])-double(b[i]);tail+=delta*delta;}
        sum=((s0+s1)+(s2+s3))+tail;
        if constexpr(Block!=0) if(sum>upper) return {sum,i};
    }
    return {sum,i};
}

#if MV_HAS_AVX2_SCREEN
template<size_t Block>
__attribute__((target("avx2")))
inline Sum avx2(const float* a,const float* b,size_t dimension,double upper) {
    __m256d s0=_mm256_setzero_pd(),s1=s0,s2=s0,s3=s0;
    double tail=0,sum=0;
    size_t i=0;
    while(i<dimension) {
        const size_t end=Block ? i+std::min(Block,dimension-i) : dimension;
        for(;i+15<end;i+=16) {
            const __m256d d0=_mm256_sub_pd(_mm256_cvtps_pd(_mm_loadu_ps(a+i)),_mm256_cvtps_pd(_mm_loadu_ps(b+i)));
            const __m256d d1=_mm256_sub_pd(_mm256_cvtps_pd(_mm_loadu_ps(a+i+4)),_mm256_cvtps_pd(_mm_loadu_ps(b+i+4)));
            const __m256d d2=_mm256_sub_pd(_mm256_cvtps_pd(_mm_loadu_ps(a+i+8)),_mm256_cvtps_pd(_mm_loadu_ps(b+i+8)));
            const __m256d d3=_mm256_sub_pd(_mm256_cvtps_pd(_mm_loadu_ps(a+i+12)),_mm256_cvtps_pd(_mm_loadu_ps(b+i+12)));
            s0=_mm256_add_pd(s0,_mm256_mul_pd(d0,d0)); s1=_mm256_add_pd(s1,_mm256_mul_pd(d1,d1));
            s2=_mm256_add_pd(s2,_mm256_mul_pd(d2,d2)); s3=_mm256_add_pd(s3,_mm256_mul_pd(d3,d3));
        }
        for(;i<end;++i) {const double delta=double(a[i])-double(b[i]);tail+=delta*delta;}
        const __m256d combined=_mm256_add_pd(_mm256_add_pd(s0,s1),_mm256_add_pd(s2,s3));
        const __m128d pair=_mm_add_pd(_mm256_castpd256_pd128(combined),_mm256_extractf128_pd(combined,1));
        sum=_mm_cvtsd_f64(_mm_add_sd(pair,_mm_unpackhi_pd(pair,pair)))+tail;
        if constexpr(Block!=0) if(sum>upper) return {sum,i};
    }
    return {sum,i};
}
#endif

inline bool has_avx2() {
#if MV_HAS_AVX2_SCREEN
    return __builtin_cpu_supports("avx2");
#else
    return false;
#endif
}

// An opt-in experiment knob; 0 uses a complete SIMD/scalar pass. Invalid
// values retain the default. Selection occurs once per query, not per row.
inline size_t configured_block() {
    static const size_t block=[]()->size_t {
        const char* value=std::getenv("MINIVECTOR_SCREEN_BLOCK");
        if(value) {
            if(!std::strcmp(value,"32")) return 32;
            if(!std::strcmp(value,"64")) return 64;
            if(!std::strcmp(value,"128")) return 128;
        }
        return 0;
    }();
    return block;
}

inline Kernel select(size_t block,bool vectorized=has_avx2()) {
#if MV_HAS_AVX2_SCREEN
    if(vectorized) {
        if(block==32) return avx2<32>;
        if(block==64) return avx2<64>;
        if(block==128) return avx2<128>;
        return avx2<0>;
    }
#else
    (void)vectorized;
#endif
    if(block==32) return scalar<32>;
    if(block==64) return scalar<64>;
    if(block==128) return scalar<128>;
    return scalar<0>;
}
}
