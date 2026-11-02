/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */
#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>

namespace oceanbase
{
namespace sql
{

// Decode a raw Snappy block into caller-provided memory. No allocation is
// performed; both the compressed input and output sizes are checked.
class FileSnappy
{
public:
  static bool uncompress(const uint8_t *input, size_t input_size, uint8_t *output,
                         size_t output_capacity, size_t &output_size)
  {
    output_size = 0;
    if (input == nullptr || (output == nullptr && output_capacity != 0)) return false;
    size_t pos = 0;
    uint32_t expected = 0;
    bool finished = false;
    for (unsigned i = 0; i < 5; ++i) {
      if (pos >= input_size) return false;
      const uint8_t b = input[pos++];
      if (i == 4 && (b & 0xf0) != 0) return false;
      expected |= static_cast<uint32_t>(b & 0x7f) << (7 * i);
      if ((b & 0x80) == 0) { finished = true; break; }
    }
    if (!finished || expected > output_capacity || (expected != 0 && output == nullptr)) return false;
    while (pos < input_size && output_size < expected) {
      const uint8_t tag = input[pos++];
      const unsigned kind = tag & 3;
      size_t length = 0;
      size_t offset = 0;
      if (kind == 0) {
        const unsigned encoded = tag >> 2;
        if (encoded < 60) {
          length = encoded + 1;
        } else {
          const unsigned bytes = encoded - 59;
          if (bytes > input_size - pos) return false;
          uint32_t value = 0;
          for (unsigned i = 0; i < bytes; ++i) value |= static_cast<uint32_t>(input[pos++]) << (8 * i);
          length = static_cast<size_t>(value) + 1;
        }
        if (length > input_size - pos || length > expected - output_size) return false;
        std::memcpy(output + output_size, input + pos, length);
        pos += length;
        output_size += length;
      } else {
        if (kind == 1) {
          if (pos >= input_size) return false;
          length = 4 + ((tag >> 2) & 7);
          offset = (static_cast<size_t>(tag & 0xe0) << 3) | input[pos++];
        } else {
          const unsigned bytes = kind == 2 ? 2 : 4;
          if (bytes > input_size - pos) return false;
          length = 1 + (tag >> 2);
          for (unsigned i = 0; i < bytes; ++i) offset |= static_cast<size_t>(input[pos++]) << (8 * i);
        }
        if (offset == 0 || offset > output_size || length > expected - output_size) return false;
        for (size_t i = 0; i < length; ++i) {
          output[output_size] = output[output_size - offset];
          ++output_size;
        }
      }
    }
    return output_size == expected && pos == input_size;
  }
};

} // namespace sql
} // namespace oceanbase
