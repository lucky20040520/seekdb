/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */
#pragma once

#include "sql/engine/basic/file_secure_output.h"
#include <cerrno>
#include <string_view>

namespace oceanbase
{
namespace sql
{

// A streaming field writer; a single SQL row need not be buffered in memory.
class FileCsvOutput
{
public:
  explicit FileCsvOutput(FileSecureOutput &output) : output_(output) {}

  bool field(std::string_view value, bool is_null, bool first, std::string_view null_string)
  {
    if (!first && !write_all(",", 1)) return false;
    if (is_null) return write_all(null_string.data(), null_string.size());
    // Quote empty fields so they stay distinct from an unquoted NULL marker.
    const bool quote = value.empty() || value == null_string ||
        value.find_first_of(",\"\r\n") != std::string_view::npos;
    if (quote && !write_all("\"", 1)) return false;
    size_t start = 0;
    while (start < value.size()) {
      const size_t end = value.find('"', start);
      if (end == std::string_view::npos) {
        if (!write_all(value.data() + start, value.size() - start)) return false;
        break;
      }
      if (!write_all(value.data() + start, end - start) || !write_all("\"\"", 2)) return false;
      start = end + 1;
    }
    return !quote || write_all("\"", 1);
  }

  bool end_row() { return write_all("\n", 1); }

private:
  bool write_all(const char *data, size_t size)
  {
    while (size > 0) {
      const ssize_t written = ::write(output_.descriptor(), data, size);
      if (written < 0 && errno == EINTR) continue;
      if (written <= 0) return false;
      data += written;
      size -= static_cast<size_t>(written);
    }
    return true;
  }
  FileSecureOutput &output_;
};

} // namespace sql
} // namespace oceanbase
