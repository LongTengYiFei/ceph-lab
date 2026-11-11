# 混合 SSD B 写放大测试报告

测试日期：2026 年 11 月 11 日。测试对象为三节点 Ceph 集群中同时承载后端 OSD 元数据和缓存 OSD 的六块 SSD B。容量统一使用二进制单位：1 KiB = 1,024 B，1 GiB = 1,073,741,824 B。

在 4 GiB 数据集、1 GiB 缓存目标和三副本配置下，Workload A 的六盘整盘写入量为 **105.907230 GiB**，相对客户端成功更新载荷的写入比为 **18.537375×**；Workload B 为 **41.697376 GiB** 和 **72.924438×**。B 的绝对写盘量更少，但更新载荷分母下降得更多，因此比值更高。两轮写入量中，占比最大的内部路径都是缓存 OSD 自身的 BlueFS DB/日志。

本报告中的“写放大”定义为指定测量窗口内的 **SSD B 主机块层写入量 / 客户端成功更新载荷**。它描述该布局和工作负载的主机写入代价；SSD 内部 NAND/FTL 写放大不在本次测量范围内。两轮各运行一次，预热限速历史存在差异，具体比较范围见“结果解释与限制”。

## 实验目标与设备布局

实验要回答两个问题：在混合 SSD B 上，每成功更新 1 B 客户端数据会产生多少块层写入；这些写入分别来自后端元数据、缓存对象数据以及缓存 OSD 自身的数据库和日志路径中的哪些部分。

每组设备由两块 SSD 组成，三台存储节点各有两组，总计 12 块 SSD、12 个 OSD：

```text
SSD A 整盘 ───────── 后端 OSD 的 block 数据设备

SSD B 的 backend_db LV ─ 同一后端 OSD 的 block.db，DB 与 WAL 共置
SSD B 的 cache_data LV ─ 独立缓存 OSD 的 block，含其自身 DB/WAL
└─────────────────── SSD B 整盘写入量是本次统计对象
```

SSD B 的两个 LV 使用相同数量的 LVM extent，每个 LV 为 **960,189,431,808 B**。六块 SSD B 的配对如下；配对描述物理 DB 设备关系，缓存对象与后端对象仍由各池的 CRUSH 放置决定。

| 主机 | 后端数据 SSD A | 后端 OSD | 共享 SSD B | 缓存 OSD |
|---|---|---:|---|---:|
| node1 | `/dev/nvme0n1` | 0 | `/dev/nvme2n1` | 2 |
| node1 | `/dev/nvme1n1` | 1 | `/dev/nvme3n1` | 3 |
| node2 | `/dev/nvme1n1` | 4 | `/dev/nvme3n1` | 6 |
| node2 | `/dev/nvme2n1` | 5 | `/dev/nvme4n1` | 7 |
| node3 | `/dev/nvme1n1` | 8 | `/dev/nvme3n1` | 10 |
| node3 | `/dev/nvme2n1` | 9 | `/dev/nvme4n1` | 11 |

SSD A 的写入不进入本报告的分子。所有设备均为 SSD；本实验模拟缓存与后端分层的逻辑及混合盘写入，不据此评价 HDD 与 SSD 的性能差异。完整序列号、LV 路径和设备身份保存在 [layout.json](layout.json)。

node4 运行客户端和编排脚本，node1 至 node3 运行 OSD。软件版本由每轮 `results.json` 的 `before.versions` 保存：

| 组件 | 版本 |
|---|---|
| OSD | Ceph 17.2.7 snapWrite，提交 `69fbba696bee9762a0025b6b128ddd978120ac78` |
| MON/MGR | Ceph 17.2.7，提交 `b12291d110049b2f35e32e0de30d70e9a4c060d2` |
| 负载生成器 | YCSB 0.17.0 CoreWorkload，Java 11 |
| 访问适配层 | 本实验自定义 Java/JNI RADOS binding |

因此，结果适用于本次 snapWrite 构建和当前布局，不能直接代表未经修改的 Ceph 17.2.7。

## 工作负载与缓存配置

### YCSB 负载

