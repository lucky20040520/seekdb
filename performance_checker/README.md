# MiniVectorDB 独立性能检测程序

本目录与数据库实现分离；不修改数据库源码，不包含数据库实现，也不会自动启动 Docker、构建镜像或提交比赛。只有显式执行 `run` 命令才会启动性能负载。默认使用标准库，不需要 NumPy。

## 测量流程

默认每轮生成 10,000 条 128 维 float32 向量，每批 100 条写入并提交；发送 1,000 条 K=10 查询，其中无过滤和普通列等值过滤各一半。数据与查询由公开的固定算法和种子生成，但**未经确认与官方生成方法及隐藏数据一致**。每轮分别使用独立写入容器和查询容器，查询容器内先装载数据、可选建索引和预热，再开始查询计时。

- 耗时 T：预先生成 SQL；写入包括 BEGIN、INSERT、COMMIT 的请求与响应；查询包括每条请求发送和完整 JSON 响应接收，不含预热和事后校验。
- 内存 M：读取容器 cgroup v2 的 `memory.peak`，范围从容器启动到测量结束，包含该容器的缓存和资源读取辅助进程。写入和查询采用不同 cgroup；查询的峰值包含数据装载、建索引和预热。
- I/O B：读取 `io.stat` 中用户明确指定的设备 `major:minor` 的 `rbytes+wbytes`。采集时逐个容器核对 `/data` 文件系统设备编号，并尝试运行 `sync -f`；原始计数和错误均保留。设备无法核实或不匹配、计数缺失或同步失败时，I/O 为 `null`，不能计算综合指标。设备匹配本身不证明全部计数均来自数据目录。
- 正确性：在计时结束后独立进行暴力精确计算，检查结果数量、ID、去重、过滤条件、距离和排序，分别计算两组 Recall@K。数据库正常关闭后，使用另一个容器在原数据卷上重启并核对全部记录的内容。
- 默认三轮；任一轮正确性、已检查的资源条件或任一组平均召回率低于 0.95，均不产生有效综合指标。

## 必须区分的限制

本程序给出的是**本地诊断或估算**，不是平台正式成绩：

1. cgroup 计数在数据库关闭前读取；关闭期间或之后的写入可能没有计入。Docker Desktop 的物理设备映射、文件系统及异步写回归属必须单独验证；`sync -f` 和 `docker exec` 不能自动证明与正式平台统计窗口一致，辅助进程也会影响计数。
2. 官方冷缓存流程、冻结的全部硬性资源限制和真实数据分布尚未由本程序自动验证；本程序无法保证与官方硬件和 cgroup 配置相同。I/O 缺失不会视为零。
3. `resources_ok` 仅表示本地容器成功运行、已读取内存且数据目录同步命令成功；不是对所有正式 CPU、磁盘容量、I/O 带宽及超时限制的确认。
4. 综合指标依赖用户提供的实际基线。应使用官方冻结的写入和查询基线各三轮数据，或各指标的三轮中位数；不要用测试示例、缺失值或未经验证的本地数据冒充官方基线。输入文件里的真实性及测试环境是否匹配仍需人工确认。
5. 输出的综合指标仅用于排序，不能直接变成分数。排名需要正式榜单；只有额外提供已经知道的真实排名，才可以按照题目公式换算分数。

## 运行本地检测

从仓库根目录执行。以下命令是示例，**不会由程序自动执行**：

```powershell
python -m performance_checker.checker run --image minivector-round2:f1b9f4e --output performance-results/run-001
```

`--output` 必须是不存在的新目录。该命令仅创建并清理自身随机命名的 Docker 容器和数据卷，不构建镜像。结果保存在新目录的 `result.json`；运行失败也会保存错误和已经完成的轮次。不提供 `--io-device` 时，会保留原始计数，但 I/O 和综合指标显示不可用。

确认 `/data` 的实际数据设备及写回归属后，可以提供设备编号；如已有真实基线，还可以提供基线 JSON：

```powershell
python -m performance_checker.checker run --image minivector-round2:f1b9f4e --output performance-results/run-002 --io-device 8:96 --baseline path/to/baseline.json
```

上面的 `8:96` 只是设备编号格式示例，**不是本机或官方设备的判断结果**。选用 `ann` 或建立向量索引需显式添加 `--mode ann` 和/或 `--create-index`；默认是 `exact`，不创建索引。测试尺寸、种子和资源限制可以通过 `--help` 查看。

## 使用已有测量计算指标

基线 JSON 可直接给每阶段的三轮中位数：

```json
{
  "write": {"time_ms": 100, "io_bytes": 1048576, "memory_bytes": 1048576},
  "query": {"time_ms": 100, "io_bytes": 1048576, "memory_bytes": 1048576}
}
```

**上面数字仅为说明格式的虚构数值，不是官方基线。** 也可使用 `{"rounds":[...]}` 提供恰好三轮；每轮含 `write`、`query` 两个对象，每个对象含 `time_ms`、`io_bytes`、`memory_bytes`。程序会分别取每个指标的中位数作为基线。参考 [基线模板](baseline.template.json)。

对已有结果文件计算：

```powershell
python -m performance_checker.checker score --baseline path/to/baseline.json --measurements performance-results/run-002/result.json
```

每轮按 `R=(baseline+ε)/(actual+ε)`、`f(R)=R/(1+R)`，计算写入和查询各自的 `0.6f(R_T)+0.2f(R_B)+0.2f(R_M)`，再计算 `E=(E_write+2E_query)/3`。时间平滑常量是 1 ms，I/O 和内存各为 1 MiB。最后取**三个每轮 E 的中位数**，四舍五入六位小数；不能把每轮最好的指标拼起来。需查已知正式排名对应分数时，才添加 `--rank N`；该排名由使用者提供，程序不生成排名。

## 只验证程序逻辑

不启动数据库性能负载：

```powershell
python -m unittest discover -s performance_checker/tests -v
```

测试使用小型内存数据、伪进程及 mock，覆盖评分公式、边界、资源数据缺失、JSONL 通信、排序和召回率校验。
