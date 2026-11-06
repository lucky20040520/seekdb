# jemalloc 静态链接约束：根因、最小修复与验证

> 以下为初始排查阶段的历史记录。后续已修复构建阻塞并实际执行测试；当前结果见 [构建与测试执行报告](../test_execution/README.md)。

日期：2026-11-05。基于 dcabb078a02fdcfe3a8e2e911a2484d15b7f32df。

## 结论与最小修改

原始 Analysis Phase 错误已解除。产品改动仅在 src/oblib/BUILD.bazel 的 _jemalloc_headers 声明上增加 linkstatic = True，并补充一行说明。依赖选择、jemalloc 构建规则、最终链接规则及静态检查器均未修改。

Release 与 Sanity 的生产、share/storage 测试分析已通过；实际生产编译及 share/storage 测试被已有 charset 头文件声明问题阻断，SQL 测试被独立模块依赖问题阻断。没有测试用例运行，不能报告完整生产构建或测试通过。

## 六项排查结果

### 1. 定义及所有引用

src/oblib/BUILD.bazel 中 _jemalloc_headers 使用 rules_cc 的原生 cc_library，是没有 srcs/hdrs 的依赖转接目标；唯一直接消费者是 _ob_malloc_impl 的 implementation_deps。

原声明未设置 linkstatic，Bazel 的有效属性为 False。该目标无论 Sanity 是否启用都存在，即使默认分支 deps 为空，静态约束也会检查它。因此普通生产构建和测试同样在分析阶段失败，不能将错误解释为 Sanity 开关选错。

### 2. 头文件目标与静态归档目标

deps/external/bazel/jemalloc.bzl 的 _seekdb_jemalloc_build 生成两个不同产物：

| 目标 | 内容 | 作用 |
| --- | --- | --- |
| jemalloc_headers | 通过 jemalloc_header 输出组获得 jemalloc/include/jemalloc/jemalloc.h，再由 cc_library 暴露 include 路径 | 编译期头文件依赖 |
| jemalloc_archive | 通过 jemalloc_archive 输出组获得 jemalloc/lib/libjemalloc_pic.a | 最终链接的静态归档输入 |

头文件目标没有把归档混入 CcInfo 链接依赖。最终归档由 observer 的 ordered_link_files 单独控制，这是当前设计；本次没有删除、合并或替换这些目标。

### 3. Sanity 选择

//bazel:sanity_enabled 匹配 //bazel:enable_sanity=True。它与源代码的 ENABLE_SANITY/OB_HAVE_BUNDLED_JEMALLOC 定义相对应：Sanity 内存包装实现需要生成的 jemalloc 头文件，普通 Release 不选择该外部头文件依赖。

配置后的 cquery 已验证：

- Release：_jemalloc_headers 不依赖 //deps/external:jemalloc_headers。
- Sanity：保留 _jemalloc_headers → //deps/external:jemalloc_headers。

条件保持原样，没有为通过检查强制启用或关闭 Sanity。

### 4. 最终静态归档链接

src/observer/BUILD.bazel 的 _SEEKDB_ORDERED_LINK_FILES 在 sanity_enabled 时加入 //deps/external:jemalloc_archive。该列表同时用于 seekdb_link 和 _liboceanbase_link。

实际 aquery 的两个 SeekdbFinalLink 动作验证结果：

| 配置 | 生产 seekdb | 测试运行库 liboceanbase.so |
| --- | --- | --- |
| Release | 原条件不加入独立 jemalloc_archive | 同左 |
| Sanity | 参数包含 deps/external/jemalloc/lib/libjemalloc_pic.a | 同左 |

测试原本使用 liboceanbase.so 承载生产实现；本次没有改变这个结构。其中 jemalloc 仍以 .a 链入，没有引入 jemalloc 动态库。aquery 证明的是已注册动作及其链接参数，不代表最终链接已经成功执行。

### 5. 检查器是否要求头文件提供静态库

bazel/architecture/module_dependency_aspect.bzl 检查受管理源码目录下 cc_library 的 linkstatic 属性，不检查它是否生成 .a。它施加的是静态链接策略，不是“纯头文件目标也必须产出归档”的要求。

