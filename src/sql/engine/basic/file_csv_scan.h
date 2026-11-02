/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */

#pragma once

#include "sql/engine/basic/file_csv_schema.h"
#include "sql/engine/basic/file_secure_handle.h"
#include <ext/stdio_filebuf.h>
#include <fcntl.h>
#include <istream>
#include <memory>

namespace oceanbase
{
namespace sql
{

enum class FileCsvScanStatus { OK, ROW, END, DISABLED, ACCESS_DENIED, CHANGED, INVALID, IO_ERROR };

// Scanner groundwork: binds the secure descriptor, full-file schema pass,
// and streaming row pass. SQL session/plan integration remains in the caller.
class FileCsvScan
{
public:
  FileCsvScan() = default;
  ~FileCsvScan() { close_stream(); }
  FileCsvScan(const FileCsvScan &) = delete;
  FileCsvScan &operator=(const FileCsvScan &) = delete;

  FileCsvScanStatus open(bool enabled, const std::string &secure_dir, const std::string &path,
                         bool header, size_t max_record_bytes, size_t max_columns)
  {
    close_stream();
    handle_.close();
    columns_.clear();
    if (!enabled) return FileCsvScanStatus::DISABLED;
    if (!handle_.open(secure_dir, path)) return FileCsvScanStatus::ACCESS_DENIED;
    header_ = header;
    max_record_bytes_ = max_record_bytes;
    max_columns_ = max_columns;
    if (!start_stream()) return FileCsvScanStatus::IO_ERROR;
    const FileCsvStatus status = FileCsvSchema::infer(*reader_, header_, columns_);
    close_stream();
    if (!handle_.unchanged()) return FileCsvScanStatus::CHANGED;
    if (status != FileCsvStatus::ROW) return map_error(status);
    return rescan();
  }

  FileCsvScanStatus rescan()
  {
    close_stream();
    if (!handle_.unchanged()) return FileCsvScanStatus::CHANGED;
    if (::lseek(handle_.descriptor(), 0, SEEK_SET) < 0 || !start_stream()) return FileCsvScanStatus::IO_ERROR;
    if (header_) {
      std::vector<FileCsvField> ignored;
      const auto status = reader_->next(ignored);
      if (status != FileCsvStatus::ROW) return map_error(status);
    }
    return FileCsvScanStatus::OK;
  }

  FileCsvScanStatus next(std::vector<FileCsvField> &row)
  {
    if (!reader_) return FileCsvScanStatus::INVALID;
    if (!handle_.unchanged()) return FileCsvScanStatus::CHANGED;
    const auto status = reader_->next(row);
    if (!handle_.unchanged()) return FileCsvScanStatus::CHANGED;
    if (status == FileCsvStatus::END) return FileCsvScanStatus::END;
    if (status != FileCsvStatus::ROW) return map_error(status);
    return row.size() == columns_.size() ? FileCsvScanStatus::ROW : FileCsvScanStatus::INVALID;
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
    reader_.reset(new FileCsvReader(*stream_, max_record_bytes_, max_columns_));
    return true;
  }

  void close_stream()
  {
    reader_.reset();
    stream_.reset();
    buffer_.reset();
  }

  static FileCsvScanStatus map_error(FileCsvStatus status)
  {
    return status == FileCsvStatus::IO_ERROR ? FileCsvScanStatus::IO_ERROR : FileCsvScanStatus::INVALID;
  }

  FileSecureHandle handle_;
  std::unique_ptr<__gnu_cxx::stdio_filebuf<char>> buffer_;
  std::unique_ptr<std::istream> stream_;
  std::unique_ptr<FileCsvReader> reader_;
  std::vector<FileCsvColumn> columns_;
  size_t max_record_bytes_ = 0;
  size_t max_columns_ = 0;
  bool header_ = true;
};

} // namespace sql
} // namespace oceanbase
