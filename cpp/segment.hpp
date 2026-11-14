#pragma once
#include "minivector.hpp"
#include "profile.hpp"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <fstream>
#include <limits>

namespace mv::segment {
inline constexpr char magic[8]={'M','V','R','O','W','S','1','\0'};
inline uint64_t integer(const char* p,size_t width) {
    uint64_t value=0;
    for(size_t i=0;i<width;++i) value|=uint64_t(static_cast<unsigned char>(p[i]))<<(8*i);
    return value;
}
inline void append(std::string& bytes,uint64_t value,size_t width) {
    for(size_t i=0;i<width;++i) bytes.push_back(static_cast<char>(value>>(8*i)));
}
inline std::string encode(const Row& row) {
    std::string bytes;
    size_t size=0;
    for(const auto& cell:row) {
        if(const auto* text=std::get_if<std::string>(&cell)) size+=8+text->size();
        else if(const auto* vector=std::get_if<std::vector<float>>(&cell)) size+=4*vector->size();
        else size+=8;
    }
    bytes.reserve(size);
    for(const auto& cell:row) {
        if(const auto* value=std::get_if<int64_t>(&cell)) append(bytes,static_cast<uint64_t>(*value),8);
        else if(const auto* text=std::get_if<std::string>(&cell)) {append(bytes,text->size(),8);bytes+=*text;}
        else for(float value:std::get<std::vector<float>>(cell)) {
            uint32_t bits; std::memcpy(&bits,&value,4); append(bytes,bits,4);
        }
    }
    return bytes;
}

// A segment is immutable after publication. Header: magic, column count,
// reserved zero, row count, key-directory offset (32 bytes, little endian).
// Rows: uint64 payload size followed by schema-typed data. Footer: contiguous
// (int64 primary key, uint64 row offset) entries, allowing key-only validation.
class Reader {
    const Table& table_;
    std::filesystem::path path_;
    std::ifstream input_;
    bool binary_;
    std::optional<uint64_t> count_;
    uint64_t footer_=0, consumed_=0;
    void read(char* out,size_t count) {
        if(count>size_t(std::numeric_limits<std::streamsize>::max())) fail("segment read too large","STORAGE");
        input_.read(out,static_cast<std::streamsize>(count));
        if(size_t(input_.gcount())!=count) fail("truncated row segment","STORAGE");
    }
    Row decode() {
        profile::Scope timer(profile::Phase::stored_row_decode);
        if(!binary_) {
            std::string line;
            if(!std::getline(input_,line)) fail("invalid row location","STORAGE");
            Json encoded=Json::parse(line);
            if(!encoded.is_array()||encoded.size()!=table_.columns.size()) fail("invalid stored row","STORAGE");
            Row row; row.reserve(table_.columns.size());
            for(size_t i=0;i<encoded.size();++i) row.push_back(validate(table_.columns[i],encoded[i]));
            return row;
        }
        const auto position=input_.tellg();
        if(position<32 || uint64_t(position)>footer_ || footer_-uint64_t(position)<8) fail("invalid binary row location","STORAGE");
        char header[8]; read(header,8);
        const uint64_t length=integer(header,8);
        if(length>footer_-uint64_t(position)-8 || length>std::numeric_limits<size_t>::max()) fail("invalid binary row length","STORAGE");
        std::string bytes(static_cast<size_t>(length),'\0'); read(bytes.data(),bytes.size());
        size_t at=0;
        auto take=[&](size_t width) {
            if(width>bytes.size()-at) fail("truncated binary cell","STORAGE");
            const char* result=bytes.data()+at; at+=width; return result;
        };
        Row row; row.reserve(table_.columns.size());
        for(const auto& column:table_.columns) {
            if(column.kind=="BIGINT") {
                uint64_t bits=integer(take(8),8); int64_t value; std::memcpy(&value,&bits,8); row.emplace_back(value);
            } else if(column.kind=="VARCHAR") {
                const uint64_t size=integer(take(8),8);
                if(size>bytes.size()-at) fail("invalid binary string length","STORAGE");
                std::string text(take(static_cast<size_t>(size)),static_cast<size_t>(size));
                row.push_back(validate(column,Json(text)));
            } else if(column.kind=="VECTOR") {
                if(column.size<=0 || uint64_t(column.size)>(bytes.size()-at)/4) fail("invalid binary vector length","STORAGE");
                std::vector<float> vector(static_cast<size_t>(column.size));
                for(float& value:vector) {
                    uint32_t bits=static_cast<uint32_t>(integer(take(4),4)); std::memcpy(&value,&bits,4);
                    if(!std::isfinite(value)) fail("nonfinite stored vector","STORAGE");
                }
                row.emplace_back(std::move(vector));
            } else fail("invalid stored column kind","STORAGE");
        }
        if(at!=bytes.size()) fail("extra binary row bytes","STORAGE");
        return row;
    }
public:
    Reader(const std::filesystem::path& path,const Table& table):table_(table),path_(path),input_(path,std::ios::binary),binary_(path.extension()==".mvb") {
        if(!input_) fail("cannot open row segment","STORAGE");
        if(binary_) {
            char header[32]; read(header,32);
            if(std::memcmp(header,magic,8)||integer(header+8,4)!=table.columns.size()||integer(header+12,4)) fail("invalid binary segment header","STORAGE");
            count_=integer(header+16,8); footer_=integer(header+24,8);
            const uint64_t size=std::filesystem::file_size(path);
            if(footer_<32||footer_>size||*count_>(size-footer_)/16||size-footer_!=*count_*16) fail("invalid binary key directory","STORAGE");
        } else {
            std::error_code error;
            const auto size=std::filesystem::file_size(path.string()+".keys",error);
            if(!error) {if(size%8) fail("truncated legacy key directory","STORAGE");count_=size/8;}
        }
    }
    std::optional<uint64_t> count() const {return count_;}
    bool next(Row& row,uint64_t* location=nullptr) {
        if(binary_) {
            if(consumed_==*count_) {
                if(input_.tellg()<0 || uint64_t(input_.tellg())!=footer_) fail("binary row count mismatch","STORAGE");
                return false;
            }
        } else if(input_.peek()==std::char_traits<char>::eof()) {
            if(input_.bad()) fail("row segment read failed","STORAGE");
            return false;
        }
        const auto position=input_.tellg();
        if(position<0) fail("invalid row offset","STORAGE");
        if(location) *location=static_cast<uint64_t>(position);
        row=decode(); ++consumed_; return true;
    }
    Row at(uint64_t offset) {
        if(offset>uint64_t(std::numeric_limits<std::streamoff>::max())) fail("row offset out of range","STORAGE");
        input_.clear();input_.seekg(static_cast<std::streamoff>(offset));
        if(!input_) fail("row seek failed","STORAGE");
        return decode();
    }
    void keys(const std::function<void(int64_t)>& visit) {
        if(!count_) {
            Row row;
            while(next(row)) visit(std::get<int64_t>(row[primary_index(table_)]));
            return;
        }
        std::ifstream source(binary_?path_:std::filesystem::path(path_.string()+".keys"),std::ios::binary);
        if(!source) fail("cannot open key directory","STORAGE");
        if(binary_) source.seekg(static_cast<std::streamoff>(footer_));
        const size_t width=binary_?16:8;
        std::array<char,65536> buffer{};
        uint64_t remaining=*count_,prior=0;
        while(remaining) {
            const size_t entries=static_cast<size_t>(std::min<uint64_t>(remaining,buffer.size()/width));
            source.read(buffer.data(),static_cast<std::streamsize>(entries*width));
            if(size_t(source.gcount())!=entries*width) fail("truncated key directory","STORAGE");
            for(size_t i=0;i<entries;++i) {
                uint64_t bits=integer(buffer.data()+i*width,8); int64_t key; std::memcpy(&key,&bits,8);
                if(binary_) {
                    const uint64_t offset=integer(buffer.data()+i*width+8,8);
                    if(offset<32||offset>=footer_||offset<=prior) fail("invalid key row offset","STORAGE");
                    prior=offset;
                }
                visit(key);
            }
            remaining-=entries;
        }
    }
};
}
