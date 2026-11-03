#pragma once

#include <algorithm>
#include <cctype>
#include <stdexcept>
#include <string>
#include <vector>

namespace oceanbase { namespace sql { namespace ai_column {

// Token offsets always refer to the original SQL. In particular, literals,
// quoted identifiers and comments are never searched for keywords.
struct Token
{
  std::string text;
  size_t begin = 0;
  size_t end = 0;
  char quote = 0;
  bool is(const char *word) const
  {
    if (quote != 0 || text.size() != std::char_traits<char>::length(word)) return false;
    for (size_t i = 0; i < text.size(); ++i) {
      if (std::tolower(static_cast<unsigned char>(text[i])) != word[i]) return false;
    }
    return true;
  }
  bool identifier() const
  {
    return quote == '`' || (quote == 0 && !text.empty() &&
        (std::isalpha(static_cast<unsigned char>(text[0])) || text[0] == '_' ||
         static_cast<unsigned char>(text[0]) >= 128));
  }
};

class LexerError : public std::invalid_argument
{
public:
  explicit LexerError(const char *message) : std::invalid_argument(message) {}
};

inline std::vector<Token> tokenize(const std::string &sql, bool backslash_escapes = true,
                                   size_t limit = std::string::npos)
{
  std::vector<Token> out;
  for (size_t i = 0; i < sql.size();) {
    unsigned char c = sql[i];
    if (std::isspace(c)) { ++i; continue; }
    if (c == '#' || (c == '-' && i + 2 < sql.size() && sql[i + 1] == '-' &&
                    std::isspace(static_cast<unsigned char>(sql[i + 2])))) {
      while (i < sql.size() && sql[i] != '\n') ++i;
      continue;
    }
    if (c == '/' && i + 1 < sql.size() && sql[i + 1] == '*') {
      size_t end = sql.find("*/", i + 2);
      if (end == std::string::npos) throw LexerError("unterminated SQL comment");
      i = end + 2;
      continue;
    }
    Token t;
    t.begin = i;
    if (c == '\'' || c == '"' || c == '`') {
      t.quote = c;
      ++i;
      bool closed = false;
      while (i < sql.size()) {
        char v = sql[i++];
        if (v == c) {
          if (i < sql.size() && sql[i] == c) { t.text += v; ++i; }
          else { closed = true; break; }
        } else if (v == '\\' && backslash_escapes && c != '`' && i < sql.size()) {
          t.text += sql[i++];
        } else { t.text += v; }
      }
      if (!closed) throw LexerError("unterminated SQL string");
    } else if (std::isalnum(c) || c == '_' || c == '$' || c >= 128) {
      while (i < sql.size()) {
        unsigned char v = sql[i];
        if (!std::isalnum(v) && v != '_' && v != '$' && v < 128) break;
        t.text += sql[i++];
      }
    } else { t.text += sql[i++]; }
    t.end = i;
    out.push_back(std::move(t));
    if (out.size() >= limit) break;
  }
  return out;
}

inline std::string identifier(const std::string &v)
{
  std::string out = "`";
  for (char c : v) { out += c; if (c == '`') out += c; }
  return out + "`";
}

// Hex literals are independent of NO_BACKSLASH_ESCAPES and client escaping.
inline std::string literal(const std::string &v)
{
  static const char digits[] = "0123456789abcdef";
  std::string out = "CONVERT(X'";
  for (unsigned char c : v) { out += digits[c >> 4]; out += digits[c & 15]; }
  return out + "' USING utf8mb4)";
}

inline size_t matching(const std::vector<Token> &t, size_t open)
{
  if (open >= t.size() || !t[open].is("(")) throw std::invalid_argument("expected opening parenthesis");
  int depth = 0;
  for (size_t i = open; i < t.size(); ++i) {
    if (t[i].is("(")) ++depth;
    else if (t[i].is(")") && --depth == 0) return i;
  }
  throw std::invalid_argument("unbalanced AI column definition");
}

struct Definition
{
  std::string name;
  std::string type;
  std::string expression;
  std::string model;
  std::string input;
  std::string prompt;
  std::vector<std::string> dependencies;
  int retries = 2;
  int dimension = 0;
  bool embedding = false;
  size_t begin = 0;
  size_t end = 0;
};

class DimensionError : public std::range_error
{
public:
  DimensionError(int expected_value, int actual_value)
      : std::range_error("inconsistent AI embedding dimension"), expected(expected_value), actual(actual_value) {}
  int expected;
  int actual;
};

class DependencyError : public std::logic_error
{
public:
  DependencyError() : std::logic_error("AI column dependency") {}
};

inline std::string type_prompt(const std::string &type)
{
  auto t = tokenize(type);
  if (t.empty()) throw std::invalid_argument("missing AI result type");
  if (t[0].is("json")) return "Return exactly one valid JSON value. Do not include Markdown or commentary.";
  if (t[0].is("bigint") || t[0].is("int") || t[0].is("integer") || t[0].is("smallint") || t[0].is("tinyint"))
    return "Return exactly one base-10 integer. Do not include Markdown or commentary.";
  if (t[0].is("double") || t[0].is("float") || t[0].is("decimal"))
    return "Return exactly one finite number. Do not include Markdown or commentary.";
  if (t[0].is("varchar") || t[0].is("char") || t[0].is("text") || t[0].is("longtext")) return "Return only the requested text.";
  if (t[0].is("vector")) return "";
  throw std::invalid_argument("unsupported AI result type");
}

inline Definition parse_definition(const std::string &sql, const std::vector<Token> &t,
                                   size_t first, size_t ai)
{
  Definition d;
  if (ai <= first + 1 || !t[first].identifier() || ai + 2 >= t.size() ||
      !t[ai].is("ai") || !t[ai + 1].is("column")) throw std::invalid_argument("invalid AI column definition");
  d.name = t[first].text;
  d.type = sql.substr(t[first + 1].begin, t[ai].begin - t[first + 1].begin);
  d.prompt = type_prompt(d.type);
  d.begin = t[ai].begin;
  const size_t expr_end = matching(t, ai + 2);
  const size_t fun = ai + 3;
  if (fun + 3 >= expr_end || (!t[fun].is("ai_complete") && !t[fun].is("ai_embed")) ||
      !t[fun + 1].is("(")) throw std::invalid_argument("AI column must contain one AI_COMPLETE or AI_EMBED call");
  const size_t call_end = matching(t, fun + 1);
  if (call_end + 1 != expr_end || t[fun + 2].quote != '\'' || !t[fun + 3].is(","))
    throw std::invalid_argument("AI column model must be a string literal");
  d.embedding = t[fun].is("ai_embed");
  d.model = t[fun + 2].text;
  d.expression = sql.substr(t[fun].begin, t[call_end].end - t[fun].begin);
  size_t input_end = call_end;
  int input_depth = 0;
  for (size_t i = fun + 4; i < call_end; ++i) {
    if (t[i].is("ai_complete") || t[i].is("ai_embed") || t[i].is("ai_rerank") || t[i].is("select") || t[i].is("@"))
      throw std::invalid_argument("invalid AI input expression");
    if (t[i].is("(")) ++input_depth;
    else if (t[i].is(")")) --input_depth;
    else if (t[i].is(",") && input_depth == 0) { input_end = i; break; }
  }
  if (input_end == fun + 4) throw std::invalid_argument("missing AI input");
  d.input = sql.substr(t[fun + 4].begin, t[input_end - 1].end - t[fun + 4].begin);
  if (input_end != call_end && (!d.embedding || input_end + 2 != call_end))
    throw std::invalid_argument("unsupported AI function arguments");
  if (d.embedding) {
    auto ty = tokenize(d.type);
    if (ty.size() != 4 || !ty[0].is("vector") || !ty[1].is("(") || !ty[3].is(")"))
      throw std::invalid_argument("AI_EMBED requires a VECTOR result");
    d.dimension = std::stoi(ty[2].text);
    if (d.dimension < 1 || d.dimension > 16000) throw std::invalid_argument("invalid vector dimension");
    if (input_end != call_end && std::stoi(t[input_end + 1].text) != d.dimension)
      throw DimensionError(d.dimension, std::stoi(t[input_end + 1].text));
  } else if (tokenize(d.type)[0].is("vector")) {
    throw std::invalid_argument("VECTOR AI columns require AI_EMBED");
  }
  size_t end = expr_end;
  if (end + 1 < t.size() && t[end + 1].is("with")) {
    size_t opts = end + 2;
    size_t close = matching(t, opts);
    bool seen_mode = false, seen_retry = false;
    for (size_t i = opts + 1; i < close;) {
      if (i + 2 >= close || !t[i + 1].is("=")) throw std::invalid_argument("invalid AI option");
      if (t[i].is("sync_mode") && !seen_mode) {
        if (t[i + 2].quote != '\'' || t[i + 2].text != "async") throw std::invalid_argument("only asynchronous AI columns are supported");
        seen_mode = true;
      } else if (t[i].is("max_retries") && !seen_retry) {
        if (t[i + 2].quote || t[i + 2].text.find_first_not_of("0123456789") != std::string::npos)
          throw std::invalid_argument("max_retries must be a nonnegative integer");
        d.retries = std::stoi(t[i + 2].text);
        if (d.retries > 100) throw std::invalid_argument("max_retries exceeds 100");
        seen_retry = true;
      } else throw std::invalid_argument("unknown or duplicate AI option");
      i += 3;
      if (i < close) { if (!t[i].is(",")) throw std::invalid_argument("invalid AI options"); ++i; }
    }
    end = close;
  }
  d.end = t[end].end;
  return d;
}

inline std::string qualify_input(const Definition &d, const std::vector<std::string> &columns,
                                 const std::string &qualifier)
{
  const auto tokens = tokenize(d.input);
  std::string out;
  size_t previous = 0;
  for (size_t i = 0; i < tokens.size(); ++i) {
    const auto &t = tokens[i];
    out += d.input.substr(previous, t.begin - previous);
    bool found = false;
    for (const auto &name : columns) {
      if (name.size() != t.text.size()) continue;
      bool equal = true;
      for (size_t j = 0; j < name.size(); ++j) if (std::tolower(static_cast<unsigned char>(name[j])) !=
          std::tolower(static_cast<unsigned char>(t.text[j]))) equal = false;
      if (equal) { found = true; break; }
    }
    bool is_column = t.identifier() && found
                     && (i + 1 == tokens.size() || !tokens[i + 1].is("("));
    out += is_column ? qualifier + identifier(t.text) : d.input.substr(t.begin, t.end - t.begin);
    previous = t.end;
  }
  return out + d.input.substr(previous);
}

} } }
