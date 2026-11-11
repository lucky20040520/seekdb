#include "minivector.hpp"
#include <algorithm>
#include <array>
#include <cmath>
#include <cstring>
#include <fstream>
#include <limits>
#include <map>
#include <queue>
#include <system_error>

namespace mv {
double l2_distance(const std::vector<float>& a,const std::vector<float>& b) {
    if(a.size()!=b.size()) fail("VECTOR dimension mismatch","TYPE");
    // Sum non-overlapping partials like math.fsum. Even long double loses tiny
    // terms at a binary64 halfway boundary, which can change the TopK tie set.
    // Binary32 squared differences span fewer than 600 exponent bits, so 32
    // partials are ample; use a vector to avoid relying on that bound.
    std::array<double,32> local;
    std::vector<double> overflow;
    double* partials=local.data(); size_t size=0,capacity=local.size();
    for(size_t i=0;i<a.size();++i) {
        double delta=double(a[i])-double(b[i]); double x=delta*delta;
        size_t count=0;
        for(size_t j=0;j<size;++j) {
            double y=partials[j]; if(std::abs(x)<std::abs(y)) std::swap(x,y);
            double high=x+y,low=y-(high-x);
            if(low!=0) partials[count++]=low;
            x=high;
        }
        if(count==capacity) {
            if(overflow.empty()) overflow.assign(partials,partials+size);
            capacity*=2; overflow.resize(capacity); partials=overflow.data();
        }
        partials[count]=x; size=count+1;
    }
    double high=0,low=0;
    if(size) {
        high=partials[--size];
        while(size) {
            double x=high,y=partials[--size]; high=x+y; low=y-(high-x);
            if(low!=0) break;
        }
        // Correct a half-even boundary using the sign of remaining partials.
        if(size&&((low<0&&partials[size-1]<0)||(low>0&&partials[size-1]>0))) {
            double twice=low*2,rounded=high+twice;
            if(rounded-high==twice) high=rounded;
        }
    }
    return std::sqrt(high);
}
namespace {
using Key=std::variant<int64_t,std::string,double>;
double approximate_l2(const std::vector<float>& a,const std::vector<float>& b) {
    // Independent accumulators permit SIMD without reassociating the accurate
    // distance function. This estimate only rejects candidates outside a very
    // conservative floating-point error interval.
    double s0=0,s1=0,s2=0,s3=0; size_t i=0;
    for(;i+3<a.size();i+=4) {
        double d0=double(a[i])-double(b[i]),d1=double(a[i+1])-double(b[i+1]);
        double d2=double(a[i+2])-double(b[i+2]),d3=double(a[i+3])-double(b[i+3]);
        s0+=d0*d0;s1+=d1*d1;s2+=d2*d2;s3+=d3*d3;
    }
    double sum=(s0+s1)+(s2+s3);
    for(;i<a.size();++i) {double d=double(a[i])-double(b[i]);sum+=d*d;}
    return std::sqrt(sum);
}
struct Compiled {
    size_t index=0;
    bool distance=false;
    std::vector<float> vector;
    std::string kind;
    Json project(const Row& row) const { if(distance) return l2_distance(std::get<std::vector<float>>(row[index]),vector); return cell_json(row[index]); }
    Key key(const Row& row) const {
        if(distance) return l2_distance(std::get<std::vector<float>>(row[index]),vector);
        if(kind=="BIGINT") return std::get<int64_t>(row[index]);
        if(kind=="VARCHAR") return std::get<std::string>(row[index]);
        fail("ordering VECTOR unsupported");
    }
};
Compiled compile_expression(const Table& table,const Expr& expression) {
    size_t i=column_index(table,expression.column);
    const auto& column=table.columns[i];
    Compiled result; result.index=i; result.distance=expression.distance; result.kind=column.kind;
    if(expression.distance) {
        if(column.kind!="VECTOR") fail("distance requires VECTOR","TYPE");
        result.vector=std::get<std::vector<float>>(validate(column,expression.vector));
    }
    return result;
}
Row decode_row(const Table& table,const Json& value) {
    if(!value.is_array()||value.size()!=table.columns.size()) fail("invalid stored row","STORAGE");
    Row row; row.reserve(value.size());
    for(size_t i=0;i<value.size();++i) row.push_back(validate(table.columns[i],value[i]));
    return row;
}
struct Candidate { Key key; int64_t primary=0; Row row; std::string file; uint64_t offset=0; };
struct Less {
    bool descending=false;
    bool operator()(const Candidate& a,const Candidate& b) const {
        int comparison=std::visit([&](const auto& x)->int {
            using T=std::decay_t<decltype(x)>;
            const auto& y=std::get<T>(b.key);
            return x<y?-1:(y<x?1:0);
        },a.key);
        if(comparison) return descending?comparison>0:comparison<0;
        return a.primary<b.primary;
    }
};
Json candidate_json(const Candidate& value) {
    Json key=std::visit([](const auto& x)->Json{return x;},value.key);
    return Json::array({key,value.primary,row_json(value.row),value.file,value.offset});
}
Candidate decode_candidate(const Json& value,const Table& table,const Compiled& order) {
    Candidate result;
    if(order.distance) result.key=value[0].get<double>();
    else if(order.kind=="BIGINT") result.key=value[0].get<int64_t>();
    else result.key=value[0].get<std::string>();
    result.primary=value[1].get<int64_t>(); result.file=value[3].get<std::string>(); result.offset=value[4].get<uint64_t>();
    if(result.file.empty()) result.row=decode_row(table,value[2]);
    return result;
}
size_t memory_size(const Candidate& value) {
    size_t bytes=sizeof(Candidate)+value.file.capacity()+value.row.capacity()*sizeof(Cell);
    if(auto text=std::get_if<std::string>(&value.key)) bytes+=text->capacity();
    for(auto& cell:value.row) {
        if(auto text=std::get_if<std::string>(&cell)) bytes+=text->capacity();
        if(auto vector=std::get_if<std::vector<float>>(&cell)) bytes+=vector->capacity()*sizeof(float);
    }
    return bytes;
}
class ExternalSort {
    std::filesystem::path directory;
    Table table;
    Compiled order;
    Less less;
    std::vector<Candidate> batch;
    size_t bytes=0;
    std::vector<std::filesystem::path> paths;
    std::vector<std::filesystem::path> cleanup;
    std::filesystem::path new_path() { auto p=directory/unique_name("sort-",".jsonl"); cleanup.push_back(p); return p; }
    void spill() {
        std::sort(batch.begin(),batch.end(),less);
        auto path=new_path(); std::ofstream output(path,std::ios::binary);
        if(!output) fail("cannot create sort run","STORAGE");
        for(const auto& value:batch) output<<candidate_json(value).dump()<<'\n';
        output.close(); if(!output) fail("cannot write sort run","STORAGE");
        paths.push_back(path); batch.clear(); bytes=0;
    }
    void merge(const std::vector<std::filesystem::path>& group,const std::function<bool(const Candidate&)>& consume) {
        struct Head { Candidate value; size_t source; };
        auto later=[this](const Head& a,const Head& b){return less(b.value,a.value);};
        std::priority_queue<Head,std::vector<Head>,decltype(later)> heap(later);
        std::vector<std::ifstream> readers; readers.reserve(group.size());
        auto advance=[&](size_t index) {
            std::string line;
            if(std::getline(readers[index],line)) heap.push({decode_candidate(Json::parse(line),table,order),index});
            else if(!readers[index].eof()) fail("cannot read sort run","STORAGE");
        };
        for(size_t i=0;i<group.size();++i) { readers.emplace_back(group[i],std::ios::binary); if(!readers.back()) fail("cannot open sort run","STORAGE"); advance(i); }
        while(!heap.empty()) { Head head=heap.top(); heap.pop(); if(!consume(head.value)) break; advance(head.source); }
    }
public:
    ExternalSort(const std::filesystem::path& path,const Table& t,const Compiled& o,Less l):directory(path),table(t),order(o),less(l) {}
    ~ExternalSort() { for(auto& path:cleanup) {std::error_code error; std::filesystem::remove(path,error);} }
    void add(Candidate value) { bytes+=memory_size(value); batch.push_back(std::move(value)); if(bytes>=2*1024*1024) spill(); }
    void consume(const std::function<bool(const Candidate&)>& output) {
        if(paths.empty()) { std::sort(batch.begin(),batch.end(),less); for(auto& item:batch) if(!output(item)) break; return; }
        if(!batch.empty()) spill();
        while(paths.size()>32) {
            std::vector<std::filesystem::path> next;
            for(size_t begin=0;begin<paths.size();begin+=32) {
                size_t end=std::min(paths.size(),begin+32);
                std::vector<std::filesystem::path> group(paths.begin()+begin,paths.begin()+end);
                auto path=new_path(); std::ofstream target(path,std::ios::binary); if(!target) fail("cannot create merge run","STORAGE");
                merge(group,[&](const Candidate& value){target<<candidate_json(value).dump()<<'\n'; if(!target) fail("cannot write merge run","STORAGE"); return true;});
                target.close(); if(!target) fail("cannot finish merge run","STORAGE");
                next.push_back(path);
                for(auto& old:group) {std::error_code error; std::filesystem::remove(old,error);}
            }
            paths=std::move(next);
        }
        merge(paths,output);
    }
};
uint32_t read_u32(const char* p) { const auto* b=reinterpret_cast<const unsigned char*>(p); return uint32_t(b[0])|(uint32_t(b[1])<<8)|(uint32_t(b[2])<<16)|(uint32_t(b[3])<<24); }
uint64_t read_u64(const char* p) { return uint64_t(read_u32(p))|(uint64_t(read_u32(p+4))<<32); }
void indexed_candidates(Database& db,const Table& table,const Json& descriptor,const Compiled& order,
                        const std::function<bool(const Row&)>& matches,bool filtered,
                        const std::function<void(Candidate)>& accept) {
    size_t dimension=order.vector.size();
    if(dimension>(std::numeric_limits<size_t>::max()-16)/4) fail("invalid index dimension","STORAGE");
    std::vector<char> buffer(16+4*dimension); std::vector<float> vector(dimension);
    for(auto segment=descriptor.at("segments").begin();segment!=descriptor.at("segments").end();++segment) {
        std::string row_file=segment.key(); std::string index_file=segment.value().get<std::string>();
        std::ifstream source(db.path/index_file,std::ios::binary); if(!source) fail("cannot open flat index","STORAGE");
        std::array<char,16> header{}; source.read(header.data(),header.size());
        if(source.gcount()!=16||std::memcmp(header.data(),"MVFLAT1\0",8)!=0||read_u32(header.data()+8)!=dimension||read_u32(header.data()+12)!=order.index) fail("flat index header mismatch","STORAGE");
        std::ifstream rows;
        if(filtered) {rows.open(db.path/row_file,std::ios::binary);if(!rows) fail("cannot open indexed rows","STORAGE");}
        while(true) {
            source.read(buffer.data(),static_cast<std::streamsize>(buffer.size()));
            if(source.gcount()==0&&source.eof()) break;
            if(size_t(source.gcount())!=buffer.size()) fail("truncated flat index record","STORAGE");
            uint64_t key=read_u64(buffer.data()),offset=read_u64(buffer.data()+8); int64_t primary; std::memcpy(&primary,&key,sizeof(primary));
            if(filtered) {
                rows.clear(); rows.seekg(static_cast<std::streamoff>(offset)); std::string line;
                if(!std::getline(rows,line)) fail("invalid flat index row location","STORAGE");
                if(!matches(decode_row(table,Json::parse(line)))) continue;
            }
            for(size_t i=0;i<dimension;++i) { uint32_t bits=read_u32(buffer.data()+16+4*i); std::memcpy(&vector[i],&bits,sizeof(float)); }
            accept({l2_distance(vector,order.vector),primary,{},row_file,offset});
        }
    }
}
bool bounded_heap(const Table& table,uint64_t limit) {
    if(limit>4096) return false;
    long double row_bytes=sizeof(Candidate)+table.columns.size()*sizeof(Cell);
    for(auto& column:table.columns) {
        if(column.kind=="VARCHAR") row_bytes+=64+4*static_cast<long double>(column.size);
        else if(column.kind=="VECTOR") row_bytes+=64+4*static_cast<long double>(column.size);
        else row_bytes+=32;
    }
    return row_bytes*limit<=2*1024*1024;
}
struct LimitReached {};
}
Result Database::select(const std::string& name,const std::optional<std::vector<Projection>>& requested,
                        const std::vector<Condition>& conditions,const std::optional<Order>& order,
                        const std::optional<Json>& requested_limit,const std::string& mode) {
    if(mode!="exact"&&mode!="ann") fail("unknown search mode");
    Table selected=table(name); size_t pk=primary_index(selected);
    auto matches=compile_conditions(selected,conditions);
    std::optional<uint64_t> limit;
    if(requested_limit) {
        const Json& value=*requested_limit;
        if(!value.is_number_integer()||(!value.is_number_unsigned()&&value.get<int64_t>()<0)) fail("nonnegative integer LIMIT required");
        limit=value.get<uint64_t>();
    }
    std::vector<Projection> projections;
    if(requested) projections=*requested;
    else for(auto& column:selected.columns) projections.push_back({{false,column.name,Json()},column.name});
    std::vector<Compiled> functions; std::map<std::string,Compiled> aliases;
    Result result; result.query=true;
    for(auto& projection:projections) {auto fn=compile_expression(selected,projection.expression); functions.push_back(fn); result.columns.push_back(projection.alias); if(!projection.alias.empty()) aliases[projection.alias]=fn;}
    std::optional<Compiled> ordering;
    std::optional<Json> descriptor;
    bool descending=false;
    if(order) {
        descending=order->descending;
        auto alias=aliases.find(order->expression.column);
        ordering=(!order->expression.distance&&alias!=aliases.end())?alias->second:compile_expression(selected,order->expression);
        if(!ordering->distance&&ordering->kind=="VECTOR") fail("ordering VECTOR unsupported");
        if(ordering->distance) for(auto& index:selected.indexes) {
            if(index.at("column").get<std::string>()==selected.columns[ordering->index].name) { descriptor=index; break; }
        }
    }
    bool use_heap=ordering&&limit&&bounded_heap(selected,*limit);
    // A Flat index retains only locations in the heap, rather than full rows.
    if(descriptor&&limit&&*limit<=4096) use_heap=true;
    result.rows=[this,selected=std::move(selected),pk,matches=std::move(matches),functions=std::move(functions),ordering,descending,descriptor,limit,use_heap,filtered=!conditions.empty()](const JsonVisitor& output) {
        if(limit&&*limit==0) return;
        uint64_t emitted=0;
        auto emit_row=[&](const Row& row) {
            Json projected=Json::array(); for(auto& fn:functions) projected.push_back(fn.project(row));
            output(projected); ++emitted; return !limit||emitted<*limit;
        };
        if(!ordering) {
            try { scan(selected,[&](const Row& row){if(matches(row)&&!emit_row(row)) throw LimitReached{};}); } catch(const LimitReached&) {}
            return;
        }
        Less less{descending};
        std::priority_queue<Candidate,std::vector<Candidate>,Less> heap(less);
        std::unique_ptr<ExternalSort> sorter;
        if(!use_heap) sorter=std::make_unique<ExternalSort>(path,selected,*ordering,less);
        auto add=[&](Candidate value) {
            if(!use_heap) {sorter->add(std::move(value));return;}
            if(heap.size()<*limit) heap.push(std::move(value));
            else if(less(value,heap.top())) {heap.pop();heap.push(std::move(value));}
        };
        if(descriptor) indexed_candidates(*this,selected,*descriptor,*ordering,matches,filtered,add);
        else {
            auto visit=[&](const Row& row) {
                if(!matches(row)) return;
                if(use_heap&&ordering->distance&&heap.size()>=*limit) {
                    double estimate=approximate_l2(std::get<std::vector<float>>(row[ordering->index]),ordering->vector);
                    double tolerance=std::abs(estimate)*std::numeric_limits<double>::epsilon()*std::max<size_t>(1,ordering->vector.size())*16;
                    double boundary=std::get<double>(heap.top().key);
                    if((!descending&&estimate>std::nextafter(boundary+tolerance,std::numeric_limits<double>::infinity()))||
                       (descending&&estimate<std::nextafter(boundary-tolerance,-std::numeric_limits<double>::infinity()))) return;
                }
                Candidate value; value.key=ordering->key(row); value.primary=std::get<int64_t>(row[pk]);
                if(use_heap&&heap.size()>=*limit&&!less(value,heap.top())) return;
                value.row=row; add(std::move(value));
            };
            auto cached=(ordering->distance&&!descending&&use_heap)?cached_rows(selected):nullptr;
            if(cached) for(const auto& row:*cached) visit(row); else scan(selected,visit);
        }
        auto emit_candidate=[&](const Candidate& candidate) {
            if(candidate.file.empty()) return emit_row(candidate.row);
            return emit_row(read_row(selected,candidate.file,candidate.offset));
        };
        if(use_heap) {
            std::vector<Candidate> ordered; ordered.reserve(heap.size());
            while(!heap.empty()) {ordered.push_back(heap.top());heap.pop();}
            std::sort(ordered.begin(),ordered.end(),less);
            for(auto& candidate:ordered) if(!emit_candidate(candidate)) break;
        } else sorter->consume(emit_candidate);
    };
    return result;
}
}
