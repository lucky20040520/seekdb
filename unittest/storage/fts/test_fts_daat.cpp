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
#include <map>
#include <memory>
#include "data_plane/access/ob_tablet_scan.h"
#include "query/engine/expr/ob_expr.h"
#include "storage/retrieval/ob_sparse_daat_iter.h"

namespace oceanbase
{
namespace storage
{

class TestFtsDaatComparison : public ::testing::Test
{
protected:
  TestFtsDaatComparison() : allocator_(), ids_(), left_(), right_(), comparator_() {}
  void SetUp() override
  {
    ids_.set_allocator(&allocator_);
    ASSERT_EQ(OB_SUCCESS, ids_.init(2));
    ASSERT_EQ(OB_SUCCESS, ids_.prepare_allocate(2));
    ids_[0] = &left_;
    ids_[1] = &right_;
    ASSERT_EQ(OB_SUCCESS, comparator_.init(sql::ObDatumMeta(ObVarcharType, CS_TYPE_BINARY, -1), &ids_));
  }
  int64_t compare()
  {
    ObSRMergeItem left;
    ObSRMergeItem right;
    left.iter_idx_ = 0;
    right.iter_idx_ = 1;
    int64_t result = 99;
    EXPECT_EQ(OB_SUCCESS, comparator_.cmp(left, right, result));
    return result;
  }
  ObArenaAllocator allocator_;
  ObFixedArray<const ObDatum *, ObIAllocator> ids_;
  ObDatum left_;
  ObDatum right_;
  ObSRMergeCmp comparator_;
};

TEST_F(TestFtsDaatComparison, binary_strings_match_charset_reference)
{
  const std::vector<std::string> values = {
      "", "a", "a ", "a  ", "aa", "A", std::string("a\0", 2),
      std::string("a\0b", 3), std::string(1, '\x80'), std::string(1, '\xff'),
  };
  for (const std::string &left : values) {
    for (const std::string &right : values) {
      left_.set_string(left.data(), left.size());
      right_.set_string(right.data(), right.size());
      const int reference = ObCharset::strcmpsp(CS_TYPE_BINARY,
          left.data(), left.size(), right.data(), right.size(), false);
      EXPECT_EQ((reference > 0) - (reference < 0), compare());
    }
  }
}

TEST_F(TestFtsDaatComparison, randomized_binary_strings_match_reference)
{
  uint32_t state = 17;
  auto next = [&state]() { state = state * 1664525U + 1013904223U; return state; };
  for (int sample = 0; sample < 1000; ++sample) {
    std::string left(next() % 33, '\0');
    std::string right(next() % 33, '\0');
    for (char &byte : left) byte = static_cast<char>(next() >> 24);
    for (char &byte : right) byte = static_cast<char>(next() >> 24);
    left_.set_string(left.data(), left.size());
    right_.set_string(right.data(), right.size());
    const int reference = ObCharset::strcmpsp(CS_TYPE_BINARY,
        left.data(), left.size(), right.data(), right.size(), false);
    EXPECT_EQ((reference > 0) - (reference < 0), compare());
  }
}

TEST_F(TestFtsDaatComparison, nulls_remain_first)
{
  left_.set_null();
  right_.set_null();
  EXPECT_EQ(0, compare());
  right_.set_string("x", 1);
  EXPECT_LT(compare(), 0);
  left_.set_string("x", 1);
  right_.set_null();
  EXPECT_GT(compare(), 0);
}

TEST_F(TestFtsDaatComparison, other_collations_and_integer_ids_keep_generic_order)
{
  ASSERT_EQ(OB_SUCCESS, comparator_.init(sql::ObDatumMeta(ObVarcharType, CS_TYPE_UTF8MB4_GENERAL_CI, -1), &ids_));
  left_.set_string("A", 1);
  right_.set_string("a", 1);
  EXPECT_EQ(0, compare());
  ASSERT_EQ(OB_SUCCESS, comparator_.init(sql::ObDatumMeta(ObIntType, CS_TYPE_BINARY, -1), &ids_));
  int64_t left_value = -3;
  int64_t right_value = 5;
  left_.ptr_ = reinterpret_cast<const char *>(&left_value);
  right_.ptr_ = reinterpret_cast<const char *>(&right_value);
  left_.set_int(left_value);
  right_.set_int(right_value);
  EXPECT_LT(compare(), 0);
}

class OneRowFtsDimension final : public ObISRDaaTDimIter
{
public:
  OneRowFtsDimension() : advances_(0), score_reads_(0), id_reads_(0), value_(42), datum_()
  {
    datum_.ptr_ = reinterpret_cast<const char *>(&value_);
    datum_.set_int(value_);
  }
  int get_next_row() override { return ++advances_ == 1 ? OB_SUCCESS : OB_ITER_END; }
  int get_curr_score(double &score) const override { ++score_reads_; score = 0.25; return OB_SUCCESS; }
  int get_curr_id(const ObDatum *&datum) const override { ++id_reads_; datum = &datum_; return OB_SUCCESS; }
  int advances_;
  mutable int score_reads_;
  mutable int id_reads_;
  int64_t value_;
  ObDatum datum_;
};

TEST(TestFtsDaatDimension, combined_fetch_advances_once_and_preserves_end)
{
  OneRowFtsDimension iter;
  const ObDatum *datum = nullptr;
  double score = -1;
  ASSERT_EQ(OB_SUCCESS, iter.get_next_row_with_data(datum, score));
  ASSERT_NE(nullptr, datum);
  EXPECT_EQ(42, datum->get_int());
  EXPECT_DOUBLE_EQ(0.25, score);
  EXPECT_EQ(OB_ITER_END, iter.get_next_row_with_data(datum, score));
  EXPECT_EQ(2, iter.advances_);
  EXPECT_EQ(1, iter.score_reads_);
  EXPECT_EQ(1, iter.id_reads_);
}


class PostingFtsDimension final : public ObISRDaaTDimIter
{
public:
  PostingFtsDimension(const std::vector<int64_t> &ids, double score)
      : ids_(ids), score_(score), offset_(0), value_(0), datum_() {}
  int get_next_row() override
  {
    if (offset_ == ids_.size()) return OB_ITER_END;
    value_ = ids_[offset_++];
    datum_.ptr_ = reinterpret_cast<const char *>(&value_);
    datum_.set_int(value_);
    return OB_SUCCESS;
  }
  int get_curr_score(double &score) const override { score = score_; return OB_SUCCESS; }
  int get_curr_id(const ObDatum *&datum) const override { datum = &datum_; return OB_SUCCESS; }
  void rewind() { offset_ = 0; }
private:
  std::vector<int64_t> ids_;
  double score_;
  size_t offset_;
  int64_t value_;
  ObDatum datum_;
};

class FtsMergeHarness : public ObSRDaaTIterImpl
{
public:
  ~FtsMergeHarness() { reset(); }
  int prepare(ObSparseRetrievalMergeParam &param, ObIArray<ObISRDaaTDimIter *> &dims,
              ObIAllocator &allocator, ObSRDaaTRelevanceCollector &collector)
  {
    int ret = OB_SUCCESS;
    iter_allocator_ = &allocator;
    iter_param_ = &param;
    dim_iters_ = &dims;
    relevance_collector_ = &collector;
    iter_domain_ids_.set_allocator(&allocator);
    next_round_iter_idxes_.set_allocator(&allocator);
    if (OB_FAIL(iter_domain_ids_.init(dims.count()))) {
    } else if (OB_FAIL(iter_domain_ids_.prepare_allocate(dims.count()))) {
    } else if (OB_FAIL(next_round_iter_idxes_.init(dims.count()))) {
    } else if (OB_FAIL(next_round_iter_idxes_.prepare_allocate(dims.count()))) {
    } else if (OB_FAIL(merge_cmp_.init(sql::ObDatumMeta(ObIntType, CS_TYPE_BINARY, -1), &iter_domain_ids_))) {
    } else if (OB_FAIL(init_merge_heap(dims.count()))) {
    } else {
      next_round_cnt_ = dims.count();
      for (int64_t i = 0; i < dims.count(); ++i) next_round_iter_idxes_[i] = i;
    }
    return ret;
  }
  int next(const ObDatum *&id, double &score, bool &valid)
  {
    int ret = OB_SUCCESS;
    if (OB_FAIL(fill_merge_heap())) {
    } else if (OB_FAIL(collect_dims_by_id(id, score, valid))) {
    }
    return ret;
  }
};

TEST(TestFtsDaatMerge, short_and_loser_tree_merges_preserve_ids_scores_and_reuse)
{
  for (int dim_count = 1; dim_count <= 6; ++dim_count) {
    for (int should_match = 1; should_match <= dim_count; ++should_match) {
      ObArenaAllocator allocator;
      ObSparseRetrievalMergeParam param;
      ObSRDaaTInnerProductRelevanceCollector collector;
      ASSERT_EQ(OB_SUCCESS, collector.init(should_match));
      ObSEArray<ObISRDaaTDimIter *, 8> dims;
      std::vector<std::unique_ptr<PostingFtsDimension>> owners;
      std::map<int64_t, std::pair<double, int>> reference;
      for (int dim = 0; dim < dim_count; ++dim) {
        std::vector<int64_t> ids;
        for (int64_t id = -20; id < 80; ++id) {
          // Includes cross-stream equal IDs, negative IDs and exhausted streams.
          if (dim != 2 && (id + 20) % (dim + 2) != 1) {
            ids.push_back(id);
            reference[id].first += 0.25 * (dim + 1);
            ++reference[id].second;
          }
        }
        owners.emplace_back(new PostingFtsDimension(ids, 0.25 * (dim + 1)));
        ASSERT_EQ(OB_SUCCESS, dims.push_back(owners.back().get()));
      }
      FtsMergeHarness merge;
      ASSERT_EQ(OB_SUCCESS, merge.prepare(param, dims, allocator, collector));
      for (int round = 0; round < 2; ++round) {
        if (round > 0) {
          for (auto &dim : owners) dim->rewind();
          merge.reuse();
        }
        for (const auto &expected : reference) {
          const ObDatum *id = nullptr;
          double score = -1;
          bool valid = false;
          ASSERT_EQ(OB_SUCCESS, merge.next(id, score, valid));
          ASSERT_NE(nullptr, id);
          EXPECT_EQ(expected.first, id->get_int());
          EXPECT_DOUBLE_EQ(expected.second.first, score);
          EXPECT_EQ(expected.second.second >= should_match, valid);
        }
        const ObDatum *id = nullptr;
        double score = 0;
        bool valid = false;
        EXPECT_EQ(OB_ITER_END, merge.next(id, score, valid));
      }
    }
  }
}

} // namespace storage
} // namespace oceanbase
