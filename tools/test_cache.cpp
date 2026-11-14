#include "minivector.hpp"
#include <iostream>

using namespace mv;
void require(bool condition,const char* message) {
    if(!condition) throw std::runtime_error(message);
}

VectorCache dense_cache(size_t rows) {
    VectorCache cache;
    cache.columns=33; cache.vector_column=16; cache.dimension=2;
    cache.vectors.resize(rows*cache.dimension);
    cache.scalars.reserve(rows*(cache.columns-1));
    for(size_t row=0;row<rows;++row) {
        for(size_t column=0;column<cache.columns;++column) if(column!=cache.vector_column)
            cache.scalars.emplace_back(column==0 ? int64_t(row) : int64_t(7));
        cache.vectors[row*2]=float(row); cache.vectors[row*2+1]=1.0f;
    }
    cache.data_bytes=sizeof(VectorCache)+cache.vectors.capacity()*sizeof(float)+cache.scalars.capacity()*sizeof(Cell);
    require(cache.data_bytes<VectorCache::budget,"test data must fit the cache");
    return cache;
}

int main() {
    auto cache=dense_cache(10000);
    for(size_t row=0;row<cache.size();++row) {
        require(cache.vector(row)==cache.vectors.data()+2*row,"vector storage is not contiguous");
        require(std::get<int64_t>(cache.cell(row,0))==int64_t(row),"scalar row mapping");
        require(std::get<int64_t>(cache.cell(row,32))==7,"scalar mapping after vector column");
    }
    const auto row=cache.materialize(123);
    require(std::get<std::vector<float>>(row[16])==std::vector<float>({123.0f,1.0f}),"vector projection");
    size_t dense_lists=0;
    for(size_t column=1;column<cache.columns;++column) if(column!=cache.vector_column) {
        Predicates predicates{{{column,"=",int64_t(7)}}};
        const auto* rows=cache.matching_rows(predicates);
        require(rows && rows->size()==10000,"dense list must be complete");
        require(rows->front()==0 && rows->back()==9999,"list row order");
        require(cache.posting_bytes<=VectorCache::posting_budget,"posting byte budget");
        require(cache.data_bytes+cache.posting_bytes<=VectorCache::budget,"shared byte budget");
    }
    for(const auto& slot:cache.postings) dense_lists+=bool(slot);
    require(dense_lists<31,"byte-budget eviction did not occur");
    Predicates primary{{{0,"=",int64_t(999)}}};
    require(cache.matching_rows(primary)->size()==1,"primary equality list");
    Predicates combined{{{32,"=",int64_t(7)},{0,"=",int64_t(999)},{1,"<",int64_t(7)}}};
    size_t covered=999;
    const auto* smallest=cache.matching_rows(combined,&covered);
    require(smallest && smallest->size()==1 && smallest->front()==999,"choose shortest cached list");
    require(covered==1,"covered predicate must identify the chosen list, not the first equality");
    auto residual=combined; residual.tests.erase(residual.tests.begin()+covered);
    require(!cache.matches(smallest->front(),residual),"remaining filters must reject the row");
    require(!cache.matches(smallest->front(),combined),"other predicates must still be checked");
    for(int64_t id=-40;id<40;++id) {
        Predicates predicates{{{0,"=",id},{1,">=",int64_t(7)}}};
        const auto* rows=cache.matching_rows(predicates);
        require(rows && rows->size()==size_t(id>=0),"LRU / empty-list correctness");
        if(id>=0) require(rows->front()==uint32_t(id) && cache.matches(id,predicates),"residual filter");
    }
    size_t resident=0,bytes=0;
    for(const auto& slot:cache.postings) if(slot) { ++resident; bytes+=slot->bytes; }
    require(resident==32 && bytes==cache.posting_bytes,"entry limit / accounting");
    Predicates range{{{0,">=",int64_t(0)}}};
    require(cache.matching_rows(range,&covered)==nullptr && covered==range.tests.size(),"range must retain all predicates");
    auto near_limit=dense_cache(13000);
    Predicates dense{{{1,"=",int64_t(7)}}}, selective{{{0,"=",int64_t(999)}}};
    require(near_limit.matching_rows(dense,&covered)==nullptr && covered==dense.tests.size(),"budget fallback must retain all predicates");
    const auto* rows=near_limit.matching_rows(selective);
    require(rows && rows->size()==1 && rows->front()==999,"a selective list should still fit");
    require(near_limit.data_bytes+near_limit.posting_bytes<=VectorCache::budget,"near-limit accounting");
    std::cout<<Json({{"passed",true},{"dense_lists_retained",dense_lists},
                    {"entry_limit",resident},{"cache_budget_bytes",VectorCache::budget},
                    {"posting_budget_bytes",VectorCache::posting_budget},
                    {"near_limit_data_bytes",near_limit.data_bytes},
                    {"near_limit_posting_bytes",near_limit.posting_bytes}}).dump()<<'\n';
}
