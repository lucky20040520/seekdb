#pragma once
#include "minivector.hpp"
#include "unicode_data.hpp"
#include <algorithm>
#include <iterator>
#include <string_view>

// Match the Python 3.12 SQL parser's str.lower(), str.isspace(), and re \d.
// No locale-sensitive conversion, normalization, or compatibility case folding.
namespace mv::unicode {
struct Rune { uint32_t value; size_t width; };

inline Rune decode(std::string_view text,size_t position) {
    if(position>=text.size()) return {0,0};
    const auto first=static_cast<unsigned char>(text[position]);
    if(first<0x80) return {first,1};
    size_t width; uint32_t value,minimum;
    if(first>=0xc2&&first<=0xdf) {width=2;value=first&0x1f;minimum=0x80;}
    else if(first>=0xe0&&first<=0xef) {width=3;value=first&0xf;minimum=0x800;}
    else if(first>=0xf0&&first<=0xf4) {width=4;value=first&7;minimum=0x10000;}
    else fail("invalid UTF-8 in SQL","SYNTAX");
    if(width>text.size()-position) fail("invalid UTF-8 in SQL","SYNTAX");
    for(size_t i=1;i<width;++i) {
        const auto byte=static_cast<unsigned char>(text[position+i]);
        if((byte&0xc0)!=0x80) fail("invalid UTF-8 in SQL","SYNTAX");
        value=(value<<6)|(byte&0x3f);
    }
    if(value<minimum||value>0x10ffff||(value>=0xd800&&value<=0xdfff)) fail("invalid UTF-8 in SQL","SYNTAX");
    return {value,width};
}

template<size_t N>
inline bool contains(const data::Range (&ranges)[N],uint32_t value) {
    const auto* found=std::lower_bound(std::begin(ranges),std::end(ranges),value,
        [](const data::Range& range,uint32_t cp){return range.last<cp;});
    return found!=std::end(ranges)&&found->first<=value;
}

inline bool is_space(uint32_t value) {
    if(value<128) return value==32||(value>=9&&value<=13)||(value>=28&&value<=31);
    return contains(data::space,value);
}

struct Digit { int value; size_t width; };
inline Digit decimal_at(std::string_view text,size_t position) {
    if(position>=text.size()) return {-1,0};
    const auto first=static_cast<unsigned char>(text[position]);
    if(first<128) return {first>='0'&&first<='9'?int(first-'0'):-1,1};
    const auto rune=decode(text,position);
    const auto* end=std::upper_bound(std::begin(data::decimal_zero),std::end(data::decimal_zero),rune.value);
    if(end==std::begin(data::decimal_zero)||rune.value-*(end-1)>9) return {-1,rune.width};
    return {static_cast<int>(rune.value-*(end-1)),rune.width};
}

inline void append(std::string& text,uint32_t cp) {
    if(cp<0x80) text+=static_cast<char>(cp);
    else if(cp<0x800) {text+=char(0xc0|(cp>>6));text+=char(0x80|(cp&0x3f));}
    else if(cp<0x10000) {text+=char(0xe0|(cp>>12));text+=char(0x80|((cp>>6)&0x3f));text+=char(0x80|(cp&0x3f));}
    else {text+=char(0xf0|(cp>>18));text+=char(0x80|((cp>>12)&0x3f));text+=char(0x80|((cp>>6)&0x3f));text+=char(0x80|(cp&0x3f));}
}

inline std::string lower(std::string text) {
    if(std::all_of(text.begin(),text.end(),[](unsigned char c){return c<128;})) {
        for(char& c:text) if(c>='A'&&c<='Z') c=char(c-'A'+'a');
        return text;
    }
    std::string result; result.reserve(text.size());
    bool preceded_by_cased=false;
    for(size_t position=0;position<text.size();) {
        const auto rune=decode(text,position); position+=rune.width;
        uint32_t first=rune.value,second=0;
        const auto* mapping=std::lower_bound(std::begin(data::lower),std::end(data::lower),rune.value,
            [](const data::Lower& entry,uint32_t cp){return entry.source<cp;});
        if(mapping!=std::end(data::lower)&&mapping->source==rune.value) {first=mapping->first;second=mapping->second;}
        // Unicode Final_Sigma: ignore Case_Ignorable characters in both
        // directions, even those which also have the Cased property.
        if(rune.value==0x3a3&&preceded_by_cased) {
            bool followed_by_cased=false;
            for(size_t next=position;next<text.size();) {
                const auto following=decode(text,next); next+=following.width;
                if(contains(data::case_ignorable,following.value)) continue;
                followed_by_cased=contains(data::cased,following.value); break;
            }
            if(!followed_by_cased) first=0x3c2;
        }
        append(result,first); if(second) append(result,second);
        if(!contains(data::case_ignorable,rune.value)) preceded_by_cased=contains(data::cased,rune.value);
    }
    return result;
}
}
