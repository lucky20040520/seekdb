/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */

#ifdef NDEBUG
#undef NDEBUG
#endif
#include "sql/engine/basic/file_csv_scan.h"
#include <cassert>
#include <cstring>

using namespace oceanbase::sql;

int main()
{
  char temp[] = "/tmp/seekdb-file-scan-test-XXXXXX";
  const char *dir = ::mkdtemp(temp);
  assert(dir != nullptr);
  const std::string root(dir);
  const std::string path = root + "/data.csv";
  const char content[] = "id,name\n1,Alice\n2,Bob\n";
  const int fd = ::open(path.c_str(), O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC, 0600);
  assert(fd >= 0 && ::write(fd, content, sizeof(content) - 1) == sizeof(content) - 1 && ::close(fd) == 0);
  {
    FileCsvScan scan;
    std::vector<FileCsvField> row;
    assert(scan.open(false, root, path, true, 1024, 4) == FileCsvScanStatus::DISABLED);
    assert(scan.open(true, root, "/etc/passwd", true, 1024, 4) == FileCsvScanStatus::ACCESS_DENIED);
    assert(scan.open(true, root, path, true, 1024, 4) == FileCsvScanStatus::OK);
    assert(scan.columns().size() == 2 && scan.columns()[0].inference.type() == FileValueType::BIGINT);
    assert(scan.next(row) == FileCsvScanStatus::ROW && row[0].value == "1");
    assert(scan.next(row) == FileCsvScanStatus::ROW && row[0].value == "2");
    assert(scan.next(row) == FileCsvScanStatus::END);
    assert(scan.rescan() == FileCsvScanStatus::OK);
    assert(scan.next(row) == FileCsvScanStatus::ROW && row[1].value == "Alice");
    const int change = ::open(path.c_str(), O_WRONLY | O_APPEND | O_CLOEXEC);
    assert(change >= 0 && ::write(change, "3,C\n", 4) == 4 && ::close(change) == 0);
    assert(scan.next(row) == FileCsvScanStatus::CHANGED);
    assert(scan.rescan() == FileCsvScanStatus::CHANGED);
  }
  assert(::unlink(path.c_str()) == 0 && ::rmdir(root.c_str()) == 0);
  {
    char fixture_path[PATH_MAX];
    assert(::realpath("tools/deploy/mysql_test/test_suite/file_sql/data/basic.csv", fixture_path) != nullptr);
    FileCsvScan scan;
    std::vector<FileCsvField> row;
    assert(scan.open(true, "/", fixture_path, true, 4096, 16) == FileCsvScanStatus::OK);
    assert(scan.columns().size() == 8);
    assert(scan.next(row) == FileCsvScanStatus::ROW && row[1].value == "Alice");
  }
  return 0;
}