修复后，Release/Sanity 下 cquery --output=files 查询 _jemalloc_headers 的输出均为空，仍能通过保留原 aspect 的分析。这直接验证了不需要伪造静态库，也不需要给头文件目标添加归档依赖。

最小修复选择显式 linkstatic=True，而非改用额外注入编译选项/依赖的包装宏，使现有编译依赖及属性变化最小。

### 6. 生产和测试的共同失败路径

配置后的依赖图为：

~~~text
seekdb_link
  → ob_malloc_objects → _ob_malloc_impl → _jemalloc_headers

share/storage/sql 测试
  → liboceanbase → _liboceanbase_link
  → ob_malloc_objects → _ob_malloc_impl → _jemalloc_headers
~~~

生产路径和测试运行库路径已通过 cquery 核实；三个测试由 seekdb_cc_test 显式依赖 liboceanbase。因此共同阻断点在共享的头文件转接声明，不在各测试或最终归档本身。

## 验证命令

所有命令在 WSL Ubuntu-22.04 的隔离源码目录执行，Bazel 8.2.1，默认 opt。修复后的工作树相对基准只有 src/oblib/BUILD.bazel 变化。原始完整命令和返回码保存在 JSON 中。

~~~bash
cd /var/tmp/seekdb-retest-dcabb07-20261105/src

# 公共启动参数；使用本机已有 Bazel，独立缓存，不配置远程执行。
launcher=(python3 bazel.py
  --bazel=/mnt/e/project/seekdb-2026-competition/bazel-8.2.1-linux-x86_64
  --build-dir=/var/tmp/seekdb-retest-dcabb07-20261105/bazel-build
  --disk-cache=/var/tmp/seekdb-retest-dcabb07-20261105/bazel-disk-cache
  --launcher-cache-dir=/var/tmp/seekdb-retest-dcabb07-20261105/bazel-launcher)
resources=(--jobs=2 --local_resources=cpu=2 --local_resources=memory=5500
  --distdir=/mnt/e/project/seekdb-2026-competition/bazel-distfiles)

"${launcher[@]}" build "${resources[@]}" //src/observer:seekdb_link
for module in share storage sql; do
  "${launcher[@]}" test --test_output=errors "${resources[@]}" \
    "//unittest/${module}:${module}_tests"
done

# 分析验证；生产与 share/storage 分别在 Release/Sanity 下通过。
"${launcher[@]}" build --nobuild --sanity=true "${resources[@]}" \
  //src/observer:seekdb_link //unittest/share:share_tests //unittest/storage:storage_tests
~~~

bazel.py 自动注入 module_dependency_aspect；验证未移除这个 aspect、未关闭静态检查或模块策略。

## 实际结果及剩余阻断

| 验证 | 结果 |
| --- | --- |
| Release 生产分析 | 通过 |
| Release share/storage 分析 | 均通过 |
| Sanity 生产及 share/storage 分析 | 均通过 |
| Release/Sanity 头文件条件及静态归档链接动作 | 通过 |
| 实际生产构建 | 越过分析后，charset 编译失败 |
| 实际 share 测试 | charset 编译失败，0 个用例执行 |
| 实际 storage 测试 | charset 编译失败，0 个用例执行 |
| 实际 SQL 测试 | 独立模块依赖分析失败，0 个用例执行 |

剩余问题均保留原始日志：

1. charset：ob_ctype_uca.cc 包含 lib/charset/ob_ctype_uca_tab.h。该文件在受跟踪源码中存在，但未列入 oblib_header_inventory.bzl，未进入 Bazel 沙箱输入，编译报 file not found。本次没有改动该清单或业务代码。
2. SQL 测试：sql_unit_test_interface 直接依赖 storage_runtime_interface，现有策略不允许 sql → storage。此错误在解除 jemalloc 阻断后显现，本次没有放宽策略或删除依赖。

因此，本次修复已验证能解除指定的 jemalloc Analysis Phase 错误，并保持编译头文件及 Sanity 静态归档链接要求；全量构建/测试通过仍受上述独立问题限制。未提交或推送本次改动。
