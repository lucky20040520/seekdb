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
#include <cstdio>
#include <vector>
#include "storage/fts/dict/ob_ft_dat_dict.h"

namespace oceanbase
{
namespace storage
{

TEST(TestFtsDictionaryCache, reused_map_address_reads_current_dictionary)
{
  // Deliberately reuse one allocation, as happens when a DAT is rebuilt.
  std::vector<char> memory(ObArrayHashMap::estimate_size(4));
  ObArrayHashMap *map = reinterpret_cast<ObArrayHashMap *>(memory.data());
  const ObString text("a");
  ObFTSingleWord word;
  word.set_word(text.ptr(), text.length());
  ASSERT_EQ(OB_SUCCESS, map->init(4));
  ASSERT_EQ(OB_SUCCESS, map->insert(text, 7));
  ObFTWordCode code = 0;
  ASSERT_EQ(OB_SUCCESS, map->find(word, code));
  ASSERT_EQ(7, code);

  ASSERT_EQ(OB_SUCCESS, map->init(4));
  ASSERT_EQ(OB_ENTRY_NOT_EXIST, map->find(word, code));
  ASSERT_EQ(OB_SUCCESS, map->insert(text, 23));
  ASSERT_EQ(OB_SUCCESS, map->find(word, code));
  EXPECT_EQ(23, code);
}

TEST(TestFtsDictionaryCache, rebuilt_map_with_different_probe_locations)
{
  constexpr int count = 40;
  std::vector<char> memory(ObArrayHashMap::estimate_size(count));
  ObArrayHashMap *map = reinterpret_cast<ObArrayHashMap *>(memory.data());
  char keys[count][4];
  ObFTSingleWord words[count];
  for (int i = 0; i < count; ++i) {
    std::snprintf(keys[i], sizeof(keys[i]), "k%02d", i);
    ASSERT_EQ(OB_SUCCESS, words[i].set_word(keys[i], 3));
  }
  ASSERT_EQ(OB_SUCCESS, map->init(count));
  for (int i = 0; i < count; ++i) {
    ASSERT_EQ(OB_SUCCESS, map->insert(ObString(3, keys[i]), i + 1));
  }
  ObFTWordCode code = 0;
  for (int i = 0; i < count; ++i) {
    ASSERT_EQ(OB_SUCCESS, map->find(words[i], code));
    ASSERT_EQ(i + 1, code);
  }
  ASSERT_EQ(OB_SUCCESS, map->init(count));
  // Reversing insertion order also relocates keys in colliding hash buckets.
  for (int i = count - 1; i >= 0; --i) {
    ASSERT_EQ(OB_SUCCESS, map->insert(ObString(3, keys[i]), i + 101));
  }
  for (int i = 0; i < count; ++i) {
    ASSERT_EQ(OB_SUCCESS, map->find(words[i], code));
    EXPECT_EQ(i + 101, code);
  }
}

} // namespace storage
} // namespace oceanbase
