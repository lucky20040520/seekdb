/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */

#pragma once

// A deliberately small, bounded Parquet reader for file SQL.  It accepts the
// flat scalar subset emitted by Arrow/DuckDB: PLAIN and dictionary encoded
// BOOLEAN/INT32/INT64/FLOAT/DOUBLE/BYTE_ARRAY columns, with optional values
// and UNCOMPRESSED or SNAPPY pages.  Nested schemas and unsupported encodings
// are rejected before exposing any rows.

#include "sql/engine/basic/file_csv_scan.h"
#include "sql/engine/basic/file_snappy.h"
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <limits>
#include <string>
#include <utility>
#include <vector>
#include <sys/stat.h>
#include <unistd.h>

namespace oceanbase
{
namespace sql
{

namespace file_parquet_detail
{

enum CompactType : uint8_t { STOP = 0, TRUE_VALUE = 1, FALSE_VALUE = 2, BYTE = 3, I16 = 4,
  I32 = 5, I64 = 6, DOUBLE = 7, BINARY = 8, LIST = 9, SET = 10, MAP = 11, STRUCT = 12 };

class CompactReader
{
public:
  CompactReader(const uint8_t *data, size_t size) : data_(data), size_(size) {}
  size_t position() const { return pos_; }
  bool read_field(int16_t &last, int16_t &id, uint8_t &type)
  {
    if (pos_ >= size_) return false;
    const uint8_t header = data_[pos_++];
    type = header & 15;
    if (type == STOP) { id = 0; return true; }
    const uint8_t delta = header >> 4;
    if (delta != 0) id = static_cast<int16_t>(last + delta);
    else if (!read_i16(id)) return false;
    last = id;
    return true;
  }
  bool read_varint(uint64_t &value)
  {
    value = 0;
    for (unsigned shift = 0; shift < 64; shift += 7) {
      if (pos_ >= size_) return false;
      const uint8_t byte = data_[pos_++];
      if (shift == 63 && (byte & 0xfe) != 0) return false;
      value |= static_cast<uint64_t>(byte & 0x7f) << shift;
      if ((byte & 0x80) == 0) return true;
    }
    return false;
  }
  bool read_i16(int16_t &value) { int64_t v = 0; return read_zigzag(v) && v >= INT16_MIN && v <= INT16_MAX && (value = static_cast<int16_t>(v), true); }
  bool read_i32(int32_t &value) { int64_t v = 0; return read_zigzag(v) && v >= INT32_MIN && v <= INT32_MAX && (value = static_cast<int32_t>(v), true); }
  bool read_i64(int64_t &value) { return read_zigzag(value); }
  bool read_binary(std::string &value)
  {
    uint64_t length = 0;
    if (!read_varint(length) || length > size_ - pos_) return false;
    value.assign(reinterpret_cast<const char *>(data_ + pos_), static_cast<size_t>(length));
    pos_ += static_cast<size_t>(length);
    return true;
  }
  bool read_list(uint8_t &element_type, uint32_t &count)
  {
    if (pos_ >= size_) return false;
    const uint8_t header = data_[pos_++];
    element_type = header & 15;
    uint64_t value = header >> 4;
    if (value == 15 && !read_varint(value)) return false;
    if (value > UINT32_MAX) return false;
    count = static_cast<uint32_t>(value);
    return true;
  }
  bool skip(uint8_t type)
  {
    if (type == TRUE_VALUE || type == FALSE_VALUE || type == STOP) return true;
    if (type == BYTE) return pos_ < size_ && (++pos_, true);
    if (type == I16 || type == I32 || type == I64) { uint64_t ignored = 0; return read_varint(ignored); }
    if (type == DOUBLE) return size_ - pos_ >= 8 && (pos_ += 8, true);
    if (type == BINARY) { std::string ignored; return read_binary(ignored); }
    if (type == LIST || type == SET) {
      uint8_t elem = 0; uint32_t count = 0;
      if (!read_list(elem, count)) return false;
      for (uint32_t i = 0; i < count; ++i) if (!skip(elem)) return false;
      return true;
    }
    if (type == STRUCT) {
      int16_t last = 0, id = 0; uint8_t child = STOP;
      while (read_field(last, id, child) && child != STOP) if (!skip(child)) return false;
      return child == STOP;
    }
    return false;
  }
private:
  bool read_zigzag(int64_t &value)
  {
    uint64_t raw = 0;
    if (!read_varint(raw)) return false;
    value = static_cast<int64_t>((raw >> 1) ^ (~(raw & 1) + 1));
    return true;
  }
  const uint8_t *data_;
  size_t size_;
  size_t pos_ = 0;
};

inline uint32_t load_u32(const uint8_t *p)
{
  return static_cast<uint32_t>(p[0]) | (static_cast<uint32_t>(p[1]) << 8) |
         (static_cast<uint32_t>(p[2]) << 16) | (static_cast<uint32_t>(p[3]) << 24);
}
inline uint64_t load_u64(const uint8_t *p)
{
  uint64_t value = 0;
  for (unsigned i = 0; i < 8; ++i) value |= static_cast<uint64_t>(p[i]) << (8 * i);
  return value;
}

} // namespace file_parquet_detail

class FileParquetScan
{
public:
  FileParquetScan() = default;
  FileParquetScan(const FileParquetScan &) = delete;
  FileParquetScan &operator=(const FileParquetScan &) = delete;

