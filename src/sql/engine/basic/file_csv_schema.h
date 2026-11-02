/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */

#pragma once

#include "sql/engine/basic/file_csv_reader.h"
#include "sql/engine/basic/file_type_inference.h"

#include <string>
#include <unordered_set>
#include <vector>

namespace oceanbase
{
namespace sql
{

struct FileCsvColumn
{
  std::string name;
  FileTypeInference inference;
};

// Consumes one input stream, retaining only schema and the current record.
// The caller must reopen/revalidate the file before scanning its data.
class FileCsvSchema
{
public:
  static FileCsvStatus infer(FileCsvReader &reader, bool header,
                             std::vector<FileCsvColumn> &columns)
  {
    columns.clear();
    std::vector<FileCsvField> row;
    FileCsvStatus status = reader.next(row);
    if (status == FileCsvStatus::END) return FileCsvStatus::INVALID;
    if (status != FileCsvStatus::ROW) return status;
    std::unordered_set<std::string> used;
    for (size_t i = 0; i < row.size(); ++i) {
      const std::string base = header && !row[i].value.empty()
          ? row[i].value : "column" + std::to_string(i + 1);
      std::string name = base;
      for (size_t suffix = 2; used.count(name) != 0; ++suffix) {
        name = base + "__" + std::to_string(suffix);
      }
      used.insert(name);
      FileCsvColumn column;
      column.name = std::move(name);
      columns.push_back(std::move(column));
    }
    if (!header && !add_row(row, columns)) return FileCsvStatus::INVALID;
    while ((status = reader.next(row)) == FileCsvStatus::ROW) {
      if (!add_row(row, columns)) return FileCsvStatus::INVALID;
    }
    return status == FileCsvStatus::END ? FileCsvStatus::ROW : status;
  }

private:
  static bool add_row(const std::vector<FileCsvField> &row, std::vector<FileCsvColumn> &columns)
  {
    if (row.size() != columns.size()) return false;
    for (size_t i = 0; i < row.size(); ++i) {
      if (!row[i].quoted && row[i].value.empty()) {
        columns[i].inference.add_null();
      } else {
        columns[i].inference.add_type(FileTypeInference::infer_text(row[i].value));
      }
    }
    return true;
  }
};

} // namespace sql
} // namespace oceanbase
