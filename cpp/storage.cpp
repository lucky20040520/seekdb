#include "minivector.hpp"
#include "profile.hpp"
#include "segment.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <limits>
#include <map>
#include <random>
#include <set>
#include <sstream>
#include <system_error>
#include <unordered_set>
#ifdef _WIN32
#include <io.h>
#else
#include <fcntl.h>
#include <unistd.h>
#endif

namespace mv {
namespace {
namespace fs = std::filesystem;
constexpr char kMagic[8] = {'M','V','F','L','A','T','1','\0'};

[[noreturn]] void io_error(const std::string& message) {
    throw std::system_error(errno ? errno : EIO, std::generic_category(), message);
}

class Output {
public:
    explicit Output(const fs::path& path) : file_(std::fopen(path.string().c_str(), "wb")) {
        if (!file_) io_error("cannot open " + path.string());
    }
    ~Output() { if (file_) std::fclose(file_); }
    Output(const Output&) = delete;
    Output& operator=(const Output&) = delete;
    void write(const void* data,size_t bytes) {
        if (bytes && std::fwrite(data,1,bytes,file_) != bytes) io_error("write failed");
    }
    void write(const std::string& data) { write(data.data(),data.size()); }
    uint64_t position() {
#ifdef _WIN32
        auto result=_ftelli64(file_);
#else
        auto result=::ftello(file_);
#endif
        if(result<0) io_error("cannot obtain file position");
        return static_cast<uint64_t>(result);
    }
    void seek(uint64_t offset) {
        if(offset>uint64_t(std::numeric_limits<int64_t>::max())) io_error("file position out of range");
#ifdef _WIN32
        if(_fseeki64(file_,static_cast<int64_t>(offset),SEEK_SET)) io_error("cannot seek output");
#else
        if(::fseeko(file_,static_cast<off_t>(offset),SEEK_SET)) io_error("cannot seek output");
#endif
    }
    void durable() {
        profile::Scope timer(profile::Phase::file_sync);
        if (std::fflush(file_)) io_error("flush failed");
#ifdef _WIN32
        if (_commit(_fileno(file_))) io_error("file sync failed");
#else
        if (::fsync(fileno(file_))) io_error("file sync failed");
#endif
        FILE* closed=file_; file_=nullptr;
        if (std::fclose(closed)) io_error("close failed");
    }
private:
    FILE* file_;
};

std::ifstream input(const fs::path& path) {
    std::ifstream source(path,std::ios::binary);
    if (!source) io_error("cannot open " + path.string());
    return source;
}

void put_le(Output& output,uint64_t value,size_t width) {
    std::array<unsigned char,8> bytes{};
    for (size_t i=0;i<width;++i) bytes[i]=static_cast<unsigned char>(value>>(8*i));
    output.write(bytes.data(),width);
}

uint64_t get_le(const unsigned char* bytes,size_t width) {
    uint64_t value=0;
    for (size_t i=0;i<width;++i) value |= uint64_t(bytes[i])<<(8*i);
    return value;
}

// Keep small insert directories in memory. Large rewrites spill at 256 KiB;
// the spool is copied into the final segment and never needs its own fsync.
class KeyDirectory {
    fs::path temporary_;
    std::string buffer_;
    std::fstream spool_;
public:
    explicit KeyDirectory(fs::path temporary):temporary_(std::move(temporary)) {buffer_.reserve(4096);}
    ~KeyDirectory() {
        if(spool_.is_open()) spool_.close();
        std::error_code error; fs::remove(temporary_,error);
    }
    void add(int64_t key,uint64_t offset) {
        std::string entry; entry.reserve(16);
        segment::append(entry,static_cast<uint64_t>(key),8); segment::append(entry,offset,8);
        if(!spool_.is_open() && buffer_.size()+16<=256*1024) {buffer_+=entry;return;}
        if(!spool_.is_open()) {
            spool_.open(temporary_,std::ios::in|std::ios::out|std::ios::binary|std::ios::trunc);
            if(!spool_) io_error("cannot create key spool");
            spool_.write(buffer_.data(),static_cast<std::streamsize>(buffer_.size())); buffer_.clear();
        }
        spool_.write(entry.data(),16); if(!spool_) io_error("cannot write key spool");
    }
    void copy_to(Output& output) {
        if(!spool_.is_open()) {output.write(buffer_);return;}
        spool_.flush(); if(!spool_) io_error("cannot flush key spool");
        spool_.seekg(0); if(!spool_) io_error("cannot rewind key spool");
        std::array<char,65536> block;
        while(spool_.read(block.data(),block.size()) || spool_.gcount())
            output.write(block.data(),static_cast<size_t>(spool_.gcount()));
        if(spool_.bad()) io_error("cannot read key spool");
    }
};

Table decode_table(const Json& value) {
    Table table;
    for (const auto& c:value.at("columns")) {
        Column column;
        column.name=c.at("name").get<std::string>();
        column.kind=c.at("kind").get<std::string>();
        column.primary=c.value("primary",false);
        if (c.contains("size") && !c["size"].is_null()) column.size=c["size"].get<int64_t>();
        table.columns.push_back(std::move(column));
    }
    if (value.contains("files")) table.files=value.at("files").get<std::vector<std::string>>();
    else if (value.contains("file") && !value["file"].is_null()) table.files.push_back(value["file"].get<std::string>());
    table.indexes=value.value("indexes",Json::object());
    return table;
}

// Count Unicode scalar values, including rejection of overlong encodings and
// UTF-16 surrogate values. VARCHAR length is characters, not UTF-8 bytes.
size_t utf8_length(const std::string& value) {
    size_t result=0;
    for (size_t i=0;i<value.size();) {
        const unsigned char first=static_cast<unsigned char>(value[i++]);
        uint32_t scalar;
        size_t trailing;
        uint32_t minimum;
        if (first<0x80) { scalar=first; trailing=0; minimum=0; }
        else if (first>=0xc2 && first<=0xdf) { scalar=first&0x1f; trailing=1; minimum=0x80; }
        else if (first>=0xe0 && first<=0xef) { scalar=first&0x0f; trailing=2; minimum=0x800; }
        else if (first>=0xf0 && first<=0xf4) { scalar=first&0x07; trailing=3; minimum=0x10000; }
        else fail("invalid Unicode string","TYPE");
        if (trailing>value.size()-i) fail("invalid Unicode string","TYPE");
        for (size_t j=0;j<trailing;++j) {
            const unsigned char next=static_cast<unsigned char>(value[i++]);
            if ((next&0xc0)!=0x80) fail("invalid Unicode string","TYPE");
            scalar=(scalar<<6)|(next&0x3f);
        }
        if (scalar<minimum || scalar>0x10ffff || (scalar>=0xd800 && scalar<=0xdfff)) fail("invalid Unicode string","TYPE");
        ++result;
    }
    return result;
}

bool removable(const std::string& name) {
    const auto ends=[&](const std::string& suffix) { return name.size()>=suffix.size() && name.compare(name.size()-suffix.size(),suffix.size(),suffix)==0; };
    return (name.rfind("rows-",0)==0 && (ends(".jsonl") || ends(".keys") || ends(".mvb") || ends(".keys.tmp"))) ||
           (name.rfind("index-",0)==0 && ends(".flat")) ||
           (name.rfind("sort-",0)==0 && ends(".jsonl")) ||
           (name.rfind("catalog-",0)==0 && ends(".tmp")) ||
           (name.rfind("response-",0)==0 && ends(".tmp"));
}

void remove_quietly(const fs::path& path) { std::error_code error; fs::remove(path,error); }

bool valid_index(const fs::path& directory,const std::string& rows,const Json& filename,
                 const Table& table,const std::string& column) {
    if (!filename.is_string() || filename.get_ref<const std::string&>().empty()) return false;
    const size_t vector_column=column_index(table,column);
    const auto dimension=table.columns[vector_column].size;
    if (dimension<0 || uint64_t(dimension)>std::numeric_limits<uint32_t>::max() || vector_column>std::numeric_limits<uint32_t>::max()) return false;
    const fs::path path=directory/filename.get<std::string>();
    std::ifstream source(path,std::ios::binary);
    std::array<unsigned char,16> header{};
    if (!source.read(reinterpret_cast<char*>(header.data()),header.size())) return false;
    if (std::memcmp(header.data(),kMagic,8) || get_le(header.data()+8,4)!=uint64_t(dimension) || get_le(header.data()+12,4)!=vector_column) return false;
    std::error_code error;
    const uint64_t size=fs::file_size(path,error);
    const uint64_t width=16+4*uint64_t(dimension);
    if (error || size<16 || (size-16)%width) return false;
    const auto count=segment::Reader(directory/rows,table).count();
    if(count && (size-16)/width!=*count) return false;
    return true;
}

std::string build_index(const fs::path& directory,const std::string& rows,const Table& table,const std::string& column) {
    const size_t vector_column=column_index(table,column), pk=primary_index(table);
    const int64_t dimension=table.columns[vector_column].size;
    if (dimension<=0 || uint64_t(dimension)>std::numeric_limits<uint32_t>::max() || vector_column>std::numeric_limits<uint32_t>::max()) fail("index dimension out of range","SCHEMA");
    const std::string filename=unique_name("index-",".flat");
    const auto target=directory/filename;
    try {
        segment::Reader source(directory/rows,table);
        Output output(target);
        output.write(kMagic,8);
        put_le(output,static_cast<uint32_t>(dimension),4);
        put_le(output,vector_column,4);
        Row row; uint64_t offset=0;
        while (source.next(row,&offset)) {
            put_le(output,static_cast<uint64_t>(std::get<int64_t>(row[pk])),8);
            put_le(output,static_cast<uint64_t>(offset),8);
            for (float value:std::get<std::vector<float>>(row[vector_column])) {
                uint32_t bits;
                static_assert(sizeof(value)==sizeof(bits));
                std::memcpy(&bits,&value,sizeof(bits));
                put_le(output,bits,4);
            }
        }
        output.durable();
        return filename;
    } catch (...) { remove_quietly(target); throw; }
}
}

std::string unique_name(const std::string& prefix,const std::string& suffix) {
    static std::atomic<uint64_t> counter{0};
    static const uint64_t seed=[] { std::random_device random; return (uint64_t(random())<<32)^random(); }();
    const uint64_t now=static_cast<uint64_t>(std::chrono::high_resolution_clock::now().time_since_epoch().count());
    std::ostringstream result;
    result<<prefix<<std::hex<<seed<<now<<counter.fetch_add(1)<<suffix;
    return result.str();
}

void sync_directory(const std::filesystem::path& path) {
#ifndef _WIN32
    profile::Scope timer(profile::Phase::directory_sync);
    const int descriptor=::open(path.c_str(),O_RDONLY|O_DIRECTORY);
    if (descriptor<0) io_error("cannot open data directory");
    const int status=::fsync(descriptor), saved=errno;
    ::close(descriptor);
    if (status) { errno=saved; io_error("directory sync failed"); }
#else
    (void)path;
#endif
}

Json cell_json(const Cell& cell) {
    return std::visit([](const auto& value)->Json { return Json(value); },cell);
}

Json row_json(const Row& row) {
    Json result=Json::array();
    for (const auto& cell:row) result.push_back(cell_json(cell));
    return result;
}

Cell validate(const Column& column,const Json& value,bool for_storage) {
    if (column.kind=="BIGINT") {
        if (!value.is_number_integer() || (value.is_number_unsigned() && value.get<uint64_t>()>uint64_t(std::numeric_limits<int64_t>::max()))) fail("BIGINT value out of range or wrong type","TYPE");
        return value.get<int64_t>();
    }
    if (column.kind=="VARCHAR") {
        if (!value.is_string()) fail("VARCHAR type or length mismatch","TYPE");
        const auto& text=value.get_ref<const std::string&>();
        const size_t length=utf8_length(text);
        if (for_storage && (column.size<0 || uint64_t(length)>uint64_t(column.size))) fail("VARCHAR type or length mismatch","TYPE");
        return text;
    }
    if (column.kind!="VECTOR") fail("unsupported type","SCHEMA");
    Json decoded;
    const Json* array=&value;
    if (value.is_string()) {
        // String vectors found inside a stored row are already covered by the
        // row-decode timer; avoid attributing the same interval twice.
        profile::Scope timer(profile::Phase::vector_literal_parse,profile::stored_decode_depth==0);
        try { decoded=Json::parse(value.get_ref<const std::string&>()); }
        catch (const Json::exception&) { fail("invalid VECTOR array literal","TYPE"); }
        array=&decoded;
    }
    if (!array->is_array() || column.size<0 || array->size()!=uint64_t(column.size)) fail("VECTOR dimension mismatch","TYPE");
    std::vector<float> result;
    result.reserve(array->size());
    for (const auto& element:*array) {
        if (!element.is_number()) fail("VECTOR element must be numeric","TYPE");
        const double number=element.get<double>();
        const float stored=static_cast<float>(number);
        if (!std::isfinite(number) || !std::isfinite(stored)) fail("VECTOR requires finite float32","TYPE");
        result.push_back(stored);
    }
    return result;
}

size_t column_index(const Table& table,const std::string& name) {
    for (size_t i=0;i<table.columns.size();++i) if (table.columns[i].name==name) return i;
    fail("unknown column: "+name,"SCHEMA");
}

size_t primary_index(const Table& table) {
    for (size_t i=0;i<table.columns.size();++i) if (table.columns[i].primary) return i;
    fail("missing primary key","SCHEMA");
}

bool Predicate::matches(const Cell& actual) const {
    return op=="=" ? actual==value : op=="<" ? actual<value : op=="<=" ? actual<=value : op==">" ? actual>value : actual>=value;
}

bool Predicates::operator()(const Row& row) const {
    profile::Scope timer(profile::Phase::filter,!tests.empty());
    for (const auto& test:tests) if (!test.matches(row[test.column])) return false;
    return true;
}

Predicates compile_conditions(const Table& table,const std::vector<Condition>& conditions) {
    Predicates result;
    for (const auto& condition:conditions) {
        const size_t index=column_index(table,condition.column);
        if (table.columns[index].kind=="VECTOR") fail("vector comparison unsupported");
        if (condition.op!="=" && condition.op!="<" && condition.op!="<=" && condition.op!=">" && condition.op!=">=") fail("unsupported comparison operator");
        result.tests.push_back({index,condition.op,validate(table.columns[index],condition.value,false)});
    }
    return result;
}

Database::Database(const fs::path& directory) : path(directory) {
    fs::create_directories(path);
    const auto catalog_path=path/"catalog.json";
    if (fs::exists(catalog_path)) {
        auto source=input(catalog_path);
        source>>catalog_;
        if (!catalog_.is_object()) fail("invalid catalog","IO");
    }
    collect();
    Json recovered=catalog_;
    if (prepare_indexes(recovered)) { publish(recovered); collect(); }
}

const Json& Database::visible() const { return transaction_ ? *transaction_ : catalog_; }

Table Database::table(const std::string& name) const {
    const auto found=visible().find(name);
    if (found==visible().end()) fail("unknown table: "+name,"SCHEMA");
    return decode_table(*found);
}

void Database::collect() {
    profile::Scope timer(profile::Phase::collect);
    std::unordered_set<std::string> live;
    const auto add=[&](const Json& catalog) {
        for (const auto& value:catalog) {
            const Table table=decode_table(value);
            for (const auto& file:table.files) { live.insert(file); live.insert(file+".keys"); }
            for (const auto& descriptor:table.indexes) {
                if (descriptor.contains("segments")) for (const auto& filename:descriptor["segments"]) live.insert(filename.get<std::string>());
            }
        }
    };
    add(catalog_);
    if (transaction_) add(*transaction_);
    if (cache_ && std::any_of(cache_signature_.begin(),cache_signature_.end(),[&](const std::string& file) { return !live.count(file); })) { cache_.reset(); cache_signature_.clear(); }
    for (const auto& entry:fs::directory_iterator(path)) {
        const auto filename=entry.path().filename().string();
        if (removable(filename) && !live.count(filename)) fs::remove(entry.path());
    }
}

void Database::publish(const Json& catalog) {
    Json published=catalog;
    const auto temporary=path/unique_name("catalog-",".tmp");
    Output output(temporary);
    output.write(published.dump());
    output.durable();
    sync_directory(path);
    fs::rename(temporary,path/"catalog.json");
    // The catalog rename is the commit point. Update visible state before a
    // subsequent directory-sync error so collection preserves committed files.
    catalog_=std::move(published);
    sync_directory(path);
}

void Database::accept(const Json& catalog) {
    if (transaction_) *transaction_=catalog;
    else publish(catalog);
    collect();
}

void Database::begin() {
    if (transaction_) fail("transaction already active","TRANSACTION");
    transaction_=catalog_;
}

void Database::commit() {
    if (!transaction_) fail("no active transaction","TRANSACTION");
    publish(*transaction_);
    transaction_.reset();
    collect();
}

void Database::rollback() {
    if (!transaction_) fail("no active transaction","TRANSACTION");
    transaction_.reset();
    collect();
}

void Database::close() { transaction_.reset(); collect(); }

void Database::create(const std::string& name,const std::vector<Column>& columns) {
    if (visible().contains(name)) fail("table exists","SCHEMA");
    std::set<std::string> names;
    size_t primaries=0,vectors=0;
    Json encoded=Json::array();
    for (const auto& column:columns) {
        if (!names.insert(column.name).second) fail("duplicate or empty columns","SCHEMA");
        if (column.primary) { ++primaries; if (column.kind!="BIGINT") fail("exactly one BIGINT primary key required","SCHEMA"); }
        if (column.kind=="VECTOR") ++vectors;
        if (column.kind!="BIGINT" && column.kind!="VARCHAR" && column.kind!="VECTOR") fail("unsupported type","SCHEMA");
        if (column.kind!="BIGINT" && column.size<=0) fail("positive type size required","SCHEMA");
        encoded.push_back({{"name",column.name},{"kind",column.kind},{"size",column.size},{"primary",column.primary}});
    }
    if (columns.empty()) fail("duplicate or empty columns","SCHEMA");
    if (primaries!=1) fail("exactly one BIGINT primary key required","SCHEMA");
    if (vectors>1) fail("at most one vector column","SCHEMA");
    Json catalog=visible();
    catalog[name]={{"columns",encoded},{"file",nullptr},{"indexes",Json::object()}};
    accept(catalog);
}

void Database::scan(const Table& table,const RowVisitor& visit) const {
    for (const auto& file:table.files) {
        segment::Reader source(path/file,table);
        Row row;
        while(source.next(row)) visit(row);
    }
}

Row Database::read_row(const Table& table,const std::string& file,uint64_t offset) const {
    return segment::Reader(path/file,table).at(offset);
}

std::optional<uint64_t> Database::row_count(const Table& table,const std::string& file) const {
    return segment::Reader(path/file,table).count();
}

Row VectorCache::materialize(size_t row) const {
    Row result; result.reserve(columns);
    for (size_t column=0;column<columns;++column) {
        if (column==vector_column) result.emplace_back(std::vector<float>(vector(row),vector(row)+dimension));
        else result.push_back(cell(row,column));
    }
    return result;
}

bool VectorCache::matches(size_t row,const Predicates& conditions) const {
    profile::Scope timer(profile::Phase::filter,!conditions.tests.empty());
    for (const auto& test:conditions.tests) if (!test.matches(cell(row,test.column))) return false;
    return true;
}

const std::vector<uint32_t>* VectorCache::matching_rows(const Predicates& conditions,size_t* covered) {
    profile::Scope timer(profile::Phase::filter_lists,!conditions.tests.empty());
    if (covered) *covered=conditions.tests.size();
    Posting* best=nullptr;
    const Predicate* build=nullptr;
    size_t best_test=0,build_test=0;
    for (size_t i=0;i<conditions.tests.size();++i) {
        const auto& test=conditions.tests[i];
        if (test.op!="=") continue;
        if (!build) { build=&test; build_test=i; }
        for (auto& slot:postings) if (slot && slot->column==test.column && slot->value==test.value) {
            if (!best || slot->rows.size()<best->rows.size()) { best=&*slot; best_test=i; }
        }
    }
    if (best) { best->used=++clock; if (covered) *covered=best_test; return &best->rows; }
    if (!build) return nullptr;
    const auto* text=std::get_if<std::string>(&build->value);
    const size_t key_bytes=text ? text->capacity()+1 : 0;
    const size_t available=std::min(posting_budget,budget-data_bytes);
    if (key_bytes>available) return nullptr;
    size_t count=0;
    for (size_t row=0;row<size();++row) if (cell(row,build->column)==build->value) {
        if (++count>(available-key_bytes)/sizeof(uint32_t)) return nullptr;
    }
    const size_t bytes=key_bytes+count*sizeof(uint32_t);
    size_t empty=postings.size();
    while (true) {
        size_t oldest=postings.size();
        for (size_t i=0;i<postings.size();++i) {
            if (!postings[i]) empty=i;
            else if (oldest==postings.size() || postings[i]->used<postings[oldest]->used) oldest=i;
        }
        if (empty<postings.size() && posting_bytes<=available-bytes) break;
        posting_bytes-=postings[oldest]->bytes; postings[oldest].reset(); empty=oldest;
    }
    Posting posting{build->column,build->value,{},++clock,0};
    posting.rows.reserve(count);
    for (size_t row=0;row<size();++row) if (cell(row,build->column)==build->value) posting.rows.push_back(static_cast<uint32_t>(row));
    const auto* stored_text=std::get_if<std::string>(&posting.value);
    posting.bytes=posting.rows.capacity()*sizeof(uint32_t)+(stored_text ? stored_text->capacity()+1 : 0);
    if (posting.bytes>available-posting_bytes) return nullptr;
    posting_bytes+=posting.bytes; postings[empty]=std::move(posting);
    if (covered) *covered=build_test;
    return &postings[empty]->rows;
}

std::shared_ptr<VectorCache> Database::cached_vectors(const Table& table) {
    if (table.files.empty()) return {};
    if (cache_ && cache_signature_==table.files) return cache_;
    cache_.reset(); cache_signature_.clear();
    auto cache=std::make_shared<VectorCache>();
    cache->columns=table.columns.size();
    for (size_t i=0;i<table.columns.size();++i) if (table.columns[i].kind=="VECTOR") {
        if (table.columns[i].size<=0 || uint64_t(table.columns[i].size)>VectorCache::budget/sizeof(float)) return {};
        cache->vector_column=i; cache->dimension=static_cast<size_t>(table.columns[i].size);
    }
    if (!cache->dimension || cache->columns>VectorCache::budget/sizeof(Cell)) return {};
    const size_t minimum=(cache->columns-1)*sizeof(Cell)+cache->dimension*sizeof(float);
    size_t overhead=sizeof(VectorCache)+256+table.files.size()*sizeof(std::string);
    for (const auto& file:table.files) overhead+=file.capacity()+1;
    if (overhead>VectorCache::budget) return {};
    const size_t max_rows=(VectorCache::budget-overhead)/minimum;
    // Binary headers and legacy sidecars provide counts without decoding rows.
    uint64_t count=0;
    size_t binary_payload_bytes=0;
    for (const auto& file:table.files) {
        const auto rows=row_count(table,file);
        if(!rows || *rows>max_rows || count>max_rows-*rows) return {};
        count+=*rows;
        if(fs::path(file).extension()==".mvb") {
            const auto bytes=fs::file_size(path/file), framing=32+24*(*rows);
            if(bytes<framing) fail("invalid binary segment size","STORAGE");
            // Typed payload bytes are a lower bound on cache storage: every
            // scalar Cell is at least eight bytes, with string storage extra.
            // Reject wide tables from headers/sizes before a partial build.
            const auto payload=bytes-framing;
            if(payload>VectorCache::budget-overhead-binary_payload_bytes) return {};
            binary_payload_bytes+=static_cast<size_t>(payload);
        }
    }
    cache->vectors.reserve(static_cast<size_t>(count)*cache->dimension);
    cache->scalars.reserve(static_cast<size_t>(count)*(cache->columns-1));
    cache->data_bytes=overhead+cache->vectors.capacity()*sizeof(float)+cache->scalars.capacity()*sizeof(Cell);
    if (cache->data_bytes>VectorCache::budget) return {};
    struct TooLarge {};
    try {
        scan(table,[&](const Row& row) {
            if (cache->size()>=count) throw TooLarge{};
            for (size_t i=0;i<row.size();++i) if (i!=cache->vector_column) {
                if (const auto* text=std::get_if<std::string>(&row[i])) {
                    if (text->capacity()+1>VectorCache::budget-cache->data_bytes) throw TooLarge{};
                    cache->data_bytes+=text->capacity()+1;
                }
                cache->scalars.push_back(row[i]);
            }
            const auto& vector=std::get<std::vector<float>>(row[cache->vector_column]);
            cache->vectors.insert(cache->vectors.end(),vector.begin(),vector.end());
        });
    } catch (const TooLarge&) { return {}; }
    cache_signature_=table.files;
    cache_=std::move(cache);
    return cache_;
}

void Database::rewrite(const std::string& name,const std::function<void(const RowVisitor&)>& producer,bool append) {
    Json catalog=visible();
    const Table schema=decode_table(catalog.at(name));
    const size_t pk=primary_index(schema);
    const std::string filename=unique_name("rows-",".mvb");
    const auto target=path/filename, key_target=path/(filename+".keys.tmp");
    try {
        Output output(target);
        KeyDirectory keys(key_target);
        if(schema.columns.size()>std::numeric_limits<uint32_t>::max()) fail("too many columns","SCHEMA");
        output.write(segment::magic,8); put_le(output,schema.columns.size(),4); put_le(output,0,4);
        put_le(output,0,8); put_le(output,0,8);
        uint64_t count=0;
        producer([&](const Row& row) {
            const auto offset=output.position();
            std::string encoded=segment::encode(row);
            put_le(output,encoded.size(),8);
            output.write(encoded);
            keys.add(std::get<int64_t>(row[pk]),offset); ++count;
        });
        const auto footer=output.position();
        keys.copy_to(output);
        output.seek(16); put_le(output,count,8); put_le(output,footer,8);
        output.durable();
        if (append) {
            auto files=schema.files; files.push_back(filename);
            catalog[name]["files"]=files; catalog[name]["file"]=nullptr;
        } else {
            catalog[name].erase("files"); catalog[name]["file"]=filename;
        }
        prepare_indexes(catalog,name);
    } catch (...) {
        remove_quietly(target); remove_quietly(key_target); throw;
    }
    // accept() may already have published the catalog when directory fsync
    // fails. Do not unlink files in that case: recovery must still find them.
    accept(catalog);
}

int64_t Database::insert(const std::string& name,const std::vector<Json>& values,const std::optional<std::vector<std::string>>& column_names) {
    const Table schema=table(name);
    std::vector<std::string> supplied;
    for (const auto& column:schema.columns) supplied.push_back(column.name);
    if (column_names) supplied=*column_names;
    std::set<std::string> unique(supplied.begin(),supplied.end());
    if (unique.size()!=supplied.size() || supplied.size()!=schema.columns.size()) fail("INSERT must supply each column once","SCHEMA");
    std::vector<size_t> positions;
    for (const auto& column:schema.columns) {
        const auto position=std::find(supplied.begin(),supplied.end(),column.name);
        if (position==supplied.end()) fail("INSERT must supply each column once","SCHEMA");
        positions.push_back(static_cast<size_t>(position-supplied.begin()));
    }
    const size_t pk=primary_index(schema);
    std::unordered_set<int64_t> keys;
    std::vector<Row> added;
    added.reserve(values.size());
    for (const auto& value:values) {
        if (!value.is_array() || value.size()!=supplied.size()) fail("INSERT value count mismatch");
        Row row; row.reserve(schema.columns.size());
        for (size_t i=0;i<schema.columns.size();++i) row.push_back(validate(schema.columns[i],value[positions[i]]));
        if (!keys.insert(std::get<int64_t>(row[pk])).second) fail("duplicate primary key","DUPLICATE");
        added.push_back(std::move(row));
    }
    for (const auto& file:schema.files)
        segment::Reader(path/file,schema).keys([&](int64_t key) {
            if(keys.count(key)) fail("duplicate primary key","DUPLICATE");
        });
    rewrite(name,[&](const RowVisitor& emit) { for (const auto& row:added) emit(row); },true);
    return static_cast<int64_t>(added.size());
}

int64_t Database::modify(const std::string& name,const std::vector<Condition>& conditions,const std::optional<std::vector<Assignment>>& assignments) {
    const Table schema=table(name);
    const auto matches=compile_conditions(schema,conditions);
    std::map<size_t,Cell> changes;
    if (assignments) for (const auto& assignment:*assignments) {
        const size_t index=column_index(schema,assignment.column);
        if (schema.columns[index].primary || changes.count(index)) fail("primary key update or duplicate assignment unsupported");
        changes.emplace(index,validate(schema.columns[index],assignment.value));
    }
    int64_t count=0;
    rewrite(name,[&](const RowVisitor& emit) {
        scan(schema,[&](const Row& original) {
            if (!matches(original)) { emit(original); return; }
            ++count;
            if (!assignments) return;
            Row row=original;
            for (const auto& change:changes) row[change.first]=change.second;
            emit(row);
        });
    });
    return count;
}

void Database::create_index(const std::string& name,const std::string& table_name,const std::string& column) {
    const Table schema=table(table_name);
    for (const auto& value:visible()) if (value.at("indexes").contains(name)) fail("index exists","SCHEMA");
    const auto found=std::find_if(schema.columns.begin(),schema.columns.end(),[&](const Column& c) { return c.name==column && c.kind=="VECTOR"; });
    if (found==schema.columns.end()) fail("index requires VECTOR column","SCHEMA");
    Json catalog=visible();
    catalog[table_name]["indexes"][name]={{"column",column}};
    prepare_indexes(catalog,table_name);
    accept(catalog);
}

bool Database::prepare_indexes(Json& catalog,const std::optional<std::string>& only) {
    bool changed=false;
    std::vector<std::string> created;
    try {
        for (auto entry=catalog.begin();entry!=catalog.end();++entry) {
            if (only && *only!=entry.key()) continue;
            const Table schema=decode_table(entry.value());
            for (auto& descriptor:entry.value()["indexes"]) {
                const std::string column=descriptor.at("column").get<std::string>();
                Json segments=Json::object();
                const Json old=descriptor.value("algorithm",std::string())=="flat_l2_v1" ? descriptor.value("segments",Json::object()) : Json::object();
                for (const auto& row_file:schema.files) {
                    Json index_file=old.value(row_file,Json());
                    if (!valid_index(path,row_file,index_file,schema,column)) {
                        index_file=build_index(path,row_file,schema,column);
                        created.push_back(index_file.get<std::string>());
                    }
                    segments[row_file]=index_file;
                }
                if (descriptor.value("algorithm",std::string())!="flat_l2_v1" || descriptor.value("segments",Json())!=segments) changed=true;
                descriptor["algorithm"]="flat_l2_v1";
                descriptor["segments"]=std::move(segments);
            }
        }
    } catch (...) { for (const auto& filename:created) remove_quietly(path/filename); throw; }
    return changed;
}
}