  FileCsvScanStatus open(bool enabled, const std::string &secure_dir, const std::string &path,
                         size_t max_file_bytes = 64 * 1024 * 1024, size_t max_columns = 4096)
  {
    clear();
    if (!enabled) return FileCsvScanStatus::DISABLED;
    if (!handle_.open(secure_dir, path)) return FileCsvScanStatus::ACCESS_DENIED;
    struct stat st = {};
    if (::fstat(handle_.descriptor(), &st) != 0 || st.st_size < 12 ||
        static_cast<uintmax_t>(st.st_size) > max_file_bytes) return FileCsvScanStatus::INVALID;
    bytes_.resize(static_cast<size_t>(st.st_size));
    size_t offset = 0;
    while (offset < bytes_.size()) {
      const ssize_t read = ::pread(handle_.descriptor(), bytes_.data() + offset, bytes_.size() - offset, offset);
      if (read <= 0) return FileCsvScanStatus::IO_ERROR;
      offset += static_cast<size_t>(read);
    }
    if (!handle_.unchanged()) return FileCsvScanStatus::CHANGED;
    if (!parse(max_columns)) return FileCsvScanStatus::INVALID;
    return FileCsvScanStatus::OK;
  }

  FileCsvScanStatus rescan()
  {
    if (!handle_.unchanged()) return FileCsvScanStatus::CHANGED;
    row_index_ = 0;
    return FileCsvScanStatus::OK;
  }

  FileCsvScanStatus next(std::vector<FileCsvField> &row)
  {
    if (!handle_.unchanged()) return FileCsvScanStatus::CHANGED;
    if (row_index_ >= row_count_) return FileCsvScanStatus::END;
    row.clear();
    for (const auto &column : values_) row.push_back(column[row_index_]);
    ++row_index_;
    return FileCsvScanStatus::ROW;
  }

  const std::vector<FileCsvColumn> &columns() const { return columns_; }

private:
  struct SchemaColumn { std::string name; int32_t type = -1; int32_t repetition = 0; int32_t converted = -1; };
  struct ColumnChunk { int32_t type = -1; int32_t codec = -1; int64_t value_count = -1; int64_t data_offset = -1;
    int64_t dictionary_offset = -1; int64_t compressed_size = -1; std::string name; };
  struct PageHeader { int32_t type = -1; int32_t uncompressed_size = -1; int32_t compressed_size = -1;
    int32_t value_count = -1; int32_t encoding = -1; int32_t null_count = 0;
    int32_t definition_levels_size = 0; int32_t repetition_levels_size = 0; bool values_compressed = true; };

  void clear()
  {
    handle_.close(); bytes_.clear(); columns_.clear(); values_.clear(); row_count_ = 0; row_index_ = 0;
  }

