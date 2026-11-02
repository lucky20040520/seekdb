/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */
#pragma once

#include "sql/engine/basic/file_csv_scan.h"
#include "sql/engine/basic/file_jsonl_schema.h"
#include <rapidjson/stringbuffer.h>
#include <rapidjson/writer.h>

namespace oceanbase
{
namespace sql
{

class FileJsonlScan
{
public:
  FileJsonlScan() = default;
  ~FileJsonlScan() { close_stream(); }
  FileJsonlScan(const FileJsonlScan &) = delete;
  FileJsonlScan &operator=(const FileJsonlScan &) = delete;

  FileCsvScanStatus open(bool enabled, const std::string &secure_dir, const std::string &path,
                         size_t max_line_bytes, size_t max_columns)
  {
    close_stream();
    handle_.close();
    columns_.clear();
    if (!enabled) return FileCsvScanStatus::DISABLED;
    if (!handle_.open(secure_dir, path)) return FileCsvScanStatus::ACCESS_DENIED;
    max_line_bytes_ = max_line_bytes;
    if (!start_stream()) return FileCsvScanStatus::IO_ERROR;
    std::vector<FileJsonlColumn> inferred;
    size_t error_line = 0;
    const FileJsonlStatus status = FileJsonlSchema::infer(*stream_, max_line_bytes, max_columns, inferred, error_line);
    close_stream();
    if (!handle_.unchanged()) return FileCsvScanStatus::CHANGED;
    if (status != FileJsonlStatus::OK) return FileCsvScanStatus::INVALID;
    for (const auto &column : inferred) {
      FileCsvColumn result;
      result.name = column.name;
      result.inference = column.inference;
      columns_.push_back(std::move(result));
    }
    return rescan();
  }

  FileCsvScanStatus rescan()
  {
    close_stream();
    if (!handle_.unchanged()) return FileCsvScanStatus::CHANGED;
    if (::lseek(handle_.descriptor(), 0, SEEK_SET) < 0 || !start_stream()) return FileCsvScanStatus::IO_ERROR;
    reader_.reset(new FileJsonlReader(*stream_, max_line_bytes_));
    return FileCsvScanStatus::OK;
  }

  FileCsvScanStatus next(std::vector<FileCsvField> &row)
  {
    if (!reader_) return FileCsvScanStatus::INVALID;
    if (!handle_.unchanged()) return FileCsvScanStatus::CHANGED;
    const auto status = reader_->next();
    if (!handle_.unchanged()) return FileCsvScanStatus::CHANGED;
    if (status == FileJsonlReadStatus::END) return FileCsvScanStatus::END;
    if (status != FileJsonlReadStatus::ROW) return FileCsvScanStatus::INVALID;
    row.clear();
    const auto &doc = reader_->document();
    for (const auto &column : columns_) {
      FileCsvField field;
      const auto member = doc.FindMember(rapidjson::StringRef(column.name.data(), column.name.size()));
      if (member != doc.MemberEnd() && !member->value.IsNull()) {
        const auto &value = member->value;
        if (value.IsString()) {
          field.value.assign(value.GetString(), value.GetStringLength());
          field.quoted = true;
        } else {
          rapidjson::StringBuffer buffer;
          rapidjson::Writer<rapidjson::StringBuffer> writer(buffer);
          if (!value.Accept(writer)) return FileCsvScanStatus::INVALID;
          field.value.assign(buffer.GetString(), buffer.GetSize());
        }
      }
      row.push_back(std::move(field));
    }
    return FileCsvScanStatus::ROW;
  }

  const std::vector<FileCsvColumn> &columns() const { return columns_; }

private:
  bool start_stream()
  {
    const int copy = ::fcntl(handle_.descriptor(), F_DUPFD_CLOEXEC, 0);
    if (copy < 0) return false;
    buffer_.reset(new __gnu_cxx::stdio_filebuf<char>(copy, std::ios::in));
    if (!buffer_->is_open()) { ::close(copy); buffer_.reset(); return false; }
    stream_.reset(new std::istream(buffer_.get()));
    return true;
  }
  void close_stream() { reader_.reset(); stream_.reset(); buffer_.reset(); }

  FileSecureHandle handle_;
  std::unique_ptr<__gnu_cxx::stdio_filebuf<char>> buffer_;
  std::unique_ptr<std::istream> stream_;
  std::unique_ptr<FileJsonlReader> reader_;
  std::vector<FileCsvColumn> columns_;
  size_t max_line_bytes_ = 0;
};

} // namespace sql
} // namespace oceanbase
