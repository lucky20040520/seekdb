#pragma once

#include <atomic>
#include <mutex>
#include <string>
#include <vector>
#include "lib/thread/thread_pool.h"
#include "sql/parser/ai_column_definition.h"

namespace oceanbase {
namespace sql { class ObSQLSessionInfo; }
namespace observer {

// Native row triggers maintain the durable queue in the business transaction.
// Workers see committed tasks only, and publish each request group in one SQL
// transaction, through the ordinary DML/index maintenance path.
class AiColumnService : public lib::ThreadPool
{
public:
  static AiColumnService &instance();
  static int prepare(const common::ObString &sql, sql::ObSQLSessionInfo &session,
                     common::ObIAllocator &allocator, common::ObString &replacement);
  static int check_read(sql::ObSQLSessionInfo &session, uint64_t table_id, uint64_t column_id);
  static int check_assignment(sql::ObSQLSessionInfo &session, uint64_t table_id, uint64_t column_id);
  int start() override;
  void run(int64_t index) override;
private:
  struct Column
  {
    sql::ai_column::Definition definition;
    std::string database;
    std::string table;
    uint64_t table_id = 0;
    uint64_t column_id = 0;
    uint64_t version = 0;
    std::string state_name() const;
    std::string request_name() const;
    std::string spec_name() const;
    std::string qualified_table() const;
  };
  struct Task
  {
    Column column;
    std::string rowkey;
    std::string request;
    std::string input;
    std::string result;
    bool input_null = false;
    bool result_null = false;
    int state = 0;
    int retries = 0;
  };
  AiColumnService() : lib::ThreadPool(2), ready_(false) {}
  int initialize_tables();
  int reload_columns();
  int create_columns(const std::string &text, const std::vector<sql::ai_column::Token> &tokens,
                     sql::ObSQLSessionInfo &session);
  int install_triggers(const std::vector<Column> &columns);
  int process_group(const std::string &request, const std::string &owner);
  int invoke(Task &task);
  int publish(const std::vector<Task> &tasks, const std::string &owner);
  int prepare_inner(const std::string &text, sql::ObSQLSessionInfo &session,
                    std::string &replacement);
  std::mutex ddl_mutex_;
  std::mutex columns_mutex_;
  std::vector<Column> columns_;
  std::atomic<bool> ready_;
};

} }
