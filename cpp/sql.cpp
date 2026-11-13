#include "minivector.hpp"
#include "profile.hpp"
#include "unicode.hpp"
#include <algorithm>
#include <charconv>
#include <cmath>
#include <cstdlib>
#include <limits>

namespace mv {
namespace {
std::string upper(std::string s) {
    for (char& c:s) if(c>='a' && c<='z') c=char(c-'a'+'A');
    return s;
}
bool alpha(char c) { return (c>='a'&&c<='z')||(c>='A'&&c<='Z')||c=='_'; }
bool digit(char c) { return c>='0'&&c<='9'; }
class Parser {
    std::vector<std::string> tokens;
    size_t position=0;
public:
    explicit Parser(const std::string& sql) {
        size_t i=0;
        while(i<sql.size()) {
            const auto current=unicode::decode(sql,i);
            if(unicode::is_space(current.value)) { i+=current.width; continue; }
            if(sql.compare(i,2,"--")==0) { auto end=sql.find('\n',i+2); i=end==std::string::npos?sql.size():end+1; continue; }
            if(sql.compare(i,2,"/*")==0) { auto end=sql.find("*/",i+2); if(end==std::string::npos) fail("unterminated comment","SYNTAX"); i=end+2; continue; }
            size_t start=i;
            if(sql[i]=='\''||sql[i]=='`') {
                char quote=sql[i++]; bool closed=false;
                while(i<sql.size()) {
                    if(sql[i++]!=quote) continue;
                    if(i<sql.size()&&sql[i]==quote) { ++i; continue; }
                    closed=true; break;
                }
                if(!closed) fail("unterminated quoted token","SYNTAX");
            } else if(alpha(sql[i])) {
                while(i<sql.size()&&(alpha(sql[i])||digit(sql[i]))) ++i;
            } else if(unicode::decimal_at(sql,i).value>=0||(sql[i]=='.'&&unicode::decimal_at(sql,i+1).value>=0)) {
                // Normalize only a numeric SQL token. Quoted text (including
                // JSON vector literals) and identifiers keep their code points.
                std::string number;
                const auto take_digits=[&]() {
                    while(i<sql.size()) {
                        const auto next=unicode::decimal_at(sql,i);
                        if(next.value<0) break;
                        number+=char('0'+next.value); i+=next.width;
                    }
                };
                take_digits();
                if(i<sql.size()&&sql[i]=='.') { number+=sql[i++]; take_digits(); }
                if(i<sql.size()&&(sql[i]=='e'||sql[i]=='E')) {
                    const size_t exponent=i,length=number.size(); number+=sql[i++];
                    if(i<sql.size()&&(sql[i]=='+'||sql[i]=='-')) number+=sql[i++];
                    const size_t first=i; take_digits();
                    if(first==i) {i=exponent;number.resize(length);}
                }
                tokens.push_back(std::move(number)); continue;
            } else if(std::string("(),;*[]=<>+-").find(sql[i])!=std::string::npos) {
                ++i; if(i<sql.size()&&(sql[start]=='<'||sql[start]=='>')&&sql[i]=='=') ++i;
            } else fail("unrecognized SQL token","SYNTAX");
            tokens.push_back(sql.substr(start,i-start));
        }
        if(!tokens.empty()&&tokens.back()==";") tokens.pop_back();
    }
    std::string peek() const { return position<tokens.size()?tokens[position]:""; }
    std::string take() { if(position>=tokens.size()) fail("unexpected end of statement","SYNTAX"); return tokens[position++]; }
    bool eat(const std::string& token) { if(upper(peek())!=token) return false; ++position; return true; }
    void expect(const std::string& token) { if(!eat(token)) fail("expected "+token,"SYNTAX"); }
    void finish() { if(position<tokens.size()) fail("unsupported or trailing SQL: "+peek(),"SYNTAX"); }
    std::string identifier() {
        auto token=take();
        if(token[0]=='`') {
            std::string value;
            for(size_t i=1;i+1<token.size();++i) { value+=token[i]; if(token[i]=='`'&&i+2<token.size()&&token[i+1]=='`') ++i; }
            return unicode::lower(std::move(value));
        }
        if(!alpha(token[0])||!std::all_of(token.begin(),token.end(),[](char c){return alpha(c)||digit(c);})) fail("expected identifier","SYNTAX");
        return unicode::lower(std::move(token));
    }
    Json value(unsigned depth=0) {
        if(depth>256) fail("literal nesting too deep","SYNTAX");
        if(eat("[")) {
            Json values=Json::array();
            if(!eat("]")) while(true) { values.push_back(value(depth+1)); if(eat("]")) break; expect(","); }
            return values;
        }
        bool negative=eat("-"); bool positive=negative?false:eat("+");
        std::string token=take();
        if(token[0]=='\'') {
            if(negative||positive) fail("invalid string sign","SYNTAX");
            std::string result;
            for(size_t i=1;i+1<token.size();++i) { result+=token[i]; if(token[i]=='\''&&i+2<token.size()&&token[i+1]=='\'') ++i; }
            return result;
        }
        auto keyword=upper(token);
        if(keyword=="NAN") return Json(std::numeric_limits<double>::quiet_NaN());
        if(keyword=="INFINITY"||keyword=="INF") return Json(negative?-std::numeric_limits<double>::infinity():std::numeric_limits<double>::infinity());
        if(!digit(token[0])&&token[0]!='.') fail("expected literal","SYNTAX");
        if(negative) token="-"+token;
        if(token.find_first_of(".eE")==std::string::npos) {
            int64_t signed_value=0;
            auto parsed=std::from_chars(token.data(),token.data()+token.size(),signed_value);
            if(parsed.ec==std::errc()&&parsed.ptr==token.data()+token.size()) return Json(signed_value);
            if(!negative) {
                uint64_t unsigned_value=0;
                auto unsigned_parsed=std::from_chars(token.data(),token.data()+token.size(),unsigned_value);
                if(unsigned_parsed.ec==std::errc()&&unsigned_parsed.ptr==token.data()+token.size()) return Json(unsigned_value);
            }
            // Keep overflowing integer literals distinguishable from valid BIGINTs.
            // A floating JSON value is rejected by the BIGINT/LIMIT validators.
        }
        char* end=nullptr; double number=std::strtod(token.c_str(),&end);
        if(end!=token.c_str()+token.size()) fail("expected literal","SYNTAX");
        return Json(number);
    }
    Json limit_value() {
        // LIMIT is a row count, so values above uint64 cannot constrain any
        // realizable table. Preserve Python's arbitrary positive integer
        // semantics here without accepting overflowing BIGINT literals.
        size_t saved=position;
        bool negative=eat("-"); if(!negative) eat("+");
        auto token=peek();
        if(!negative&&!token.empty()&&std::all_of(token.begin(),token.end(),digit)) {
            take(); uint64_t count=0;
            auto parsed=std::from_chars(token.data(),token.data()+token.size(),count);
            if(parsed.ec==std::errc::result_out_of_range) return Json(std::numeric_limits<uint64_t>::max());
            if(parsed.ec==std::errc()&&parsed.ptr==token.data()+token.size()) return Json(count);
        }
        position=saved; return value();
    }
    Expr expression() {
        if(eat("L2_DISTANCE")) { expect("("); auto column=identifier(); expect(","); auto vector=value(); expect(")"); return {true,column,vector}; }
        return {false,identifier(),Json()};
    }
    std::vector<Condition> where() {
        std::vector<Condition> conditions;
        if(eat("WHERE")) while(true) {
            auto column=identifier(),op=take();
            if(op!="="&&op!="<"&&op!="<="&&op!=">"&&op!=">=") fail("unsupported comparison","SYNTAX");
            conditions.push_back({column,op,value()}); if(!eat("AND")) break;
        }
        return conditions;
    }
    Result run(Database& db,const std::string& mode,profile::Scope& parse_time) {
        auto verb=upper(take());
        if(verb=="BEGIN"||verb=="COMMIT"||verb=="ROLLBACK") { finish(); parse_time.stop(); if(verb=="BEGIN") db.begin(); else if(verb=="COMMIT") db.commit(); else db.rollback(); return {}; }
        if(verb=="CREATE") {
            if(eat("VECTOR")) {
                expect("INDEX"); auto index=identifier(); expect("ON"); auto table=identifier(); expect("("); auto column=identifier(); expect(")"); finish(); parse_time.stop(); db.create_index(index,table,column); return {};
            }
            expect("TABLE"); auto table=identifier(); expect("("); std::vector<Column> columns; std::optional<std::string> primary;
            while(true) {
                if(eat("PRIMARY")) { expect("KEY"); expect("("); if(primary) fail("multiple primary key declarations","SCHEMA"); primary=identifier(); expect(")"); }
                else {
                    Column column; column.name=identifier(); column.kind=upper(take());
                    if(column.kind=="VARCHAR"||column.kind=="VECTOR") {
                        expect("("); auto size=value();
                        if(!size.is_number_integer()||(size.is_number_unsigned()&&size.get<uint64_t>()>uint64_t(INT64_MAX))) fail("integer type size required","SYNTAX");
                        column.size=size.get<int64_t>(); expect(")");
                    }
                    column.primary=eat("PRIMARY"); if(column.primary) expect("KEY"); columns.push_back(std::move(column));
                }
                if(eat(")")) break; expect(",");
            }
            if(primary) {
                bool exists=false; for(auto& column:columns) { if(column.primary) fail("invalid primary key declaration","SCHEMA"); if(column.name==*primary) {column.primary=true;exists=true;} }
                if(!exists) fail("invalid primary key declaration","SCHEMA");
            }
            finish(); parse_time.stop(); db.create(table,columns); return {};
        }
        if(verb=="INSERT") {
            expect("INTO"); auto table=identifier(); std::optional<std::vector<std::string>> columns;
            if(eat("(")) { columns=std::vector<std::string>{identifier()}; while(eat(",")) columns->push_back(identifier()); expect(")"); }
            expect("VALUES"); std::vector<Json> rows;
            do { expect("("); Json row=Json::array(); row.push_back(value()); while(eat(",")) row.push_back(value()); expect(")"); rows.push_back(std::move(row)); } while(eat(","));
            finish(); parse_time.stop(); Result result; result.affected=db.insert(table,rows,columns); return result;
        }
        if(verb=="SELECT") {
            std::optional<std::vector<Projection>> projections;
            if(!eat("*")) { projections=std::vector<Projection>{}; do { auto expr=expression(); auto alias=eat("AS")?identifier():(expr.distance?"l2_distance":expr.column); projections->push_back({std::move(expr),std::move(alias)}); } while(eat(",")); }
            expect("FROM"); auto table=identifier(); auto conditions=where(); std::optional<Order> order;
            if(eat("ORDER")) { expect("BY"); auto expr=expression(); bool desc=eat("DESC"); if(!desc) eat("ASC"); order=Order{std::move(expr),desc}; }
            std::optional<Json> limit; if(eat("LIMIT")) limit=limit_value(); finish();
            parse_time.stop(); return db.select(table,projections,conditions,order,limit,mode);
        }
        if(verb=="UPDATE") {
            auto table=identifier(); expect("SET"); std::vector<Assignment> assignments;
            do { auto column=identifier(); expect("="); assignments.push_back({column,value()}); } while(eat(","));
            auto conditions=where(); finish(); parse_time.stop(); Result result; result.affected=db.modify(table,conditions,assignments); return result;
        }
        if(verb=="DELETE") { expect("FROM"); auto table=identifier(); auto conditions=where(); finish(); parse_time.stop(); Result result; result.affected=db.modify(table,conditions,std::nullopt); return result; }
        fail("unsupported statement","SYNTAX");
    }
};
}
Result execute(Database& database,const std::string& sql,const std::string& mode) {
    profile::Scope parse_time(profile::Phase::sql_parse);
    return Parser(sql).run(database,mode,parse_time);
}
}
