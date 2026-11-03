#pragma once
#include <cstdint>

namespace oceanbase { namespace sql {
class ObSQLSessionInfo;
class AiColumnCheck
{
public:
  using ReadCheck = int (*)(ObSQLSessionInfo &, uint64_t, uint64_t);
  static inline ReadCheck read_check = nullptr;
  static inline ReadCheck assignment_check = nullptr;
  static int check(ObSQLSessionInfo &session, uint64_t table, uint64_t column)
  { return read_check == nullptr ? 0 : read_check(session, table, column); }
  static int check_assignment(ObSQLSessionInfo &session, uint64_t table, uint64_t column)
  { return assignment_check == nullptr ? 0 : assignment_check(session, table, column); }
};
} }
