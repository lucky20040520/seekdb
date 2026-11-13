#include "unicode.hpp"
#include <iostream>

int main(int argc,char**) {
    using namespace mv;
    if(argc>1) {
        Json lower=Json::array(),space=Json::array(),decimal=Json::array();
        for(uint32_t cp=0;cp<=0x10ffff;++cp) {
            if(cp>=0xd800&&cp<=0xdfff) continue;
            std::string text; unicode::append(text,cp);
            auto decoded=unicode::decode(text,0);
            if(decoded.value!=cp||decoded.width!=text.size()) fail("UTF-8 round trip failed");
            auto converted=unicode::lower(text);
            if(converted!=text) lower.push_back({cp,converted});
            if(unicode::is_space(cp)) space.push_back(cp);
            const auto digit=unicode::decimal_at(text,0);
            if(digit.value>=0) decimal.push_back({cp,digit.value});
        }
        // Overlong, surrogate, out-of-range, truncated, and stray continuation.
        for(const std::string text:{"\xc0\x80","\xed\xa0\x80","\xf4\x90\x80\x80","\xe2\x82","\x80"}) {
            bool rejected=false;
            try {unicode::decode(text,0);} catch(const DBError&) {rejected=true;}
            if(!rejected) fail("accepted malformed UTF-8");
        }
        std::cout<<Json({{"lower",lower},{"space",space},{"decimal",decimal}}).dump()<<'\n';
        return 0;
    }
    std::string line;
    while(std::getline(std::cin,line)) {
        Json output=Json::array();
        for(const auto& text:Json::parse(line)) output.push_back(unicode::lower(text.get<std::string>()));
        std::cout<<output.dump()<<'\n'<<std::flush;
    }
}
