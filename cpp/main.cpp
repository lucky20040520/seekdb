#include "minivector.hpp"
#include "profile.hpp"
#include <fstream>
#include <iostream>
#include <cmath>
#include <cstdlib>
#include <unordered_set>

namespace {
using namespace mv;
// Validate JSON framing without converting request IDs to a fixed-width number.
// The original ID token is echoed verbatim, including arbitrary-size integers.
class RequestScanner {
    const std::string& text; size_t pos=0;
    void ws() { while(pos<text.size() && (text[pos]==' '||text[pos]=='\t'||text[pos]=='\r'||text[pos]=='\n')) ++pos; }
    bool eat(char c) { ws(); if(pos<text.size()&&text[pos]==c){++pos;return true;} return false; }
    void need(char c) { if(!eat(c)) fail("malformed JSON request","PROTOCOL"); }
    std::string string() {
        ws(); size_t start=pos; if(pos==text.size()||text[pos++]!='"') fail("JSON string required","PROTOCOL");
        bool escaped=false;
        while(pos<text.size()) {
            char c=text[pos++];
            if(escaped){escaped=false;continue;}
            if(c=='\\'){escaped=true;continue;}
            if(c=='"') return Json::parse(text.substr(start,pos-start)).get<std::string>();
        }
        fail("unterminated JSON string","PROTOCOL");
    }
    void value(unsigned depth) {
        if(depth>128) fail("JSON nesting too deep","PROTOCOL");
        ws(); if(pos==text.size()) fail("missing JSON value","PROTOCOL");
        char c=text[pos];
        if(c=='"'){string();return;}
        if(c=='{'){object(depth+1,false);return;}
        if(eat('[')){if(eat(']'))return;do{value(depth+1);}while(eat(','));need(']');return;}
        for(const char* word:{"true","false","null"}) {
            std::string s=word; if(text.compare(pos,s.size(),s)==0){pos+=s.size();return;}
        }
        size_t number_start=pos;
        if(c=='-') ++pos;
        auto digit=[&](){return pos<text.size()&&text[pos]>='0'&&text[pos]<='9';};
        if(!digit()) fail("invalid JSON number","PROTOCOL");
        if(text[pos]=='0') ++pos; else while(digit()) ++pos;
        if(pos<text.size()&&text[pos]=='.'){++pos;if(!digit())fail("invalid JSON fraction","PROTOCOL");while(digit())++pos;}
        if(pos<text.size()&&(text[pos]=='e'||text[pos]=='E')) {
            ++pos;if(pos<text.size()&&(text[pos]=='+'||text[pos]=='-'))++pos;
            if(!digit())fail("invalid JSON exponent","PROTOCOL");
            while(digit())++pos;
        }
        const std::string number=text.substr(number_start,pos-number_start);
        if(number.find_first_of(".eE")!=std::string::npos && !std::isfinite(std::strtod(number.c_str(),nullptr)))
            fail("nonfinite JSON number","PROTOCOL");
    }
    void object(unsigned depth,bool root) {
        need('{'); std::unordered_set<std::string> keys;
        if(eat('}'))return;
        do {
            std::string key=string(); if(!keys.insert(key).second)fail("duplicate JSON key","PROTOCOL");
            need(':');ws();size_t start=pos;value(depth);
            if(root&&key=="id"){id_start=start;id_end=pos;}
        } while(eat(','));
        need('}');
    }
public:
    size_t id_start=std::string::npos,id_end=0;
    explicit RequestScanner(const std::string& s):text(s){}
    Json parse(std::string& id) {
        object(0,true); ws();if(pos!=text.size())fail("trailing JSON data","PROTOCOL");
        if(id_start==std::string::npos)fail("request id required","PROTOCOL");
        id=text.substr(id_start,id_end-id_start);
        return Json::parse(text.substr(0,id_start)+"null"+text.substr(id_end));
    }
};

class ResponseBuffer {
    std::string buffer;
    std::filesystem::path file, directory;
    std::ofstream stream;
public:
    explicit ResponseBuffer(const std::filesystem::path& directory):directory(directory){}
    ~ResponseBuffer(){if(stream.is_open())stream.close();if(!file.empty()){std::error_code error;std::filesystem::remove(file,error);}}
    void append(const std::string& s) {
        if(file.empty()&&buffer.size()+s.size()<=256*1024){buffer+=s;return;}
        if(file.empty()) {
            file=directory/unique_name("response-",".tmp");
            stream.exceptions(std::ios::failbit|std::ios::badbit);
            stream.open(file,std::ios::binary);stream<<buffer;buffer.clear();
        }
        stream<<s;
    }
    void emit() {
        if(file.empty())std::cout<<buffer;
        else {stream.close();std::ifstream input(file,std::ios::binary);input.exceptions(std::ios::badbit);std::cout<<input.rdbuf();}
        std::cout.flush();
    }
};
void emit(const std::string& id,const Json& fields) {
    std::string payload=fields.dump();
    std::cout<<"{\"id\":"<<id<<','<<payload.substr(1)<<'\n'<<std::flush;
}
void serve(Database& db) {
    std::cout<<"{\"type\":\"ready\"}\n"<<std::flush;
    std::string line; uint64_t sequence=0;
    while(std::getline(std::cin,line)) {
        profile::Request request_profile("request",++sequence);
        std::string id="null";
        try {
            Json request;
            { profile::Scope timer(profile::Phase::protocol_parse); request=RequestScanner(line).parse(id); }
            if(request.value("op",Json())=="shutdown"){db.close();emit(id,{{"ok",true}});return;}
            if(request.value("op",Json())!="execute")fail("unsupported operation","UNSUPPORTED_OP");
            if(!request.contains("sql")||!request["sql"].is_string())fail("SQL string required","PROTOCOL");
            Json mode=request.value("search_mode",Json("exact"));
            if(mode!="exact"&&mode!="ann")fail("exact/ann search_mode required","PROTOCOL");
            Result result=execute(db,request["sql"].get<std::string>(),mode.get<std::string>());
            if(!result.query){emit(id,{{"ok",true},{"affected_rows",result.affected}});continue;}
            ResponseBuffer output(db.path);
            output.append("{\"id\":"+id+",\"ok\":true,\"columns\":"+Json(result.columns).dump()+",\"rows\":[");
            bool first=true;
            result.rows([&](const Json& row){if(!first)output.append(",");output.append(row.dump());first=false;});
            output.append("]}\n"); output.emit();
        } catch(const DBError& error) {
            emit(id,{{"ok",false},{"error",{{"code",error.code},{"message",error.what()}}}});
        } catch(const Json::exception& error) {
            emit(id,{{"ok",false},{"error",{{"code","PROTOCOL"},{"message",error.what()}}}});
        }
    }
    {profile::Request close_profile("eof_close",++sequence);db.close();}
}
void diagnostic(Database& db,const std::string& mode) {
    std::cout<<"LOCAL_READY\n"<<std::flush;
    std::string sql; char state='n',delimiter=0,c; uint64_t sequence=0;
    auto run=[&](){
        if(sql.find_first_not_of(" \t\r\n")==std::string::npos){sql.clear();return;}
        profile::Request request_profile("diagnostic",++sequence);
        try {
            auto result=execute(db,sql,mode);
            if(result.query){
                for(size_t i=0;i<result.columns.size();++i){if(i)std::cout<<'\t';std::cout<<result.columns[i];}
                std::cout<<'\n';result.rows([](const Json& row){for(size_t i=0;i<row.size();++i){if(i)std::cout<<'\t';std::cout<<row[i].dump();}std::cout<<'\n';});
                std::cout<<"END\n"<<std::flush;
            }else std::cout<<"OK "<<result.affected<<'\n'<<std::flush;
        }catch(const DBError& e){std::cout<<"ERROR "<<e.code<<' '<<e.what()<<'\n'<<std::flush;}
        sql.clear();
    };
    while(std::cin.get(c)){
        sql+=c;
        if(state=='l'){if(c=='\n')state='n';continue;}
        if(state=='b'){if(c=='*')state='a';continue;}
        if(state=='a'){if(c=='/')state='n';else if(c!='*')state='b';continue;}
        if(state=='q'){if(c==delimiter)state='e';continue;}
        if(state=='e'){if(c==delimiter){state='q';continue;}state='n';}
        else if(state=='h'){if(c=='-'){state='l';continue;}state='n';}
        else if(state=='s'){if(c=='*'){state='b';continue;}state='n';}
        if(c=='\''||c=='`'){delimiter=c;state='q';}else if(c=='-')state='h';else if(c=='/')state='s';else if(c==';')run();
    }
    run();{profile::Request close_profile("eof_close",++sequence);db.close();}
}
}
int main(int argc,char** argv) {
    std::ios::sync_with_stdio(false);std::cin.tie(nullptr);
    std::string directory="/data",protocol="jsonl",mode="exact";
    try {
        for(int i=1;i<argc;++i) {
            std::string arg=argv[i];
            if(arg=="--help"){std::cout<<"minivector --data-dir PATH --protocol jsonl|diagnostic --mode exact|ann\n";return 0;}
            if(i+1>=argc)throw std::runtime_error("missing argument value");
            std::string value=argv[++i];
            if(arg=="--data-dir")directory=value;else if(arg=="--protocol")protocol=value;else if(arg=="--mode")mode=value;else throw std::runtime_error("unknown argument");
        }
        if((protocol!="jsonl"&&protocol!="diagnostic")||(mode!="exact"&&mode!="ann"))throw std::runtime_error("invalid mode/protocol");
        auto startup=profile::Clock::time_point{};
        if(profile::enabled()) {profile::reset();startup=profile::Clock::now();}
        mv::Database db(directory);
        if(profile::enabled()) profile::report("startup",0,startup);
        if(protocol=="jsonl")serve(db);else diagnostic(db,mode);
    }catch(const std::exception& e){std::cerr<<"fatal: "<<e.what()<<'\n';return 1;}
    return 0;
}
