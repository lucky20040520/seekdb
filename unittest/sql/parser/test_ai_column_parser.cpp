#ifdef NDEBUG
#undef NDEBUG
#endif
#include "sql/parser/ai_column_definition.h"
#include <cassert>
#include <iostream>
#include <random>

using namespace oceanbase::sql::ai_column;

static Definition parse(const std::string &sql)
{
  auto t = tokenize(sql);
  for (size_t i = 0; i + 1 < t.size(); ++i) {
    if (t[i].is("ai") && t[i + 1].is("column")) return parse_definition(sql, t, 0, i);
  }
  throw std::invalid_argument("missing AI COLUMN");
}

int main()
{
  const auto d = parse("`分类` JSON AI /* comment */ COLUMN (AI_COMPLETE('model', CONCAT('AI COLUMN (,)', content))) WITH (sync_mode='async', max_retries=0)");
  assert(d.name == "分类" && d.model == "model" && d.retries == 0);
  assert(d.input == "CONCAT('AI COLUMN (,)', content)");
  assert(d.prompt.find("valid JSON value") != std::string::npos);
  const auto v = parse("emb VECTOR(4) AI COLUMN (AI_EMBED('embed', content, 4))");
  assert(v.dimension == 4 && v.embedding);
  assert(parse("score BIGINT AI COLUMN (AI_COMPLETE('model', content))").prompt.find("base-10 integer") != std::string::npos);
  assert(qualify_input(d, {"content"}, "NEW.") == "CONCAT('AI COLUMN (,)', NEW.`content`)");
  assert(identifier("a`b") == "`a``b`");
  assert(literal("'; DROP TABLE t; --").find("DROP") == std::string::npos);
  for (const char *s : {
       "x JSON AI COLUMN (AI_COMPLETE(content, content))",
       "x JSON AI COLUMN (AI_COMPLETE('m', content)) WITH(sync_mode='sync')",
       "x JSON AI COLUMN (AI_COMPLETE('m', content)) WITH(max_retries=-1)",
       "x JSON AI COLUMN (AI_COMPLETE('m', content)) WITH(max_retries=1,max_retries=2)",
       "x JSON AI COLUMN (AI_COMPLETE('m', content)) WITH(unknown=1)",
       "x VECTOR(4) AI COLUMN (AI_EMBED('m', content, 3))",
       "x JSON AI COLUMN (AI_EMBED('m', content))",
       "x JSON AI COLUMN (AI_COMPLETE('m', content) + 1)",
       "x JSON AI COLUMN (AI_COMPLETE('m', concat(AI_EMBED('e',content),content)))",
       "x JSON AI COLUMN (AI_COMPLETE('m', ))"}) {
    bool rejected = false;
    try { (void)parse(s); } catch (const std::exception &) { rejected = true; }
    assert(rejected);
  }
  const auto t = tokenize("select 'AI COLUMN', `AI`, 1 /* AI COLUMN */ -- AI COLUMN\n from docs");
  assert(t.size() == 8 && !t[1].is("ai") && !t[3].is("ai"));
  std::mt19937 random(2026);
  const std::string alphabet = "abcAI_COLUMN'm,()=/*-`\\\" 0123456789\n";
  for (int run = 0; run < 2000; ++run) {
    std::string text;
    for (unsigned i = 0, count = random() % 128; i < count; ++i) text += alphabet[random() % alphabet.size()];
    try {
      const auto tokens = tokenize(text);
      for (size_t i = 0; i + 1 < tokens.size(); ++i) {
        if (tokens[i].is("ai") && tokens[i + 1].is("column")) (void)parse_definition(text, tokens, 0, i);
      }
    } catch (const std::exception &) {}
  }
  std::cout << "AI column parser checks passed\n";
}
