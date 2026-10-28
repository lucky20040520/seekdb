/*
 * Copyright (c) 2025 OceanBase.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#pragma once

#include "sql/engine/expr/ob_expr_ai/ob_ai_func_utils.h"
#include <cstdio>
#include <cstdlib>

#define AI_CHECK(condition) do { \
  if (!(condition)) { \
    std::fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #condition); \
    std::exit(1); \
  } \
} while (false)

inline oceanbase::common::ObJsonObject *ai_test_json(
    oceanbase::common::ObIAllocator &allocator, const char *text)
{
  using namespace oceanbase::common;
  ObString input(text);
  ObJsonObject *object = nullptr;
  AI_CHECK(OB_SUCCESS == ObAIFuncJsonUtils::get_json_object_form_str(allocator, input, object));
  return object;
}

void run_ai_prompt_tests();
