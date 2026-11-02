/* Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0. */
#pragma once

#include "sql/engine/basic/file_csv_scan.h"
#include <dirent.h>
#include <fnmatch.h>
#include <strings.h>
#include <cerrno>

namespace oceanbase
{
namespace sql
{

// Walk each pattern component below a pinned directory descriptor. Symlinks
// are never followed, including on intermediate path components.
class FileGlobScan
{
public:
  FileGlobScan() = default;
  ~FileGlobScan() { close(); }
  FileGlobScan(const FileGlobScan &) = delete;
  FileGlobScan &operator=(const FileGlobScan &) = delete;

  FileCsvScanStatus open(bool enabled, const std::string &secure_dir, const std::string &pattern)
  {
    close();
    components_.clear();
    columns_.clear();
    if (!enabled) return FileCsvScanStatus::DISABLED;
    if (secure_dir.empty() || pattern.empty() || pattern[0] != '/' ||
        secure_dir.find('\0') != std::string::npos || pattern.find('\0') != std::string::npos ||
        (secure_dir.size() == 4 && strcasecmp(secure_dir.c_str(), "NULL") == 0)) {
      return FileCsvScanStatus::ACCESS_DENIED;
    }
    char canonical[PATH_MAX];
    if (::realpath(secure_dir.c_str(), canonical) == nullptr) return FileCsvScanStatus::ACCESS_DENIED;
    root_ = canonical;
    size_t begin = 1;
    if (root_ != "/") {
      if (pattern.size() <= root_.size() || pattern.compare(0, root_.size(), root_) != 0 ||
          pattern[root_.size()] != '/') return FileCsvScanStatus::ACCESS_DENIED;
      begin = root_.size() + 1;
    }
    while (begin < pattern.size()) {
      const size_t end = pattern.find('/', begin);
      const std::string part = pattern.substr(begin, end == std::string::npos ? end : end - begin);
      if (part.empty() || part == "." || part == ".." || components_.size() >= 64) {
        return FileCsvScanStatus::ACCESS_DENIED;
      }
      components_.push_back(part);
      if (end == std::string::npos) break;
      begin = end + 1;
    }
    if (components_.empty() || pattern.back() == '/') return FileCsvScanStatus::INVALID;
    FileCsvColumn column;
    column.name = "file";
    column.inference.add_type(FileValueType::VARCHAR);
    columns_.push_back(std::move(column));
    return rescan();
  }

  FileCsvScanStatus rescan()
  {
    close();
    const int fs = ::open("/", O_PATH | O_DIRECTORY | O_CLOEXEC);
    if (fs < 0) return FileCsvScanStatus::ACCESS_DENIED;
    const char *path = root_ == "/" ? "." : root_.c_str() + 1;
    const int fd = open_beneath(fs, path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    ::close(fs);
    if (fd < 0) return FileCsvScanStatus::ACCESS_DENIED;
    DIR *dir = ::fdopendir(fd);
    if (dir == nullptr) { ::close(fd); return FileCsvScanStatus::IO_ERROR; }
    stack_.push_back({dir, root_, 0});
    return FileCsvScanStatus::OK;
  }

  FileCsvScanStatus next(std::vector<FileCsvField> &row)
  {
    while (!stack_.empty()) {
      Frame &frame = stack_.back();
      errno = 0;
      const dirent *entry = ::readdir(frame.dir);
      if (entry == nullptr) {
        const int error = errno;
        ::closedir(frame.dir);
        stack_.pop_back();
        if (error != 0) return FileCsvScanStatus::IO_ERROR;
        continue;
      }
      if (::fnmatch(components_[frame.index].c_str(), entry->d_name, FNM_PERIOD) != 0 ||
          std::string_view(entry->d_name) == "." || std::string_view(entry->d_name) == "..") continue;
      const std::string name = frame.path == "/" ? frame.path + entry->d_name : frame.path + "/" + entry->d_name;
      if (name.size() >= PATH_MAX) return FileCsvScanStatus::INVALID;
      if (frame.index + 1 == components_.size()) {
        FileSecureHandle secure;
        if (!secure.open(root_, name)) continue;
        row.clear();
        FileCsvField field;
        field.value = name;
        field.quoted = true;
        row.push_back(std::move(field));
        return FileCsvScanStatus::ROW;
      }
      const size_t index = frame.index + 1;
      const int fd = open_beneath(::dirfd(frame.dir), entry->d_name, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
      if (fd < 0) continue;
      DIR *dir = ::fdopendir(fd);
      if (dir == nullptr) { ::close(fd); return FileCsvScanStatus::IO_ERROR; }
      stack_.push_back({dir, name, index});
    }
    return FileCsvScanStatus::END;
  }

  const std::vector<FileCsvColumn> &columns() const { return columns_; }

private:
  struct Frame { DIR *dir; std::string path; size_t index; };
  static int open_beneath(int fd, const char *path, int flags)
  {
    struct open_how options = {};
    options.flags = static_cast<unsigned long long>(flags);
    options.resolve = RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS;
    return static_cast<int>(::syscall(SYS_openat2, fd, path, &options, sizeof(options)));
  }
  void close()
  {
    for (auto &frame : stack_) ::closedir(frame.dir);
    stack_.clear();
  }
  std::string root_;
  std::vector<std::string> components_;
  std::vector<Frame> stack_;
  std::vector<FileCsvColumn> columns_;
};

} // namespace sql
} // namespace oceanbase
