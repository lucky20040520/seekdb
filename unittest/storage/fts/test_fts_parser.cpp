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
#include <cstring>
#include "data_plane/fts/ob_fts_parser_helper.h"
#include "storage/fts/ik/ob_ik_char_util.h"
#include "storage/fts/ob_fts_parser_property.h"
#include "storage/fts/ob_fts_stop_word.h"
#include "storage/fts/ik/ob_ik_arbitrator.h"
#include "storage/fts/ik/ob_ik_processor.h"
#include <string>
#include <vector>

namespace oceanbase
{
namespace storage
{

using CharType = ObFTCharUtil::CharType;

// The IK segmenters classify every character of the document and then run a
// few more block predicates on it. The predicates below pin down the expected
// classification of the characters, so that a change of the classification
// code can not silently change the produced tokens.
class TestFtsCharClassify : public ::testing::Test
{
protected:
  static CharType classify(const char *str, const int64_t len)
  {
    CharType type = CharType::USELESS;
    EXPECT_EQ(OB_SUCCESS,
              ObFTCharUtil::classify_first_char(ObCollationType::CS_TYPE_UTF8MB4_BIN,
                                                str,
                                                static_cast<uint8_t>(len),
                                                type));
    return type;
  }
};

TEST_F(TestFtsCharClassify, ascii_characters)
{
  EXPECT_EQ(CharType::ENGLISH_LETTER, classify("a", 1));
  EXPECT_EQ(CharType::ENGLISH_LETTER, classify("Z", 1));
  EXPECT_EQ(CharType::ARABIC_LETTER, classify("0", 1));
  EXPECT_EQ(CharType::ARABIC_LETTER, classify("9", 1));
  EXPECT_EQ(CharType::USELESS, classify(" ", 1));
  EXPECT_EQ(CharType::USELESS, classify(",", 1));
  EXPECT_EQ(CharType::USELESS, classify("~", 1));
}

TEST_F(TestFtsCharClassify, cjk_characters)
{
  // U+4E2D
  EXPECT_EQ(CharType::CHINESE, classify("\xe4\xb8\xad", 3));
  // U+4E00
  EXPECT_EQ(CharType::CHINESE, classify("\xe4\xb8\x80", 3));
  // U+3042, hiragana
  EXPECT_EQ(CharType::OTHER_CJK, classify("\xe3\x81\x82", 3));
  // U+D55C, hangul
  EXPECT_EQ(CharType::OTHER_CJK, classify("\xed\x95\x9c", 3));
}

TEST_F(TestFtsCharClassify, full_width_characters)
{
  // U+FF21, full width 'A'
  EXPECT_EQ(CharType::ENGLISH_LETTER, classify("\xef\xbc\xa1", 3));
  // U+FF11, full width '1'
  EXPECT_EQ(CharType::ARABIC_LETTER, classify("\xef\xbc\x91", 3));
}

TEST_F(TestFtsCharClassify, utf8_fast_path_matches_charset_for_all_scalar_values)
{
  const ObCollationType coll = ObCollationType::CS_TYPE_UTF8MB4_BIN;
  for (int32_t cp = 0; cp <= 0x10ffff; ++cp) {
    if (cp >= 0xd800 && cp <= 0xdfff) {
      continue; // Surrogates are not Unicode scalar values.
    }
    char bytes[4];
    int32_t size = 0;
    ASSERT_EQ(OB_SUCCESS, ObCharset::wc_mb(coll, cp, bytes, sizeof(bytes), size)) << cp;
    int64_t reference_len = -1;
    int64_t fast_len = -1;
    ASSERT_EQ(OB_SUCCESS, ObCharset::first_valid_char(coll, bytes, size, reference_len)) << cp;
    ASSERT_EQ(OB_SUCCESS, ObFTCharUtil::first_valid_char_fast(coll, bytes, size, fast_len)) << cp;
    ASSERT_EQ(reference_len, fast_len) << cp;

    CharType expected = CharType::USELESS;
    if (ObUnicodeBlockUtils::is_chinese(cp)) {
      expected = CharType::CHINESE;
    } else if (ObUnicodeBlockUtils::is_alpha(cp)) {
      expected = CharType::ENGLISH_LETTER;
    } else if (ObUnicodeBlockUtils::is_arabic(cp)) {
      expected = CharType::ARABIC_LETTER;
    } else if (ObUnicodeBlockUtils::is_other_cjk(cp)) {
      expected = CharType::OTHER_CJK;
    }
    ASSERT_EQ(expected, classify(bytes, size)) << cp;
  }
}

TEST_F(TestFtsCharClassify, utf8_fast_path_preserves_invalid_input_results)
{
  struct Input { const char *bytes; int64_t size; };
  const Input inputs[] = {
      {nullptr, 0}, {"", 0}, {"a", -1}, {"\x80", 1}, {"\xbf", 1},
      {"\xc0\x80", 2}, {"\xc1\xbf", 2}, {"\xc2", 1}, {"\xc2 ", 2},
      {"\xe0\x9f\xbf", 3}, {"\xe0\xa0", 2}, {"\xed\xa0\x80", 3},
      {"\xed\xbf\xbf", 3}, {"\xe4\xb8", 2}, {"\xe4 \xad", 3},
      {"\xf0\x8f\xbf\xbf", 4}, {"\xf0\x90\x80", 3},
      {"\xf4\x90\x80\x80", 4}, {"\xf5\x80\x80\x80", 4}, {"\xff", 1},
  };
  int case_idx = 0;
  for (const Input &input : inputs) {
    SCOPED_TRACE(case_idx++);
    int64_t reference_len = -1;
    int64_t fast_len = -1;
    const int reference_ret = ObCharset::first_valid_char(
        ObCollationType::CS_TYPE_UTF8MB4_BIN, input.bytes, input.size, reference_len);
    EXPECT_EQ(reference_ret, ObFTCharUtil::first_valid_char_fast(
        ObCollationType::CS_TYPE_UTF8MB4_BIN, input.bytes, input.size, fast_len));
    EXPECT_EQ(reference_len, fast_len);
  }
}

TEST_F(TestFtsCharClassify, unretained_charsets_preserve_generic_errors)
{
  const ObCollationType collations[] = {CS_TYPE_UTF16_BIN, CS_TYPE_UTF16LE_BIN};
  const char bytes[] = {'A', 0, 'B', 0};
  for (ObCollationType coll : collations) {
    int64_t reference_len = -1;
    int64_t fast_len = -1;
    const int reference_ret = ObCharset::first_valid_char(coll, bytes, sizeof(bytes), reference_len);
    // This repository retains UTF8MB4 and binary collations, not UTF-16.
    EXPECT_EQ(OB_NOT_SUPPORTED, reference_ret);
    EXPECT_EQ(reference_ret, ObFTCharUtil::first_valid_char_fast(coll, bytes, sizeof(bytes), fast_len));
    EXPECT_EQ(reference_len, fast_len);
    CharType type = CharType::USELESS;
    EXPECT_EQ(OB_NOT_SUPPORTED, ObFTCharUtil::classify_first_char(coll, bytes, 2, type));
  }
}

TEST_F(TestFtsCharClassify, cn_number_predicate)
{
  const char *numbers[] = {"一", "二", "三", "四", "五", "六", "七", "八",
                           "九", "十", "百", "千", "万", "亿", "零"};
  for (const char *number : numbers) {
    bool is_cn_number = false;
    EXPECT_EQ(OB_SUCCESS,
              ObFTCharUtil::check_cn_number(ObCollationType::CS_TYPE_UTF8MB4_BIN,
                                            number,
                                            static_cast<uint8_t>(strlen(number)),
                                            is_cn_number));
    EXPECT_TRUE(is_cn_number) << number;
  }

  bool is_cn_number = true;
  EXPECT_EQ(OB_SUCCESS,
            ObFTCharUtil::check_cn_number(ObCollationType::CS_TYPE_UTF8MB4_BIN, "a", 1, is_cn_number));
  EXPECT_FALSE(is_cn_number);
}

TEST_F(TestFtsCharClassify, connector_predicates)
{
  bool is_connector = false;
  EXPECT_EQ(OB_SUCCESS,
            ObFTCharUtil::check_num_connector(ObCollationType::CS_TYPE_UTF8MB4_BIN, ",", 1, is_connector));
  EXPECT_TRUE(is_connector);

  is_connector = true;
  EXPECT_EQ(OB_SUCCESS,
            ObFTCharUtil::check_num_connector(ObCollationType::CS_TYPE_UTF8MB4_BIN, "a", 1, is_connector));
  EXPECT_FALSE(is_connector);

  EXPECT_EQ(OB_SUCCESS,
            ObFTCharUtil::check_letter_connector(ObCollationType::CS_TYPE_UTF8MB4_BIN,
                                                 "#",
                                                 1,
                                                 is_connector));
  EXPECT_TRUE(is_connector);

  is_connector = true;
  EXPECT_EQ(OB_SUCCESS,
            ObFTCharUtil::check_letter_connector(ObCollationType::CS_TYPE_UTF8MB4_BIN,
                                                 "a",
                                                 1,
                                                 is_connector));
  EXPECT_FALSE(is_connector);
}

TEST_F(TestFtsCharClassify, ignore_single_cjk_predicate)
{
  bool is_ignore = false;
  // U+FF0C, full width comma, it is dropped when it is output alone
  EXPECT_EQ(OB_SUCCESS,
            ObFTCharUtil::is_ignore_single_cjk(ObCollationType::CS_TYPE_UTF8MB4_BIN,
                                               "\xef\xbc\x8c",
                                               3,
                                               is_ignore));
  EXPECT_TRUE(is_ignore);

  is_ignore = true;
  // U+4E2D
  EXPECT_EQ(OB_SUCCESS,
            ObFTCharUtil::is_ignore_single_cjk(ObCollationType::CS_TYPE_UTF8MB4_BIN,
                                               "\xe4\xb8\xad",
                                               3,
                                               is_ignore));
  EXPECT_FALSE(is_ignore);
}

// The properties of a parser are parsed from a json string and the result is
// cached. Make sure that the cached result is the same as the parsed one.
class TestFtsParserProperty : public ::testing::Test
{
protected:
  static ObFTParser make_ik_parser()
  {
    ObFTParser parser;
    EXPECT_EQ(OB_SUCCESS, parser.init(ObString("ik")));
    return parser;
  }
};

TEST_F(TestFtsParserProperty, ik_properties)
{
  ObFTParser parser = make_ik_parser();
  ObFTParserProperty property;
  ObString props("{\"ik_mode\":\"SMART\"}");
  ASSERT_EQ(OB_SUCCESS, property.parse_for_parser_helper(parser, props));
  EXPECT_TRUE(property.ik_mode_smart_);
  EXPECT_EQ(ObString(ObFTSLiteral::CONFIG_NAME_DICT_TABLE), property.dict_table_);
  EXPECT_EQ(ObString(ObFTSLiteral::CONFIG_NAME_STOPWORD_TABLE), property.stopword_table_);
  EXPECT_EQ(ObString(ObFTSLiteral::CONFIG_NAME_QUANTIFIER_TABLE), property.quantifier_table_);

  // The second call is served by the cache and has to return the same result.
  ObFTParserProperty cached_property;
  ASSERT_EQ(OB_SUCCESS, cached_property.parse_for_parser_helper(parser, props));
  EXPECT_TRUE(cached_property.is_equal(property));
}

TEST_F(TestFtsParserProperty, ik_max_word_mode)
{
  ObFTParser parser = make_ik_parser();
  ObFTParserProperty property;
  ObString props("{\"ik_mode\":\"MAX_WORD\"}");
  ASSERT_EQ(OB_SUCCESS, property.parse_for_parser_helper(parser, props));
  EXPECT_FALSE(property.ik_mode_smart_);
}

TEST(TestFtsIkArbitrator, document_local_chains_preserve_tokens_and_reset)
{
  ObArenaAllocator allocator;
  ObIKArbitrator arbitrator;
  const char *text = "abc def";
  for (bool smart : {false, true}) {
    TokenizeContext ctx(CS_TYPE_UTF8MB4_BIN, allocator, text, 7, smart);
    ASSERT_EQ(OB_SUCCESS, ctx.init());
    ObIKToken first{text, 0, 3, 3, ObIKTokenType::IK_ENGLISH_TOKEN};
    ObIKToken second{text, 4, 3, 3, ObIKTokenType::IK_ENGLISH_TOKEN};
    ASSERT_EQ(OB_SUCCESS, ctx.token_list().add_token(first));
    ASSERT_EQ(OB_SUCCESS, ctx.token_list().add_token(second));
    ASSERT_EQ(OB_SUCCESS, arbitrator.process(ctx));
    ASSERT_EQ(OB_SUCCESS, arbitrator.output_result(ctx));
    ASSERT_EQ(2, ctx.result_list().size());
    EXPECT_EQ(0, ctx.result_list().get_first().offset_);
    EXPECT_EQ(4, ctx.result_list().get_last().offset_);
    EXPECT_EQ(3, ctx.result_list().get_first().length_);
    EXPECT_EQ(3, ctx.result_list().get_last().length_);
    arbitrator.reset();
  }
}

TEST(TestFtsIkArbitrator, uncovered_chinese_characters_and_punctuation)
{
  ObArenaAllocator allocator;
  const char *text = "\xe4\xb8\xad\xe6\x96\x87\xe3\x80\x82";
  TokenizeContext ctx(CS_TYPE_UTF8MB4_BIN, allocator, text, 9, true);
  ObIKArbitrator arbitrator;
  ASSERT_EQ(OB_SUCCESS, ctx.init());
  ASSERT_EQ(OB_SUCCESS, arbitrator.process(ctx));
  ASSERT_EQ(OB_SUCCESS, arbitrator.output_result(ctx));
  ASSERT_EQ(2, ctx.result_list().size());
  EXPECT_EQ(0, ctx.result_list().get_first().offset_);
  EXPECT_EQ(3, ctx.result_list().get_last().offset_);
  EXPECT_EQ(ObIKTokenType::IK_CHINESE_TOKEN, ctx.result_list().get_first().type_);
}

TEST(TestFtsWordGrouping, preserves_latest_collation_equal_key_and_frequency)
{
  ObArenaAllocator allocator;
  ObFTWordMap words;
  ASSERT_EQ(OB_SUCCESS, words.create(2, ObMemAttr("FTWordMap")));
  ObObjMeta meta;
  meta.set_varchar();
  meta.set_collation_type(CS_TYPE_UTF8MB4_GENERAL_CI);
  ObFTParserProperty property;
  ObAddWordFlag flag;
  flag.set_groupby_word();
  ObAddWord add_word(property, meta, flag, allocator, words);
  ASSERT_EQ(OB_SUCCESS, add_word.process_word("Alpha", 5, 5, 7));
  ASSERT_EQ(OB_SUCCESS, add_word.process_word("aLPHA", 5, 5, 5));
  ASSERT_EQ(1, words.size());
  EXPECT_EQ(6, words.begin()->second); // Existing first occurrence counts as one.
  EXPECT_EQ(ObString("aLPHA"), words.begin()->first.get_word().get_string());
  EXPECT_EQ(12, add_word.get_add_word_count());
}

TEST(TestFtsWordGrouping, updates_colliding_keys_without_reordering)
{
  ObArenaAllocator allocator;
  ObFTWordMap words;
  ASSERT_EQ(OB_SUCCESS, words.create(2, ObMemAttr("FTWordMap")));
  ObObjMeta meta;
  meta.set_varchar();
  meta.set_collation_type(CS_TYPE_UTF8MB4_BIN);
  ObFTParserProperty property;
  ObAddWordFlag flag;
  flag.set_groupby_word();
  ObAddWord add_word(property, meta, flag, allocator, words);
  std::vector<std::string> keys;
  keys.reserve(100);
  for (int i = 0; i < 100; ++i) {
    keys.push_back("word" + std::to_string(i));
    const std::string &key = keys.back();
    ASSERT_EQ(OB_SUCCESS, add_word.process_word(key.data(), key.size(), key.size(), 1));
  }
  std::vector<std::string> order;
  for (auto it = words.begin(); it != words.end(); ++it) {
    const ObString key = it->first.get_word().get_string();
    order.emplace_back(key.ptr(), key.length());
  }
  for (int pass = 0; pass < 5; ++pass) {
    for (const std::string &key : keys) {
      ASSERT_EQ(OB_SUCCESS, add_word.process_word(key.data(), key.size(), key.size(), 3));
    }
  }
  int index = 0;
  for (auto it = words.begin(); it != words.end(); ++it) {
    const ObString key = it->first.get_word().get_string();
    EXPECT_EQ(order[index++], std::string(key.ptr(), key.length()));
    EXPECT_EQ(16, it->second);
  }
}

TEST(TestFtsWordGrouping, ungrouped_duplicate_still_returns_hash_exist)
{
  ObArenaAllocator allocator;
  ObFTWordMap words;
  ASSERT_EQ(OB_SUCCESS, words.create(2, ObMemAttr("FTWordMap")));
  ObObjMeta meta;
  meta.set_varchar();
  meta.set_collation_type(CS_TYPE_UTF8MB4_BIN);
  ObFTParserProperty property;
  ObAddWordFlag flag;
  ObAddWord add_word(property, meta, flag, allocator, words);
  ASSERT_EQ(OB_SUCCESS, add_word.process_word("word", 4, 4, 1));
  EXPECT_EQ(OB_HASH_EXIST, add_word.process_word("word", 4, 4, 1));
  EXPECT_EQ(1, words.begin()->second);
}


TEST(TestFtsStopwords, collations_match_live_buffer_conversion_reference)
{
  ObStopWordChecker checker;
  ASSERT_EQ(OB_SUCCESS, checker.init());
  ObObjMeta target;
  target.set_varchar();
  target.set_collation_type(CS_TYPE_UTF8MB4_GENERAL_CI);
  const std::vector<std::string> words = {
      "the", "THE", "and", "database", "Database", "audit", "\xe4\xb8\xad\xe6\x96\x87",
      "\xc3\x89" "cole", "\xef\xbc\xa1", std::string("a\0b", 3),
      std::string(8192, 'a'),
  };
  for (ObCollationType coll : {CS_TYPE_UTF8MB4_BIN, CS_TYPE_UTF8MB4_GENERAL_CI,
                              CS_TYPE_UTF8MB4_UNICODE_CI, CS_TYPE_BINARY}) {
    ObObjMeta source = target;
    source.set_collation_type(coll);
    for (const auto &text : words) {
      for (int pass = 0; pass < 3; ++pass) {
        ObArenaAllocator allocator;
        ObString converted;
        const int conversion_ret = ObCharset::charset_convert(allocator,
            ObString(text.size(), text.data()), coll, CS_TYPE_UTF8MB4_GENERAL_CI, converted);
        bool expected = false;
        int expected_ret = conversion_ret;
        if (OB_SUCCESS == conversion_ret) {
          expected_ret = checker.check_stopword(
              ObFTWord(converted.length(), converted.ptr(), target), expected);
        }
        bool actual = false;
        EXPECT_EQ(expected_ret, checker.check_stopword(
            ObFTWord(text.size(), text.data(), source), actual));
        if (OB_SUCCESS == expected_ret) EXPECT_EQ(expected, actual);
      }
    }
  }
}

} // namespace storage
} // namespace oceanbase
