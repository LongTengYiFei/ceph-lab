# 六个后端 OSD + 六个缓存 OSD：对象元数据实验环境

本目录保存本次集群改造的配置、脚本和验收记录。三台存储节点仍各运行 4 个 OSD，
但分为 2 个后端 OSD 和 2 个缓存 OSD；node4 继续作为主控与客户端。
集群 FSID、MON/MGR、原有 `.mgr` 数据和 `snapWrite` 运行版本均保留。

已完成配置与验收，结果见 [配置验收记录](acceptance-report.md)：12 OSD up/in，
129 PG active+clean，八个临时对象通过脏覆盖写、回写、淘汰与再次提升验证并全部清理。

Workload A 和 B 的实验布局、负载配置、写放大公式、测试流程、结果与写入来源分解，
统一整理在 [混合 SSD B 写放大测试报告](test-report-workload-a-b.md)。

## YCSB Workload A 整盘写放大结果

已完成一轮 4 GiB 数据、1 GiB 缓存目标的 YCSB A 测试：官方 0.17.0 CoreWorkload，
目标 50% READ / 50% UPDATE、Zipfian 分布，自定义 4 KiB 单字段 RADOS 覆盖写适配层。
预热和正式负载各 300 s，32 并发，目标 10,000 ops/s。

正式成功写入 **5.713173 GiB**，六块 SSD B 在正式负载加回写尾部共写入
**105.907230 GiB**，主机侧整盘写放大 **18.537375×**，除以三副本后为
**6.179125×**。正式窗口自然逐出计数为 716,692。全部 1,048,576 条记录从后端
读回并通过大小和 CRC32 检查，客户端错误为 0；临时池及提升限速变更均已恢复。

这轮为制造短时缓存容量压力，在预热期间取消了六个 cache OSD 的默认提升限速，
正式期间参数固定，结束后恢复 25 对象/s、5 MiB/s。它是该配置下的单轮探索性结果，
未证明稳态，也没有重复实验置信区间，不等同于 NAND/FTL 内部写放大。

完整结果见 [本轮报告](results/20261111T094932Z-ycsb-a/report.md)，
代码、复现与统计口径见 [YCSB 测试说明](ycsb/README.md)。

## YCSB Workload B 结果与 A 对照

同样的 4 GiB 数据、1 GiB 缓存、32 并发、300 s 预热与 300 s 正式阶段，
Workload B 为 95% READ / 5% UPDATE。成功更新 149,891 次，载荷
**0.571789 GiB**；六块 SSD B 整盘写入 **41.697376 GiB**，W/D 为
**72.924438×**，除以三副本后为 **24.308146×**。

本轮新增两个 LV 的直接块层分账：后端 DB/WAL 区写入 **2.990639 GiB（7.172%）**，
缓存 OSD 区写入 **38.706738 GiB（92.828%）**，两者之和与六盘整盘计数完全相等。
缓存 OSD 区含自身的 DB/WAL，内部计数估计其中 BlueFS DB/日志写入约 32.145061 GiB。
正式阶段自然逐出 492,999 次，未发生自然 agent_flush；尾部排空已计入主值。
全量后端大小/CRC32 校验通过，客户端错误为 0，临时资源和配置均已恢复。

B 的整盘绝对写入小于 A，但客户端写入分母也更小，因此比值更高。读触发的提升和
元数据写入会计入分子；不能将这个比值解释为 B 比 A 写盘更多。本轮装载前即取消
提升限速，A 则在预热期间调整，两轮预热历史不同。正式起点保留预热脏缓存，
尾部可能包含预热遗留回写；两轮均为单次探索性主机写入测量。

完整记录见 [B 报告](results/20261111T122359Z-ycsb-b/report.md)、
[A/B 对照](results/20261111T122359Z-ycsb-b/comparison-with-a.md) 和
[恢复核对](results/20261111T122359Z-ycsb-b/final-state.json)。

## 设备布局

每台机器前两块实验盘保留为后端 OSD 的整盘 `block`，后两块改为 LVM PV。
每块共享 SSD 单独创建一个 VG，不跨物理盘；两个 LV 使用相同的 extent 数：

- `backend_db`：对应后端 OSD 的 `block.db`。不单独设置 `block.wal`，WAL 与 DB 共置。
- `cache_data`：独立缓存 OSD 的 `block`；该缓存 OSD 自己的 DB/WAL 与缓存数据共置。

