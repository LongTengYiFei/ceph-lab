# Workload A：混合 SSD B 整盘写放大

开始时间：2026-11-11 17:49:32（北京时间）。

运行通过：**True**；临时资源清理完成：**True**。

## 负载与测量口径

- 使用官方 YCSB **0.17.0 CoreWorkload** 的 Workload A：目标 50% READ / 50% UPDATE，Zipfian 分布。
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
于 17:55:23（北京时间，预热期间）把六个缓存 OSD 的两个提升限速都设为 0，即取消限速。
正式窗口开始前已全部生效，正式期间没有修改这些参数。原值和恢复记录见 promotion-overrides.json。
本轮预热中调整过提升参数，不能据此声称已严格证明稳态。

## 主结果

| 指标 | 结果 |
|---|---:|
| 成功 READ | 1,498,081 |
| 成功 UPDATE | 1,497,674 |
| 实际 READ / UPDATE | 50.0068% / 49.9932% |
| 成功写入 D | 6,134,472,704 B，5.713173 GiB |
| 六块 SSD B 合计写入 W | 113,717,022,720 B，105.907230 GiB |
| W / D | **18.537375** |
| W / (3D) | **6.179125** |
| 负载期间 SSD B 写入 | 99.878952 GiB |
| 回写尾部 SSD B 写入 | 6.028278 GiB |
| 主窗口 | 352.715 s |
| 客户端失败 / CRC 错误 | 0 / 0 |

## 自然缓存活动

以下增量只取正式负载窗口，不包含排空尾部和手动清理；来自缓存 OSD 的累计计数差值。

| 计数器 | 正式窗口增量 |
|---|---:|
| agent_evict | 716,692 |
| agent_flush | 81,750 |
| op_cache_hit | 1,726,815 |
| tier_clean | 81,743 |
| tier_dirty | 151,476 |
| tier_evict | 716,692 |
| tier_flush | 0 |
| tier_promote | 790,320 |
| tier_proxy_read | 648,993 |
| tier_proxy_write | 648,444 |
| tier_try_flush | 28,722 |

## 六块 SSD B

| 主机 | 物理盘 | 整盘写入 GiB |
|---|---|---:|
| node1 | /dev/nvme2n1 | 18.561470 |
| node1 | /dev/nvme3n1 | 16.692139 |
| node2 | /dev/nvme3n1 | 17.998535 |
| node2 | /dev/nvme4n1 | 17.437683 |
| node3 | /dev/nvme3n1 | 14.957756 |
| node3 | /dev/nvme4n1 | 20.259647 |

SMART 主机写入交叉核对覆盖 4/6 块盘；这部分 SMART 合计 75,902,976,000 B，块层合计 75,902,623,744 B。
未取得 SMART 前后快照的设备：node3:/dev/nvme3n1、node3:/dev/nvme4n1；六块盘的块层主计数均完整。

## OSD 写入路径交叉核对

| 路径计数 | GiB |
|---|---:|
| 后端 OSD BlueFS DB/日志写入 | 22.673916 |
| 缓存 OSD BlueFS DB/日志写入 | 64.454548 |
| 缓存 OSD 对象数据写入路径 | 18.769181 |

这些是 BlueFS/BlueStore 内部路径计数的近似归因，不是另一份物理盘总量。RocksDB submitted_bytes 是逻辑输入，不与 BlueFS 输出重复相加。

正式窗口观测样本中，缓存池 stored 为 883.179–970.241 MiB，dirty 为 46,538–98,690；样本见 cache-occupancy.csv。该范围不是严格硬容量限制。

## 吞吐与延迟

| 指标 | YCSB 日志值 |
|---|---:|
| OVERALL RunTime(ms) | 300123.0 |
| OVERALL Throughput(ops/sec) | 9981.757479433432 |
| READ AverageLatency(us) | 1316.9827198929831 |
| READ 95thPercentileLatency(us) | 1814.0 |
| READ 99thPercentileLatency(us) | 30847.0 |
| UPDATE AverageLatency(us) | 2496.998499673494 |
| UPDATE 95thPercentileLatency(us) | 4763.0 |
| UPDATE 99thPercentileLatency(us) | 42687.0 |

排空后的短空闲对照为 32.125 s，六块 SSD B 写入 0 B，平均 0.000 B/s；不是与正式窗口等长的对照，未从主值扣除。

## 校验与范围

完整读回后端 1,048,576 条记录，检查大小及内嵌 CRC32；绕过缓存和 overlay，数据量 4.000000 GiB。
并发更新没有逐对象记录最终版本的客户端字节镜像，因此这里证明全量记录存在、大小与校验和正确，不声称逐对象与最后一次写入日志逐字节比对。

标准 NVMe SMART Data Units Written 以 512,000 B 为单位，交叉核对保存在 smart-crosscheck.json；它仍是主机侧量，不是 NAND 写入量。
本次只有一轮正式运行，没有重复实验置信区间。结果同时包含读引发的提升、写命中/未命中、回写合并以及后端元数据开销。

逐边界和周期原始计数见 raw-counters.json.gz；逐 SSD 数据见 per-ssd.csv；成功字节计数和命令/恢复记录见 results.json。
初始永久池的完整配置、全局 flags 和 balancer 状态在清理后逐项核对。