  static bool parse_schema(file_parquet_detail::CompactReader &reader, SchemaColumn &schema, bool &is_leaf)
  {
    int16_t last = 0, id = 0; uint8_t type = 0; is_leaf = true;
    while (reader.read_field(last, id, type) && type != file_parquet_detail::STOP) {
      int32_t value = 0;
      if (id == 1 && type == file_parquet_detail::I32) { if (!reader.read_i32(schema.type)) return false; }
      else if (id == 3 && type == file_parquet_detail::I32) { if (!reader.read_i32(schema.repetition)) return false; }
      else if (id == 4 && type == file_parquet_detail::BINARY) { if (!reader.read_binary(schema.name)) return false; }
      else if (id == 5 && type == file_parquet_detail::I32) { if (!reader.read_i32(value) || value < 0) return false; is_leaf = value == 0; }
      else if (id == 6 && type == file_parquet_detail::I32) { if (!reader.read_i32(schema.converted)) return false; }
      else if (!reader.skip(type)) return false;
    }
    return type == file_parquet_detail::STOP && !schema.name.empty();
  }

  static bool parse_column_meta(file_parquet_detail::CompactReader &reader, ColumnChunk &chunk)
  {
    int16_t last = 0, id = 0; uint8_t type = 0;
    while (reader.read_field(last, id, type) && type != file_parquet_detail::STOP) {
      if (id == 1 && type == file_parquet_detail::I32) { if (!reader.read_i32(chunk.type)) return false; }
      else if (id == 3 && type == file_parquet_detail::LIST) {
        uint8_t elem = 0; uint32_t count = 0;
        if (!reader.read_list(elem, count) || elem != file_parquet_detail::BINARY || count == 0) return false;
        std::string part;
        for (uint32_t i = 0; i < count; ++i) if (!reader.read_binary(part)) return false;
        chunk.name = part;
      } else if (id == 4 && type == file_parquet_detail::I32) { if (!reader.read_i32(chunk.codec)) return false; }
      else if (id == 5 && type == file_parquet_detail::I64) { if (!reader.read_i64(chunk.value_count)) return false; }
      else if (id == 7 && type == file_parquet_detail::I64) { if (!reader.read_i64(chunk.compressed_size)) return false; }
      else if (id == 9 && type == file_parquet_detail::I64) { if (!reader.read_i64(chunk.data_offset)) return false; }
      else if (id == 11 && type == file_parquet_detail::I64) { if (!reader.read_i64(chunk.dictionary_offset)) return false; }
      else if (!reader.skip(type)) return false;
    }
    return type == file_parquet_detail::STOP && chunk.type >= 0 && chunk.codec >= 0 && chunk.value_count >= 0 &&
           chunk.data_offset >= 0 && chunk.compressed_size >= 0 && !chunk.name.empty();
  }

  static bool parse_column_chunk(file_parquet_detail::CompactReader &reader, ColumnChunk &chunk)
  {
    int16_t last = 0, id = 0; uint8_t type = 0;
    while (reader.read_field(last, id, type) && type != file_parquet_detail::STOP) {
      if (id == 3 && type == file_parquet_detail::STRUCT) { if (!parse_column_meta(reader, chunk)) return false; }
      else if (!reader.skip(type)) return false;
    }
    return type == file_parquet_detail::STOP;
  }

  static bool parse_row_group(file_parquet_detail::CompactReader &reader, std::vector<ColumnChunk> &chunks, int64_t &rows)
  {
    int16_t last = 0, id = 0; uint8_t type = 0; rows = -1;
    while (reader.read_field(last, id, type) && type != file_parquet_detail::STOP) {
      if (id == 1 && type == file_parquet_detail::LIST) {
        uint8_t elem = 0; uint32_t count = 0;
        if (!reader.read_list(elem, count) || elem != file_parquet_detail::STRUCT || count == 0) return false;
        for (uint32_t i = 0; i < count; ++i) { ColumnChunk chunk; if (!parse_column_chunk(reader, chunk)) return false; chunks.push_back(std::move(chunk)); }
      } else if (id == 3 && type == file_parquet_detail::I64) { if (!reader.read_i64(rows) || rows < 0) return false; }
      else if (!reader.skip(type)) return false;
    }
    return type == file_parquet_detail::STOP && rows >= 0;
  }

