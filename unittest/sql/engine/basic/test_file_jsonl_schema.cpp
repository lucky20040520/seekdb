/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */

#ifdef NDEBUG
#undef NDEBUG
#endif
#include "sql/engine/basic/file_jsonl_schema.h"
#include <cassert>
#include <fstream>
#include <sstream>

using namespace oceanbase::sql;

int main()
{
  std::vector<FileJsonlColumn> columns;
  size_t line = 0;
  {
    std::ifstream input("tools/deploy/mysql_test/test_suite/file_sql/data/basic.jsonl", std::ios::binary);
    assert(input.is_open());
    assert(FileJsonlSchema::infer(input, 1024, 16, columns, line) == FileJsonlStatus::OK);
    assert(columns.size() == 6);
    assert(columns[0].name == "id" && columns[0].inference.type() == FileValueType::BIGINT);
    assert(columns[2].name == "score" && columns[2].inference.type() == FileValueType::DOUBLE);
    assert(columns[2].inference.nullable());
    assert(columns[4].name == "day" && columns[4].inference.type() == FileValueType::DATE);
    assert(columns[4].inference.nullable());
  }
  {
    std::istringstream input("{\"a\":1}\n{\"b\":true}\n");
    assert(FileJsonlSchema::infer(input, 100, 2, columns, line) == FileJsonlStatus::OK);
    assert(columns.size() == 2 && columns[0].inference.nullable() && columns[1].inference.nullable());
  }
  {
    std::istringstream input("{\"a\":1}\n{\"a\":[1]}\n");
    assert(FileJsonlSchema::infer(input, 100, 2, columns, line) == FileJsonlStatus::INVALID && line == 2);
  }
  {
    std::istringstream input("{\"a\":1,\"a\":2}\n");
    assert(FileJsonlSchema::infer(input, 100, 2, columns, line) == FileJsonlStatus::INVALID);
  }
  {
    std::istringstream input("{\"key\":123}\n");
    assert(FileJsonlSchema::infer(input, 5, 2, columns, line) == FileJsonlStatus::TOO_LARGE);
  }
  {
    std::istringstream input("{\"a\":\"123\"}\n");
    assert(FileJsonlSchema::infer(input, 100, 2, columns, line) == FileJsonlStatus::OK);
    assert(columns[0].inference.type() == FileValueType::VARCHAR);
  }
  return 0;
}
