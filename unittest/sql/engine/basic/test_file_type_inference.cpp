/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */

#ifdef NDEBUG
#undef NDEBUG
#endif
#include "sql/engine/basic/file_type_inference.h"
#include <cassert>

using namespace oceanbase::sql;

int main()
{
  using T = FileValueType;
  assert(FileTypeInference::infer_text("true") == T::BOOLEAN);
  assert(FileTypeInference::infer_text("FaLsE") == T::BOOLEAN);
  assert(FileTypeInference::infer_text("+42") == T::BIGINT);
  assert(FileTypeInference::infer_text("+-42") == T::VARCHAR);
  assert(FileTypeInference::infer_text("-9223372036854775808") == T::BIGINT);
  assert(FileTypeInference::infer_text("9223372036854775808") == T::VARCHAR);
  assert(FileTypeInference::infer_text("1.2e3") == T::DOUBLE);
  assert(FileTypeInference::infer_text("nan") == T::VARCHAR);
  assert(FileTypeInference::infer_text("2024-02-29") == T::DATE);
  assert(FileTypeInference::infer_text("1900-02-29") == T::VARCHAR);
  assert(FileTypeInference::infer_text("2026-08-01 10:11:12.123456") == T::DATETIME);
  assert(FileTypeInference::infer_text("2026-08-01 24:11:12") == T::VARCHAR);
  assert(FileTypeInference::infer_text("2026-08-01 10:11:12.1234567") == T::VARCHAR);
  FileTypeInference state;
  state.add_null();
  assert(state.type() == T::NULL_VALUE && state.nullable());
  state.add_type(T::BIGINT);
  state.add_type(T::DOUBLE);
  assert(state.type() == T::DOUBLE && state.nullable());
  assert(FileTypeInference::widen(T::DATE, T::DATETIME) == T::DATETIME);
  assert(FileTypeInference::widen(T::DATE, T::BIGINT) == T::VARCHAR);
  assert(FileTypeInference::widen(T::BOOLEAN, T::BIGINT) == T::BIGINT);
  return 0;
}