  static bool parse_page_header(file_parquet_detail::CompactReader &reader, PageHeader &page)
  {
    int16_t last = 0, id = 0; uint8_t type = 0;
    while (reader.read_field(last, id, type) && type != file_parquet_detail::STOP) {
      if (id == 1 && type == file_parquet_detail::I32) { if (!reader.read_i32(page.type)) return false; }
      else if (id == 2 && type == file_parquet_detail::I32) { if (!reader.read_i32(page.uncompressed_size)) return false; }
      else if (id == 3 && type == file_parquet_detail::I32) { if (!reader.read_i32(page.compressed_size)) return false; }
      else if (id == 5 && type == file_parquet_detail::STRUCT) {
        int16_t nested_last = 0, nested_id = 0; uint8_t nested_type = 0;
        while (reader.read_field(nested_last, nested_id, nested_type) && nested_type != file_parquet_detail::STOP) {
          if (nested_id == 1 && nested_type == file_parquet_detail::I32) { if (!reader.read_i32(page.value_count)) return false; }
          else if (nested_id == 2 && nested_type == file_parquet_detail::I32) { if (!reader.read_i32(page.encoding)) return false; }
          else if (!reader.skip(nested_type)) return false;
        }
        if (nested_type != file_parquet_detail::STOP) return false;
      } else if (id == 7 && type == file_parquet_detail::STRUCT) {
        int16_t nested_last = 0, nested_id = 0; uint8_t nested_type = 0;
        while (reader.read_field(nested_last, nested_id, nested_type) && nested_type != file_parquet_detail::STOP) {
          if (nested_id == 1 && nested_type == file_parquet_detail::I32) { if (!reader.read_i32(page.value_count)) return false; }
          else if (!reader.skip(nested_type)) return false;
        }
        if (nested_type != file_parquet_detail::STOP) return false;
      } else if (id == 8 && type == file_parquet_detail::STRUCT) {
        int16_t nested_last = 0, nested_id = 0; uint8_t nested_type = 0;
        while (reader.read_field(nested_last, nested_id, nested_type) && nested_type != file_parquet_detail::STOP) {
          if (nested_id == 1 && nested_type == file_parquet_detail::I32) { if (!reader.read_i32(page.value_count)) return false; }
          else if (nested_id == 2 && nested_type == file_parquet_detail::I32) { if (!reader.read_i32(page.null_count)) return false; }
          else if (nested_id == 4 && nested_type == file_parquet_detail::I32) { if (!reader.read_i32(page.encoding)) return false; }
          else if (nested_id == 5 && nested_type == file_parquet_detail::I32) { if (!reader.read_i32(page.definition_levels_size)) return false; }
          else if (nested_id == 6 && nested_type == file_parquet_detail::I32) { if (!reader.read_i32(page.repetition_levels_size)) return false; }
          else if (nested_id == 7 && (nested_type == file_parquet_detail::TRUE_VALUE || nested_type == file_parquet_detail::FALSE_VALUE)) {
            page.values_compressed = nested_type == file_parquet_detail::TRUE_VALUE;
          } else if (!reader.skip(nested_type)) return false;
        }
        if (nested_type != file_parquet_detail::STOP || page.value_count < 0 || page.null_count < 0 ||
            page.definition_levels_size < 0 || page.repetition_levels_size < 0) return false;
      } else if (!reader.skip(type)) return false;
    }
    return type == file_parquet_detail::STOP && page.type >= 0 && page.compressed_size >= 0 && page.uncompressed_size >= 0;
  }

  static FileValueType value_type(int32_t parquet_type, int32_t converted)
  {
    if (parquet_type == 0) return FileValueType::BOOLEAN;
    if (parquet_type == 1 || parquet_type == 2) return converted == 6 ? FileValueType::DATE : FileValueType::BIGINT;
    if (parquet_type == 4 || parquet_type == 5) return FileValueType::DOUBLE;
    if (parquet_type == 6) return FileValueType::VARCHAR;
    return FileValueType::NULL_VALUE;
  }

