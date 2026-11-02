/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */
#pragma once

#include <fcntl.h>
#include <linux/openat2.h>
#include <limits.h>
#include <strings.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <cstdlib>
#include <string>

namespace oceanbase
{
namespace sql
{

// Create a new regular output under secure_dir without following symlinks or
// overwriting an existing path. The caller owns any partially written file.
class FileSecureOutput
{
public:
  FileSecureOutput() = default;
  ~FileSecureOutput() { close(); }
  FileSecureOutput(const FileSecureOutput &) = delete;
  FileSecureOutput &operator=(const FileSecureOutput &) = delete;

  bool open(bool enabled, const std::string &secure_dir, const std::string &path)
  {
    close();
    if (!enabled || secure_dir.empty() || path.empty() || path[0] != '/' ||
        secure_dir.find('\0') != std::string::npos || path.find('\0') != std::string::npos ||
        (secure_dir.size() == 4 && ::strcasecmp(secure_dir.c_str(), "NULL") == 0)) return false;
    char canonical[PATH_MAX];
    if (::realpath(secure_dir.c_str(), canonical) == nullptr) return false;
    const std::string root(canonical);
    std::string relative;
    if (root == "/") {
      relative = path.substr(1);
    } else if (path.size() > root.size() && path.compare(0, root.size(), root) == 0 && path[root.size()] == '/') {
      relative = path.substr(root.size() + 1);
    } else {
      return false;
    }
    if (relative.empty()) return false;
    const int fs = ::open("/", O_PATH | O_DIRECTORY | O_CLOEXEC);
    if (fs < 0) return false;
    const char *root_relative = root == "/" ? "." : root.c_str() + 1;
    const int root_fd = open_beneath(fs, root_relative, O_PATH | O_DIRECTORY | O_CLOEXEC, 0);
    ::close(fs);
    if (root_fd < 0) return false;
    fd_ = open_beneath(root_fd, relative.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    ::close(root_fd);
    return fd_ >= 0;
  }

  int descriptor() const { return fd_; }
  void close() { if (fd_ >= 0) ::close(fd_); fd_ = -1; }

private:
  static int open_beneath(int directory, const char *path, int flags, mode_t mode)
  {
    struct open_how options = {};
    options.flags = static_cast<unsigned long long>(flags);
    options.mode = mode;
    options.resolve = RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS;
    return static_cast<int>(::syscall(SYS_openat2, directory, path, &options, sizeof(options)));
  }
  int fd_ = -1;
};

} // namespace sql
} // namespace oceanbase
