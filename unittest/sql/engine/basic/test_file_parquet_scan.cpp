#include "sql/engine/basic/file_parquet_scan.h"
#include <cassert>
#include <string>
#include <vector>

using oceanbase::sql::FileCsvField;
using oceanbase::sql::FileCsvScanStatus;
using oceanbase::sql::FileParquetScan;

int main(int argc, char **argv)
{
  // The test driver creates simple.parquet from the checked-in base64 fixture
  // before invoking this binary.  Keeping the binary fixture out of Git lets
  // mysqltest copy the same source artifact into its isolated observer dir.
  assert(argc == 2);
  FileParquetScan scan;
  assert(scan.open(true, "/", argv[1]) == FileCsvScanStatus::OK);
  assert(scan.columns().size() == 1);
  assert(scan.columns()[0].name == "c1");
  std::vector<FileCsvField> row;
  for (int i = 0; i < 7; ++i) {
    assert(scan.next(row) == FileCsvScanStatus::ROW);
    assert(row.size() == 1 && row[0].value == std::to_string(i));
  }
  assert(scan.next(row) == FileCsvScanStatus::END);
  assert(scan.rescan() == FileCsvScanStatus::OK);
  assert(scan.next(row) == FileCsvScanStatus::ROW && row[0].value == "0");
  return 0;
}
