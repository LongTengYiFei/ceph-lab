# YCSB Workload A / B 与混合 SSD B 写入量

使用官方 YCSB **0.17.0** 的 CoreWorkload、Zipfian 生成器。
Workload A 为 50% READ / 50% UPDATE，Workload B 为 95% READ / 5% UPDATE。
记录格式改为一个固定 4 KiB 字段，`writeallfields=true`。读写比例是操作数比例。

本目录的 RADOS binding 是独立适配层：UPDATE 直接调用 `rados_write_full`，没有使用
官方旧 RADOS binding 的 delete+insert，也没有 JSON 序列化。记录内的 8 B 标识和 CRC32
属于这 4096 B 载荷的一部分。所有成功读操作检查大小和 CRC32；所有写成功返回后才累计
4096 B 到客户端分母。正式负载结束后，从后端完整读回全部记录校验。

## 准备

```bash
bash Exp_ObjectMetaWriteAmplification/ycsb/bootstrap.sh
```

脚本将 Maven 3.9.9 和 YCSB 0.17.0 放在已忽略的 `tools/`，校验源码压缩包 SHA256，
只构建官方 core 和本目录 JNI 适配层，不安装系统 Maven。需要 Java 11、JDK 头文件、
GCC，以及本环境已经构建的 Ceph 头文件和 librados。

## 运行与统计

```bash
# 256 个对象的端到端检查；不要求发生自然逐出。
python3 -B -u Exp_ObjectMetaWriteAmplification/run_ycsb.py --smoke

# 正式配置：4 GiB（1,048,576 个 4 KiB 对象）、1 GiB 缓存目标、32 并发，
# 预热 300 s、正式 300 s，目标总速率 10,000 ops/s。
python3 -B -u Exp_ObjectMetaWriteAmplification/run_ycsb.py --workload a --unlimited-promotion

# 相同数据集、缓存和窗口的 Workload B。
python3 -B -u Exp_ObjectMetaWriteAmplification/run_ycsb.py --workload b --unlimited-promotion
```

每轮使用独立临时池和相同的 backend/cache CRUSH 规则，均为三副本、host 故障域、64 PG。
永久的 `objmeta-data` / `objmeta-cache` 配置不变。初始数据通过 IGNORE_CACHE 和
IGNORE_OVERLAY 直接写后端，明确建立冷缓存起点；load 不计入主结果。预热和正式阶段
通过正常 overlay 访问。两段是独立 JVM，只保留 Ceph 缓存状态，不声称正式 JVM 的
JIT 已完成预热。
正式开始前不额外排空预热脏对象，因此尾部回写可能包含预热遗留数据；本指标描述
测量窗口的主机写入代价，不能把全部分子逐条归属于正式 UPDATE。

**提升限速与短时逐出实验：** Quincy 默认每 OSD 只提升 25 对象/s、5 MiB/s。
仅按默认值运行几分钟，可能还没填满 1 GiB 缓存，脚本会因正式窗口没有自然逐出而失败，
不会把它标记为有效逐出实验。`--unlimited-promotion` 会在装载和预热前保存原配置、
取消六个缓存 OSD 的提升限速，并在正常结束或捕获到异常后自动恢复。
首轮 A 是在预热期间手动取消限速，B 则在装载前自动设置；两轮正式阶段均固定为不限速，
但预热历史不同，不能把差异全部归因于读写比例。

以下工具用于检查点恢复或旧流程手动设置；已经自动应用时不要重复执行 apply：

```bash
python3 -B -u Exp_ObjectMetaWriteAmplification/ycsb_promote.py apply \
  Exp_ObjectMetaWriteAmplification/results/<本轮目录>
```

它只将六个 cache OSD 的 `osd_tier_promote_max_objects_sec` 和
`osd_tier_promote_max_bytes_sec` 临时设为 0（取消限速），先保存每项原显式配置和有效值。
若进程被强制终止而未进入自动恢复，按保存目录执行：

```bash
python3 -B -u Exp_ObjectMetaWriteAmplification/ycsb_promote.py restore \
  Exp_ObjectMetaWriteAmplification/results/<本轮目录>
```

