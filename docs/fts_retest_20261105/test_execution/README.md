# WSL / Bazel 构建与测试执行结果

日期：2026-11-05。基于 `dcabb078a02fdcfe3a8e2e911a2484d15b7f32df` 加当前未提交的构建及测试夹具修复。不是原始提交未经修改的验证结果。

## 结论

Release/O2 生产目标已完成实际编译和链接，三个 C++ 测试套件均已实际执行。首次共运行 141 个用例，140 个通过、1 个失败，没有跳过用例。随后每个完整套件各重复 10 次，共 1410 次用例执行，1409 次通过、1 次失败。

当前不能报告所有测试通过，也不能将当前版本视为稳定的正确性基线。失败用例为 `FtsDictViewTest.immutable_views_match_legacy_ranges_and_survive_allocator_reuse`，是间歇性失败，完整重复测试的 Bazel 退出码为 3。

| 验证项 | 实际结果 | 证据 |
| --- | --- | --- |
| `//src/observer:seekdb_link` | 实际编译、链接成功，退出码 0 | `production-build.log`、`production-build-result.json` |
| Share | 每轮 2 个用例，10/10 轮全部通过 | `repeat10/share/` |
| SQL | 每轮 60 个用例，10/10 轮全部通过 | `repeat10/sql/` |
| Storage | 每轮 79 个用例，9/10 轮全部通过；另一轮 78 通过、1 失败 | `repeat10/storage/` |
| 模块检查器自身的回归测试 | 12/12 通过 | `module-checker-tests.log` |
| Sanity 生产和三个测试目标 | 分析通过，退出码 0；未完整编译或执行 Sanity | `sanity-final-analysis.log` |
| Sanity 最终链接动作 | seekdb、liboceanbase.so 均保留 `libjemalloc_pic.a` | `sanity-final-link-checks.json` |

每次运行的 XML、用例数量、失败名称与跳过数量见 `repeat10-summary.json`；首次完整执行的日志、XML 分别位于 `share/`、`sql/`、`storage/`，统计见 `full-suite-summary.json`。重复执行只统计 `run_*_of_10`，未重复计算 Bazel 保留的旧 XML。

## 失败详情及边界

断言位置：`unittest/storage/fts/test_fts_dict_view.cpp:555`。

```text
Expected: OB_SUCCESS (0)
Actual:   hub.load_cache(desc, container) == OB_ENTRY_NOT_EXIST (-4018)
trace:    i == 0 (DICT_IK_MAIN)
```

同一用例前面的 `get_builtin_dict()` 已成功。错误来自旧缓存接口重新读取主词典，而不是测试程序未找到某个外部词典文件。

- 首次完整执行：该用例失败，其余 140 个用例通过。
- 独立运行该用例：3/3 次通过。
- 单独重复完整 Storage 套件：3/3 次通过。
- 单独重复整个 FtsDictViewTest 测试组：3/3 次通过。
- 三个完整套件各重复 10 次：Storage 的 `run_1_of_10` 在同一断言失败，余下 9 轮通过。

已经确认存在间歇性缓存读取失败，根因尚未确认，不能据此断言是固定用例顺序、缓存淘汰或并发实现中的哪一种问题。此次没有修改词典缓存业务代码、放宽断言、过滤完整测试中的失败用例或以重跑成功覆盖首次失败。应先定位该问题，再将正确性验证标为通过并开展正式性能比较。

## 为执行测试修复的构建与测试声明

jemalloc 的原始分析根因和最小修复详见 `../jemalloc_static_fix/README.md`。其后实际编译暴露了独立的清单遗漏及测试模块归属问题：