  bool page_data(const ColumnChunk &chunk, size_t offset, const PageHeader &page, std::vector<uint8_t> &decoded) const
  {
    if (page.compressed_size < 0 || page.uncompressed_size < 0 || offset > bytes_.size() ||
        static_cast<size_t>(page.compressed_size) > bytes_.size() - offset) return false;
    const uint8_t *source = bytes_.data() + offset;
    const size_t level_bytes = static_cast<size_t>(page.repetition_levels_size) + static_cast<size_t>(page.definition_levels_size);
    if (level_bytes > static_cast<size_t>(page.compressed_size) || level_bytes > static_cast<size_t>(page.uncompressed_size)) return false;
    const size_t compressed_values = static_cast<size_t>(page.compressed_size) - level_bytes;
    const size_t uncompressed_values = static_cast<size_t>(page.uncompressed_size) - level_bytes;
    if (page.type == 3 && !page.values_compressed) {
      if (compressed_values != uncompressed_values) return false;
      decoded.assign(source, source + page.compressed_size);
      return true;
    }
    if (chunk.codec == 0) {
      if (page.compressed_size != page.uncompressed_size) return false;
      decoded.assign(source, source + page.compressed_size);
      return true;
    }
    if (chunk.codec != 1) return false;
    decoded.resize(level_bytes + uncompressed_values);
    if (level_bytes != 0) std::memcpy(decoded.data(), source, level_bytes);
    size_t output = 0;
    return FileSnappy::uncompress(source + level_bytes, compressed_values, decoded.data() + level_bytes, uncompressed_values, output) &&
           output == uncompressed_values;
  }

  static bool decode_hybrid(const uint8_t *data, size_t size, unsigned bit_width, size_t count, std::vector<uint32_t> &out)
  {
    out.clear(); size_t pos = 0;
    while (out.size() < count) {
      uint64_t header = 0; unsigned shift = 0;
      for (;;) {
        if (pos >= size || shift >= 64) return false;
        const uint8_t byte = data[pos++]; header |= static_cast<uint64_t>(byte & 0x7f) << shift;
        if ((byte & 0x80) == 0) break; shift += 7;
      }
      if ((header & 1) == 0) {
        const size_t run = static_cast<size_t>(header >> 1); const size_t bytes = (bit_width + 7) / 8;
        if (run == 0 || bytes > size - pos) return false;
        uint32_t value = 0; for (size_t i = 0; i < bytes; ++i) value |= static_cast<uint32_t>(data[pos++]) << (8 * i);
        if (run > count - out.size()) return false;
        out.insert(out.end(), run, value);
      } else {
        const size_t groups = static_cast<size_t>(header >> 1); const size_t values = groups * 8;
        if (groups == 0 || bit_width > 32 || groups > (size - pos) / bit_width) return false;
        const size_t end = pos + groups * bit_width;
        uint64_t bits = 0; unsigned available = 0;
        for (size_t i = 0; i < values; ++i) {
          while (available < bit_width) { if (pos >= end) return false; bits |= static_cast<uint64_t>(data[pos++]) << available; available += 8; }
          const uint32_t value = bit_width == 32 ? static_cast<uint32_t>(bits) : static_cast<uint32_t>(bits & ((UINT64_C(1) << bit_width) - 1));
          bits >>= bit_width; available -= bit_width;
          if (out.size() < count) out.push_back(value);
        }
        pos = end;
      }
    }
    return out.size() == count;
  }

  static bool decode_levels(const std::vector<uint8_t> &data, size_t &pos, unsigned bit_width, size_t count, std::vector<uint32_t> &levels)
  {
    if (bit_width == 0) { levels.assign(count, 0); return true; }
    if (data.size() - pos < 4) return false;
    const uint32_t length = file_parquet_detail::load_u32(data.data() + pos); pos += 4;
    if (length > data.size() - pos) return false;
    const bool ok = decode_hybrid(data.data() + pos, length, bit_width, count, levels); pos += length;
    return ok;
  }

