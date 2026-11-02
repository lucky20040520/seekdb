/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */

#pragma once

#include <fcntl.h>
#include <linux/openat2.h>
#include <limits.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <cstdlib>
#include <string>

namespace oceanbase
{
namespace sql
{

// Linux only. The caller must also enforce the enable_file_sql session flag.
// All path traversal is rooted at a pinned directory descriptor and rejects
// symlinks. A missing openat2 syscall is an error, never an insecure fallback.
class FileSecureHandle
{
public:
  FileSecureHandle() = default;
  ~FileSecureHandle() { close(); }
  FileSecureHandle(const FileSecureHandle &) = delete;
  FileSecureHandle &operator=(const FileSecureHandle &) = delete;

  bool open(const std::string &secure_dir, const std::string &path)
  {
    close();
    if (secure_dir.empty() || is_null_setting(secure_dir) || path.empty() || path[0] != '/' ||
        secure_dir.find('\0') != std::string::npos || path.find('\0') != std::string::npos) return false;
    char canonical[PATH_MAX];
    if (::realpath(secure_dir.c_str(), canonical) == nullptr) return false;
    root_path_ = canonical;
    if (root_path_ != "/") {
      if (path.size() <= root_path_.size() || path.compare(0, root_path_.size(), root_path_) != 0 ||
          path[root_path_.size()] != '/') return false;
      relative_path_ = path.substr(root_path_.size() + 1);
    } else {
      relative_path_ = path.substr(1);
    }
    if (relative_path_.empty()) return false;
    root_fd_ = open_root();
    if (root_fd_ < 0 || ::fstat(root_fd_, &root_stat_) != 0) { close(); return false; }
    file_fd_ = open_beneath(root_fd_, relative_path_.c_str(), O_RDONLY | O_NONBLOCK | O_CLOEXEC);
    if (file_fd_ < 0 || ::fstat(file_fd_, &file_stat_) != 0 || !S_ISREG(file_stat_.st_mode)) {
      close();
      return false;
    }
    return true;
  }

  int descriptor() const { return file_fd_; }

  // Check both the open inode and its directory entry, so replacement and
  // in-place modification fail rather than silently changing the scan input.
  bool unchanged() const
  {
    if (file_fd_ < 0 || root_fd_ < 0) return false;
    struct stat now_root, now_file, named_file;
    const int current_root = open_root();
    if (current_root < 0) return false;
    const bool same_root = ::fstat(current_root, &now_root) == 0 &&
        now_root.st_dev == root_stat_.st_dev && now_root.st_ino == root_stat_.st_ino;
    ::close(current_root);
    if (!same_root || ::fstat(file_fd_, &now_file) != 0 || !same_file(file_stat_, now_file)) return false;
    const int named_fd = open_beneath(root_fd_, relative_path_.c_str(), O_RDONLY | O_NONBLOCK | O_CLOEXEC);
    if (named_fd < 0) return false;
    const bool same = ::fstat(named_fd, &named_file) == 0 && same_file(file_stat_, named_file);
    ::close(named_fd);
    return same;
  }

  void close()
  {
    if (file_fd_ >= 0) ::close(file_fd_);
    if (root_fd_ >= 0) ::close(root_fd_);
    file_fd_ = -1;
    root_fd_ = -1;
    relative_path_.clear();
    root_path_.clear();
  }

private:
  static bool is_null_setting(const std::string &value)
  {
    if (value.size() != 4) return false;
    return (value[0] == 'n' || value[0] == 'N') && (value[1] == 'u' || value[1] == 'U') &&
        (value[2] == 'l' || value[2] == 'L') && (value[3] == 'l' || value[3] == 'L');
  }

  static int open_beneath(int directory, const char *path, int flags)
  {
    struct open_how options = {};
    options.flags = static_cast<unsigned long long>(flags);
    options.resolve = RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS;
    return static_cast<int>(::syscall(SYS_openat2, directory, path, &options, sizeof(options)));
  }

  int open_root() const
  {
    const int filesystem_root = ::open("/", O_PATH | O_DIRECTORY | O_CLOEXEC);
    if (filesystem_root < 0) return -1;
    const char *relative = root_path_ == "/" ? "." : root_path_.c_str() + 1;
    const int result = open_beneath(filesystem_root, relative, O_PATH | O_DIRECTORY | O_CLOEXEC);
    ::close(filesystem_root);
    return result;
  }

  static bool same_file(const struct stat &a, const struct stat &b)
  {
    return S_ISREG(b.st_mode) && a.st_dev == b.st_dev && a.st_ino == b.st_ino && a.st_size == b.st_size &&
        a.st_mtim.tv_sec == b.st_mtim.tv_sec && a.st_mtim.tv_nsec == b.st_mtim.tv_nsec &&
        a.st_ctim.tv_sec == b.st_ctim.tv_sec && a.st_ctim.tv_nsec == b.st_ctim.tv_nsec;
  }

  int root_fd_ = -1;
  int file_fd_ = -1;
  struct stat root_stat_ = {};
  struct stat file_stat_ = {};
  std::string root_path_;
  std::string relative_path_;
};

} // namespace sql
} // namespace oceanbase
