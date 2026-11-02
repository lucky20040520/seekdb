/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */
#ifdef NDEBUG
#undef NDEBUG
#endif
#include "sql/engine/basic/file_snappy.h"
#include <cassert>
#include <cstring>

using namespace oceanbase::sql;

int main()
{
  uint8_t output[32] = {};
  size_t size = 0;
  const uint8_t empty[] = {0};
  assert(FileSnappy::uncompress(empty, sizeof(empty), nullptr, 0, size) && size == 0);
  const uint8_t literal[] = {5, 16, 'h', 'e', 'l', 'l', 'o'};
  assert(FileSnappy::uncompress(literal, sizeof(literal), output, sizeof(output), size));
  assert(size == 5 && std::memcmp(output, "hello", 5) == 0);
  const uint8_t copy2[] = {10, 16, 'h', 'e', 'l', 'l', 'o', 18, 5, 0};
  assert(FileSnappy::uncompress(copy2, sizeof(copy2), output, sizeof(output), size));
  assert(size == 10 && std::memcmp(output, "hellohello", 10) == 0);
  const uint8_t overlap[] = {8, 0, 'x', 13, 1};
  assert(FileSnappy::uncompress(overlap, sizeof(overlap), output, sizeof(output), size));
  assert(size == 8 && std::memcmp(output, "xxxxxxxx", 8) == 0);
  const uint8_t copy4[] = {6, 0, 'z', 19, 1, 0, 0, 0};
  assert(FileSnappy::uncompress(copy4, sizeof(copy4), output, sizeof(output), size));
  assert(size == 6 && std::memcmp(output, "zzzzzz", 6) == 0);
  const uint8_t extended_literal[] = {3, 240, 2, 'a', 'b', 'c'};
  assert(FileSnappy::uncompress(extended_literal, sizeof(extended_literal), output, sizeof(output), size));
  assert(size == 3 && std::memcmp(output, "abc", 3) == 0);
  const uint8_t invalid_offset[] = {1, 2, 0, 0};
  assert(!FileSnappy::uncompress(invalid_offset, sizeof(invalid_offset), output, sizeof(output), size));
  assert(!FileSnappy::uncompress(literal, sizeof(literal) - 1, output, sizeof(output), size));
  assert(!FileSnappy::uncompress(literal, sizeof(literal), output, 4, size));
  const uint8_t overflow[] = {0xff, 0xff, 0xff, 0xff, 0x10};
  assert(!FileSnappy::uncompress(overflow, sizeof(overflow), output, sizeof(output), size));
  uint32_t random = 1234567;
  for (int i = 0; i < 10000; ++i) {
    uint8_t bytes[32];
    for (uint8_t &byte : bytes) {
      random = random * 1664525u + 1013904223u;
      byte = static_cast<uint8_t>(random >> 24);
    }
    FileSnappy::uncompress(bytes, static_cast<size_t>(i % 33), output, sizeof(output), size);
  }
  return 0;
}
