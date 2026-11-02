/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */
#ifdef NDEBUG
#undef NDEBUG
#endif
#include "sql/engine/basic/file_csv_output.h"
#include <cassert>
#include <fstream>
#include <iterator>

using namespace oceanbase::sql;

int main()
{
  char directory[] = "/tmp/seekdb-output-test-XXXXXX";
  assert(::mkdtemp(directory));
  const std::string root(directory);
  const std::string file = root + "/new.csv";
  const std::string link = root + "/escape";
  FileSecureOutput output;
  assert(!output.open(false, root, file));
  assert(!output.open(true, root, root + "/../outside.csv"));
  assert(::symlink("/etc", link.c_str()) == 0);
  assert(!output.open(true, root, link + "/seekdb-test.csv"));
  assert(output.open(true, root, file));
  FileCsvOutput csv(output);
  assert(csv.field("a,b", false, true, "NULL"));
  assert(csv.field("a\"b\n", false, false, "NULL"));
  assert(csv.field("", false, false, "NULL"));
  assert(csv.field("", true, false, "NULL"));
  assert(csv.field("NULL", false, false, "NULL"));
  assert(csv.end_row());
  output.close();
  std::ifstream input(file, std::ios::binary);
  const std::string contents((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
  assert(contents == "\"a,b\",\"a\"\"b\n\",\"\",NULL,\"NULL\"\n");
  input.close();
  assert(!output.open(true, root, file));
  assert(::unlink(file.c_str()) == 0);
  assert(::unlink(link.c_str()) == 0);
  assert(::rmdir(root.c_str()) == 0);
  return 0;
}
