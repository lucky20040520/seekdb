#define USING_LOG_PREFIX SERVER_OMT
#include "observer/ai_service/ai_column_service.h"
#include "common/mysqlclient/ob_mysql_transaction.h"
#include "observer/ai_service/ob_ai_service_executor.h"
#include "observer/ob_inner_sql_connection.h"
#include "share/ob_server_struct.h"
#include "sql/ob_sql.h"
#include "sql/ai_column_check.h"
#include "sql/privilege_check/ob_ai_model_priv_util.h"
#include "sql/engine/expr/ob_expr_ai/ob_ai_func_utils.h"
#include "common/mysqlclient/ob_mysql_proxy.h"
#include "common/mysqlclient/ob_mysql_transaction.h"
#include "share/rc/ob_server_runtime.h"
#include "share/ob_errno.h"
#include "lib/oblog/ob_warning_buffer.h"
#include <limits>
#include <cerrno>
#include <cmath>
#include <cstdlib>
#include <functional>

namespace oceanbase { namespace observer {
using namespace common;
using namespace sql::ai_column;
namespace {
thread_local int ai_column_internal_depth = 0;
struct InternalGuard {
  InternalGuard() { ++ai_column_internal_depth; }
  ~InternalGuard() { --ai_column_internal_depth; }
};
// Auxiliary SQL has its own diagnostics: successful housekeeping must not add
// notes or warnings to the statement which caused it to run.
struct DiagnosticsGuard {
  ObWarningBuffer local;
  ObWarningBuffer *outer = ob_get_tsi_warning_buffer();
  DiagnosticsGuard() { ob_setup_tsi_warning_buffer(&local); }
  ~DiagnosticsGuard() { ob_setup_tsi_warning_buffer(outer); }
  void propagate_error() {
    if (outer != nullptr && local.get_err_code() != OB_MAX_ERROR_CODE) {
      outer->set_error(local.get_err_msg(), local.get_err_code());
      outer->set_sql_state(local.get_sql_state());
    }
  }
};
struct DdlCleanup {
  std::function<int()> rollback;
  bool complete = false;
  int run() { complete = true; return rollback(); }
  ~DdlCleanup() { if (!complete) { try { int ret = run(); if (ret != OB_SUCCESS) LOG_WARN("AI column DDL cleanup failed", K(ret)); } catch (...) {} } }
};
using Rows = std::vector<std::vector<std::string>>;
const char *META = "oceanbase.__all_ai_column";
const char *TASKS = "oceanbase.__all_ai_column_task";

int read_rows(ObISQLClient &client, const std::string &text, size_t width, Rows &out)
{
  int ret = OB_SUCCESS;
  InternalGuard internal;
  DiagnosticsGuard diagnostics;
  ObMySQLProxy::MySQLResult result;
  if (OB_FAIL(client.read(result, text.c_str()))) {
  } else if (result.get_result() == nullptr) { ret = OB_ERR_UNEXPECTED;
  } else {
    auto *r = result.get_result();
    while (OB_SUCC(ret) && OB_SUCC(ret = r->next())) {
      std::vector<std::string> row;
      for (size_t i = 0; OB_SUCC(ret) && i < width; ++i) {
        ObString value;
        if (OB_FAIL(r->get_varchar(static_cast<int64_t>(i), value))) {
        } else row.emplace_back(value.ptr() == nullptr ? "" : value.ptr(), value.length());
      }
      if (OB_SUCC(ret)) out.push_back(std::move(row));
    }
    if (ret == OB_ITER_END) ret = OB_SUCCESS;
  }
  return ret;
}

int write_sql(ObISQLClient &client, const std::string &text, int64_t *count = nullptr)
{
  InternalGuard internal;
  DiagnosticsGuard diagnostics;
  int64_t affected = 0;
  int ret = client.write(text.c_str(), affected);
  if (count != nullptr) *count = affected;
  if (ret != OB_SUCCESS) LOG_WARN("AI column internal write failed", K(ret), "sql", text.c_str());
  return ret;
}

int user_write(sql::ObSQLSessionInfo &session, const std::string &text)
{
  InternalGuard internal;
  sqlclient::ObISQLConnectionGuard connection;
  int ret = ObInnerSQLConnection::create_connection_with_external_session(&session, connection);
  if (OB_SUCC(ret)) {
    int64_t affected = 0;
    bool saved_ddl = sql::ObSql::is_ai_column_internal_ddl();
    sql::ObSql::set_ai_column_internal_ddl(true);
    ret = connection.get_ptr()->execute_write(ObString(text.size(), text.data()), affected, true);
    sql::ObSql::set_ai_column_internal_ddl(saved_ddl);
  }
  return ret;
}

int read_count_in_session(sql::ObSQLSessionInfo &session, const std::string &text, int64_t &count)
{
  InternalGuard internal;
  sqlclient::ObISQLConnectionGuard connection;
  int ret = ObInnerSQLConnection::create_connection_with_external_session(&session, connection);
  if (OB_FAIL(ret)) return ret;
  auto *inner = static_cast<ObInnerSQLConnection *>(connection.get_ptr());
  sql::ObSQLSessionInfo::StmtSavedValue saved;
  sql::ObIInnerSQLConnection::SavedValue saved_connection;
  // Resolution precedes start_stmt(), so it does not yet have a nesting level.
  // Borrow level zero only for this read and restore it after nested execution;
  // the external session still supplies its own transaction and snapshot.
  const int64_t old_nesting = session.get_nested_count();
  if (old_nesting < 0) session.set_nested_count(0);
  if (OB_FAIL(inner->begin_nested_session(saved, saved_connection, true))) {
    session.set_nested_count(old_nesting);
    return ret;
  }
  const bool old_flag = sql::ObSql::is_ai_column_internal_ddl();
  sql::ObSql::set_ai_column_internal_ddl(true);
  {
    ObMySQLProxy::MySQLResult result;
    if (OB_FAIL(inner->execute_read(ObString(text.size(), text.data()), result))) {
    } else if (result.get_result() == nullptr) { ret = OB_ERR_UNEXPECTED;
    } else if (OB_FAIL(result.get_result()->next())) {
    } else ret = result.get_result()->get_int(static_cast<int64_t>(0), count);
  }
  sql::ObSql::set_ai_column_internal_ddl(old_flag);
  const int end_ret = inner->end_nested_session(saved, saved_connection);
  session.set_nested_count(old_nesting);
  return OB_SUCC(ret) ? end_ret : ret;
}

std::string lower(std::string value)
{
  for (char &c : value) c = std::tolower(static_cast<unsigned char>(c));
  return value;
}

std::string number(uint64_t value) { return std::to_string(value); }

std::string text_cast(const std::string &value) { return "CAST(" + value + " AS CHAR)"; }

std::string key_condition(const std::string &database, const std::string &table)
{
  return "d.database_name=" + literal(database) + " AND t.table_name=" + literal(table);
}

int table_identity(const std::string &database, const std::string &table, uint64_t &id)
{
  Rows rows;
  int ret = read_rows(*GCTX.sql_proxy_, "SELECT CAST(t.table_id AS CHAR) FROM oceanbase.__all_table t"
      " JOIN oceanbase.__all_database d ON d.database_id=t.database_id WHERE " + key_condition(database, table), 1, rows);
  if (OB_SUCC(ret)) {
    if (rows.size() != 1) ret = OB_SCHEMA_ERROR;
    else id = std::stoull(rows[0][0]);
  }
  return ret;
}

// Only the hidden-column ALTER is retried. A successful earlier CREATE/ADD
// must never be replayed, and a replacement table must never be modified.
int add_hidden_columns(sql::ObSQLSessionInfo &session, const std::string &sql_text,
                       const std::string &database, const std::string &table, uint64_t table_id,
                       const std::string &state, const std::string &request, const std::string &spec)
{
  int ret = OB_SUCCESS;
  constexpr int max_attempts = 8;
  for (int attempt = 0; attempt < max_attempts; ++attempt) {
    DiagnosticsGuard diagnostics;
    ret = user_write(session, sql_text);
    if (ret != OB_EAGAIN || attempt + 1 == max_attempts) {
      if (ret != OB_SUCCESS) diagnostics.propagate_error();
      break;
    }
    int status = OB_SUCCESS;
    if (session.is_terminate(status)) { ret = status; break; }
    if (THIS_WORKER.is_timeout()) { ret = OB_TIMEOUT; break; }
    Rows rows;
    ret = read_rows(*GCTX.sql_proxy_, "SELECT CAST(t.table_id AS CHAR),CAST(COUNT(c.column_id) AS CHAR)"
        " FROM oceanbase.__all_table t JOIN oceanbase.__all_database d ON d.database_id=t.database_id"
        " LEFT JOIN oceanbase.__all_column c ON c.table_id=t.table_id AND c.column_name IN (" +
        literal(state) + "," + literal(request) + "," + literal(spec) + ") WHERE " +
        key_condition(database, table) + " GROUP BY t.table_id", 2, rows);
    if (OB_FAIL(ret)) break;
    if (rows.size() != 1 || std::stoull(rows[0][0]) != table_id || rows[0][1] != "0") {
      ret = OB_SCHEMA_ERROR;
      break;
    }
    const int64_t delay = std::min<int64_t>(100000, 10000L << attempt);
    const int64_t remain = THIS_WORKER.get_timeout_remain();
    if (remain <= 0) { ret = OB_TIMEOUT; break; }
    ob_usleep(std::min(delay, remain));
  }
  return ret;
}

int trigger_ddl(const std::string &sql_text, const std::string &database, const std::string &table,
                uint64_t table_id, const std::string &trigger_name, bool creating)
{
  int ret = OB_SUCCESS;
  for (int attempt = 0; attempt < 8; ++attempt) {
    ret = write_sql(*GCTX.sql_proxy_, sql_text);
    if (ret != OB_EAGAIN || attempt == 7) break;
    if (THIS_WORKER.is_timeout()) { ret = OB_TIMEOUT; break; }
    uint64_t current_id = 0;
    ret = table_identity(database, table, current_id);
    if (OB_FAIL(ret)) break;
    if (current_id != table_id) { ret = OB_SCHEMA_ERROR; break; }
    if (creating) {
      Rows rows;
      ret = read_rows(*GCTX.sql_proxy_, "SELECT CAST(COUNT(*) AS CHAR) FROM information_schema.triggers"
          " WHERE trigger_schema=" + literal(database) + " AND trigger_name=" + literal(trigger_name), 1, rows);
      if (OB_FAIL(ret)) break;
      // Never assume an existing trigger is ours or silently accept it.
      if (rows.size() != 1 || rows[0][0] != "0") { ret = OB_SCHEMA_ERROR; break; }
    }
    const int64_t remain = THIS_WORKER.get_timeout_remain();
    if (remain <= 0) { ret = OB_TIMEOUT; break; }
    ob_usleep(std::min<int64_t>(remain, std::min<int64_t>(100000, 10000L << attempt)));
  }
  return ret;
}

bool contains_name(const std::vector<Token> &tokens, const std::string &name)
{
  for (const auto &t : tokens) if (t.identifier() && lower(t.text) == lower(name)) return true;
  return false;
}

std::pair<std::string, std::string> table_name(const std::vector<Token> &tokens, size_t &i,
                                               const std::string &database)
{
  if (i >= tokens.size() || !tokens[i].identifier()) throw std::invalid_argument("expected table name");
  std::string first = tokens[i++].text;
  if (i + 1 < tokens.size() && tokens[i].is(".")) {
    ++i;
    if (!tokens[i].identifier()) throw std::invalid_argument("expected table name");
    return {first, tokens[i++].text};
  }
  if (database.empty()) throw std::invalid_argument("no database selected");
  return {database, first};
}
}

AiColumnService &AiColumnService::instance()
{
  static AiColumnService service;
  return service;
}

std::string AiColumnService::Column::state_name() const { return "__ai_state_" + definition.name; }
std::string AiColumnService::Column::request_name() const { return "__ai_request_" + definition.name; }
std::string AiColumnService::Column::spec_name() const { return "__ai_spec_" + definition.name; }
std::string AiColumnService::Column::qualified_table() const { return identifier(database) + "." + identifier(table); }

int AiColumnService::start()
{
  sql::ObSql::set_ai_column_sql_hook(&AiColumnService::prepare);
  sql::AiColumnCheck::read_check = &AiColumnService::check_read;
  sql::AiColumnCheck::assignment_check = &AiColumnService::check_assignment;
  return lib::ThreadPool::start();
}

int AiColumnService::initialize_tables()
{
  if (GCTX.sql_proxy_ == nullptr) return OB_NOT_INIT;
  int ret = write_sql(*GCTX.sql_proxy_, std::string("CREATE TABLE IF NOT EXISTS ") + META +
    "(table_id BIGINT UNSIGNED NOT NULL, column_id BIGINT UNSIGNED NOT NULL,"
    "column_name VARCHAR(128) NOT NULL, database_name VARCHAR(128) NOT NULL, table_name VARCHAR(128) NOT NULL,"
    "state_column_id BIGINT UNSIGNED NOT NULL, request_column_id BIGINT UNSIGNED NOT NULL,"
    "result_spec_column_id BIGINT UNSIGNED NOT NULL, definition_version BIGINT UNSIGNED NOT NULL,"
    "normalized_expr TEXT NOT NULL, input_expr TEXT NOT NULL, result_type VARCHAR(256) NOT NULL,"
    "model_name VARCHAR(128) NOT NULL, system_prompt TEXT NOT NULL, max_retries INT NOT NULL,"
    "dimension INT NOT NULL, PRIMARY KEY(table_id,column_id))");
  if (OB_SUCC(ret)) ret = write_sql(*GCTX.sql_proxy_, std::string("CREATE TABLE IF NOT EXISTS ") + TASKS +
    "(table_id BIGINT UNSIGNED NOT NULL,column_id BIGINT UNSIGNED NOT NULL,request_id VARCHAR(64) NOT NULL,"
    "rowkey_predicate VARCHAR(2048) NOT NULL,definition_version BIGINT UNSIGNED NOT NULL,"
    "input_value LONGTEXT, state INT NOT NULL DEFAULT 0,retry_count INT NOT NULL DEFAULT 0,"
    "eligible_ts DATETIME(6) NOT NULL,owner VARCHAR(128),lease_until DATETIME(6),result_value LONGTEXT,"
    "PRIMARY KEY(table_id,column_id,rowkey_predicate),KEY eligible(eligible_ts,state))");
  if (OB_SUCC(ret)) ret = reload_columns();
  if (OB_SUCC(ret)) ready_.store(true);
  return ret;
}

int AiColumnService::reload_columns()
{
  Rows rows;
  int ret = read_rows(*GCTX.sql_proxy_, std::string("SELECT CAST(a.table_id AS CHAR),CAST(a.column_id AS CHAR),") +
    "a.column_name,a.database_name,a.table_name,CAST(a.definition_version AS CHAR),CAST(a.normalized_expr AS CHAR),"
    "CAST(a.input_expr AS CHAR),a.result_type,a.model_name,CAST(a.system_prompt AS CHAR),CAST(a.max_retries AS CHAR),CAST(a.dimension AS CHAR) FROM " + META +
    " a JOIN oceanbase.__all_table t ON t.table_id=a.table_id AND t.table_name=a.table_name"
    " JOIN oceanbase.__all_database d ON d.database_id=t.database_id AND d.database_name=a.database_name", 13, rows);
  if (OB_SUCC(ret)) {
    std::vector<Column> columns;
    for (const auto &r : rows) {
      Column c;
      c.table_id = std::stoull(r[0]); c.column_id = std::stoull(r[1]); c.definition.name = r[2];
      c.database = r[3]; c.table = r[4]; c.version = std::stoull(r[5]);
      c.definition.expression = r[6]; c.definition.input = r[7]; c.definition.type = r[8];
      c.definition.model = r[9]; c.definition.prompt = r[10]; c.definition.retries = std::stoi(r[11]);
      c.definition.dimension = std::stoi(r[12]); c.definition.embedding = c.definition.dimension != 0;
      columns.push_back(std::move(c));
    }
    std::lock_guard<std::mutex> lock(columns_mutex_);
    columns_.swap(columns);
  }
  return ret;
}

int AiColumnService::prepare(const ObString &text, sql::ObSQLSessionInfo &session,
                             ObIAllocator &allocator, ObString &replacement)
{
  if (ai_column_internal_depth != 0) return OB_SUCCESS;
  InternalGuard guard;
  int ret = OB_SUCCESS;
  try {
    std::string output;
    ret = instance().prepare_inner(std::string(text.ptr(), text.length()), session, output);
    if (OB_SUCC(ret) && !output.empty()) {
      ret = ob_write_string(allocator, ObString(output.size(), output.data()), replacement);
    }
  } catch (const LexerError &) {
    // Preserve the native parser's diagnostics for ordinary malformed SQL.
    ret = OB_SUCCESS;
  } catch (const DimensionError &e) {
    ret = OB_ERR_INVALID_VECTOR_DIM;
    LOG_USER_ERROR(OB_ERR_INVALID_VECTOR_DIM, static_cast<uint32_t>(e.expected), static_cast<uint32_t>(e.actual));
  } catch (const DependencyError &) { ret = OB_NOT_SUPPORTED;
  } catch (const std::bad_alloc &) { ret = OB_ALLOCATE_MEMORY_FAILED;
  } catch (const std::range_error &) { ret = OB_ERR_INVALID_VECTOR_DIM;
  } catch (const std::exception &e) {
    ret = OB_NOT_SUPPORTED;
    LOG_USER_ERROR(OB_NOT_SUPPORTED, e.what());
  }
  return ret;
}

int AiColumnService::prepare_inner(const std::string &text, sql::ObSQLSessionInfo &session,
                                  std::string &replacement)
{
  bool no_backslash_escapes = false;
  IS_NO_BACKSLASH_ESCAPES(session.get_sql_mode(), no_backslash_escapes);
  const auto first = tokenize(text, !no_backslash_escapes, 1);
  if (first.empty() || (!first[0].is("create") && !first[0].is("alter") && !first[0].is("show"))) return OB_SUCCESS;
  const auto tokens = tokenize(text, !no_backslash_escapes);
  if (tokens.empty()) return OB_SUCCESS;
  if (!tokens[0].is("create") && !tokens[0].is("alter") && !tokens[0].is("show")) return OB_SUCCESS;
  bool ai_ddl = false;
  for (size_t i = 0; i + 1 < tokens.size(); ++i) {
    if (tokens[i].is("ai") && tokens[i + 1].is("column")) ai_ddl = true;
  }
  if (ai_ddl && tokens.size() > 1 && tokens[1].is("table") && (tokens[0].is("create") || tokens[0].is("alter"))) {
    bool retry = false;
    for (const auto &t : tokens) if (t.is("retry")) retry = true;
    if (!retry) {
      std::lock_guard<std::mutex> lock(ddl_mutex_);
      int ret = ready_.load() ? OB_SUCCESS : initialize_tables();
      if (OB_SUCC(ret)) ret = create_columns(text, tokens, session);
      if (OB_SUCC(ret)) replacement = "DO 0";
      return ret;
    }
  }
  std::vector<Column> columns;
  {
    std::lock_guard<std::mutex> lock(columns_mutex_);
    columns = columns_;
  }
  if (columns.empty()) return OB_SUCCESS;
  std::string database(session.get_database_name().ptr(), session.get_database_name().length());
  if (tokens[0].is("show") && tokens.size() >= 4 && tokens[1].is("ai") && tokens[2].is("columns")) {
    size_t pos = tokens[3].is("from") ? 4 : 3;
    auto table = table_name(tokens, pos, database);
    replacement = std::string("SELECT column_name,normalized_expr,max_retries FROM ") + META +
      " a WHERE database_name=" + literal(table.first) + " AND table_name=" + literal(table.second) +
      " AND EXISTS(SELECT 1 FROM oceanbase.__all_table t JOIN oceanbase.__all_database d"
      " ON t.database_id=d.database_id WHERE t.table_id=a.table_id AND t.table_name=a.table_name"
      " AND d.database_name=a.database_name) ORDER BY column_id";
    return OB_SUCCESS;
  }
  if (tokens[0].is("alter") && tokens.size() > 5 && tokens[1].is("table")) {
    size_t pos = 2;
    auto table = table_name(tokens, pos, database);
    if (pos + 3 < tokens.size() && tokens[pos].is("retry") && tokens[pos + 1].is("ai") && tokens[pos + 2].is("column")) {
      const auto &target = tokens[pos + 3];
      const Column *column = nullptr;
      for (const auto &c : columns) if (lower(c.database) == lower(table.first) && lower(c.table) == lower(table.second) && lower(c.definition.name) == lower(target.text)) column = &c;
      if (column == nullptr) { LOG_USER_ERROR(OB_NOT_SUPPORTED, "retrying a column that is not an AI column"); return OB_NOT_SUPPORTED; }
      size_t start = pos + 4;
      std::string predicate = "1";
      if (start < tokens.size() && !tokens[start].is(";")) {
        if (!tokens[start].is("where")) throw std::invalid_argument("invalid AI COLUMN RETRY predicate");
        ++start;
        if (start == tokens.size()) throw std::invalid_argument("missing AI COLUMN RETRY predicate");
        size_t previous = tokens[start].begin;
        predicate.clear();
        int depth = 0;
        for (size_t i = start; i < tokens.size(); ++i) {
          const auto &t = tokens[i];
          if (t.is("(")) ++depth;
          else if (t.is(")")) --depth;
          if (t.is(",") && depth == 0) { LOG_USER_ERROR(OB_NOT_SUPPORTED, "combining AI COLUMN RETRY with another ALTER action"); return OB_NOT_SUPPORTED; }
          predicate += text.substr(previous, t.begin - previous);
          predicate += t.is("status") ? identifier(column->state_name()) : text.substr(t.begin, t.end - t.begin);
          previous = t.end;
        }
      }
      int ret = user_write(session, "UPDATE " + column->qualified_table() + " SET " + identifier(column->request_name()) + "=UUID() WHERE " + predicate);
      if (OB_SUCC(ret)) replacement = "DO 0";
      return ret;
    }
  }
  return OB_SUCCESS;
}

int AiColumnService::check_assignment(sql::ObSQLSessionInfo &session, uint64_t table_id, uint64_t column_id)
{
  if (ai_column_internal_depth != 0 || !session.is_user_session()) return OB_SUCCESS;
  auto &service = instance();
  std::lock_guard<std::mutex> lock(service.columns_mutex_);
  for (const auto &c : service.columns_) if (c.table_id == table_id && c.column_id == column_id) {
    LOG_USER_ERROR(OB_NOT_SUPPORTED, "assigning an AI result column");
    return OB_NOT_SUPPORTED;
  }
  return OB_SUCCESS;
}

int AiColumnService::check_read(sql::ObSQLSessionInfo &session, uint64_t table_id, uint64_t column_id)
{
  if (ai_column_internal_depth != 0) return OB_SUCCESS;
  bool strict = false;
  int ret = session.get_sys_variable(share::SYS_VAR_AI_COLUMN_STRICT_READ, strict);
  if (OB_FAIL(ret) || !strict) return ret;
  try {
    Column column;
    bool found = false;
    {
      auto &service = instance();
      std::lock_guard<std::mutex> lock(service.columns_mutex_);
      for (const auto &c : service.columns_) if (c.table_id == table_id && c.column_id == column_id) {
        column = c; found = true; break;
      }
    }
    if (!found) return OB_SUCCESS;
    InternalGuard internal;
    int64_t unfinished = 0;
    ret = read_count_in_session(session, "SELECT COUNT(*) FROM " + column.qualified_table() +
      " WHERE " + identifier(column.state_name()) + " <> 'READY' OR " + identifier(column.state_name()) + " IS NULL", unfinished);
    if (OB_SUCC(ret) && unfinished != 0) {
      LOG_USER_ERROR(OB_AI_COLUMN_NOT_READY, table_id, column_id);
      ret = OB_AI_COLUMN_NOT_READY;
    }
  } catch (const std::bad_alloc &) { ret = OB_ALLOCATE_MEMORY_FAILED; }
  return ret;
}

int AiColumnService::create_columns(const std::string &text, const std::vector<Token> &tokens,
                                   sql::ObSQLSessionInfo &session)
{
  std::string database(session.get_database_name().ptr(), session.get_database_name().length());
  if (tokens.size() < 4 || !tokens[1].is("table")) throw std::invalid_argument("AI COLUMN requires CREATE TABLE or ALTER TABLE");
  size_t pos = 2;
  if (tokens[pos].is("if")) pos += 3;
  const auto name = table_name(tokens, pos, database);
  std::vector<Definition> definitions;
  size_t field = pos + 1;
  int depth = 0;
  if (tokens[0].is("alter")) {
    if (pos < tokens.size() && tokens[pos].is("add")) ++pos;
    else throw std::invalid_argument("only ADD AI COLUMN is supported by ALTER TABLE");
    if (pos < tokens.size() && tokens[pos].is("column")) ++pos;
    field = pos;
  }
  for (size_t i = pos; i + 1 < tokens.size(); ++i) {
    if (tokens[i].is("ai") && tokens[i + 1].is("column")) {
      Definition d = parse_definition(text, tokens, field, i);
      definitions.push_back(d);
      while (i + 1 < tokens.size() && tokens[i + 1].end <= d.end) ++i;
    } else if (tokens[i].is("(")) { ++depth;
    } else if (tokens[i].is(")")) { --depth;
    } else if (tokens[i].is(",") && (depth == 1 || tokens[0].is("alter"))) field = i + 1;
  }
  if (definitions.empty()) throw std::invalid_argument("missing AI COLUMN definition");
  {
    ObArenaAllocator allocator(ObMemAttr("AiColumnDDL"));
    auto *context = session.get_cur_exec_ctx();
    auto *guard = context == nullptr || context->get_sql_ctx() == nullptr ? nullptr : context->get_sql_ctx()->schema_guard_;
    if (guard == nullptr) return OB_ERR_UNEXPECTED;
    share::schema::ObSessionPrivInfo privilege;
    sql::ObAIServiceEndpointPrivUtil access(*guard);
    bool allowed = false;
    int ret = guard->get_session_priv_info(session.get_priv_user_id(), session.get_database_name(), privilege);
    if (OB_SUCC(ret)) ret = access.check_access_ai_model_priv(allocator, privilege, allowed);
    if (OB_FAIL(ret)) return ret;
    if (!allowed) { LOG_USER_ERROR(OB_ERR_NO_PRIVILEGE, "access ai model"); return OB_ERR_NO_PRIVILEGE; }
    for (const auto &d : definitions) {
      ObAIFuncExprInfo *info = nullptr;
      ret = ObAIFuncUtils::get_ai_func_info(allocator, ObString(d.model.size(), d.model.data()), info);
      if (OB_FAIL(ret)) return ret;
      const auto expected = d.embedding ? share::EndpointType::DENSE_EMBEDDING : share::EndpointType::COMPLETION;
      if (info == nullptr || info->type_ != expected) {
        LOG_USER_ERROR(OB_INVALID_ARGUMENT, "AI column model type");
        return OB_INVALID_ARGUMENT;
      }
    }
  }
  for (const auto &d : definitions) {
    if (d.name.size() > 45 || d.name.rfind("__ai_", 0) == 0) throw std::invalid_argument("AI column name is too long or reserved");
    const auto input_tokens = tokenize(d.input);
    for (const auto &other : definitions) if (contains_name(input_tokens, other.name))
      throw DependencyError();
  }
  std::vector<Column> all;
  if (tokens[0].is("alter")) {
    // DROP/CREATE can reuse a name before the periodic cache refresh. A new
    // table must never inherit the prior table's column ids or triggers.
    int refresh_ret = reload_columns();
    if (refresh_ret != OB_SUCCESS) return refresh_ret;
    std::lock_guard<std::mutex> lock(columns_mutex_);
    for (const auto &c : columns_) if (lower(c.database) == lower(name.first) && lower(c.table) == lower(name.second)) all.push_back(c);
  }
  for (const auto &d : definitions) for (const auto &c : all) if (contains_name(tokenize(d.input), c.definition.name))
    throw DependencyError();
  std::string lowered;
  size_t previous = 0;
  for (const auto &d : definitions) {
    lowered += text.substr(previous, d.begin - previous);
    lowered += " COMMENT 'AI COLUMN'";
    previous = d.end;
  }
  lowered += text.substr(previous);
  Rows existing;
  int ret = read_rows(*GCTX.sql_proxy_, "SELECT CAST(COUNT(*) AS CHAR) FROM oceanbase.__all_table t JOIN oceanbase.__all_database d"
    " ON d.database_id=t.database_id WHERE " + key_condition(name.first, name.second), 1, existing);
  if (OB_FAIL(ret)) return ret;
  if (tokens[0].is("create") && !existing.empty() && existing[0][0] != "0") return user_write(session, lowered);
  if (tokens[0].is("alter")) {
    // Detect reserved-name collisions before changing the user table. The
    // outer SQL retry controller can replay a duplicate-column error, which
    // would otherwise repeatedly add and remove the visible result column.
    for (const auto &d : definitions) {
      Rows collisions;
      ret = read_rows(*GCTX.sql_proxy_, "SELECT CAST(COUNT(*) AS CHAR) FROM oceanbase.__all_table t"
          " JOIN oceanbase.__all_database d ON d.database_id=t.database_id"
          " JOIN oceanbase.__all_column c ON c.table_id=t.table_id WHERE " + key_condition(name.first, name.second) +
          " AND c.column_name IN (" + literal("__ai_state_" + d.name) + "," + literal("__ai_request_" + d.name) +
          "," + literal("__ai_spec_" + d.name) + ")", 1, collisions);
      if (OB_FAIL(ret)) return ret;
      if (collisions.size() != 1 || collisions[0][0] != "0") {
        LOG_USER_ERROR(OB_INVALID_ARGUMENT, "AI column hidden name conflicts with existing column");
        return OB_INVALID_ARGUMENT;
      }
    }
  }
  const std::vector<Column> old_columns = all;
  std::vector<std::string> hidden_added;
  // A concurrent creator may win after the existence check. In that case
  // IF NOT EXISTS must remain a no-op and must not claim its table for cleanup.
  if (tokens[0].is("create") && tokens[2].is("if") && tokens.size() > 4 &&
      tokens[3].is("not") && tokens[4].is("exists")) {
    std::string exclusive = lowered;
    exclusive.erase(tokens[2].begin, tokens[4].end - tokens[2].begin);
    {
      DiagnosticsGuard diagnostics;
      ret = user_write(session, exclusive);
      if (ret != OB_SUCCESS && ret != OB_ERR_TABLE_EXIST) diagnostics.propagate_error();
    }
    if (ret == OB_ERR_TABLE_EXIST) return user_write(session, lowered);
  } else {
    ret = user_write(session, lowered);
  }
  if (OB_FAIL(ret)) return ret;
  uint64_t owned_table_id = 0;
  DdlCleanup cleanup{[&]() -> int {
    uint64_t current_id = 0;
    int cleanup_ret = table_identity(name.first, name.second, current_id);
    if (cleanup_ret != OB_SUCCESS || owned_table_id == 0 || current_id != owned_table_id) {
      LOG_WARN("AI column cleanup cannot establish table ownership", K(cleanup_ret), K(owned_table_id), K(current_id));
      return cleanup_ret != OB_SUCCESS ? cleanup_ret : OB_SCHEMA_ERROR;
    }
    int first_error = OB_SUCCESS;
    auto record = [&](int code) { if (first_error == OB_SUCCESS && code != OB_SUCCESS) first_error = code; };
    if (tokens[0].is("create")) {
      record(user_write(session, "DROP TABLE " + identifier(name.first) + "." + identifier(name.second)));
    } else {
      for (const auto &d : definitions) {
        const std::string table = identifier(name.first) + "." + identifier(name.second);
        if (std::find(hidden_added.begin(), hidden_added.end(), d.name) != hidden_added.end()) {
          record(user_write(session, "ALTER TABLE " + table + " DROP COLUMN " + identifier("__ai_state_" + d.name) +
            ", DROP COLUMN " + identifier("__ai_request_" + d.name) + ", DROP COLUMN " + identifier("__ai_spec_" + d.name)));
        }
        record(user_write(session, "ALTER TABLE " + table + " DROP COLUMN " + identifier(d.name)));
      }
    }
    for (const auto &d : definitions) record(write_sql(*GCTX.sql_proxy_, std::string("DELETE FROM ") + META + " WHERE table_id=" +
      number(owned_table_id) + " AND column_name=" + literal(d.name)));
    if (!old_columns.empty()) record(install_triggers(old_columns));
    record(reload_columns());
    return first_error;
  }};
  auto perform = [&]() -> int {
  ret = table_identity(name.first, name.second, owned_table_id);
  if (OB_FAIL(ret)) return ret;
  for (const auto &d : definitions) {
    Column c;
    c.database = name.first; c.table = name.second; c.definition = d;
    const std::string hidden = " COMMENT '__seekdb_ai_hidden'";
    ret = add_hidden_columns(session, "ALTER TABLE " + c.qualified_table() + " ADD COLUMN " + identifier(c.state_name()) +
      " VARCHAR(8) DEFAULT 'PENDING'" + hidden + ", ADD COLUMN " + identifier(c.request_name()) +
      " VARCHAR(64)" + hidden + ", ADD COLUMN " + identifier(c.spec_name()) + " LONGTEXT" + hidden,
      c.database, c.table, owned_table_id, c.state_name(), c.request_name(), c.spec_name());
    if (OB_FAIL(ret)) return ret;
    hidden_added.push_back(d.name);
    Rows schema;
    ret = read_rows(*GCTX.sql_proxy_, "SELECT CAST(t.table_id AS CHAR),CAST(c.column_id AS CHAR),c.column_name,CAST(t.schema_version AS CHAR)"
      " FROM oceanbase.__all_table t JOIN oceanbase.__all_database d ON d.database_id=t.database_id"
      " JOIN oceanbase.__all_column c ON c.table_id=t.table_id WHERE " + key_condition(c.database, c.table), 4, schema);
    if (OB_FAIL(ret)) return ret;
    uint64_t state_id = 0, request_id = 0, spec_id = 0;
    for (const auto &r : schema) {
      c.table_id = std::stoull(r[0]); c.version = std::stoull(r[3]);
      if (r[2] == d.name) c.column_id = std::stoull(r[1]);
      if (r[2] == c.state_name()) state_id = std::stoull(r[1]);
      if (r[2] == c.request_name()) request_id = std::stoull(r[1]);
      if (r[2] == c.spec_name()) spec_id = std::stoull(r[1]);
    }
    if (c.table_id != owned_table_id || !c.column_id || !state_id || !request_id || !spec_id) return OB_ERR_UNEXPECTED;
    std::string expr = d.expression + (d.prompt.empty() ? "" : " /* " + d.prompt + " */");
    ret = write_sql(*GCTX.sql_proxy_, std::string("INSERT INTO ") + META + " VALUES(" + number(c.table_id) + "," + number(c.column_id) + "," +
      literal(d.name) + "," + literal(c.database) + "," + literal(c.table) + "," + number(state_id) + "," + number(request_id) + "," + number(spec_id) + "," + number(c.version) + "," +
      literal(expr) + "," + literal(d.input) + "," + literal(d.type) + "," + literal(d.model) + "," + literal(d.prompt) + "," + number(d.retries) + "," + number(d.dimension) + ")");
    if (OB_FAIL(ret)) return ret;
    all.push_back(c);
  }
  if (OB_SUCC(ret)) ret = install_triggers(all);
  if (OB_SUCC(ret)) ret = reload_columns();
  if (OB_SUCC(ret) && tokens[0].is("alter")) {
    std::string assignments;
    for (const auto &d : definitions) {
      if (!assignments.empty()) assignments += ",";
      assignments += identifier("__ai_request_" + d.name) + "=UUID()";
    }
    ret = write_sql(*GCTX.sql_proxy_, "UPDATE " + identifier(name.first) + "." + identifier(name.second) + " SET " + assignments);
  }
  return ret;
  };
  ret = perform();
  if (OB_SUCC(ret)) cleanup.complete = true;
  else {
    int cleanup_ret = cleanup.run();
    if (cleanup_ret != OB_SUCCESS) {
      LOG_WARN("AI column DDL failed and cleanup was incomplete", K(ret), K(cleanup_ret));
      ret = cleanup_ret;
    }
  }
  return ret;
}

int AiColumnService::install_triggers(const std::vector<Column> &columns)
{
  if (columns.empty()) return OB_INVALID_ARGUMENT;
  const auto &first = columns[0];
  Rows schema;
  int ret = read_rows(*GCTX.sql_proxy_, "SELECT c.column_name,CAST(c.rowkey_position AS CHAR),CAST(c.data_type AS CHAR) FROM oceanbase.__all_column c WHERE c.table_id=" +
    number(first.table_id) + " ORDER BY c.rowkey_position,c.column_id", 3, schema);
  if (OB_FAIL(ret)) return ret;
  std::vector<std::string> names, keys;
  std::string new_key = "CONCAT(", old_key = "CONCAT(";
  for (const auto &r : schema) {
    names.push_back(r[0]);
    if (std::stoi(r[1]) > 0) {
      bool numeric = ob_is_numeric_type(static_cast<ObObjType>(std::stoi(r[2])));
      if (!keys.empty()) { new_key += ",' AND ',"; old_key += ",' AND ',"; }
      keys.push_back(r[0]);
      const std::string part = literal(identifier(r[0]) + " = ") + ",";
      new_key += part + (numeric ? text_cast("NEW." + identifier(r[0])) : "QUOTE(NEW." + identifier(r[0]) + ")");
      old_key += part + (numeric ? text_cast("OLD." + identifier(r[0])) : "QUOTE(OLD." + identifier(r[0]) + ")");
    }
  }
  if (keys.empty()) throw std::invalid_argument("AI columns require a primary key");
  new_key += ")"; old_key += ")";
  std::string insert = "BEGIN DECLARE ai_request VARCHAR(64); SET ai_request=UUID(); ";
  std::string update = insert;
  std::string remove = std::string("BEGIN DELETE FROM ") + TASKS + " WHERE table_id=" + number(first.table_id) + " AND rowkey_predicate=" + old_key + "; END";
  for (const auto &c : columns) {
    const auto &d = c.definition;
    const auto input_tokens = tokenize(d.input);
    std::string changed = "NOT(NEW." + identifier(c.request_name()) + " <=> OLD." + identifier(c.request_name()) + ")";
    for (const auto &n : names) {
      if (std::find(keys.begin(), keys.end(), n) != keys.end() || contains_name(input_tokens, n))
        changed += " OR NOT(NEW." + identifier(n) + " <=> OLD." + identifier(n) + ")";
    }
    const std::string set = "SET NEW." + identifier(d.name) + "=NULL,NEW." + identifier(c.state_name()) + "='PENDING',NEW." +
      identifier(c.request_name()) + "=ai_request,NEW." + identifier(c.spec_name()) + "=" + qualify_input(d, names, "NEW.") + "; ";
    const std::string enqueue = std::string("INSERT INTO ") + TASKS + "(table_id,column_id,request_id,rowkey_predicate,definition_version,input_value,eligible_ts) VALUES(" +
      number(c.table_id) + "," + number(c.column_id) + ",ai_request," + new_key + "," + number(c.version) + ",NEW." + identifier(c.spec_name()) + ",NOW(6)); ";
    insert += set + enqueue;
    update += "IF " + changed + " THEN DELETE FROM " + TASKS + " WHERE table_id=" + number(c.table_id) + " AND column_id=" + number(c.column_id) +
      " AND rowkey_predicate=" + old_key + "; " + set + enqueue + "END IF; ";
  }
  insert += "END"; update += "END";
  const char *events[] = {"INSERT", "UPDATE", "DELETE"};
  const std::string bodies[] = {insert, update, remove};
  for (size_t i = 0; i < 3 && OB_SUCC(ret); ++i) {
    const std::string trigger_name = "__ai_" + number(first.table_id) + "_" + events[i];
    std::string trigger = identifier(first.database) + "." + identifier(trigger_name);
    ret = trigger_ddl("DROP TRIGGER IF EXISTS " + trigger, first.database, first.table, first.table_id, trigger_name, false);
    if (OB_SUCC(ret)) ret = trigger_ddl("CREATE TRIGGER " + trigger + " BEFORE " + events[i] + " ON " + first.qualified_table() + " FOR EACH ROW " + bodies[i],
                                       first.database, first.table, first.table_id, trigger_name, true);
  }
  return ret;
}

void AiColumnService::run(int64_t index)
{
  lib::set_thread_name("AiColumnWorker");
  int64_t last_cleanup = 0;
  while (!has_set_stop()) {
    try {
      InternalGuard internal;
      if (!ready_.load()) {
        if (index == 0) {
          std::lock_guard<std::mutex> lock(ddl_mutex_);
          if (!ready_.load()) (void)initialize_tables();
        }
      } else if (GCTX.sql_proxy_ != nullptr) {
        if (index == 0 && ObTimeUtility::current_time() - last_cleanup >= 1000000) {
          last_cleanup = ObTimeUtility::current_time();
          const std::string live = std::string("SELECT a.table_id FROM ") + META + " a JOIN oceanbase.__all_table t ON t.table_id=a.table_id"
            " AND t.table_name=a.table_name JOIN oceanbase.__all_database d ON d.database_id=t.database_id AND d.database_name=a.database_name";
          (void)write_sql(*GCTX.sql_proxy_, std::string("DELETE FROM ") + TASKS + " WHERE table_id NOT IN (" + live + ")");
          (void)write_sql(*GCTX.sql_proxy_, std::string("DELETE FROM ") + META + " WHERE table_id NOT IN (SELECT table_id FROM (" + live + ") live_tables)");
          (void)reload_columns();
        }
        Rows rows;
        int ret = read_rows(*GCTX.sql_proxy_, std::string("SELECT request_id FROM ") + TASKS +
          " WHERE eligible_ts<=NOW(6) AND (owner IS NULL OR lease_until<NOW(6)) ORDER BY eligible_ts LIMIT 1", 1, rows);
        if (OB_SUCC(ret) && !rows.empty()) {
          std::string owner = number(ObTimeUtility::current_time()) + "-" + number(index);
          (void)process_group(rows[0][0], owner);
        }
      }
    } catch (const std::exception &e) {
      int ret = OB_ERR_UNEXPECTED;
      LOG_WARN("AI column worker failure", "error", e.what());
    }
    ob_usleep(50000);
  }
}

int AiColumnService::process_group(const std::string &request, const std::string &owner)
{
  int64_t affected = 0;
  int ret = write_sql(*GCTX.sql_proxy_, std::string("UPDATE ") + TASKS + " SET owner=" + literal(owner) +
    ",lease_until=DATE_ADD(NOW(6),INTERVAL 60 SECOND) WHERE request_id=" + literal(request) +
    " AND (owner IS NULL OR lease_until<NOW(6))", &affected);
  if (OB_FAIL(ret) || affected == 0) return ret;
  Rows rows;
  ret = read_rows(*GCTX.sql_proxy_, std::string("SELECT CAST(table_id AS CHAR),CAST(column_id AS CHAR),rowkey_predicate,CAST(COALESCE(input_value,'') AS CHAR),") +
    "CAST(state AS CHAR),CAST(retry_count AS CHAR),CAST(COALESCE(result_value,'') AS CHAR),CAST(definition_version AS CHAR),"
    "CAST(input_value IS NULL AS CHAR),CAST(result_value IS NULL AS CHAR) FROM " + TASKS +
    " WHERE request_id=" + literal(request) + " AND owner=" + literal(owner) + " ORDER BY column_id", 10, rows);
  if (OB_FAIL(ret)) return ret;
  std::vector<Column> columns;
  { std::lock_guard<std::mutex> lock(columns_mutex_); columns = columns_; }
  std::vector<Task> tasks;
  bool complete = true;
  for (const auto &r : rows) {
    auto it = std::find_if(columns.begin(), columns.end(), [&](const Column &c) {
      return c.table_id == std::stoull(r[0]) && c.column_id == std::stoull(r[1]) && c.version == std::stoull(r[7]);
    });
    if (it == columns.end()) continue;
    Task t;
    t.column = *it; t.rowkey = r[2]; t.request = request; t.input = r[3];
    t.state = std::stoi(r[4]); t.retries = std::stoi(r[5]); t.result = r[6];
    t.input_null = r[8] == "1";
    t.result_null = r[9] == "1";
    if (t.state != 4 && t.state != 5) {
      // Keep the lease uniform for the whole publication group, including
      // columns not yet called. Otherwise a large group could be partially
      // reclaimed while this worker is still processing its earlier columns.
      int64_t renewed = 0;
      ret = write_sql(*GCTX.sql_proxy_, std::string("UPDATE ") + TASKS +
        " SET lease_until=DATE_ADD(NOW(6),INTERVAL 60 SECOND) WHERE request_id=" + literal(request) +
        " AND owner=" + literal(owner) + " AND lease_until>NOW(6)", &renewed);
      if (OB_FAIL(ret)) return ret;
      if (renewed == 0) {
        // SQL affected rows count actual changes. An idempotent renewal can
        // legitimately report zero; distinguish it from losing ownership.
        Rows owned;
        ret = read_rows(*GCTX.sql_proxy_, std::string("SELECT CAST(COUNT(*) AS CHAR) FROM ") + TASKS +
          " WHERE request_id=" + literal(request) + " AND owner=" + literal(owner) + " AND lease_until>NOW(6)", 1, owned);
        if (OB_FAIL(ret) || owned.empty() || owned[0][0] == "0") return ret;
      }
      const int call_ret = invoke(t);
      if (call_ret == OB_SUCCESS) t.state = 4;
      else if (t.retries < t.column.definition.retries) { ++t.retries; t.state = 3; complete = false; }
      else { t.state = 5; t.result.clear(); }
      ret = write_sql(*GCTX.sql_proxy_, std::string("UPDATE ") + TASKS + " SET state=" + number(t.state) + ",retry_count=" + number(t.retries) +
        ",result_value=" + (t.result_null ? std::string("NULL") : literal(t.result)) + ",eligible_ts=DATE_ADD(NOW(6),INTERVAL 1 SECOND),lease_until=DATE_ADD(NOW(6),INTERVAL 60 SECOND)"
        " WHERE table_id=" + number(t.column.table_id) + " AND column_id=" + number(t.column.column_id) + " AND rowkey_predicate=" + literal(t.rowkey) +
        " AND request_id=" + literal(request) + " AND owner=" + literal(owner));
      if (OB_FAIL(ret)) return ret;
    }
    tasks.push_back(std::move(t));
  }
  if (complete && !tasks.empty()) ret = publish(tasks, owner);
  else ret = write_sql(*GCTX.sql_proxy_, std::string("UPDATE ") + TASKS + " SET owner=NULL,lease_until=NULL WHERE request_id=" + literal(request) + " AND owner=" + literal(owner));
  return ret;
}

int AiColumnService::invoke(Task &task)
{
  // Match the scalar AI functions: SQL NULL input produces a completed SQL
  // NULL without an external request. It must not become an empty prompt.
  if (task.input_null) {
    task.result_null = true;
    task.result.clear();
    return OB_SUCCESS;
  }
  task.result_null = false;
  ObArenaAllocator allocator(ObMemAttr("AiColumnCall"));
  ObAIFuncExprInfo *info = nullptr;
  ObString model_id(task.column.definition.model.size(), task.column.definition.model.data());
  share::ObAiModelEndpointInfo endpoint;
  int ret = ObAIFuncUtils::get_ai_func_info(allocator, model_id, info);
  if (OB_SUCC(ret)) ret = share::ObAiServiceExecutor::read_ai_endpoint_by_ai_model_name(allocator, model_id, endpoint);
  if (OB_SUCC(ret) && info != nullptr) {
    ObAIFuncModel model(allocator, *info, endpoint);
    ObString input(task.input.size(), task.input.data());
    ObString prompt(task.column.definition.prompt.size(), task.column.definition.prompt.data());
    ObString result;
    if (task.column.definition.embedding) ret = model.call_column_embedding(input, result, task.column.definition.dimension);
    else ret = model.call_column_completion(input, prompt, result);
    if (OB_SUCC(ret)) task.result.assign(result.ptr(), result.length());
  }
  // Validate using exactly the SQL cast used by publication, before deciding
  // whether this call is terminal. Invalid JSON, numbers and vector dimensions
  // participate in the same finite retry policy as transport errors.
  if (OB_SUCC(ret)) {
    Rows rows;
    const auto &d = task.column.definition;
    if (d.embedding) {
      const char *p = task.result.c_str();
      auto whitespace = [&]() { while (std::isspace(static_cast<unsigned char>(*p))) ++p; };
      whitespace();
      if (*p++ != '[') return OB_INVALID_DATA;
      int count = 0;
      for (;;) {
        whitespace();
        char *end = nullptr;
        errno = 0;
        const double value = std::strtod(p, &end);
        if (end == p || errno != 0 || !std::isfinite(value) ||
            std::fabs(value) > std::numeric_limits<float>::max()) return OB_INVALID_DATA;
        p = end; ++count; whitespace();
        if (*p == ']') { ++p; break; }
        if (*p++ != ',' || count >= d.dimension) return OB_INVALID_DATA;
      }
      whitespace();
      return *p == 0 && count == d.dimension ? OB_SUCCESS : OB_ERR_INVALID_VECTOR_DIM;
    }
    if (d.prompt.find("integer") != std::string::npos || d.prompt.find("finite number") != std::string::npos) {
      const char *p = task.result.c_str();
      while (std::isspace(static_cast<unsigned char>(*p))) ++p;
      char *end = nullptr;
      errno = 0;
      if (d.prompt.find("integer") != std::string::npos) {
        const auto type = tokenize(d.type);
        const int64_t value = std::strtoll(p, &end, 10);
        int bits = type[0].is("tinyint") ? 8 : type[0].is("smallint") ? 16 :
          (type[0].is("int") || type[0].is("integer")) ? 32 : 64;
        if (bits < 64 && (value < -(int64_t(1) << (bits - 1)) || value >= (int64_t(1) << (bits - 1)))) return OB_INVALID_DATA;
      }
      else if (!std::isfinite(std::strtod(p, &end))) return OB_INVALID_DATA;
      if (end == p || errno != 0) return OB_INVALID_DATA;
      while (std::isspace(static_cast<unsigned char>(*end))) ++end;
      if (*end != 0) return OB_INVALID_DATA;
    }
    std::string cast_type = (tokenize(d.type)[0].is("json") ? "JSON" :
      (d.prompt.find("integer") != std::string::npos ? "SIGNED" :
       (d.prompt.find("finite number") != std::string::npos ? "DECIMAL(65,20)" : "CHAR")));
    const auto type = tokenize(d.type);
    if ((type[0].is("varchar") || type[0].is("char")) && type.size() >= 4) {
      ret = read_rows(*GCTX.sql_proxy_, "SELECT CAST(CHAR_LENGTH(" + literal(task.result) + ") AS CHAR)", 1, rows);
      if (OB_SUCC(ret) && (!rows.empty() && std::stoull(rows[0][0]) > std::stoull(type[2].text))) ret = OB_SIZE_OVERFLOW;
    } else ret = read_rows(*GCTX.sql_proxy_, "SELECT CAST(CAST(" + literal(task.result) + " AS " + cast_type + ") AS CHAR)", 1, rows);
  }
  return ret;
}

int AiColumnService::publish(const std::vector<Task> &tasks, const std::string &owner)
{
  ObMySQLTransaction transaction;
  int ret = transaction.start(GCTX.sql_proxy_);
  if (OB_FAIL(ret)) return ret;
  Rows ownership;
  ret = read_rows(transaction, std::string("SELECT CAST(column_id AS CHAR) FROM ") + TASKS + " WHERE request_id=" + literal(tasks[0].request) +
    " AND owner=" + literal(owner) + " AND lease_until>NOW(6) FOR UPDATE", 1, ownership);
  for (const auto &t : tasks) {
    bool owned = false;
    for (const auto &r : ownership) if (r[0] == number(t.column.column_id)) owned = true;
    if (!owned) continue;
    const auto &c = t.column;
    const std::string matches = identifier(c.request_name()) + "=" + literal(t.request);
    const std::string result = t.state == 4 && !t.result_null ? literal(t.result) : "NULL";
    if (OB_SUCC(ret)) ret = write_sql(transaction, "UPDATE " + c.qualified_table() + " SET " + identifier(c.definition.name) + "=" + result + "," +
      identifier(c.state_name()) + "=" + literal(t.state == 4 ? "READY" : "FAILED") + " WHERE " + t.rowkey + " AND " + matches);
  }
  if (OB_SUCC(ret)) ret = write_sql(transaction, std::string("DELETE FROM ") + TASKS + " WHERE request_id=" + literal(tasks[0].request) + " AND owner=" + literal(owner));
  const int end_ret = transaction.end(OB_SUCC(ret));
  if (OB_FAIL(ret) || end_ret != OB_SUCCESS) {
    (void)write_sql(*GCTX.sql_proxy_, std::string("UPDATE ") + TASKS + " SET owner=NULL,lease_until=NULL WHERE request_id=" + literal(tasks[0].request) + " AND owner=" + literal(owner));
  }
  return OB_SUCC(ret) ? end_ret : ret;
}

} }