保留官方 [Workload A](https://raw.githubusercontent.com/brianfrankcooper/YCSB/0.17.0/workloads/workloada) 和 [Workload B](https://raw.githubusercontent.com/brianfrankcooper/YCSB/0.17.0/workloads/workloadb) 的读写比例和 Zipfian 访问分布，将记录格式统一为固定 4 KiB 单字段。读写比例按操作次数计算。

| 参数 | Workload A | Workload B |
|---|---:|---:|
| READ 比例 | 50% | 95% |
| UPDATE 比例 | 50% | 5% |
| 正式阶段 INSERT / SCAN / READMODIFYWRITE | 0 | 0 |
| 访问分布 | Zipfian，hashed key | Zipfian，hashed key |
| 对象数量 | 1,048,576 | 1,048,576 |
| 每对象载荷 | 4,096 B | 4,096 B |
| 数据集逻辑容量 | 4 GiB | 4 GiB |
| 客户端线程 | 32 | 32 |
| 目标总吞吐 | 10,000 ops/s | 10,000 ops/s |
| 预热时间 | 300 s | 300 s |
| 正式负载时间 | 300 s | 300 s |

UPDATE 直接调用 `rados_write_full` 覆盖一个完整对象。每个 4,096 B 载荷中包含 8 B 标识和 CRC32 校验头；所有成功读取都检查长度与 CRC32。客户端仅在写操作成功返回后累计写入字节。对象名和协议开销不进入客户端载荷字节数，校验头计入这 4,096 B。

这种格式与 YCSB 官方默认的多字段记录不同。实际适配层见 [RadosBlockDB.java](ycsb/RadosBlockDB.java) 和 [rados_jni.c](ycsb/rados_jni.c)，负载参数见 [workloada.properties](ycsb/workloada.properties) 与 [workloadb.properties](ycsb/workloadb.properties)。

### 池与缓存策略

每轮创建一对独立临时池，复用现有的六个后端 OSD 和六个缓存 OSD。应用打开后端池，正常访问通过 overlay 路由；缓存提升和回写由 Ceph tiering 机制处理。机制说明见 [Ceph Quincy Cache Tiering](https://docs.ceph.com/en/quincy/rados/operations/cache-tiering/)。

| 参数 | 测试值 |
|---|---|
| 后端 OSD | 0、1、4、5、8、9，CRUSH 规则 `objmeta-backend` |
| 缓存 OSD | 2、3、6、7、10、11，CRUSH 规则 `objmeta-cache` |
| 副本与故障域 | 两池均为 size 3、min_size 2，host 故障域 |
| PG 数量 | 每池 64，关闭 PG 自动调整 |
| 缓存模式 | writeback，同时设置读写 overlay |
| 压缩与 scrub | 两临时池关闭压缩，设置 noscrub、nodeep-scrub |
| 缓存 `target_max_bytes` | 1,073,741,824 B，即三副本前 1 GiB 逻辑目标 |
| dirty / dirty-high / full 比例 | 0.4 / 0.6 / 0.8 |
| 最小回写 / 逐出年龄 | 60 s / 120 s |
| HitSet | Bloom，12 个，每个覆盖 300 s |
| 读 / 写提升 recency | 1 / 0 |
| 正式阶段每缓存 OSD 提升限速 | 对象数和字节数两个上限均为 0，即取消限速 |

1 GiB 是临时缓存池的逻辑容量目标，与前述 `cache_data` LV 的实际容量是不同概念；LVM 仍按五五开布局。该目标也不是严格的物理空间硬上限。数据集逻辑容量为缓存目标的四倍，用于形成提升和逐出压力。

默认提升限速为每缓存 OSD 25 对象/s、5 MiB/s。A 在预热期间取消了限速，B 在装载前即取消；两轮正式阶段均固定为不限速。变更及恢复时间保存在各自的 `promotion-overrides.json`。

## 写放大的计算方法

### 分子为六块 SSD B 的整盘写入字节

在每块 SSD B 上读取 `/sys/class/block/<设备名>/stat` 的第 7 个字段，即写扇区累计值，取测量前后差值后乘 512：

```text
Wi = [SSD B i 在 T2 的写扇区数 − 在 T0 的写扇区数] × 512 B
W  = W1 + W2 + W3 + W4 + W5 + W6
```

这是 Linux 块层的设备写入计数，扇区单位固定为 512 B，并在 I/O 完成路径更新；它不是 NVMe 驱动的命令数量，也不能给出 NAND 实际编程字节。单位和更新时机见 [Linux 块层统计说明](https://docs.kernel.org/block/stat.html)。

W 同时包含后端 DB/WAL LV 和缓存 OSD LV 的写入；后者又包含缓存对象及缓存 OSD 自身的 DB/WAL。整盘写入中还保留了同盘后台活动。LV 计数用于分账，与整盘计数不重复累加。

### 分母为正式阶段成功更新的载荷字节

```text
D = 正式阶段成功 UPDATE 次数 × 4,096 B
```

每次成功更新都计入 D，包括对同一对象的重复更新。D 是字节量，不是请求个数、不同对象数量或数据集容量。READ 字节、初始化 INSERT 和预热 UPDATE 均不进入 D。读引发的缓存提升及相应数据库写入仍会进入 W。

### 主指标与三副本归一化

```text
主机侧整盘写放大 WA = W / D
三副本归一化值      = W / (3 × D)
```

归一化值便于展示副本倍数，不能消除缓存提升、元数据写入和后台活动的影响；由于这里只统计 SSD B，基线也不必等于 1。

### 测量窗口

```text
初始化 load → 预热 300 s → T0 → 正式负载 300 s → T1 → 排空和等待低写入 → T2
W 的计量窗口：[T0, T2]，包含正式负载和回写尾部
D 的累计窗口：[T0, T1]，只取正式阶段成功 UPDATE
T2 之后：短空闲对照 → 全量后端读回 → 删除临时池 → 恢复配置
```

T0 对应原始快照 `formal-start`，T1 对应 `formal-end`，T2 对应 `tail-end`。初始化和预热时段产生的块层写入不直接计入 W，读回校验和删除临时池也在主窗口之外。正式开始时保留预热缓存及其脏对象，因此尾部排空可能包含预热遗留回写；本指标是窗口统计，不能将全部 W 逐条归属于正式 UPDATE。

## 测试执行方法

两轮按照以下流程运行，由 [run_ycsb.py](run_ycsb.py) 编排。A 的提升限速在预热期间单独调整，B 使用脚本自动设置和恢复。

1. **检查与准备。** 核对 FSID、12 OSD up/in、PG active+clean，保存版本、原池配置、flags 和 balancer 状态。暂停 balancer，创建本轮独立的 64 PG 后端池和缓存池，设置上述 tiering 参数并等待 clean。
2. **建立冷缓存起点。** 初始化装载 1,048,576 个对象，使用 `IGNORE_CACHE | IGNORE_OVERLAY` 直接写后端。检查装载成功字节为 4 GiB，且缓存提升计数不增加，确认 load 没有把测试对象装入缓存。
3. **预热。** 使用对应 A 或 B 的读写混合负载运行 300 s，通过正常 overlay 访问。预热阶段的客户端写入和块层写入另存，不进入主指标。预热与正式阶段使用不同 JVM，保留的是 Ceph 缓存状态，不能据此认定正式 JVM 的 JIT 已预热。
4. **正式负载。** 在启动正式 YCSB 前采集 T0 快照，运行 300 s 后采集 T1。客户端记录成功读写、成功更新字节和错误；约每 10 s 采集整盘、OSD perf 等数据，阶段边界另取 SMART 快照。B 增加两个 LV 的块层计数。采样同时保留设备序列号、boot ID、OSD PID 和启动时间，用于检测设备或进程变化。
5. **记录回写尾部。** 将本轮临时缓存的 `cache_min_flush_age`、`cache_target_dirty_ratio` 和 `cache_target_dirty_high_ratio` 设为 0，等待 dirty 为 0；随后要求三个连续采样间隔内，六块 SSD B 合计写入每次均不超过 1 MiB，间隔含约 2 s 等待及采样耗时，最后采集 T2。尾部控制引发的写入也进入 W，流程不主动强制 RocksDB compaction。
6. **采集空闲对照。** 排空后单独等待约 30 s 并记录写入，作为短空闲参考；主值不减去该对照。
7. **完整读回校验。** 使用 `IGNORE_CACHE | IGNORE_OVERLAY` 绕过缓存，从后端读取全部 1,048,576 条记录，检查每条大小和 CRC32，确认回写后的后端数据可读。
8. **清理和恢复。** 排空剩余脏对象、移除 overlay，经 readproxy 切换到 none，解除 tier 关系并删除本轮临时池；恢复 balancer 和六个缓存 OSD 的提升限速。核对永久池配置、flags、OSD 状态及 PG 删除队列。

正式负载窗口必须观察到 `tier_promote` 和 `agent_evict` 均有增量，才满足本次提升及自然逐出实验的要求。该判断不要求正式阶段必须发生自然回写。

## 测试结果与计算实例

### 主结果

| 指标 | Workload A | Workload B |
|---|---:|---:|
| 成功 READ 次数 | 1,498,081 | 2,845,924 |
| 成功 UPDATE 次数 | 1,497,674 | 149,891 |
| 成功更新 D，B | 6,134,472,704 | 613,953,536 |
| 成功更新 D，GiB | 5.713173 | 0.571789 |
| 六块 SSD B 写入 W，B | 113,717,022,720 | 44,772,216,832 |
| 六块 SSD B 写入 W，GiB | 105.907230 | 41.697376 |
| **W / D** | **18.537375×** | **72.924438×** |
| W / (3D) | 6.179125× | 24.308146× |
| 正式负载时段 SSD B 写入，GiB | 99.878952 | 39.562962 |
| 排空尾部 SSD B 写入，GiB | 6.028278 | 2.134415 |
| 主计量窗口长度，s | 352.715 | 329.978 |
| 客户端失败 / CRC 错误 | 0 / 0 | 0 / 0 |

按原始整数计算，避免先四舍五入 GiB 再求比值：

```text
Workload A
D = 1,497,674 × 4,096 = 6,134,472,704 B
W = 107,244,208,128 + 6,472,814,592 = 113,717,022,720 B
WA = 113,717,022,720 / 6,134,472,704 = 18.537375×

Workload B
D = 149,891 × 4,096 = 613,953,536 B
W = 42,480,406,528 + 2,291,810,304 = 44,772,216,832 B
WA = 44,772,216,832 / 613,953,536 = 72.924438×
```

### SSD B 逐盘写入量

以下均为 T0 至 T2 整盘块层实测，单位 GiB。

| 主机 | SSD B | Workload A | Workload B |
|---|---|---:|---:|
| node1 | `/dev/nvme2n1` | 18.561470 | 6.969570 |
| node1 | `/dev/nvme3n1` | 16.692139 | 6.821255 |
| node2 | `/dev/nvme3n1` | 17.998535 | 6.515575 |
| node2 | `/dev/nvme4n1` | 17.437683 | 7.451355 |
| node3 | `/dev/nvme3n1` | 14.957756 | 6.104610 |
| node3 | `/dev/nvme4n1` | 20.259647 | 7.835011 |
| **合计** | | **105.907230** | **41.697376** |

表格展示值独立四舍五入，合计按原始字节计算。设备序列号可通过 `layout.json` 和每轮 `per-ssd.csv` 交叉核对。

## SSD B 写入来源分解

### 两轮采用相同内部计数进行近似归因

后端部分取六个后端 OSD 的 `bluefs.write_bytes` 增量；缓存部分分别取六个缓存 OSD 的 `bluefs.write_bytes`，以及 `bluestore.write_big_bytes + bluestore.write_small_bytes` 增量。所有增量均使用 T0 至 T2，与整盘主窗口一致。

| 路径 | A 写入 GiB | 占 A 整盘写入 | B 写入 GiB | 占 B 整盘写入 |
|---|---:|---:|---:|---:|
| 后端 OSD BlueFS DB/日志 | 22.673916 | 21.409% | 2.990295 | 7.171% |
| 缓存 OSD 自身 BlueFS DB/日志 | 64.454548 | 60.859% | 32.145061 | 77.091% |
| 缓存 OSD 对象数据写入路径 | 18.769181 | 17.722% | 6.557644 | 15.727% |
| 整盘总量与上述路径之和的残差 | 0.009585 | 0.009% | 0.004375 | 0.010% |
| **整盘实测合计** | **105.907230** | **100%** | **41.697376** | **100%** |

这些计数在 BlueFS/BlueStore 内部的不同位置更新，属于路径估计，不能等同于逐请求、逐物理扇区的完整追踪。较小残差提供了总量交叉核对，但不改变其近似归因性质。两轮后端 OSD 的 `bluefs.bytes_written_slow` 增量均为 0，未观察到该窗口的 BlueFS 写入溢出至后端数据设备。后端 OSD 的对象数据写入路径主要对应 SSD A，因此不加入本表的 SSD B 分解。

同一组 BlueFS 计数还能按 WAL 和 SST 写入类型细分，单位 GiB：

| OSD 类型和负载 | WAL | SST | 其他 BlueFS 写入 | BlueFS 合计 |
|---|---:|---:|---:|---:|
| 后端 OSD，A | 11.377396 | 1.071743 | 10.224777 | 22.673916 |
| 缓存 OSD，A | 35.984383 | 3.725895 | 24.744270 | 64.454548 |
| 后端 OSD，B | 1.571136 | 0.000000 | 1.419159 | 2.990295 |
| 缓存 OSD，B | 17.787865 | 1.951279 | 12.405918 | 32.145061 |

WAL、SST 分别来自 `bluefs.bytes_written_wal`、`bluefs.bytes_written_sst`；其他项是 `write_bytes` 减去这两项。该表是上一张表 BlueFS 项的进一步细分，不再与整盘总量相加。现有统计没有把 SST 写入进一步完整拆成 flush 与 compaction，其他项也不全部指定为某一种日志。

### Workload B 的直接 LV 分账

B 同步读取两个 LV 对应 `dm-*` 设备的块层计数，可直接确认写入落在哪个区域：

| SSD B 区域 | 实测写入 B | 实测写入 GiB | 占整盘写入 |
|---|---:|---:|---:|
| 后端 DB/WAL LV | 3,211,173,888 | 2.990639 | 7.172% |
| 缓存 OSD LV，含自身 DB/WAL | 41,561,042,944 | 38.706738 | 92.828% |
| **LV 合计** | **44,772,216,832** | **41.697376** | **100%** |

本轮 LV 合计与六块 SSD B 的整盘计数残差为 **0 B**。A 未采集 LV 块层计数，A 的区域占比仅能根据内部路径估计。LV 与物理盘属于不同统计层次且采样时间略有差异，其他轮次不保证也有零残差。B 的逐盘 LV 数据见 [per-lv.csv](results/20261111T122359Z-ycsb-b/per-lv.csv)。

缓存 OSD LV 中既有缓存对象也有缓存自身的 DB/WAL。按内部路径估计，B 的缓存 BlueFS DB/日志量约占整盘写入的 **77.1%**、缓存 LV 写入的 **83.0%**；不能将缓存 LV 的 92.8% 全部理解为热点对象数据。

另外，`rocksdb.submitted_bytes` 统计提交的序列化 WriteBatch 输入，包含批次头，是另一个层次的逻辑输入；它不与 BlueFS 输出或整盘写入重复累加。

## 缓存活动与客户端性能

### 正式负载中的自然缓存活动

以下只取 T0 至 T1 的缓存 OSD perf 增量，排除尾部排空和清理，不将计数解释为严格去重后的对象数量。

| 计数器 | Workload A | Workload B |
|---|---:|---:|
| `tier_promote` | 790,320 | 492,945 |
| `agent_evict` | 716,692 | 492,999 |
| `tier_evict` | 716,692 | 492,999 |
| `agent_flush` | 81,750 | 0 |
| `tier_clean` | 81,743 | 0 |
| `tier_dirty` | 151,476 | 18,043 |
| `tier_try_flush` | 28,722 | 0 |
| `tier_flush` | 0 | 0 |

两轮均观察到正式负载期间的提升和自然逐出。B 在正式阶段没有发生自然 `agent_flush`；停止负载后通过临时池阈值设置完成排空，尾部写入已包含在 W 中。`tier_try_flush` 是尝试计数，不作为成功回写次数；不同回写计数分别报告。

正式窗口的定期池采样显示，A 的缓存逻辑 stored 为 **883.179 至 970.241 MiB**，B 为 **763.475 至 767.948 MiB**。这些是离散观测范围，不是连续时刻的极值，也不代表严格容量上限。

### 正式阶段吞吐与延迟

下表来自 YCSB `formal.log`，延迟单位为微秒。吞吐按 YCSB 自身运行时间计算，与包含回写尾部的主计量窗口长度不同。

| 指标 | Workload A | Workload B |
|---|---:|---:|
| YCSB RunTime，ms | 300,123 | 300,107 |
| 实际吞吐，ops/s | 9,981.757 | 9,982.490 |
| READ 平均延迟，μs | 1,316.983 | 456.690 |
| READ P99，μs | 30,847 | 1,125 |
| UPDATE 平均延迟，μs | 2,496.998 | 1,199.507 |
| UPDATE P99，μs | 42,687 | 2,599 |

这些值描述当前读写比例与缓存状态下的单轮表现，不能视作同一读写负载的性能提升比例。

## 数据正确性与统计交叉核对

两轮客户端失败和 CRC 错误均为 0。排空后均从后端绕过缓存完整读取 **1,048,576 条记录，共 4 GiB**，所有记录的长度与内嵌 CRC32 均通过检查。并发更新没有保存逐对象的最终客户端版本镜像，因此这项验证确认全量记录存在、大小与校验和正确，不声称与最后一次更新日志逐字节比对。

两轮均完成临时池删除和配置恢复，最终为 **12 OSD up/in、129 PG active+clean**，各 OSD 的 PG 删除队列为 0。永久池保持 `.mgr`、`objmeta-data`、`objmeta-cache`。结束时仍有实验前已存在的 `MON_DISK_LOW`，因此没有将集群状态写为 HEALTH_OK。恢复证据见各轮 `final-state.json` 和 `promotion-overrides.json`。

SMART 使用 NVMe Data Units Written 差值乘 **512,000 B**，用于交叉核对主机写入量。两轮都取得 node1、node2 上四块 SSD B 的前后快照，node3 两盘未取得 SMART 快照；六盘块层主计数均完整。

| 同一组四盘的交叉核对 | Workload A | Workload B |
|---|---:|---:|
| SMART 主机写入，B | 75,902,976,000 | 29,805,056,000 |
| 块层写入，B | 75,902,623,744 | 29,804,662,784 |
| SMART 减块层，B | 352,256 | 393,216 |

该小幅差值受采样时刻和 SMART 计量粒度影响，SMART 仍是主机侧写入量。已取得快照的四盘均无新增 SMART 介质错误或错误日志条目。排空后的短空闲对照中，A 的 32.125 s、B 的 32.415 s 六盘写入均为 0 B；该对照短于正式窗口，主结果未扣除后台。

## 结果解释与限制

**B 的高比值不表示 B 写盘更多。** 与 A 相比，B 的 SSD B 绝对写入减少 **60.63%**，客户端成功更新载荷减少 **89.99%**。分母下降得更快，同时 READ 引发的缓存提升及元数据写入仍在分子内，使 W/D 从 18.537× 上升到 72.924×。

**缓存 OSD 自身的数据库和日志是两轮最大的写入路径。** A 中约占 SSD B 整盘写入的 60.9%，B 中约占 77.1%；后端 BlueFS DB/日志分别约占 21.4% 和 7.2%。现有数据支持区域与路径层面的判断，未把每次读、写、提升和逐出各自产生的底层写入独立追踪，也不能仅凭总量将数据库写入全部归因于 compaction。

比较这两轮时还应保留以下条件：

- **预热历史不同。** A 在预热期间取消提升限速，B 在装载前取消；两轮正式参数相同，但缓存起始状态并不完全一致。每轮临时池 ID 与 PG 映射也不同。
- **只有单轮结果。** 固定 300 s 预热尚未严格证明缓存和数据库已达稳态；没有重复实验置信区间，不能将全部差异仅归因于读写比例。
- **窗口包含尾部控制和预热遗留回写。** 主值保留两轮实际执行口径；若要估计严格稳态下每次正式更新的代价，应进一步统一预热与测量边界并重复实验。
- **统计范围为六块 SSD B。** 该指标不覆盖 SSD A，也不等于整个 Ceph 集群或 SSD 内部 NAND 的写放大；既有池同盘后台活动保留在块层分子中。

## 复现方法与原始记录

### 在现有实验布局上运行

以下命令供后续复现使用，在 node4 的 `/home/cyf/ceph-lab-17.2.7` 下执行。本报告根据已保存的两轮记录编写，没有为生成报告重新运行负载。

```bash
# 首次准备客户端依赖和适配层；已有构建可跳过。
bash Exp_ObjectMetaWriteAmplification/ycsb/bootstrap.sh

# 可选的小规模端到端检查。
python3 -B -u Exp_ObjectMetaWriteAmplification/run_ycsb.py --smoke

# A 与 B 必须顺序运行，使用独立临时池。
python3 -B -u Exp_ObjectMetaWriteAmplification/run_ycsb.py \
  --workload a --unlimited-promotion

python3 -B -u Exp_ObjectMetaWriteAmplification/run_ycsb.py \
  --workload b --unlimited-promotion
```

默认参数就是本报告的 4 GiB 数据、1 GiB 缓存、32 线程、10,000 ops/s、300 s 预热和 300 s 正式阶段。当前 `--unlimited-promotion` 会在装载前统一设置并在捕获到异常或正常结束后恢复限速；重新运行 A 的预热限速历史会比本次 A 更一致，结果不应要求与本报告数值完全相同。

### 离线重建单轮报告

```bash
python3 -B Exp_ObjectMetaWriteAmplification/report_ycsb.py \
  Exp_ObjectMetaWriteAmplification/results/20261111T094932Z-ycsb-a

python3 -B Exp_ObjectMetaWriteAmplification/report_ycsb.py \
  Exp_ObjectMetaWriteAmplification/results/20261111T122359Z-ycsb-b
```

离线计算可从 `summary.json` 复算 W/D，也可从 `raw-counters.json.gz` 的 `boundaries.formal-start` 与 `boundaries.tail-end` 读取每盘 `written_bytes` 差值，验证六盘合计，再除以 `formal-client.json` 的 `successful_write_bytes`。BlueFS 的 WAL、SST 细分也使用这两个边界内六个对应 OSD 的计数差值。

| 记录 | Workload A | Workload B |
|---|---|---|
| 单轮详细报告 | [A 报告](results/20261111T094932Z-ycsb-a/report.md) | [B 报告](results/20261111T122359Z-ycsb-b/report.md) |
| 参数、版本及执行状态 | [results.json](results/20261111T094932Z-ycsb-a/results.json) | [results.json](results/20261111T122359Z-ycsb-b/results.json) |
| 主结果与路径汇总 | [summary.json](results/20261111T094932Z-ycsb-a/summary.json) | [summary.json](results/20261111T122359Z-ycsb-b/summary.json) |
| 客户端成功字节 | [formal-client.json](results/20261111T094932Z-ycsb-a/formal-client.json) | [formal-client.json](results/20261111T122359Z-ycsb-b/formal-client.json) |
| 逐盘结果 | [per-ssd.csv](results/20261111T094932Z-ycsb-a/per-ssd.csv) | [per-ssd.csv](results/20261111T122359Z-ycsb-b/per-ssd.csv) |
| 边界及周期原始快照 | [raw-counters.json.gz](results/20261111T094932Z-ycsb-a/raw-counters.json.gz) | [raw-counters.json.gz](results/20261111T122359Z-ycsb-b/raw-counters.json.gz) |
| 恢复后状态 | [final-state.json](results/20261111T094932Z-ycsb-a/final-state.json) | [final-state.json](results/20261111T122359Z-ycsb-b/final-state.json) |

每轮结果还保存 `formal.log`、`readback.json`、`smart-crosscheck.json`、`promotion-overrides.json`、`stages.csv` 和 `source/`。归档源文件及其 SHA256 用于确定实际执行版本；复现入口与异常恢复说明见 [YCSB 测试说明](ycsb/README.md)。
