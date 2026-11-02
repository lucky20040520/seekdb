/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */

#ifdef NDEBUG
#undef NDEBUG
#endif
#include "sql/engine/basic/file_csv_schema.h"
#include <cassert>
#include <fstream>
#include <sstream>

using namespace oceanbase::sql;

int main()
{
  using T = FileValueType;
  std::vector<FileCsvColumn> columns;
  {
    std::istringstream input("id,id,day,amount,empty\n1,2,2024-02-29,1.0,\n3,4,,2.5,\n");
    FileCsvReader reader(input, 256, 5);
    assert(FileCsvSchema::infer(reader, true, columns) == FileCsvStatus::ROW);
    assert(columns.size() == 5 && columns[0].name == "id" && columns[1].name == "id__2");
    assert(columns[0].inference.type() == T::BIGINT && !columns[0].inference.nullable());
    assert(columns[2].inference.type() == T::DATE && columns[2].inference.nullable());
    assert(columns[3].inference.type() == T::DOUBLE);
    assert(columns[4].inference.type() == T::NULL_VALUE && columns[4].inference.nullable());
  }
  {
    std::istringstream input("1,\"\"\n2,text\n");
    FileCsvReader reader(input, 256, 5);
    assert(FileCsvSchema::infer(reader, false, columns) == FileCsvStatus::ROW);
    assert(columns.size() == 2 && columns[0].name == "column1");
    assert(columns[0].inference.type() == T::BIGINT);
    assert(columns[1].inference.type() == T::VARCHAR && !columns[1].inference.nullable());
  }
  {
    std::istringstream input("a,b\n1,2,3\n");
    FileCsvReader reader(input, 256, 5);
    assert(FileCsvSchema::infer(reader, true, columns) == FileCsvStatus::INVALID);
  }
  {
    std::ifstream input("tools/deploy/mysql_test/test_suite/file_sql/data/basic.csv", std::ios::binary);
    assert(input.is_open());
    FileCsvReader reader(input, 4096, 16);
    assert(FileCsvSchema::infer(reader, true, columns) == FileCsvStatus::ROW);
    assert(columns.size() == 8 && columns[7].name == "id__2");
    assert(columns[0].inference.type() == T::BIGINT);
    assert(columns[1].inference.type() == T::VARCHAR);
    assert(columns[2].inference.type() == T::DOUBLE);
    assert(columns[3].inference.type() == T::BOOLEAN);
    assert(columns[4].inference.type() == T::DATE && columns[4].inference.nullable());
    assert(columns[5].inference.type() == T::DATETIME && columns[5].inference.nullable());
  }
  return 0;
}
