#pragma once
#include <nlohmann/json.hpp>
#include <cstdint>
#include <filesystem>
#include <functional>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <variant>
#include <vector>

namespace mv {
using Json = nlohmann::json;
struct DBError : std::runtime_error {
    std::string code;
    DBError(std::string code, std::string message) : std::runtime_error(message), code(std::move(code)) {}
};
[[noreturn]] inline void fail(const std::string& message, const std::string& code="INVALID") { throw DBError(code,message); }
using Cell = std::variant<int64_t,std::string,std::vector<float>>;
using Row = std::vector<Cell>;
struct Column { std::string name,kind; int64_t size=0; bool primary=false; };
struct Table { std::vector<Column> columns; std::vector<std::string> files; Json indexes=Json::object(); };
struct Condition { std::string column,op; Json value; };
struct Assignment { std::string column; Json value; };
struct Expr { bool distance=false; std::string column; Json vector; };
struct Projection { Expr expression; std::string alias; };
struct Order { Expr expression; bool descending=false; };
using RowVisitor = std::function<void(const Row&)>;
using JsonVisitor = std::function<void(const Json&)>;
struct Result {
    bool query=false;
    int64_t affected=0;
    std::vector<std::string> columns;
    std::function<void(const JsonVisitor&)> rows;
};
Json row_json(const Row& row);
Json cell_json(const Cell& cell);
Cell validate(const Column& column,const Json& value,bool for_storage=true);
std::function<bool(const Row&)> compile_conditions(const Table&,const std::vector<Condition>&);
size_t column_index(const Table&,const std::string&);
size_t primary_index(const Table&);
double l2_distance(const std::vector<float>&,const std::vector<float>&);
std::string unique_name(const std::string& prefix,const std::string& suffix);
void sync_directory(const std::filesystem::path&);

class Database {
public:
    explicit Database(const std::filesystem::path& directory);
    ~Database()=default;
    std::filesystem::path path;
    Table table(const std::string&) const;
    void scan(const Table&,const RowVisitor&) const;
    Row read_row(const Table&,const std::string& file,uint64_t offset) const;
    std::shared_ptr<const std::vector<Row>> cached_rows(const Table&);
    void begin(); void commit(); void rollback(); void close();
    void create(const std::string&,const std::vector<Column>&);
    int64_t insert(const std::string&,const std::vector<Json>&,const std::optional<std::vector<std::string>>&);
    int64_t modify(const std::string&,const std::vector<Condition>&,const std::optional<std::vector<Assignment>>&);
    void create_index(const std::string&,const std::string&,const std::string&);
    Result select(const std::string&,const std::optional<std::vector<Projection>>&,
                  const std::vector<Condition>&,const std::optional<Order>&,
                  const std::optional<Json>&,const std::string& mode);
private:
    Json catalog_=Json::object();
    std::optional<Json> transaction_;
    std::vector<std::string> cache_signature_;
    std::shared_ptr<std::vector<Row>> cache_;
    const Json& visible() const;
    void collect();
    void publish(const Json&);
    void accept(const Json&);
    bool prepare_indexes(Json&,const std::optional<std::string>& only=std::nullopt);
    void rewrite(const std::string&,const std::function<void(const RowVisitor&)>&,bool append=false);
};
Result execute(Database&,const std::string& sql,const std::string& mode="exact");
}