  static unsigned bits_for(uint32_t value) { unsigned bits = 0; while (value != 0) { ++bits; value >>= 1; } return bits; }

  static bool plain_value(const uint8_t *data, size_t size, size_t &pos, int32_t type, FileCsvField &field)
  {
    field.quoted = type == 6 || type == 7;
    if (type == 0) {
      if (pos >= size) return false; field.value = (data[pos++] & 1) ? "true" : "false"; return true;
    }
    if (type == 1) { if (size - pos < 4) return false; field.value = std::to_string(static_cast<int32_t>(file_parquet_detail::load_u32(data + pos))); pos += 4; return true; }
    if (type == 2) { if (size - pos < 8) return false; field.value = std::to_string(static_cast<int64_t>(file_parquet_detail::load_u64(data + pos))); pos += 8; return true; }
    if (type == 4) { if (size - pos < 4) return false; float value = 0; const uint32_t raw = file_parquet_detail::load_u32(data + pos); std::memcpy(&value, &raw, sizeof(value)); pos += 4; field.value = std::to_string(value); return std::isfinite(value); }
    if (type == 5) { if (size - pos < 8) return false; double value = 0; const uint64_t raw = file_parquet_detail::load_u64(data + pos); std::memcpy(&value, &raw, sizeof(value)); pos += 8; field.value = std::to_string(value); return std::isfinite(value); }
    if (type == 6) {
      if (size - pos < 4) return false; const uint32_t length = file_parquet_detail::load_u32(data + pos); pos += 4;
      if (length > size - pos) return false; field.value.assign(reinterpret_cast<const char *>(data + pos), length); pos += length; return true;
    }
    return false;
  }

