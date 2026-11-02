/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */
#ifdef NDEBUG
#undef NDEBUG
#endif
#include "sql/engine/basic/file_jsonl_scan.h"
#include <cassert>
#include <cstdlib>

using namespace oceanbase::sql;

int main()
{
  char path[PATH_MAX];
  assert(realpath("tools/deploy/mysql_test/test_suite/file_sql/data/basic.jsonl", path));
  FileJsonlScan scan;
  assert(scan.open(false, "/", path, 1024, 16) == FileCsvScanStatus::DISABLED);
  assert(scan.open(true, "/", path, 1024, 16) == FileCsvScanStatus::OK);
  assert(scan.columns().size() == 6);
  std::vector<FileCsvField> row;
  assert(scan.next(row) == FileCsvScanStatus::ROW && row[0].value == "1" && row[1].value == "Alice");
  assert(scan.next(row) == FileCsvScanStatus::ROW && row[2].value.empty() && !row[2].quoted);
  assert(scan.next(row) == FileCsvScanStatus::ROW && row[5].value == "ok");
  assert(scan.next(row) == FileCsvScanStatus::END);
  assert(scan.rescan() == FileCsvScanStatus::OK);
  assert(scan.next(row) == FileCsvScanStatus::ROW && row[0].value == "1");
  return 0;
}
