/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */

#pragma once

#include <rapidjson/document.h>
#include <istream>
#include <string>
#include <unordered_set>

namespace oceanbase
{
namespace sql
{

enum class FileJsonlReadStatus { ROW, END, INVALID, TOO_LARGE, IO_ERROR };

// The returned document remains valid until the next call to next().
class FileJsonlReader
{
public:
  FileJsonlReader(std::istream &input, size_t max_line_bytes)
      : input_(input), max_line_bytes_(max_line_bytes), line_number_(0)
  {}

  size_t line_number() const { return line_number_; }
  const rapidjson::Document &document() const { return document_; }

  FileJsonlReadStatus next()
  {
    if (max_line_bytes_ == 0) return FileJsonlReadStatus::INVALID;
    line_.clear();
    bool consumed = false;
    for (;;) {
      const int ch = input_.get();
      if (ch == std::char_traits<char>::eof()) {
        if (input_.bad() || (!input_.eof() && input_.fail())) return FileJsonlReadStatus::IO_ERROR;
        break;
      }
      consumed = true;
      if (ch == '\n') break;
      if (line_.size() >= max_line_bytes_) return FileJsonlReadStatus::TOO_LARGE;
      line_.push_back(static_cast<char>(ch));
    }
    if (!consumed && line_.empty()) return FileJsonlReadStatus::END;
    ++line_number_;
    if (!line_.empty() && line_.back() == '\r') line_.pop_back();
    document_.Parse<rapidjson::kParseValidateEncodingFlag>(line_.data(), line_.size());
    if (document_.HasParseError() || !document_.IsObject()) return FileJsonlReadStatus::INVALID;
    std::unordered_set<std::string> names;
    for (auto member = document_.MemberBegin(); member != document_.MemberEnd(); ++member) {
      if (!names.emplace(member->name.GetString(), member->name.GetStringLength()).second ||
          member->value.IsArray() || member->value.IsObject()) return FileJsonlReadStatus::INVALID;
    }
    return FileJsonlReadStatus::ROW;
  }

private:
  std::istream &input_;
  size_t max_line_bytes_;
  size_t line_number_;
  std::string line_;
  rapidjson::Document document_;
};

} // namespace sql
} // namespace oceanbase