| 主机 | 后端 OSD / 数据设备 | DB 所在共享 SSD | 同盘缓存 OSD | VG |
|---|---|---|---|---|
| node1 | 0 / `/dev/nvme0n1` | `/dev/nvme2n1` | 2 | `objmeta_node1_0` |
| node1 | 1 / `/dev/nvme1n1` | `/dev/nvme3n1` | 3 | `objmeta_node1_1` |
| node2 | 4 / `/dev/nvme1n1` | `/dev/nvme3n1` | 6 | `objmeta_node2_0` |
| node2 | 5 / `/dev/nvme2n1` | `/dev/nvme4n1` | 7 | `objmeta_node2_1` |
| node3 | 8 / `/dev/nvme1n1` | `/dev/nvme3n1` | 10 | `objmeta_node3_0` |
| node3 | 9 / `/dev/nvme2n1` | `/dev/nvme4n1` | 11 | `objmeta_node3_1` |

设备名称是本次核实的名称；[layout.json](layout.json) 固定了磁盘序列号、容量、原 OSD
UUID、新缓存 OSD UUID 及 LV 路径。执行磁盘操作前再次校验这些身份，不能仅凭设备名选盘。

每块 SSD 原始大小为 1,920,383,410,176 B；每个 LV 为 **960,189,431,808 B**，
DB 与 cache 严格等大。与磁盘标称容量的差额来自 LVM 元数据、对齐，以及不能均分时
保留的一个 extent。六个后端数据设备约 11.52 TB，六个 DB LV 和六个缓存 LV 各约
5.76 TB，均为原始容量，不等于三副本可用容量。

本版本的 BlueStore `statfs` 将独立 DB 的容量计入总容量，而可用容量来自数据设备的
空闲分配器。因此 `ceph -s` 的原始已用量会显示约 **5.2 TiB**，主要对应六个独立
DB LV 的容量记账，不表示已经写入了 5.2 TiB 数据。实际 DB 占用应看验收记录中的
`bluefs.db_used_bytes`；数据盘上残留的 BlueFS 使用量应看 `slow_used_bytes`。

## 池与放置

| 池 | OSD 集合 | CRUSH 规则 | 副本 / min_size | PG |
|---|---|---|---|---|
| `.mgr` | 0、1、4、5、8、9 | `objmeta-backend` | 3 / 2 | 原有 1 PG |
| `objmeta-data` | 0、1、4、5、8、9 | `objmeta-backend` | 3 / 2 | 64 |
| `objmeta-cache` | 2、3、6、7、10、11 | `objmeta-cache` | 3 / 2 | 64 |

两条规则分别选择自定义设备类别 `backend`、`cache`，故障域均为 `host`，每个 PG 的
三个副本分布在三台机器上。不能用所有盘共同的自动 `ssd` 类别区分两个角色。
两个实验池关闭压缩和 PG 自动调整。DB LV 不是独立 OSD，不参与池的 CRUSH 放置。

`objmeta-cache` 是 `objmeta-data` 的 **writeback cache tier**，同时设置读写 overlay。
应用访问 `objmeta-data`，客户端库根据 OSDMap 自动路由；缓存与后端 OSD 不一一绑定。
本表的配对只描述哪块物理 SSD 存放哪个后端 OSD 的 DB。

默认缓存策略保存在 [layout.json](layout.json)：

- `hit_set_type=bloom`，12 个 HitSet，每个覆盖 300 秒。
- `min_read_recency_for_promote=1`，`min_write_recency_for_promote=0`。
- dirty / dirty-high / full 比例为 0.4 / 0.6 / 0.8。
- 最小回写年龄 60 秒，最小淘汰年龄 120 秒。
- `target_max_bytes` 按缓存原始总容量除以三副本，再取 80%，向下对齐到 GiB。
  确切值见 [deployment.json](deployment.json) 的 `completed.pools`。

该目标是池级逻辑字节目标，不是六个缓存 LV 的原始容量之和，也不是严格的物理空间
隔离上限。此配置保留较大的缓存目标；以后若用 16 GiB 等小负载观察淘汰，应根据具体
实验缩小目标。LVM 五五开不会因此改变。

## 改造方式与运行版本

OSD 继续运行 `snapWrite` 提交 `69fbba696bee9762a0025b6b128ddd978120ac78`，通过原有
systemd 模板和 `50-snapwrite.conf` drop-in 启动；MON/MGR 保持原版 17.2.7。
不切换或修改 Ceph 源码，不重新初始化集群 FSID。

脚本先把 `.mgr` 的放置规则迁到后端组，等待三副本 clean，再将六个旧缓存角色 OSD
标记 out、检查 `safe-to-destroy`、停止并 purge。只有这六块经过序列号核对的共享 SSD
会被重新格式化为 LVM。六个后端 OSD 的数据盘、ID 和 UUID 均保留。

