/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */
#ifdef NDEBUG
#undef NDEBUG
#endif
#include "sql/engine/basic/file_glob_scan.h"
#include <cassert>

using namespace oceanbase::sql;

int main()
{
  char path[PATH_MAX];
  assert(realpath("tools/deploy/mysql_test/test_suite/file_sql/data", path));
  const std::string root(path);
  FileGlobScan scan;
  assert(scan.open(false, root, root + "/*.csv") == FileCsvScanStatus::DISABLED);
  assert(scan.open(true, root, "/etc/*") == FileCsvScanStatus::ACCESS_DENIED);
  assert(scan.open(true, root, root + "/../*") == FileCsvScanStatus::ACCESS_DENIED);
  assert(scan.open(true, root, root + "/*.csv") == FileCsvScanStatus::OK);
  std::vector<FileCsvField> row;
  assert(scan.next(row) == FileCsvScanStatus::ROW && row.size() == 1 && row[0].value == root + "/basic.csv");
  assert(scan.next(row) == FileCsvScanStatus::END);
  assert(scan.rescan() == FileCsvScanStatus::OK);
  assert(scan.next(row) == FileCsvScanStatus::ROW);
  assert(scan.open(true, root, root + "/*.does-not-exist") == FileCsvScanStatus::OK);
  assert(scan.next(row) == FileCsvScanStatus::END);
  return 0;
}