| 修改文件 | 修改原因与范围 |
| --- | --- |
| `src/oblib/BUILD.bazel` | `_jemalloc_headers` 显式 `linkstatic=True`；头文件依赖和配置选择保持原样 |
| `src/oblib/oblib_header_inventory.bzl` | 补齐已存在的 `ob_ctype_uca_tab.h`、`str_uca_type.h` 私有编译输入 |
| `src/share/BUILD.bazel` | config 补齐 `local_storage_interface`；私有实现头目标补齐生成的 `local_table_schema`；仅对失败的 `ob_share_common_mixed_1` 编译组补齐 `oblib_mysql_client_services` |
| `src/sql/BUILD.bazel` | 移除 SQL 测试接口中的非法 Storage 模块边；增加 SQL 所属、testonly 的共享夹具实现目标 |
| `src/observer/BUILD.bazel` | 测试兼容运行库标记 testonly，并保留共享夹具归档中的符号；生产 seekdb 不链接该夹具 |
| `unittest/fts_exec_context_fixture.h`、`.cpp` | 共享夹具保留真实 SQL 执行上下文和 DAS 扫描器，通过不透明接口供 Storage 用例使用 |
| `unittest/storage/fts/test_fts_membership_batch.cpp`、`test_fts_token_batch.cpp` | 从 SQL 测试目录迁入其实现所属的 Storage 套件；7 个用例及 100 处断言宏全部保留，仅替换上下文和扫描器构造方式 |
| `unittest/BUILD.bazel`、`unittest/storage/BUILD.bazel` | 显式声明共享夹具输入 |
| `bazel/architecture/module_policy.bzl` | 仅把两份无测试用例的共享夹具文件登记为仓库级测试基础设施；模块间允许依赖集合不变 |
| `unittest/CMakeLists.txt` | 为既有 CMake Storage 测试入口同步声明夹具源码；本轮未运行完整 CMake 构建或测试 |

所有业务实现代码保持原样。静态链接检查器、模块依赖检查器、jemalloc 构建规则、Sanity 选择和最终静态归档要求均未关闭或修改。测试运行库原来就使用 liboceanbase.so；jemalloc 仍是静态归档链接。

测试布局审计与原始提交比较：原有 12 处违规降为 9 处，本次测试迁移消除 3 处引用违规，未新增违规。其余 9 处既有测试引用违规单独保留在 `test-layout-baseline-comparison.json`，不代表全仓库布局检查已通过。

## 环境及复现命令

WSL Ubuntu-22.04、Bazel 8.2.1、Clang 17.0.6、默认 opt/O2；依赖固定版本及机器信息见上一层 `environment.json`。实际测试使用 WSL 独立源码目录，其 13 个修复文件与 Windows 工作区逐字节核对一致，记录见 `validated-source.json`。

```bash
cd /var/tmp/seekdb-retest-dcabb07-20261105/src
export PATH=/home/cailucky/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
launcher=(python3 bazel.py
  --bazel=/mnt/e/project/seekdb-2026-competition/bazel-8.2.1-linux-x86_64
  --build-dir=/var/tmp/seekdb-retest-dcabb07-20261105/bazel-build
  --disk-cache=/var/tmp/seekdb-retest-dcabb07-20261105/bazel-disk-cache
  --launcher-cache-dir=/var/tmp/seekdb-retest-dcabb07-20261105/bazel-launcher)
resources=(--jobs=4 --local_resources=cpu=4 --local_resources=memory=5500
  --distdir=/mnt/e/project/seekdb-2026-competition/bazel-distfiles)

"${launcher[@]}" build "${resources[@]}" //src/observer:seekdb_link

"${launcher[@]}" test --test_output=errors --keep_going "${resources[@]}" \
  //unittest/share:share_tests //unittest/storage:storage_tests //unittest/sql:sql_tests

"${launcher[@]}" test --test_output=errors --nocache_test_results --runs_per_test=10 \
  "${resources[@]}" //unittest/share:share_tests \
  //unittest/storage:storage_tests //unittest/sql:sql_tests

# 仅分析 Sanity；实际参数及退出码见 sanity-final-analysis-result.json。
"${launcher[@]}" build --nobuild --sanity \
  --jobs=2 --local_resources=cpu=2 --local_resources=memory=5500 \
  --distdir=/mnt/e/project/seekdb-2026-competition/bazel-distfiles \
  //src/observer:seekdb_link //unittest/share:share_tests \
  //unittest/storage:storage_tests //unittest/sql:sql_tests

python3 tools/module_check/unittest_module_check_test.py
```

生产验证命令耗时约 24 秒，使用了前面测试构建已生成的核心产物，不是干净构建耗时；10 轮完整套件命令耗时约 15 秒。所有构建和测试耗时均不是数据库性能指标。

五个官方基准文件 SHA-256 与最初记录一致。数据库实例、SQL 集成回归和九项正式性能基准尚未运行，本报告不提供性能分数或新增优化收益。当前修复及报告尚未提交或推送。