后端 OSD 逐个停止，使用 `ceph-bluestore-tool bluefs-bdev-new-db` 和
`bluefs-bdev-migrate` 把已有 BlueFS/RocksDB 文件迁到独立 DB LV，执行 `fsck` 后启动，
每次等待 PG clean 才继续。工具使用同版本 17.2.7；计数器补丁未改变磁盘格式。
缓存 OSD 在新的 LV 上以原 ID、新 UUID 创建。

每个相关实例增加 `60-objectmeta-lvm.conf`，在 OSD 启动前激活所需 LV。
符号链接使用稳定的 `/dev/<vg>/<lv>` 路径，不使用会随启动变化的 `/dev/dm-N`。

## 操作入口

在 node4 的仓库根目录运行：

```bash
# 查看已固定的配置计划；对集群只读。
python3 -B Exp_ObjectMetaWriteAmplification/configure_cluster.py plan

# 执行或从已记录阶段继续；会改造指定的六块共享 SSD。
python3 -B -u Exp_ObjectMetaWriteAmplification/configure_cluster.py apply

# 核对运行版本、设备、标签、等分容量、池关系、PG 放置和状态。
python3 -B Exp_ObjectMetaWriteAmplification/configure_cluster.py verify

# 小规模写入、读回、回写、淘汰及再次提升，结束删除本次独有对象。
bash Exp_ObjectMetaWriteAmplification/validate_io.sh
```

`apply` 不会自动删除既有用户池。首次规划要求只有 `.mgr`；继续执行仅允许 `.mgr` 和
本目录的两个实验池。每个完整阶段写入 journal，可从中断处继续；遇到设备身份不符、
未预期的已有 LVM 或非预期数据时终止。已经成功完成的阶段不重复格式化。
不要删除 `layout.json` / `deployment.json` 后试图重新执行改造。

日常仍使用根目录 `clusterctl.py start/stop/restart/status` 和 `ceph.sh` / `rados.sh`。
根目录 `manifest.json` 会同步新设备角色；根目录 `clusterctl.py verify` 固定原版提交，
仍不适用于当前 `snapWrite` 版本，应使用上面的专用 verify。

## 文件与验收

| 文件 | 内容 |
|---|---|
| `configure_cluster.py` | 规划、阶段日志、Ceph 配置、清单更新和只读验收 |
| `node_storage.py` | 节点端序列号校验、LVM、DB 迁移、缓存 mkfs 及物理布局核对 |
| `validate_io.py` / `validate_io.sh` | 经过 overlay 和绕过 overlay 的数据校验 |
| `layout.json` | 固定的设备身份、配对、池参数及 UUID |
| `deployment.json` | 阶段完成记录、实际 LV 容量、缓存目标和结束状态 |
| `operations.jsonl` | 不含 keyring 的逐步操作记录 |
| `manifest.json` | 更新后的集群清单副本 |
| `validation.json` | 实时状态、OSD 元数据、所有 PG 放置和逐盘验证 |
| `io-validation.json` | 缓存写入、回写、淘汰、再次提升及对象清理记录 |
| `final-state.json` / `acceptance-report.md` | I/O 清理后的最终状态与验收说明 |
| `configuration/nodeN/` | 实际 ceph.conf 和各 OSD 的 LV 激活 drop-in 副本 |
| `backups/<UTC时间>/` | 改造前清单、状态、`.mgr` 导出；节点端还有 OSD 目录及配置备份 |

`backups/` 已忽略，节点上的 OSD 目录备份含 keyring，目录权限为 0700，不提交到 Git。
操作记录不记录传输中的 keyring。中途失败的控制台记录保留，后续成功与否以最终验收
和 `deployment.json` 为准。

本次是布局和缓存功能验收，不是性能或写放大正式测试。原有 PG 均衡脚本假设初始只有
`.mgr`，原有快照脚本也未定义本次两层测量口径，不能直接套用。统计共享 SSD 写入时，
需要区分后端 DB/WAL、缓存数据和缓存 OSD 自己的 DB/WAL；整盘写入计数不能当作纯元数据。

参考：[Quincy BlueStore](https://docs.ceph.com/en/quincy/rados/configuration/bluestore-config-ref/)、
[BlueFS 迁移工具](https://docs.ceph.com/en/quincy/man/8/ceph-bluestore-tool/)、
[Cache Tiering](https://docs.ceph.com/en/quincy/rados/operations/cache-tiering/)。