  bool decode_column(const ColumnChunk &chunk, const SchemaColumn &schema, size_t column_index, size_t row_base, size_t rows)
  {
    if (chunk.value_count != static_cast<int64_t>(rows) || chunk.codec < 0 || chunk.data_offset < 0 || chunk.compressed_size < 0 ||
        static_cast<uint64_t>(chunk.data_offset) > bytes_.size()) return false;
    size_t pos = static_cast<size_t>(chunk.dictionary_offset >= 0 ? chunk.dictionary_offset : chunk.data_offset);
    const size_t end = pos + static_cast<size_t>(chunk.compressed_size);
    if (end < pos || end > bytes_.size()) return false;
    std::vector<FileCsvField> dictionary;
    size_t output_row = row_base;
    int64_t remaining = chunk.value_count;
    while (pos < end && remaining > 0) {
      file_parquet_detail::CompactReader header_reader(bytes_.data() + pos, end - pos); PageHeader page;
      if (!parse_page_header(header_reader, page)) return false;
      const size_t data_pos = pos + header_reader.position();
      if (page.compressed_size < 0 || static_cast<size_t>(page.compressed_size) > end - data_pos) return false;
      std::vector<uint8_t> decoded;
      if (!page_data(chunk, data_pos, page, decoded)) return false;
      pos = data_pos + static_cast<size_t>(page.compressed_size);
      if (page.type == 2) { // DICTIONARY_PAGE
        if (page.value_count < 0 || page.encoding != 0) return false;
        size_t value_pos = 0; dictionary.clear(); dictionary.reserve(static_cast<size_t>(page.value_count));
        if (chunk.type == 0) {
          const size_t count = static_cast<size_t>(page.value_count);
          if (count > (decoded.size() - value_pos) * 8) return false;
          for (size_t i = 0; i < count; ++i) {
            FileCsvField value; value.value = ((decoded[value_pos + i / 8] >> (i % 8)) & 1) ? "true" : "false";
            dictionary.push_back(std::move(value));
          }
          value_pos += (count + 7) / 8;
        } else {
          for (int32_t i = 0; i < page.value_count; ++i) { FileCsvField value; if (!plain_value(decoded.data(), decoded.size(), value_pos, chunk.type, value)) return false; dictionary.push_back(std::move(value)); }
        }
        if (value_pos != decoded.size()) return false;
      } else if (page.type == 0) { // DATA_PAGE
        if (page.value_count < 0 || page.value_count > remaining) return false;
        size_t value_pos = 0; std::vector<uint32_t> definitions;
        const unsigned definition_width = schema.repetition == 1 ? 1 : 0;
        if (!decode_levels(decoded, value_pos, definition_width, static_cast<size_t>(page.value_count), definitions)) return false;
        size_t present = 0; for (uint32_t definition : definitions) { if (definition > 1) return false; if (definition == definition_width) ++present; }
        std::vector<uint32_t> indexes;
        if (page.encoding == 2 || page.encoding == 8) {
          if (dictionary.empty() || value_pos >= decoded.size()) return false;
          const unsigned width = decoded[value_pos++];
          if (width > 32 || !decode_hybrid(decoded.data() + value_pos, decoded.size() - value_pos, width, present, indexes)) return false;
          value_pos = decoded.size();
        } else if (page.encoding != 0) return false;
        size_t index = 0;
        size_t plain_index = 0;
        const bool plain_boolean = chunk.type == 0 && indexes.empty();
        if (plain_boolean && present > (decoded.size() - value_pos) * 8) return false;
        for (size_t i = 0; i < definitions.size(); ++i, ++output_row) {
          if (definitions[i] != definition_width) continue;
          FileCsvField value;
          if (!indexes.empty()) { if (indexes[index] >= dictionary.size()) return false; value = dictionary[index++]; }
          else if (plain_boolean) value.value = ((decoded[value_pos + plain_index / 8] >> (plain_index++ % 8)) & 1) ? "true" : "false";
          else if (!plain_value(decoded.data(), decoded.size(), value_pos, chunk.type, value)) return false;
          values_[column_index][output_row] = std::move(value);
        }
        if (plain_boolean) value_pos += (present + 7) / 8;
        if (index != indexes.size() || value_pos != decoded.size()) return false;
        remaining -= page.value_count;
      } else if (page.type == 3) { // DATA_PAGE_V2
        if (page.value_count < 0 || page.value_count > remaining || page.repetition_levels_size != 0 ||
            static_cast<size_t>(page.definition_levels_size) > decoded.size()) return false;
        const unsigned definition_width = schema.repetition == 1 ? 1 : 0;
        std::vector<uint32_t> definitions;
        if (definition_width == 0) definitions.assign(static_cast<size_t>(page.value_count), 0);
        else if (!decode_hybrid(decoded.data(), static_cast<size_t>(page.definition_levels_size), definition_width,
                                static_cast<size_t>(page.value_count), definitions)) return false;
        size_t present = 0;
        for (uint32_t definition : definitions) { if (definition > 1) return false; if (definition == definition_width) ++present; }
        if (static_cast<size_t>(page.null_count) != definitions.size() - present) return false;
        size_t value_pos = static_cast<size_t>(page.definition_levels_size);
        std::vector<uint32_t> indexes;
        if (page.encoding == 2 || page.encoding == 8) {
          if (dictionary.empty() || value_pos >= decoded.size()) return false;
          const unsigned width = decoded[value_pos++];
          if (width > 32 || !decode_hybrid(decoded.data() + value_pos, decoded.size() - value_pos, width, present, indexes)) return false;
          value_pos = decoded.size();
        } else if (page.encoding != 0) return false;
        size_t index = 0;
        size_t plain_index = 0;
        const bool plain_boolean = chunk.type == 0 && indexes.empty();
        if (plain_boolean && present > (decoded.size() - value_pos) * 8) return false;
        for (size_t i = 0; i < definitions.size(); ++i, ++output_row) {
          if (definitions[i] != definition_width) continue;
          FileCsvField value;
          if (!indexes.empty()) { if (indexes[index] >= dictionary.size()) return false; value = dictionary[index++]; }
          else if (plain_boolean) value.value = ((decoded[value_pos + plain_index / 8] >> (plain_index++ % 8)) & 1) ? "true" : "false";
          else if (!plain_value(decoded.data(), decoded.size(), value_pos, chunk.type, value)) return false;
          values_[column_index][output_row] = std::move(value);
        }
        if (plain_boolean) value_pos += (present + 7) / 8;
        if (index != indexes.size() || value_pos != decoded.size()) return false;
        remaining -= page.value_count;
      } else return false;
    }
    return remaining == 0 && output_row == row_base + rows;
  }

