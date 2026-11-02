/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */

#pragma once

#include <charconv>
#include <cmath>
#include <cstdint>
#include <string_view>

namespace oceanbase
{
namespace sql
{

enum class FileValueType { NULL_VALUE, BOOLEAN, BIGINT, DOUBLE, DATE, DATETIME, VARCHAR };

// Per-column inference state. NULL is supplied explicitly so callers can
// distinguish null literals from quoted strings and JSON strings from scalars.
class FileTypeInference
{
public:
  void add_null() { nullable_ = true; }
  void add_type(FileValueType type) { type_ = widen(type_, type); }
  FileValueType type() const { return type_; }
  bool nullable() const { return nullable_; }

  static FileValueType infer_text(std::string_view value)
  {
    if (equals_ascii_ci(value, "true") || equals_ascii_ci(value, "false")) {
      return FileValueType::BOOLEAN;
    }
    if (is_date(value)) return FileValueType::DATE;
    if (is_datetime(value)) return FileValueType::DATETIME;
    if (!value.empty()) {
      int64_t integer = 0;
      const char *begin = value.data();
      const char *end = begin + value.size();
      if (*begin == '+') {
        ++begin;
        if (begin != end && (*begin == '+' || *begin == '-')) return FileValueType::VARCHAR;
      }
      if (begin == end) return FileValueType::VARCHAR;
      const auto parsed_int = std::from_chars(begin, end, integer);
      if (parsed_int.ec == std::errc() && parsed_int.ptr == end) return FileValueType::BIGINT;
      // Avoid losing precision by treating an out-of-range integer as text.
      if (is_integer_literal(value)) return FileValueType::VARCHAR;
      double number = 0;
      const auto parsed_double = std::from_chars(begin, end, number, std::chars_format::general);
      if (parsed_double.ec == std::errc() && parsed_double.ptr == end && std::isfinite(number)) {
        return FileValueType::DOUBLE;
      }
    }
    return FileValueType::VARCHAR;
  }

  static FileValueType widen(FileValueType a, FileValueType b)
  {
    if (a == FileValueType::NULL_VALUE) return b;
    if (b == FileValueType::NULL_VALUE || a == b) return a;
    if (numeric(a) && numeric(b)) {
      if (a == FileValueType::DOUBLE || b == FileValueType::DOUBLE) return FileValueType::DOUBLE;
      return FileValueType::BIGINT;
    }
    if ((a == FileValueType::DATE && b == FileValueType::DATETIME) ||
        (a == FileValueType::DATETIME && b == FileValueType::DATE)) return FileValueType::DATETIME;
    return FileValueType::VARCHAR;
  }

private:
  static bool equals_ascii_ci(std::string_view a, std::string_view b)
  {
    if (a.size() != b.size()) return false;
    for (size_t i = 0; i < a.size(); ++i) {
      char c = a[i];
      if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
      if (c != b[i]) return false;
    }
    return true;
  }

  static bool numeric(FileValueType value)
  {
    return value == FileValueType::BOOLEAN || value == FileValueType::BIGINT || value == FileValueType::DOUBLE;
  }
  static bool is_integer_literal(std::string_view value)
  {
    size_t i = value.size() && (value[0] == '-' || value[0] == '+') ? 1 : 0;
    if (i == value.size()) return false;
    for (; i < value.size(); ++i) if (value[i] < '0' || value[i] > '9') return false;
    return true;
  }
  static bool digits(std::string_view s, size_t at, size_t count, unsigned &value)
  {
    value = 0;
    if (at > s.size() || count > s.size() - at) return false;
    for (size_t i = at; i < at + count; ++i) {
      if (s[i] < '0' || s[i] > '9') return false;
      value = value * 10 + static_cast<unsigned>(s[i] - '0');
    }
    return true;
  }
  static bool is_date(std::string_view s)
  {
    unsigned year = 0, month = 0, day = 0;
    if (s.size() != 10 || s[4] != '-' || s[7] != '-' ||
        !digits(s, 0, 4, year) || !digits(s, 5, 2, month) || !digits(s, 8, 2, day) ||
        year == 0 || month == 0 || month > 12 || day == 0) return false;
    static constexpr unsigned days[] = {31,28,31,30,31,30,31,31,30,31,30,31};
    unsigned limit = days[month - 1];
    if (month == 2 && (year % 400 == 0 || (year % 4 == 0 && year % 100 != 0))) ++limit;
    return day <= limit;
  }
  static bool is_datetime(std::string_view s)
  {
    unsigned hour = 0, minute = 0, second = 0;
    if (s.size() < 19 || !is_date(s.substr(0, 10)) || (s[10] != ' ' && s[10] != 'T') ||
        s[13] != ':' || s[16] != ':' || !digits(s, 11, 2, hour) ||
        !digits(s, 14, 2, minute) || !digits(s, 17, 2, second) ||
        hour > 23 || minute > 59 || second > 59) return false;
    if (s.size() == 19) return true;
    if (s[19] != '.' || s.size() == 20 || s.size() > 26) return false;
    unsigned fraction = 0;
    return digits(s, 20, s.size() - 20, fraction);
  }

  FileValueType type_ = FileValueType::NULL_VALUE;
  bool nullable_ = false;
};

} // namespace sql
} // namespace oceanbase
