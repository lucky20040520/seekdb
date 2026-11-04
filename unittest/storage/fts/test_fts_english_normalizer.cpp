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

#include <gtest/gtest.h>
#include <string>
#include <vector>
#include "share/text_analysis/ob_token_stream.h"
#include "lib/charset/ob_charset.h"

namespace oceanbase
{
namespace share
{
using namespace common;

class SingleTokenStream final : public ObITokenStream
{
public:
  explicit SingleTokenStream(const std::string &text) : text_(text), done_(false) {}
  void reset() override { done_ = false; }
  void reuse() override { reset(); }
  int get_next(ObDatum &token, int64_t &frequency) override
  {
    if (done_) return OB_ITER_END;
    done_ = true;
    token.set_string(text_.data(), text_.size());
    frequency = 7;
    return OB_SUCCESS;
  }
  TO_STRING_KV(K_(done));
private:
  const std::string &text_;
  bool done_;
};

static void check_normalization(const std::string &text, ObCollationType collation)
{
  const ObCharsetInfo *cs = ObCharset::get_charset(collation);
  ASSERT_NE(nullptr, cs);
  size_t first = 0;
  while (first < text.size() && !ob_isalnum(cs, text[first])) ++first;
  size_t end = text.size();
  while (end > first && !ob_isalnum(cs, text[end - 1])) --end;
  SingleTokenStream input(text);
  ObBasicEnglishNormalizer normalizer;
  ASSERT_EQ(OB_SUCCESS, normalizer.init(cs, input));
  for (int pass = 0; pass < 2; ++pass) {
    ObDatum token;
    int64_t frequency = 0;
    if (first == end) {
      EXPECT_EQ(OB_ITER_END, normalizer.get_next(token, frequency));
    } else {
      ObArenaAllocator allocator;
      ObString expected;
      ASSERT_EQ(OB_SUCCESS, ObCharset::tolower(cs,
          ObString(end - first, text.data() + first), expected, allocator));
      ASSERT_EQ(OB_SUCCESS, normalizer.get_next(token, frequency));
      EXPECT_EQ(std::string(expected.ptr(), expected.length()), std::string(token.ptr_, token.len_));
      EXPECT_EQ(7, frequency);
      EXPECT_EQ(OB_ITER_END, normalizer.get_next(token, frequency));
    }
    normalizer.reuse();
  }
  normalizer.reset();
}

TEST(TestFtsEnglishNormalizer, ascii_and_unicode_match_generic_lowercase)
{
  const std::vector<std::string> values = {
      "lowercase", "UPPERCASE", "MiXeD123", "...Hello!?", "---", "", "a-b.C",
      std::string("A\0B", 3), "\xc3\x89" "COLE", "A\xe4\xb8\xad" "B",
      "A\xf0\x9f\x98\x80" "B", "A\xff" "B", "I\xc4\xb0" "X",
      std::string(8192, 'A'), std::string(8192, 'a'),
  };
  for (ObCollationType collation : {CS_TYPE_UTF8MB4_BIN, CS_TYPE_UTF8MB4_GENERAL_CI, CS_TYPE_BINARY}) {
    for (const std::string &text : values) check_normalization(text, collation);
  }
}

TEST(TestFtsEnglishNormalizer, all_ascii_bytes_and_random_tokens_match_reference)
{
  for (int byte = 0; byte < 128; ++byte) {
    check_normalization(std::string("A") + static_cast<char>(byte) + "Z", CS_TYPE_UTF8MB4_BIN);
  }
  uint32_t state = 31;
  for (int sample = 0; sample < 1000; ++sample) {
    std::string text("A");
    for (int i = 0; i < sample % 80; ++i) {
      state = state * 1664525U + 1013904223U;
      text += static_cast<char>(state >> 24);
    }
    text += 'Z';
    check_normalization(text, CS_TYPE_UTF8MB4_BIN);
  }
}
} // namespace share
} // namespace oceanbase
