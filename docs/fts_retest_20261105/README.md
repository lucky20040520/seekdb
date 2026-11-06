# dcabb07 复测记录：Bazel 分析阶段失败

> 以下为初始排查阶段的历史记录。后续已修复构建阻塞并实际执行测试；当前结果见 [构建与测试执行报告](test_execution/README.md)。

日期：2026-11-05。源码：dcabb078a02fdcfe3a8e2e911a2484d15b7f32df。

## 结论

当前版本通过仓库 bazel.py 主入口构建和运行相关测试时，均在目标分析阶段被静态链接约束阻断。未生成 seekdb 生产二进制，未执行测试用例，未启动数据库或性能基准；本轮没有新的耗时、正确性通过数量或性能分数。

已确认的首个阻断点：

~~~text
first-party cc_library must be static-only: @@//src/oblib:_jemalloc_headers
  load //bazel:defs.bzl%seekdb_cc_library
  shared-library products must use an explicit cc_shared_library
~~~

src/oblib/BUILD.bazel:34 使用 native_cc_library 定义 _jemalloc_headers，没有设置 linkstatic。bazel/architecture/module_dependency_aspect.bzl:194-204 要求受管理源码目录下的 cc_library 为静态链接；bazel.py 为 build/test 自动应用该 aspect。因此该目标在编译前被拒绝。此结论来自实际构建输出，并已核对对应源码。

## 环境与执行范围

- WSL Ubuntu-22.04，Linux 6.6.114.1-microsoft-standard-WSL2，x86_64。
- Bazel 8.2.1，符合当前 .bazelversion；复用 /mnt/e/project/seekdb-2026-competition/bazel-8.2.1-linux-x86_64。
- 专用目录：/var/tmp/seekdb-retest-dcabb07-20261105；源码位于其 src 子目录。
- 使用 dcabb07 的独立克隆，复测结束时已确认该克隆没有受跟踪文件变化。
- 编译依赖依据当前 oceanbase.el9.x86_64.deps 固定版本准备，记录了 RPM 哈希。Clang 17.0.6、Rust 1.98.1；Bazel 实际编译未开始。
- 资源参数：jobs=2、local_resources cpu=2、memory=5500；默认 opt 配置。使用专用构建目录和磁盘缓存，未配置远程执行。
- 五个官方基准文件的 SHA-256 记录于 environment.json，内容未修改。

此前曾成功完成兼容 CMake 的 Release 配置并开始编译。用户澄清实际使用 Bazel 后，该编译已主动停止，其终止不是 C/C++ 编译缺陷证据，也没有用它生成本轮性能结果。CMake 相关原始日志单独保留在 WSL 专用目录。

## 已执行目标与结果

| 操作 | 目标 | 结果 |
| --- | --- | --- |
| Bazel build | //src/observer:seekdb_link | 退出码 1，_jemalloc_headers 分析失败 |
| Bazel test | //unittest/share:share_tests、//unittest/storage:storage_tests、//unittest/sql:sql_tests | 退出码 1，同一分析错误；没有测试用例运行 |
| 数据库回归与完整基准 | 原计划使用专用实例及完整官方参数 | 未运行，缺少成功构建的当前二进制 |

测试日志末尾的 “No test targets were found” 是分析失败后的输出，不代表仓库没有这些测试目标。

完整命令、退出码与持续时间见 bazel-build-result.json 和 bazel-tests-result.json；原始输出见同名 .log 文件。持续时间仅表示构建命令耗时，不是数据库性能指标。

## 后续处理

先修正 _jemalloc_headers 的声明，使其满足现有静态链接约束，例如使用已有 seekdb_cc_library 包装；不关闭模块检查或跳过约束。再从主入口重新构建并运行测试，成功后执行数据库回归和至少三轮完整基准。

修复后的源码应有独立提交号，报告应区分原始 dcabb07 的构建失败与修复版本的测试结果。当前只确认首个阻断点，不能保证不存在后续构建问题。

本轮未修改产品源码、构建规则或官方评测输入。原工作区已有的优化方案文档修改仍保留。
