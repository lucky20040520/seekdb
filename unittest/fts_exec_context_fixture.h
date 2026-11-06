/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */
#pragma once

namespace oceanbase
{
namespace common { class ObIAllocator; }
namespace storage { struct ObTableScanParam; }
namespace sql { class ObEvalCtx; class ObDASScanIter; }
namespace test_support
{
// Shared fixture infrastructure keeps SQL object layouts out of Storage tests.
class FtsExecContextFixture
{
public:
  explicit FtsExecContextFixture(common::ObIAllocator &allocator);
  ~FtsExecContextFixture();
  sql::ObEvalCtx &eval_ctx();
  storage::ObTableScanParam *scan_param();
  static sql::ObDASScanIter *make_arena_scan(common::ObIAllocator &allocator);
private:
  struct State;
  State *state_;
  FtsExecContextFixture(const FtsExecContextFixture &) = delete;
  FtsExecContextFixture &operator=(const FtsExecContextFixture &) = delete;
};
} // namespace test_support
} // namespace oceanbase
