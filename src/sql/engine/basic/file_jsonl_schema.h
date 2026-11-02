/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */

#pragma once

#include "sql/engine/basic/file_jsonl_reader.h"
#include "sql/engine/basic/file_type_inference.h"
#include <cstdint>
#include <istream>
#include <limits>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace oceanbase
{
namespace sql
{

struct FileJsonlColumn
{
  std::string name;
  FileTypeInference inference;
};

enum class FileJsonlStatus { OK, INVALID, TOO_LARGE, IO_ERROR };

// Full-file inference keeps only one input line and column metadata in memory.
class FileJsonlSchema
{
public:
  static FileJsonlStatus infer(std::istream &input, size_t max_line_bytes, size_t max_columns,
                               std::vector<FileJsonlColumn> &columns, size_t &error_line)
  {
    columns.clear();
    error_line = 1;
    if (max_line_bytes == 0 || max_columns == 0) return FileJsonlStatus::INVALID;
    std::unordered_map<std::string, size_t> indices;
    size_t records = 0;
    FileJsonlReader reader(input, max_line_bytes);
    for (;;) {
      const auto read_status = reader.next();
      if (read_status == FileJsonlReadStatus::END) return records == 0 ? FileJsonlStatus::INVALID : FileJsonlStatus::OK;
      if (read_status == FileJsonlReadStatus::IO_ERROR) return FileJsonlStatus::IO_ERROR;
      if (read_status == FileJsonlReadStatus::TOO_LARGE) return FileJsonlStatus::TOO_LARGE;
      if (read_status != FileJsonlReadStatus::ROW) return FileJsonlStatus::INVALID;
      const auto &document = reader.document();
      std::unordered_set<std::string> seen;
      for (auto member = document.MemberBegin(); member != document.MemberEnd(); ++member) {
        std::string name(member->name.GetString(), member->name.GetStringLength());
        if (!seen.insert(name).second) return FileJsonlStatus::INVALID;
        const auto &value = member->value;
        if (value.IsObject() || value.IsArray()) return FileJsonlStatus::INVALID;
        auto found = indices.find(name);
        if (found == indices.end()) {
          if (columns.size() >= max_columns) return FileJsonlStatus::TOO_LARGE;
          FileJsonlColumn column;
          column.name = name;
          if (records != 0) column.inference.add_null();
          const size_t index = columns.size();
          columns.push_back(std::move(column));
          found = indices.emplace(std::move(name), index).first;
        }
        FileTypeInference &inference = columns[found->second].inference;
        if (value.IsNull()) {
          inference.add_null();
        } else if (value.IsBool()) {
          inference.add_type(FileValueType::BOOLEAN);
        } else if (value.IsInt64()) {
          inference.add_type(FileValueType::BIGINT);
        } else if (value.IsUint64()) {
          inference.add_type(value.GetUint64() <= static_cast<uint64_t>(std::numeric_limits<int64_t>::max())
              ? FileValueType::BIGINT : FileValueType::VARCHAR);
        } else if (value.IsNumber()) {
          inference.add_type(FileValueType::DOUBLE);
        } else if (value.IsString()) {
          const auto inferred = FileTypeInference::infer_text(
              std::string_view(value.GetString(), value.GetStringLength()));
          inference.add_type(inferred == FileValueType::DATE || inferred == FileValueType::DATETIME
              ? inferred : FileValueType::VARCHAR);
        } else {
          return FileJsonlStatus::INVALID;
        }
      }
      for (auto &column : columns) {
        if (seen.count(column.name) == 0) column.inference.add_null();
      }
      ++records;
      ++error_line;
    }
  }
};

} // namespace sql
} // namespace oceanbase
