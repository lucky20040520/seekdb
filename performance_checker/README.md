# MiniVectorDB 六项指标本地对照

当前默认实现为 Python，exact 优化方案和验证进展见 [Python 优化报告](../docs/python-performance-20261114.md)。此前 C++ 实验单独保存在 [历史记录](../docs/performance-20261114.md) 和对应 JSON 中，不代表当前 Python 的性能或正式功能准入结果。

固定负载：10000 行、128 维、1000 次 exact 查询、K=10、无过滤/等值过滤各 500 次，每批 100 行显式 BEGIN/INSERT/COMMIT。三轮的种子依次为 19017/29017 加轮号；所有版本逐轮使用相同输入，轮换版本先后顺序。过滤列使用 5 个等分标签。profiling 关闭。

## 统计范围

- 写入与查询分别新建独立容器、父 cgroup 和空数据卷，限制为 2 CPU、2 GiB、禁用 swap、128 PID。父 cgroup 无采样进程，只包含该阶段的数据库容器。
- T：发送预生成 SQL 至接收完整响应的墙钟时间。写入包含 100 批 BEGIN/INSERT/COMMIT；查询包含全部 1000 次请求。启动、建表、装载、建索引、一次无过滤预热单列，不计入查询 T。
- B/M：从数据库启动至正常退出和写回完成。查询的装载、可选建索引、预热均计入。数据库退出后，由外部驱动对数据文件系统 syncfs，再等待无 dirty/writeback 且连续三次 I/O 计数稳定，读取父 cgroup 的 io.stat 和 memory.peak。子 cgroup 退出后累计值仍由父 cgroup 保留。没有 docker exec 采样进程污染数据库指标。
- B 只取经 st_dev 核对的指定设备 rbytes+wbytes；M 包含数据库及其子进程和归属的页缓存。原始计数、内存限制事件、数据文件数量/逻辑大小保存在报告中。文件大小不替代物理 I/O；计数缺失、设备不符或同步失败时 B 为 null，不能计算完整 E。
- 本地缓存流程固定为每阶段全新空卷，镜像先运行小型查询以预热可执行层；不清除宿主机全局缓存。该流程不声称等同官方冷缓存环境。
- 全部查询由驱动侧独立 Python math.fsum 暴力扫描生成真值；可按完整负载 SHA256 复用已生成真值。每版检查全部 ID、数量、过滤、距离、并列主键排序，两组 exact Recall@10 均必须为 1。统计结束后另起验证容器重启并核对全部字段，正确性核验不污染被测资源。

## 运行

需要 Linux Docker cgroupfs/v2。Windows Docker Desktop 使用外部 Linux 驱动，挂载 Docker socket、宿主 /sys/fs/cgroup 至 /host-cgroup、Docker volumes 根目录至 /host-volumes，以及本仓库至 /work。驱动不能放在被测父 cgroup 下；只创建和清理 minivector-measure-* / perfcheck-* 的本次资源。

```sh
python -m performance_checker.paired --images python=FROZEN_PYTHON_IMAGE \
  --io-device VERIFIED_MAJOR:MINOR --oracle-cache /work/.artifacts/oracle \
  --output /work/.artifacts/baseline

python -m performance_checker.paired --images before=BEFORE_IMAGE after=AFTER_IMAGE \
  --baseline /work/.artifacts/baseline/python.json --io-device VERIFIED_MAJOR:MINOR \
  --oracle-cache /work/.artifacts/oracle --output /work/.artifacts/paired
```

`tools/benchmark_jsonl.py` 现在转发至同一流程，旧的先核验后查询的测量已移除。输出目录必须不存在。额外的 `--create-index` 对照应在两版都建索引时比较，不把其指标与无索引基线混为相同场景。`--rows/--dimensions/--queries/--rounds` 只用于诊断试跑，正式本地对照保持默认规模和三轮。

Windows 可用 `tools/run_performance.ps1 -Images label=image ... -OutputDirectory .artifacts/run -IoDevice VERIFIED_MAJOR:MINOR -Baseline .artifacts/baseline/python.json` 启动相同 Linux 驱动。如果为有索引等独立场景建立新基线，显式加入冻结 Python 镜像，并用 `-FreezeBaselineLabel python`；各版本仍使用完全相同的场景参数。

`python -m performance_checker.probe --image PYTHON_IMAGE --io-device VERIFIED_MAJOR:MINOR --output probe.json` 检查无显式 fsync 的写入进程退出后，父 cgroup 是否保留累计计数，并执行外部同步采集流程。写回可能在同步前已完成，只有前后计数实际增长才构成观察到延迟写回的证据。它必须在同样的外部 Linux 驱动环境单独运行，不与计分负载并发。

没有外部采样权限的普通 CI 可以显式使用 `--allow-missing-resources`；此时只验证耗时和正确性，B/M 为 null，永远不计算 E，也不能冻结六项指标基线。本机正式对照不启用该选项。

## E_local

本仓库没有官方冻结基线，因此只使用本次冻结 Python 镜像三轮六项指标各自的中位数作为固定本地基线。每轮按 R=(base+epsilon)/(actual+epsilon)、f=R/(1+R)，计算 E_phase=0.6*f(T)+0.2*f(B)+0.2*f(M)，E=(E_write+2*E_query)/3。epsilon 为 1 ms / 1 MiB / 1 MiB。最终取三轮 E 的中位数，保留六位小数。不得拼接不同轮次最优指标。Python 固定基线自身的理论参考值是 0.5；波动轮次的实际 E 中位数可能略有不同。

这是本机、合成数据上的 E_local，不是官方性能分数或排名。Docker Desktop 的共享块设备及文件系统元数据会影响物理 I/O，使用同机成对对照并保留原始证据。官方硬件、隐藏种子和冷缓存条件未复现。

```sh
python -m unittest discover -s performance_checker/tests -v
```
