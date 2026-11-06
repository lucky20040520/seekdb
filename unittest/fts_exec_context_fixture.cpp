/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */
#include "fts_exec_context_fixture.h"
#include "sql/engine/ob_exec_context.h"
#include "sql/das/iter/ob_das_scan_iter.h"
#include "data_plane/access/ob_table_scan_param.h"

namespace oceanbase
{
namespace test_support
{
struct FtsExecContextFixture::State
{
  explicit State(common::ObIAllocator &allocator) : exec_ctx_(allocator), eval_ctx_(exec_ctx_) {}
  sql::ObExecContext exec_ctx_;
  sql::ObEvalCtx eval_ctx_;
  storage::ObTableScanParam scan_param_;
};

FtsExecContextFixture::FtsExecContextFixture(common::ObIAllocator &allocator)
  : state_(new State(allocator)) {}
FtsExecContextFixture::~FtsExecContextFixture() { delete state_; }
sql::ObEvalCtx &FtsExecContextFixture::eval_ctx() { return state_->eval_ctx_; }
storage::ObTableScanParam *FtsExecContextFixture::scan_param() { return &state_->scan_param_; }
sql::ObDASScanIter *FtsExecContextFixture::make_arena_scan(common::ObIAllocator &allocator)
{
  // Match DAS arena ownership: a default scan's destructor would call release().
  void *memory = allocator.alloc(sizeof(sql::ObDASScanIter));
  return nullptr == memory ? nullptr : new (memory) sql::ObDASScanIter();
}
} // namespace test_support
} // namespace oceanbase
