# Workload A：混合 SSD B 整盘写放大

开始时间：2026-11-11 17:47:19（北京时间）。

运行通过：**True**；临时资源清理完成：**True**。

## 负载与测量口径

- 使用官方 YCSB **0.17.0 CoreWorkload** 的 Workload A：目标 50% READ / 50% UPDATE，Zipfian 分布。
- 采用本目录公开的自定义 RADOS binding：每条记录一个 4 KiB 字段，UPDATE 直接调用 `rados_write_full`。
- 每个 4 KiB 记录内含 8 B 标识与 CRC32；每次读取校验大小和 CRC32，写入字节计数包含这 8 B。
- 这不是默认的 10 字段/100 B 更新，也没有使用原 RADOS binding 的 delete+insert 更新。
- 数据集 256 条、0.000977 GiB；缓存目标 0.007812 GiB，均为三副本前逻辑容量。
- 并发 4，目标总速率 500 ops/s；预热 2 s，正式阶段 3 s。实际时长与速率以原始日志为准。
- 两个临时池与既有环境共用相同六个后端、六个缓存 OSD，均为三副本、host 故障域、64 PG。
- 初始化使用 IGNORE_CACHE|IGNORE_OVERLAY 直接装载后端；通过提升计数不增加核对，预热开始时测试缓存为空。
- 预热、正式负载为独立 YCSB JVM；预热保留 Ceph 缓存，不保证正式 JVM 的 JIT 已预热。
- 正式分母 D 只累计返回成功的 UPDATE 的 4096 B 载荷；READ 字节、初始化和预热不计入 D。
- 正式分子仅累加六块 SSD B 物理 NVMe 设备 stat 的写扇区差值 × 512；含两个 LV 的全部写入，排除 SSD A。
- 主窗口为正式开始至回写尾部结束。尾部把临时缓存的 dirty 阈值和最小 flush 年龄改为 0，排空后等待三个连续低写入窗口；这些控制引发的元数据写入也在分子内。
- 空闲对照另列，主值不扣后台。读回校验和测试池删除均在主窗口结束后进行。
- 指标是块层主机写入代价，不是 NAND/FTL 内部 WAF；三副本归一化只是除以三，不强制基线等于 1。

## 主结果

| 指标 | 结果 |
|---|---:|
| 成功 READ | 643 |
| 成功 UPDATE | 651 |
| 实际 READ / UPDATE | 49.6909% / 50.3091% |
| 成功写入 D | 2,666,496 B，0.002483 GiB |
| 六块 SSD B 合计写入 W | 41,226,240 B，0.038395 GiB |
| W / D | **15.460829** |
| W / (3D) | **5.153610** |
| 负载期间 SSD B 写入 | 0.025375 GiB |
| 回写尾部 SSD B 写入 | 0.013020 GiB |
| 主窗口 | 26.603 s |
| 客户端失败 / CRC 错误 | 0 / 0 |

## 自然缓存活动

以下增量只取正式负载窗口，不包含排空尾部和手动清理；来自缓存 OSD 的累计计数差值。

| 计数器 | 正式窗口增量 |
|---|---:|
| agent_evict | 0 |
| agent_flush | 0 |
| op_cache_hit | 1,254 |
| tier_clean | 0 |
| tier_dirty | 109 |
| tier_evict | 0 |
| tier_flush | 0 |
| tier_promote | 38 |
| tier_proxy_read | 20 |
| tier_proxy_write | 21 |
| tier_try_flush | 0 |

## 六块 SSD B

| 主机 | 物理盘 | 整盘写入 GiB |
|---|---|---:|
| node1 | /dev/nvme2n1 | 0.005718 |
| node1 | /dev/nvme3n1 | 0.007076 |
| node2 | /dev/nvme3n1 | 0.006401 |
| node2 | /dev/nvme4n1 | 0.006413 |
| node3 | /dev/nvme3n1 | 0.006561 |
| node3 | /dev/nvme4n1 | 0.006226 |

## 吞吐与延迟

| 指标 | YCSB 日志值 |
|---|---:|
| OVERALL RunTime(ms) | 3097.0 |
| OVERALL Throughput(ops/sec) | 417.82370035518244 |
| READ AverageLatency(us) | 439.92068429237946 |
| READ 95thPercentileLatency(us) | 572.0 |
| READ 99thPercentileLatency(us) | 883.0 |
| UPDATE AverageLatency(us) | 1205.205837173579 |
| UPDATE 95thPercentileLatency(us) | 1412.0 |
| UPDATE 99thPercentileLatency(us) | 2897.0 |

## 校验与范围

完整读回后端 256 条记录，检查大小及内嵌 CRC32；绕过缓存和 overlay，数据量 0.000977 GiB。
并发更新没有逐对象记录最终版本的客户端字节镜像，因此这里证明全量记录存在、大小与校验和正确，不声称逐对象与最后一次写入日志逐字节比对。

标准 NVMe SMART Data Units Written 以 512,000 B 为单位，交叉核对保存在 smart-crosscheck.json；它仍是主机侧量，不是 NAND 写入量。
本次只有一轮正式运行，没有重复实验置信区间。结果同时包含读引发的提升、写命中/未命中、回写合并以及后端元数据开销。

逐边界和周期原始计数见 raw-counters.json.gz；逐 SSD 数据见 per-ssd.csv；成功字节计数和命令/恢复记录见 results.json。
初始永久池的完整配置、全局 flags 和 balancer 状态在清理后逐项核对。