主结果为正式开始至回写尾部结束，六块 SSD B 物理 NVMe `stat` 的写扇区增量之和乘
512。分母只取正式阶段成功 UPDATE 的实际 4096 B 载荷，不包含 READ、load、warmup。
READ 触发的缓存提升及相应元数据写入会进入分子，所以读比例更高时 W/D 可能升高；
比较负载时也应同时看 W 和 D 的绝对量。
另外报告除以三副本后的归一化值。物理盘与 LV 不重复相加，SSD A 不计入主结果。
从 B 开始同步采集两个 LV 的块层写入计数，以区分后端 DB/WAL 与缓存 OSD 区域。
缓存 OSD LV 包含缓存对象和该 OSD 自身的 DB/WAL，不能当作纯热点数据。
标准 SMART Data Units Written 以 512,000 B/单位交叉核对，不当作 NAND 写入量；
优先使用 nvme-cli，无该工具时尝试 smartctl，缺少快照的设备在报告中列明。

停止负载后，通过把临时缓存的 dirty 阈值和最小回写年龄设为 0，使脏对象排空；随后等待
三个连续约 2 s 的低写入窗口，每窗口六盘合计不超过 1 MiB。尾部控制动作和后台写入
纳入主值，不强制 RocksDB compaction。再单独记录 30 s 短空闲对照，不扣除背景。
全量读回、删除临时池及恢复操作在主窗口外。

每 10 s 采集六盘及 12 个 OSD，保留 PID、进程启动时间和 boot ID，检测进程变化及计数
异常。正式窗口要求 `agent_evict` 和 `tier_promote` 增量均大于 0。`tier_flush` /
`tier_try_flush` 与 agent 计数分别报告，不把尝试数冒充成功次数。

## 结果与恢复

每轮保存在 `results/<UTC时间>-ycsb-a/` 或 `results/<UTC时间>-ycsb-b/`；不删除失败和 smoke 历史。

- `results.json`：参数、成功读写计数、源文件 SHA256、命令、清理与最终状态。
- `summary.json` / `per-ssd.csv`：主比值、三副本归一化、逐盘写入量和自然缓存事件。
- `per-lv.csv`：从 B 开始记录的两个 LV 写入量；与整盘计数交叉核对，不重复相加。
- `load.log` / `warmup.log` / `formal.log`：原始 YCSB 状态及吞吐、延迟分位数。
- `*-client.json`：适配层在成功返回后累计的精确写入字节与错误计数。
- `raw-counters.json.gz`：逐边界与每 10 s 的整盘、LV、SMART 和完整 OSD perf 快照。
- `source/`：本轮实际使用的 Python、Java、C 和负载属性文件，与 results.json 中 SHA256 对应。
- `promotion-overrides.json`：提升限速原值、变更时间与恢复状态。
- `readback.json`：全部记录的大小/CRC32 检查结果，绕过缓存读取后端。
- `report.md` / `stages.csv` / `smart-crosscheck.json`：离线报告、分阶段写入和 SMART 核对。

若异常中断后遗留本轮临时池，按保存的结果恢复清理：

```bash
python3 -B -u Exp_ObjectMetaWriteAmplification/run_ycsb.py \
  --cleanup-result Exp_ObjectMetaWriteAmplification/results/<本轮目录>
```

`--cleanup-result` 只允许本目录固定前缀的临时池。清理时先排空脏数据，移除 overlay，
通过 readproxy 转为 none，再解除 tier 并删除本轮池，恢复 balancer。它不替代上面的
提升限速恢复步骤。清理后核对永久池完整配置和原全局 flags。

离线重建报告：

```bash
python3 -B Exp_ObjectMetaWriteAmplification/report_ycsb.py \
  Exp_ObjectMetaWriteAmplification/results/<本轮目录>
```

本次是单轮探索性结果，不提供重复实验置信区间，也不把主机侧统计解释为 NAND/FTL WAF。
缓存提升和后台操作没有按池独立的整盘字节计数，因此仍保留 `.mgr` 等背景写入。
