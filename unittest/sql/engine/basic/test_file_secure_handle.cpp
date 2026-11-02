/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */

#ifdef NDEBUG
#undef NDEBUG
#endif
#include "sql/engine/basic/file_secure_handle.h"
#include <cassert>
#include <cstring>

using namespace oceanbase::sql;

int main()
{
  char temp[] = "/tmp/seekdb-file-secure-test-XXXXXX";
  const char *directory = ::mkdtemp(temp);
  assert(directory != nullptr);
  const std::string root(directory);
  const std::string path = root + "/file.csv";
  const std::string link = root + "/link.csv";
  const std::string moved = root + "/old.csv";
  const int fd = ::open(path.c_str(), O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC, 0600);
  assert(fd >= 0 && ::write(fd, "data", 4) == 4 && ::close(fd) == 0);
  assert(::symlink("/etc/passwd", link.c_str()) == 0);
  {
    FileSecureHandle file;
    assert(!file.open("NULL", path));
    assert(!file.open("null", path));
    assert(!file.open(root, link));
    assert(!file.open(root, root + "/../etc/passwd"));
    assert(!file.open(root, "/etc/passwd"));
    assert(file.open(root, path) && file.unchanged());
    char buffer[4];
    assert(::read(file.descriptor(), buffer, sizeof(buffer)) == 4 && std::memcmp(buffer, "data", 4) == 0);
    assert(::rename(path.c_str(), moved.c_str()) == 0);
    const int replacement = ::open(path.c_str(), O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC, 0600);
    assert(replacement >= 0 && ::write(replacement, "data", 4) == 4 && ::close(replacement) == 0);
    assert(!file.unchanged());
  }
  assert(::unlink(link.c_str()) == 0);
  assert(::unlink(path.c_str()) == 0);
  assert(::unlink(moved.c_str()) == 0);
  assert(::rmdir(root.c_str()) == 0);
  return 0;
}
