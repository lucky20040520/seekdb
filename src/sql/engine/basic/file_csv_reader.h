/*
 * Copyright (c) 2026 OceanBase.
 * Licensed under the Apache License, Version 2.0.
 */

#pragma once

#include <cstddef>
#include <istream>
#include <string>
#include <utility>
#include <vector>

namespace oceanbase
{
namespace sql
{

// A record is retained only until the caller requests the next record.
// File opening, path authorization and type conversion are
// intentionally the responsibility of the caller.
struct FileCsvField
{
  std::string value;
  bool quoted = false;
};

enum class FileCsvStatus { ROW, END, INVALID, TOO_LARGE, IO_ERROR };

class FileCsvReader
{
public:
  FileCsvReader(std::istream &input, size_t max_record_bytes, size_t max_columns,
                char delimiter = ',', char quote = '"', char escape = '"')
      : input_(input), max_record_bytes_(max_record_bytes), max_columns_(max_columns),
        delimiter_(delimiter), quote_(quote), escape_(escape), line_(1), record_line_(1)
  {}

  size_t line() const { return line_; }
  size_t record_line() const { return record_line_; }

  FileCsvStatus next(std::vector<FileCsvField> &row)
  {
    row.clear();
    record_line_ = line_;
    FileCsvField field;
    enum class State { START, UNQUOTED, QUOTED, AFTER_QUOTE };
    State state = State::START;
    size_t bytes = 0;
    bool seen = false;
    if (max_columns_ == 0 || max_record_bytes_ == 0 || delimiter_ == quote_ ||
        delimiter_ == '\r' || delimiter_ == '\n' || quote_ == '\r' || quote_ == '\n' ||
        escape_ == '\r' || escape_ == '\n') {
      return FileCsvStatus::INVALID;
    }
    for (;;) {
      const int next_char = input_.get();
      if (next_char == std::char_traits<char>::eof()) {
        if (input_.bad() || (!input_.eof() && input_.fail())) return FileCsvStatus::IO_ERROR;
        if (state == State::QUOTED) return FileCsvStatus::INVALID;
        if (!seen) return FileCsvStatus::END;
        return add_field(row, field);
      }
      seen = true;
      if (++bytes > max_record_bytes_) return FileCsvStatus::TOO_LARGE;
      const char c = static_cast<char>(next_char);
      if (state == State::QUOTED) {
        if (escape_ != quote_ && c == escape_) {
          const int escaped = input_.get();
          if (escaped == std::char_traits<char>::eof()) {
            return input_.bad() || !input_.eof() ? FileCsvStatus::IO_ERROR : FileCsvStatus::INVALID;
          }
          if (++bytes > max_record_bytes_) return FileCsvStatus::TOO_LARGE;
          if (escaped != quote_ && escaped != escape_) return FileCsvStatus::INVALID;
          field.value.push_back(static_cast<char>(escaped));
        } else if (c == quote_) {
          state = State::AFTER_QUOTE;
        } else {
          field.value.push_back(c);
          if (c == '\n') ++line_;
        }
      } else if (state == State::AFTER_QUOTE && c == quote_ && escape_ == quote_) {
        field.value.push_back(c);
        state = State::QUOTED;
      } else if (c == delimiter_) {
        const FileCsvStatus result = add_field(row, field);
        if (result != FileCsvStatus::ROW) return result;
        field = FileCsvField();
        state = State::START;
      } else if (c == '\r' || c == '\n') {
        if (c == '\r' && input_.peek() == '\n') {
          input_.get();
          if (++bytes > max_record_bytes_) return FileCsvStatus::TOO_LARGE;
        }
        ++line_;
        return add_field(row, field);
      } else if (state == State::AFTER_QUOTE) {
        return FileCsvStatus::INVALID;
      } else if (c == quote_) {
        if (state != State::START) return FileCsvStatus::INVALID;
        field.quoted = true;
        state = State::QUOTED;
      } else {
        field.value.push_back(c);
        state = State::UNQUOTED;
      }
    }
  }

private:
  FileCsvStatus add_field(std::vector<FileCsvField> &row, FileCsvField &field)
  {
    if (row.size() >= max_columns_) return FileCsvStatus::TOO_LARGE;
    if (!valid_utf8(field.value)) return FileCsvStatus::INVALID;
    row.push_back(std::move(field));
    return FileCsvStatus::ROW;
  }

  static bool valid_utf8(const std::string &text)
  {
    size_t i = 0;
    while (i < text.size()) {
      const unsigned char first = static_cast<unsigned char>(text[i++]);
      if (first < 0x80) continue;
      unsigned count = 0;
      unsigned code = 0;
      unsigned minimum = 0;
      if (first >= 0xc2 && first <= 0xdf) {
        count = 1; code = first & 0x1f; minimum = 0x80;
      } else if (first >= 0xe0 && first <= 0xef) {
        count = 2; code = first & 0x0f; minimum = 0x800;
      } else if (first >= 0xf0 && first <= 0xf4) {
        count = 3; code = first & 0x07; minimum = 0x10000;
      } else {
        return false;
      }
      if (count > text.size() - i) return false;
      for (unsigned j = 0; j < count; ++j) {
        const unsigned char next = static_cast<unsigned char>(text[i++]);
        if ((next & 0xc0) != 0x80) return false;
        code = (code << 6) | (next & 0x3f);
      }
      if (code < minimum || code > 0x10ffff || (code >= 0xd800 && code <= 0xdfff)) return false;
    }
    return true;
  }

  std::istream &input_;
  size_t max_record_bytes_;
  size_t max_columns_;
  char delimiter_;
  char quote_;
  char escape_;
  size_t line_;
  size_t record_line_;
};

} // namespace sql
} // namespace oceanbase
