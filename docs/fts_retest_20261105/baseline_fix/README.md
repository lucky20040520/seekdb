# 词典缓存失败定位与可复现构建基线

日期：2026-11-06。原始源码快照为 `dcabb078a02fdcfe3a8e2e911a2484d15b7f32df`；此报告所在的提交包含实际验证的构建修复、测试修复和缓存键修复。完整变更以该提交为准，不能把原始快照单独称为通过验证的版本。

## 失败机制

在 WSL 独立源码副本中临时记录缓存节点和回收事件，重复运行 Storage 套件。诊断捕获到失败的同一内存块先被后台 `score_wash` 淘汰，随后缓存节点保护失败：节点序号为 `-1`，块当前序号为 `1`、状态为 `FREE`，`load_cache()` 因此返回 `OB_ENTRY_NOT_EXIST`。节点序号 `-1` 是 `get_seq_num_for_node()` 在块已处于 `FREE` 状态时的返回值，说明这一观测中淘汰发生在节点记录序号之前。不同失败可落在不同的主词典 range。

缓存句柄可以保持已获得的数据可用，但不承诺后续缓存查找必然命中。原用例在 `get_builtin_dict()` 成功后无条件要求 `load_cache()` 命中，遇到允许的缓存淘汰时就会间歇性失败。产品的 `get_builtin_dict()` 在 `load_cache()` 未命中时调用 `build_cache()`；测试现按相同缓存契约构建参考词典，保留既有词条比较及视图生命周期断言，没有忽略其他错误。

新增确定性回归显式删除缓存条目，验证未命中、原视图可用、缓存重建及重建结果一致。仅增加较长的定时回收间隔来隔离这一测试，其他完整套件仍使用原定时配置。诊断代码只存在于 WSL 验证副本，验证前已移除；`retire-diagnostic-40.log` 等诊断日志属于插桩运行，不能作为最终基线性能结果。

独立发现并修复另一处缓存键缺陷：`ObDictCacheKey::hash()` 包含 `range_id_`，原相等比较却不包含，现已统一按 `name_`、`dict_type_`、`range_id_` 比较。该问题不应被当作上述间歇性失败的已证实根因。新增回归在修复前失败，修复后通过；见 `regression-before-fix.log` 和 `regression-after-fix.log`。

## 最终验证

- WSL Ubuntu-22.04、Bazel 8.2.1、Clang 17，默认 opt/O2；静态链接和模块依赖检查保持启用。
- 三个完整套件各重复 50 轮，全部通过：Share 2 × 50，Storage 81 × 50，SQL 60 × 50，共 7,150 次用例执行，0 失败、0 跳过。见 `full-suites-repeat50.log` 和 `repeat50-summary.json`。
- 生产目标 `//src/observer:seekdb_link` 实际编译、链接成功，见 `production-after-fix.log`。
- 将 WSL 验证副本中之前 CMake 留下的 22 个未跟踪解析器生成文件移至备份后，三个完整套件再次通过、生产目标再次构建通过；这次使用已有 Bazel 缓存，并非无缓存干净构建。文件清单和结果见 `cmake-generated-backup.json`、`without-cmake-artifacts-*.log`。
- Sanity 配置下生产及三个测试目标的分析通过；本轮未执行完整 Sanity 编译和运行，见 `sanity-analysis.log`。
- 这 50 轮不能证明所有线程时序都已覆盖，也不代表数据库集成回归或性能基准已经完成。

实际使用的完整命令、资源参数、退出码和时间分别保存在同名 `*-result.json` 文件。构建使用 `/var/tmp/seekdb-retest-dcabb07-20261105/src` 的 WSL 副本；15 个变更源码/测试文件与工作区按 LF 规范化后逐字节一致，临时插桩源码已恢复。原始测试与失败证据保留在上一层 `test_execution/`。

## 冻结的构建输入

`baseline-manifest.json` 记录源码文件哈希、原始父提交、产物 SHA-256、构建环境和测试结果。实际构建使用的 `rust/Cargo.lock` 已纳入此基线，SHA-256 为 `099da00dbad8732ed6bb77e81be85321824dc8c8069f26a02ff14a22aedc6aa0`。原 Bazel Cargo 规则将 `*.lock` 纳入输入，但 `rust/build.py` 没有使用 `--locked`；后续构建应核对锁文件是否变化，不能假定规则会主动拒绝更新。

`seekdb` SHA-256：`2d3a2e346b9182f22e11c15a2fb8a66f3521ab75c0e6cdd6f5b98fe69b66fa66`。

`liboceanbase.so` SHA-256：`a53cc818a383d8df695160c5d0435d5a821dfdf00f627307ff92c36c5e129fb8`。

上述哈希标识本次二进制，不承诺在另一台机器或不同工具链下逐字节复现。性能比较应在相同机器、依赖、配置及资源条件下进行。五个原始基准文件未修改；本阶段尚未运行数据库性能基准、采集九项指标或实施性能优化。
