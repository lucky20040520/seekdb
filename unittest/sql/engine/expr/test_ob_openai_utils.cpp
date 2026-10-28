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

#include "ai_test_support.h"
#include <string>

using namespace oceanbase::common;

int run_ai_function_tests()
{
  run_ai_prompt_tests();
  ObArenaAllocator allocator;
  ObOpenAIUtils::ObOpenAIEmbed embed;
  ObIJsonBase *output = nullptr;
  AI_CHECK(OB_SUCCESS == embed.parse_output(allocator,
      ai_test_json(allocator, R"({"data":[{"index":1,"embedding":[3,4]},{"index":0,"embedding":[1,2]}]})"), output));
  ObString serialized;
  AI_CHECK(OB_SUCCESS == ObAIFuncJsonUtils::print_json_to_str(allocator, output, serialized));
  AI_CHECK(std::string(serialized.ptr(), serialized.length()) == "[[1, 2], [3, 4]]");
  const char *invalid[] = {
    R"({"data":{}})", R"({"data":[]})",
    R"({"data":[{"index":0,"embedding":[]}]})",
    R"({"data":[{"index":0,"embedding":[true]}]})",
    R"({"data":[{"index":0,"embedding":[null]}]})",
    R"({"data":[{"index":0,"embedding":["1"]}]})",
    R"({"data":[{"index":-1,"embedding":[1]}]})",
    R"({"data":[{"index":1,"embedding":[1]}]})",
    R"({"data":[{"index":"0","embedding":[1]}]})",
    R"({"data":[{"embedding":[1]}]})",
    R"({"data":[{"index":0,"embedding":[1]},{"index":0,"embedding":[2]}]})",
    R"({"data":[{"index":0,"embedding":[1]},{"index":1,"embedding":[2,3]}]})",
  };
  for (const char *text : invalid) {
    AI_CHECK(OB_SUCCESS != embed.parse_output(allocator, ai_test_json(allocator, text), output));
  }
  ObOllamaUtils::ObOllamaComplete complete;
  ObArray<ObString> headers;
  ObString key;
  AI_CHECK(OB_SUCCESS == complete.get_header(allocator, key, headers));
  AI_CHECK(headers.count() == 1 && headers.at(0) == ObString("Content-Type: application/json"));
  ObString model("arbitrary-model"), system, prompt("arbitrary prompt");
  ObJsonObject *body = nullptr;
  AI_CHECK(OB_SUCCESS == complete.get_body(allocator, model, system, prompt, nullptr, body));
  AI_CHECK(body->get_value("model")->get_data_length() == model.length());
  AI_CHECK(body->get_value("stream")->json_type() == ObJsonNodeType::J_BOOLEAN);
  AI_CHECK(!body->get_value("stream")->get_boolean());
  AI_CHECK(OB_SUCCESS == complete.parse_output(allocator,
      ai_test_json(allocator, R"({"response":"ok"})"), output));
  AI_CHECK(OB_SUCCESS != complete.parse_output(allocator,
      ai_test_json(allocator, R"({"response":""})"), output));
  ObOllamaUtils::ObOllamaEmbed ollama_embed;
  AI_CHECK(OB_SUCCESS == ollama_embed.parse_output(allocator,
      ai_test_json(allocator, R"({"embeddings":[[1,2],[3,4]]})"), output));
  AI_CHECK(OB_SUCCESS != ollama_embed.parse_output(allocator,
      ai_test_json(allocator, R"({"embeddings":[[1,2],[3]]})"), output));
  AI_CHECK(OB_SUCCESS != ollama_embed.parse_output(allocator,
      ai_test_json(allocator, R"({"embeddings":[[null]]})"), output));
  ObOpenAIUtils::ObOpenAIComplete openai_complete;
  const char *bad_completions[] = {
    R"({})", R"({"choices":[]})",
    R"({"choices":[{"message":{"content":""}}]})",
    R"({"choices":[{"message":{"content":null}}]})",
    R"({"choices":[{"message":{"content":{"text":"bad"}}}]})",
  };
  for (const char *text : bad_completions) {
    AI_CHECK(OB_SUCCESS != openai_complete.parse_output(allocator, ai_test_json(allocator, text), output));
  }
  ObJsonObject *config = ai_test_json(allocator,
      R"({"temperature":0.2,"max_tokens":16,"options":{"seed":123},"stream":true})");
  AI_CHECK(OB_SUCCESS == complete.get_body(allocator, model, system, prompt, config, body));
  AI_CHECK(!body->get_value("stream")->get_boolean());
  ObJsonObject *options = static_cast<ObJsonObject *>(body->get_value("options"));
  AI_CHECK(options != nullptr && options->get_value("num_predict")->get_int() == 16);
  AI_CHECK(options->get_value("seed")->get_int() == 123);
  ObDashscopeUtils::ObDashscopeComplete dashscope_complete;
  AI_CHECK(OB_SUCCESS == dashscope_complete.parse_output(allocator,
      ai_test_json(allocator, R"({"output":{"choices":[{"message":{"content":"ok"}}]}})"), output));
  AI_CHECK(OB_SUCCESS != dashscope_complete.parse_output(allocator,
      ai_test_json(allocator, R"({"output":{"choices":{}}})"), output));
  AI_CHECK(OB_SUCCESS != dashscope_complete.parse_output(allocator,
      ai_test_json(allocator, R"({"output":{"choices":[{"message":{"content":null}}]}})"), output));
  ObDashscopeUtils::ObDashscopeEmbed dashscope_embed;
  AI_CHECK(OB_SUCCESS == dashscope_embed.parse_output(allocator,
      ai_test_json(allocator, R"({"output":{"embeddings":[{"embedding":[1,2]}]}})"), output));
  const char *bad_dashscope_embeddings[] = {
    R"({"output":[]})", R"({"output":{"embeddings":{}}})",
    R"({"output":{"embeddings":[1]}})",
    R"({"output":{"embeddings":[{"embedding":null}]}})",
    R"({"output":{"embeddings":[{"embedding":[true]}]}})",
  };
  for (const char *text : bad_dashscope_embeddings) {
    AI_CHECK(OB_SUCCESS != dashscope_embed.parse_output(allocator, ai_test_json(allocator, text), output));
  }
  std::puts("Provider parsing, ordering, vector validation and Ollama protocol: passed");
  return 0;
}

#ifndef AI_FUNCTION_STANDALONE_TEST
#include <gtest/gtest.h>
TEST(AIFunctionEnhancement, PromptAndProviderUtilities)
{
  ASSERT_EQ(0, run_ai_function_tests());
}

int main(int argc, char **argv)
{
  ::testing::InitGoogleTest(&argc, argv);
  return RUN_ALL_TESTS();
}
#endif
