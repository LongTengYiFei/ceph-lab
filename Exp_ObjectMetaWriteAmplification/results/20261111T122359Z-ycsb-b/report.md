# Workload B：混合 SSD B 整盘写放大

开始时间：2026-11-11 20:23:59（北京时间）。

运行通过：**True**；临时资源清理完成：**True**。

## 负载与测量口径

- 使用官方 YCSB **0.17.0 CoreWorkload** 的 Workload B：目标 95% READ / 5% UPDATE，Zipfian 分布。
- 采用本目录公开的自定义 RADOS binding：每条记录一个 4 KiB 字段，UPDATE 直接调用 `rados_write_full`。
- 每个 4 KiB 记录内含 8 B 标识与 CRC32；每次读取校验大小和 CRC32，写入字节计数包含这 8 B。
- 这不是默认的 10 字段/100 B 更新，也没有使用原 RADOS binding 的 delete+insert 更新。
- 数据集 1,048,576 条、4.000000 GiB；缓存目标 1.000000 GiB，均为三副本前逻辑容量。
- 并发 32，目标总速率 10,000 ops/s；预热 300 s，正式阶段 300 s。实际时长与速率以原始日志为准。
- 两个临时池与既有环境共用相同六个后端、六个缓存 OSD，均为三副本、host 故障域、64 PG。
- 初始化使用 IGNORE_CACHE|IGNORE_OVERLAY 直接装载后端；通过提升计数不增加核对，预热开始时测试缓存为空。
- 预热、正式负载为独立 YCSB JVM；预热保留 Ceph 缓存，不保证正式 JVM 的 JIT 已预热。
- 正式开始前保留预热产生的脏对象；尾部排空可能包含这些遗留回写，因此 W/D 是该窗口的写入代价，不是只归属于正式 UPDATE 的因果放大。
- 正式分母 D 只累计返回成功的 UPDATE 的 4096 B 载荷；READ 字节、初始化和预热不计入 D。
- 正式分子仅累加六块 SSD B 物理 NVMe 设备 stat 的写扇区差值 × 512；含两个 LV 的全部写入，排除 SSD A。
- 主窗口为正式开始至回写尾部结束。尾部把临时缓存的 dirty 阈值和最小 flush 年龄改为 0，排空后等待三个连续低写入窗口；这些控制引发的元数据写入也在分子内。
- 空闲对照另列，主值不扣后台。读回校验和测试池删除均在主窗口结束后进行。
- 指标是块层主机写入代价，不是 NAND/FTL 内部 WAF；三副本归一化只是除以三，不强制基线等于 1。

默认提升限速为每缓存 OSD 25 对象/s、5 MiB/s。为在短实验内形成容量压力，
于 20:25:20（北京时间，装载和预热开始前）把六个缓存 OSD 的两个提升限速都设为 0，即取消限速。
正式窗口开始前已全部生效，正式期间没有修改这些参数。原值和恢复记录见 promotion-overrides.json。
本轮预热和正式阶段提升参数固定；固定时长预热不等于已严格证明稳态。

## 主结果

| 指标 | 结果 |
|---|---:|
| 成功 READ | 2,845,924 |
| 成功 UPDATE | 149,891 |
| 实际 READ / UPDATE | 94.9967% / 5.0033% |
| 成功写入 D | 613,953,536 B，0.571789 GiB |
| 六块 SSD B 合计写入 W | 44,772,216,832 B，41.697376 GiB |
| W / D | **72.924438** |
| W / (3D) | **24.308146** |
| 负载期间 SSD B 写入 | 39.562962 GiB |
| 回写尾部 SSD B 写入 | 2.134415 GiB |
| 主窗口 | 329.978 s |
| 客户端失败 / CRC 错误 | 0 / 0 |

## 自然缓存活动

以下增量只取正式负载窗口，不包含排空尾部和手动清理；来自缓存 OSD 的累计计数差值。

