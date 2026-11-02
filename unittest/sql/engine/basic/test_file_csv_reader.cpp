/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */

#ifdef NDEBUG
#undef NDEBUG
#endif
#include "sql/engine/basic/file_csv_reader.h"
#include <cassert>
#include <random>
#include <sstream>

using namespace oceanbase::sql;

int main()
{
  std::vector<FileCsvField> row;
  {
    std::istringstream input("name,note,empty\r\n中,\"line1\r\nline2 \"\"hi\"\"\",\r\nlast,\"\",x");
    FileCsvReader reader(input, 128, 3);
    assert(reader.next(row) == FileCsvStatus::ROW && row.size() == 3 && row[0].value == "name");
    assert(reader.next(row) == FileCsvStatus::ROW && reader.record_line() == 2 && row.size() == 3);
    assert(row[0].value == "中" && row[1].value == "line1\r\nline2 \"hi\"" && row[1].quoted);
    assert(row[2].value.empty() && !row[2].quoted);
    assert(reader.next(row) == FileCsvStatus::ROW && reader.record_line() == 4 && row[1].quoted && row[1].value.empty());
    assert(reader.next(row) == FileCsvStatus::END);
  }
  {
    std::istringstream input("\"unfinished");
    FileCsvReader reader(input, 100, 2);
    assert(reader.next(row) == FileCsvStatus::INVALID);
  }
  {
    std::istringstream input("\"quoted\"junk\n");
    FileCsvReader reader(input, 100, 2);
    assert(reader.next(row) == FileCsvStatus::INVALID);
  }
  {
    std::istringstream input("a,b,c\n");
    FileCsvReader reader(input, 100, 2);
    assert(reader.next(row) == FileCsvStatus::TOO_LARGE);
  }
  {
    std::istringstream input("12345\n");
    FileCsvReader reader(input, 5, 2);
    assert(reader.next(row) == FileCsvStatus::TOO_LARGE);
  }
  {
    std::istringstream input("a;b\n");
    FileCsvReader reader(input, 100, 2, ';');
    assert(reader.next(row) == FileCsvStatus::ROW && row.size() == 2 && row[1].value == "b");
  }
  {
    std::istringstream input(std::string("\xed\xa0\x80", 3) + "\n");
    FileCsvReader reader(input, 100, 2);
    assert(reader.next(row) == FileCsvStatus::INVALID);
  }
  {
    std::istringstream input(std::string("\xf0\x9f\x98\x80", 4) + "\n");
    FileCsvReader reader(input, 100, 2);
    assert(reader.next(row) == FileCsvStatus::ROW && row[0].value.size() == 4);
  }
  {
    std::istringstream input("\"a\\\"b\\\\c\"\n");
    FileCsvReader reader(input, 100, 2, ',', '"', '\\');
    assert(reader.next(row) == FileCsvStatus::ROW && row[0].value == "a\"b\\c");
  }
  {
    std::istringstream input("\"a\\q\"\n");
    FileCsvReader reader(input, 100, 2, ',', '"', '\\');
    assert(reader.next(row) == FileCsvStatus::INVALID);
  }
  {
    std::mt19937 random(2026);
    const std::string alphabet = "ab,\"\r\n";
    for (int iteration = 0; iteration < 1000; ++iteration) {
      std::vector<std::string> expected(1 + random() % 5);
      std::string csv;
      for (size_t i = 0; i < expected.size(); ++i) {
        if (i) csv += ',';
        csv += '"';
        const size_t length = random() % 20;
        for (size_t j = 0; j < length; ++j) {
          const char c = alphabet[random() % alphabet.size()];
          expected[i] += c;
          csv += c;
          if (c == '"') csv += c;
        }
        csv += '"';
      }
      csv += "\r\n";
      std::istringstream input(csv);
      FileCsvReader reader(input, 512, 5);
      assert(reader.next(row) == FileCsvStatus::ROW && row.size() == expected.size());
      for (size_t i = 0; i < expected.size(); ++i) assert(row[i].quoted && row[i].value == expected[i]);
      assert(reader.next(row) == FileCsvStatus::END);
    }
  }
  return 0;
}
