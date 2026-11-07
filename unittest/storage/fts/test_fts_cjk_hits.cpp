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
#include <array>
#include <cstdlib>
#include <cstring>
#include <map>
#include <list>
#include <string>
#include <vector>
#include "storage/fts/ik/ob_ik_cjk_processor.h"

namespace oceanbase {
namespace storage {
namespace {
using namespace common;

class HitAllocator : public ObIAllocator
{
public:
  ~HitAllocator() { for (auto entry : allocations_) std::free(entry.first); }
  void *alloc(int64_t bytes) override {
    if (calls_++ == fail_at_) return nullptr;
    void *ptr = std::malloc(bytes);
    if (ptr != nullptr) allocations_[ptr] = bytes;
    return ptr;
  }
  void *alloc(int64_t bytes, const ObMemAttr &) override { return alloc(bytes); }
  void free(void *ptr) override {
    auto found = allocations_.find(ptr);
    if (found != allocations_.end()) {
      std::memset(ptr, 0xa5, found->second);
      std::free(ptr);
      allocations_.erase(found);
    }
  }
  std::map<void *, int64_t> allocations_;
  int64_t calls_ = 0;
  int64_t fail_at_ = -1;
};

class DictionaryOwnerTag final : public ObIFTDict
{
public:
  int init() override { return OB_SUCCESS; }
  int match(const ObString &, bool &) const override { return OB_NOT_SUPPORTED; }
  int match(const ObString &, ObDATrieHit &) const override { return OB_NOT_SUPPORTED; }
  int match_with_hit(const ObString &, const ObDATrieHit &, ObDATrieHit &) const override
  { return OB_NOT_SUPPORTED; }
};

// All repeated 'a' prefixes up to max_length are valid; even lengths are words.
// Overlapping prefixes expire one at a time and together on a mismatch.
// The freeing allocator makes advancing an erased node observable.
class PrefixDictionary final : public ObIFTDict
{
public:
  explicit PrefixDictionary(int64_t max_length) : max_length_(max_length) {}
  int init() override { return OB_SUCCESS; }
  int match(const ObString &word, bool &matched) const override
  { matched = word.length() <= max_length_ && word.length() % 2 == 0; return OB_SUCCESS; }
  int match(const ObString &ch, ObDATrieHit &hit) const override
  {
    hit.dict_ = hit.start_pos_ % 2 == 0 ? &even_owner_ : &odd_owner_;
    hit.char_cnt_ = 1;
    hit.end_pos_ = hit.start_pos_ + ch.length();
    classify(ch, hit);
    return OB_SUCCESS;
  }
  int match_with_hit(const ObString &ch, const ObDATrieHit &last, ObDATrieHit &hit) const override
  {
    const ObIFTDict *expected_owner = last.start_pos_ % 2 == 0 ? &even_owner_ : &odd_owner_;
    if (last.dict_ != expected_owner) return OB_ERR_UNEXPECTED;
    const int64_t count = last.char_cnt_ + 1;
    const int64_t end = last.end_pos_ + ch.length();
    hit.char_cnt_ = count;
    hit.end_pos_ = end;
    classify(ch, hit);
    return OB_SUCCESS;
  }
private:
  void classify(const ObString &ch, ObDATrieHit &hit) const
  {
    const bool valid = ch.length() == 1 && ch.ptr()[0] == 'a' && hit.char_cnt_ <= max_length_;
    hit.is_match_ = valid && hit.char_cnt_ % 2 == 0;
    hit.is_prefix_ = valid && hit.char_cnt_ < max_length_;
  }
  int64_t max_length_;
  DictionaryOwnerTag even_owner_;
  DictionaryOwnerTag odd_owner_;
};

using Token = std::array<int64_t, 4>;
std::vector<Token> tokens(TokenizeContext &ctx)
{
  std::vector<Token> result;
  for (const auto &token : ctx.token_list().tokens()) {
    result.push_back({token.offset_, token.length_, token.char_cnt_, static_cast<int64_t>(token.type_)});
  }
  return result;
}

int reference_step(std::list<ObDATrieHit> &hits, const ObIFTDict &dict,
                   TokenizeContext &ctx, const char *ch, uint8_t length, ObFTCharUtil::CharType type)
{
  int ret = OB_SUCCESS;
  if (type == ObFTCharUtil::CharType::USELESS) {
    hits.clear();
  } else {
    for (auto it = hits.begin(); OB_SUCC(ret) && it != hits.end();) {
      if (OB_FAIL(dict.match_with_hit({length, ch}, *it, *it))) {
      } else if (it->is_match() && OB_FAIL(ctx.add_token(ctx.fulltext(), it->start_pos_,
                  it->end_pos_ - it->start_pos_, it->char_cnt_, ObIKTokenType::IK_CHINESE_TOKEN))) {
      }
      if (it->is_unmatch()) it = hits.erase(it); else ++it;
    }
    if (OB_SUCC(ret)) {
      ObDATrieHit hit(&dict, ctx.get_cursor());
      if (OB_FAIL(dict.match({length, ch}, hit))) {
      } else if (hit.is_match()) {
        hits.push_back(hit);
        ret = ctx.add_token(ctx.fulltext(), ctx.get_cursor(), length, 1, ObIKTokenType::IK_CHINESE_TOKEN);
      } else if (hit.is_prefix()) {
        hits.push_back(hit);
      }
    }
  }
  if (OB_SUCCESS != ret || ctx.is_last()) hits.clear();
  return ret;
}

void compare_scan(int64_t max_prefix, const std::string &input, bool expect_allocation)
{
  PrefixDictionary dictionary(max_prefix);
  HitAllocator hits_allocator;
  ObArenaAllocator tokens_allocator;
  TokenizeContext actual(CS_TYPE_UTF8MB4_BIN, tokens_allocator, input.data(), input.size(), true);
  TokenizeContext reference(CS_TYPE_UTF8MB4_BIN, tokens_allocator, input.data(), input.size(), true);
  ASSERT_EQ(OB_SUCCESS, actual.init());
  ASSERT_EQ(OB_SUCCESS, reference.init());
  ObIKCJKProcessor processor(dictionary, hits_allocator);
  std::list<ObDATrieHit> reference_hits;
  while (!actual.iter_end()) {
    const char *ch = nullptr;
    uint8_t length = 0;
    ObFTCharUtil::CharType type;
    ASSERT_EQ(OB_SUCCESS, actual.current_char(ch, length));
    ASSERT_EQ(OB_SUCCESS, actual.current_char_type(type));
    ASSERT_EQ(OB_SUCCESS, processor.do_process(actual, ch, length, type));
    ASSERT_EQ(OB_SUCCESS, reference_step(reference_hits, dictionary, reference, ch, length, type));
    EXPECT_EQ(tokens(reference), tokens(actual));
    const int ret = actual.step_next();
    ASSERT_EQ(ret, reference.step_next());
  }
  EXPECT_EQ(expect_allocation, hits_allocator.calls_ > 0);
}

TEST(FtsCjkHits, expired_nodes_match_reference_with_freeing_allocator)
{
  compare_scan(4, std::string(30, 'a') + "b!" + std::string(15, 'a'), true);
}

TEST(FtsCjkHits, overlapping_prefixes_match_reference_after_erasure)
{
  compare_scan(40, std::string(60, 'a') + "b!" + std::string(48, 'a'), true);
}

TEST(FtsCjkHits, allocation_failure_is_reported_and_state_cleared)
{
  PrefixDictionary dictionary(40);
  HitAllocator hits_allocator;
  hits_allocator.fail_at_ = 0;
  ObArenaAllocator tokens_allocator;
  const std::string input(20, 'a');
  TokenizeContext context(CS_TYPE_UTF8MB4_BIN, tokens_allocator, input.data(), input.size(), true);
  ASSERT_EQ(OB_SUCCESS, context.init());
  ObIKCJKProcessor processor(dictionary, hits_allocator);
  int ret = OB_SUCCESS;
  while (ret == OB_SUCCESS && !context.iter_end()) {
    const char *ch = nullptr;
    uint8_t length = 0;
    ObFTCharUtil::CharType type;
    ASSERT_EQ(OB_SUCCESS, context.current_char(ch, length));
    ASSERT_EQ(OB_SUCCESS, context.current_char_type(type));
    ret = processor.do_process(context, ch, length, type);
    if (OB_SUCCESS == ret) context.step_next();
  }
  EXPECT_EQ(OB_ALLOCATE_MEMORY_FAILED, ret);
  const std::string next("aa");
  TokenizeContext recovered(CS_TYPE_UTF8MB4_BIN, tokens_allocator, next.data(), next.size(), true);
  ASSERT_EQ(OB_SUCCESS, recovered.init());
  while (!recovered.iter_end()) {
    const char *ch = nullptr;
    uint8_t length = 0;
    ObFTCharUtil::CharType type;
    ASSERT_EQ(OB_SUCCESS, recovered.current_char(ch, length));
    ASSERT_EQ(OB_SUCCESS, recovered.current_char_type(type));
    ASSERT_EQ(OB_SUCCESS, processor.do_process(recovered, ch, length, type));
    recovered.step_next();
  }
  ASSERT_EQ(1, tokens(recovered).size());
}
} // namespace
} // namespace storage
} // namespace oceanbase