| 计数器 | 正式窗口增量 |
|---|---:|
| agent_evict | 492,999 |
| agent_flush | 0 |
| op_cache_hit | 1,599,840 |
| tier_clean | 0 |
| tier_dirty | 18,043 |
| tier_evict | 492,999 |
| tier_flush | 0 |
| tier_promote | 492,945 |
| tier_proxy_read | 1,327,962 |
| tier_proxy_write | 70,332 |
| tier_try_flush | 0 |

## 六块 SSD B

| 主机 | 物理盘 | 整盘写入 GiB |
|---|---|---:|
| node1 | /dev/nvme2n1 | 6.969570 |
| node1 | /dev/nvme3n1 | 6.821255 |
| node2 | /dev/nvme3n1 | 6.515575 |
| node2 | /dev/nvme4n1 | 7.451355 |
| node3 | /dev/nvme3n1 | 6.104610 |
| node3 | /dev/nvme4n1 | 7.835011 |

SMART 主机写入交叉核对覆盖 4/6 块盘；这部分 SMART 合计 29,805,056,000 B，块层合计 29,804,662,784 B。
未取得 SMART 前后快照的设备：node3:/dev/nvme3n1、node3:/dev/nvme4n1；六块盘的块层主计数均完整。

## 两个 LV 的块层分账

| 区域 | LV 写入 GiB | 占整盘总量 |
|---|---:|---:|
| 后端 DB/WAL LV | 2.990639 | 7.172% |
| 缓存 OSD LV（含自身 DB/WAL） | 38.706738 | 92.828% |

物理盘总量减去两个 LV 之和的残差为 0 B。
LV 与物理设备属于不同的块层统计位置，采样时刻也略有差异；分账用于交叉核对，不再叠加到整盘主指标。
缓存 LV 的数值同时包含对象数据和缓存 OSD 自身的元数据，不能直接解释为热点数据量。

## OSD 写入路径交叉核对

| 路径计数 | GiB |
|---|---:|
| 后端 OSD BlueFS DB/日志写入 | 2.990295 |
| 缓存 OSD BlueFS DB/日志写入 | 32.145061 |
| 缓存 OSD 对象数据写入路径 | 6.557644 |

这些是 BlueFS/BlueStore 内部路径计数的近似归因，不是另一份物理盘总量。RocksDB submitted_bytes 是逻辑输入，不与 BlueFS 输出重复相加。

正式窗口观测样本中，缓存池 stored 为 763.475–767.948 MiB，dirty 为 22,121–39,563；样本见 cache-occupancy.csv。该范围不是严格硬容量限制。

## 吞吐与延迟

| 指标 | YCSB 日志值 |
|---|---:|
| OVERALL RunTime(ms) | 300107.0 |
| OVERALL Throughput(ops/sec) | 9982.489578716924 |
| READ AverageLatency(us) | 456.6898462502864 |
| READ 95thPercentileLatency(us) | 729.0 |
| READ 99thPercentileLatency(us) | 1125.0 |
| UPDATE AverageLatency(us) | 1199.5074754321474 |
| UPDATE 95thPercentileLatency(us) | 1699.0 |
| UPDATE 99thPercentileLatency(us) | 2599.0 |

排空后的短空闲对照为 32.415 s，六块 SSD B 写入 0 B，平均 0.000 B/s；不是与正式窗口等长的对照，未从主值扣除。

## 校验与范围

完整读回后端 1,048,576 条记录，检查大小及内嵌 CRC32；绕过缓存和 overlay，数据量 4.000000 GiB。
并发更新没有逐对象记录最终版本的客户端字节镜像，因此这里证明全量记录存在、大小与校验和正确，不声称逐对象与最后一次写入日志逐字节比对。

标准 NVMe SMART Data Units Written 以 512,000 B 为单位，交叉核对保存在 smart-crosscheck.json；它仍是主机侧量，不是 NAND 写入量。
本次只有一轮正式运行，没有重复实验置信区间。结果同时包含读引发的提升、写命中/未命中、回写合并以及后端元数据开销。

逐边界和周期原始计数见 raw-counters.json.gz；逐 SSD 数据见 per-ssd.csv；成功字节计数和命令/恢复记录见 results.json。
初始永久池的完整配置、全局 flags 和 balancer 状态在清理后逐项核对。