  bool parse(size_t max_columns)
  {
    using namespace file_parquet_detail;
    if (bytes_.size() < 12 || std::memcmp(bytes_.data(), "PAR1", 4) != 0 ||
        std::memcmp(bytes_.data() + bytes_.size() - 4, "PAR1", 4) != 0) return false;
    const uint32_t footer_size = load_u32(bytes_.data() + bytes_.size() - 8);
    if (footer_size > bytes_.size() - 12) return false;
    CompactReader reader(bytes_.data() + bytes_.size() - 8 - footer_size, footer_size);
    std::vector<SchemaColumn> schemas; std::vector<std::vector<ColumnChunk>> groups; std::vector<int64_t> group_rows;
    int64_t file_rows = -1; int16_t last = 0, id = 0; uint8_t type = 0;
    while (reader.read_field(last, id, type) && type != STOP) {
      if (id == 2 && type == LIST) {
        uint8_t elem = 0; uint32_t count = 0;
        if (!reader.read_list(elem, count) || elem != STRUCT || count < 2 || count - 1 > max_columns) return false;
        for (uint32_t i = 0; i < count; ++i) { SchemaColumn schema; bool leaf = false; if (!parse_schema(reader, schema, leaf)) return false; if (i != 0 && leaf) schemas.push_back(std::move(schema)); else if (i != 0) return false; }
      } else if (id == 3 && type == I64) { if (!reader.read_i64(file_rows) || file_rows < 0) return false; }
      else if (id == 4 && type == LIST) {
        uint8_t elem = 0; uint32_t count = 0;
        if (!reader.read_list(elem, count) || elem != STRUCT || count == 0) return false;
        for (uint32_t i = 0; i < count; ++i) { std::vector<ColumnChunk> group; int64_t rows = -1; if (!parse_row_group(reader, group, rows)) return false; groups.push_back(std::move(group)); group_rows.push_back(rows); }
      } else if (!reader.skip(type)) return false;
    }
    if (type != STOP || file_rows < 0 || schemas.empty() || groups.empty() || schemas.size() > max_columns ||
        static_cast<uint64_t>(file_rows) > std::numeric_limits<size_t>::max()) return false;
    row_count_ = static_cast<size_t>(file_rows); columns_.reserve(schemas.size()); values_.resize(schemas.size());
    for (size_t i = 0; i < schemas.size(); ++i) {
      const FileValueType inferred = value_type(schemas[i].type, schemas[i].converted);
      if (inferred == FileValueType::NULL_VALUE) return false;
      FileCsvColumn column; column.name = schemas[i].name; column.inference.add_type(inferred); if (schemas[i].repetition == 1) column.inference.add_null();
      columns_.push_back(std::move(column)); values_[i].resize(row_count_);
    }
    size_t row_base = 0;
    for (size_t group_index = 0; group_index < groups.size(); ++group_index) {
      if (group_rows[group_index] < 0 || static_cast<uint64_t>(group_rows[group_index]) > row_count_ - row_base || groups[group_index].size() != schemas.size()) return false;
      const size_t rows = static_cast<size_t>(group_rows[group_index]);
      for (size_t column = 0; column < schemas.size(); ++column) {
        if (groups[group_index][column].name != schemas[column].name || groups[group_index][column].type != schemas[column].type ||
            !decode_column(groups[group_index][column], schemas[column], column, row_base, rows)) return false;
      }
      row_base += rows;
    }
    return row_base == row_count_;
  }

  FileSecureHandle handle_;
  std::vector<uint8_t> bytes_;
  std::vector<FileCsvColumn> columns_;
  std::vector<std::vector<FileCsvField>> values_;
  size_t row_count_ = 0;
  size_t row_index_ = 0;
};

} // namespace sql
} // namespace oceanbase
