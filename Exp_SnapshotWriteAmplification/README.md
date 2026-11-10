# Ceph 快照逻辑元数据写放大实验

基线是原版 **Ceph 17.2.7**，提交
`b12291d110049b2f35e32e0de30d70e9a4c060d2`。
补丁对应本次实验源码提交 `69fbba696bee9762a0025b6b128ddd978120ac78`。

本目录只保存可提交到 GitHub 的代码、说明和必要数据。重新运行产生的文件统一写入
`output/`，该目录已被仓库 `.gitignore` 排除。

## 文件

| 文件 | 用途 |
|---|---|
| `ceph-17.2.7-rocksdb-input-counters.patch` | 相对原版的完整补丁，包含计数器、单元测试和口径说明 |
| `run.sh` | 测试入口，设置匹配的 Python 绑定、动态库和 RDMA memlock |
| `test_snapwrite.py` | 创建测试池，串行执行对照、采集计数器、读回验证和清理 |
| `test_append.py` | 16 MiB 大对象、512 KiB 追加与周期快照的串行对照 |
| `snapwrite_osd_probe.py` | 在 OSD 节点通过 admin socket 读取/重置计数器 |
| `combine_snapwrite.py` | 按 deferred 为零的条件选择完整成对重复 |
| `analyze_snapwrite.py` | 生成逐轮 CSV、均值/置信区间、报告和图；默认只输出 PNG |
| `results.csv` | 本次 5 组有效对照的 50 行逐轮成对数据 |
| `results.tar.gz` | 原始正式测试、补跑和筛选记录，包含原始 OSD 计数快照 |
| `append-results.tar.gz` | 大对象追加场景的完整结果、原始计数器、逐轮 CSV 和报告 |
| `append-test-report.md` | 大对象追加测试正式报告：定量结果、统计口径和已有源码定性分析 |

## 补丁和统计口径

在干净的原版源码检出中应用；已经运行本次 `snapWrite` 分支的机器无需重复应用：

```bash
git -C /path/to/ceph switch -c snapWrite b12291d110049b2f35e32e0de30d70e9a4c060d2
git -C /path/to/ceph apply --check /path/to/ceph-lab-17.2.7/Exp_SnapshotWriteAmplification/ceph-17.2.7-rocksdb-input-counters.patch
git -C /path/to/ceph apply /path/to/ceph-lab-17.2.7/Exp_SnapshotWriteAmplification/ceph-17.2.7-rocksdb-input-counters.patch
```

补丁增加 `rocksdb.submitted_txns`、`submitted_ops`、`submitted_bytes`，
覆盖同步/异步两条成功提交路径。字节数是序列化 WriteBatch 的大小，包含每批次
12 B 固定头、key/value 和编码开销；空事务也计入，失败提交不计入。
RocksDB 内部 WAL、flush、compaction 的输出不计入。

这些是 OSD 级总计数，没有按 pool 拆分。baseline 和 snapshot 必须串行执行。
deferred 事务可能包含用户数据，因此本次只采用测量窗口内
`bluestore.issued_deferred_write_bytes` 增量为零的完整成对重复。

增量编译目标：

```bash
cmake --build /path/to/ceph/build --parallel 6 --target \
  ceph-osd ceph_test_keyvaluedb erasure_code_plugins compressor_plugins
```

OSD 与插件必须使用同一版本。新增的 3 项 RocksDB 单元测试已经通过；
本次完整 KV 测试为 30 项通过、6 项 MemDB 不适用而跳过。
单元测试过滤器为 `KeyValueDB/KVTest.SubmittedCounters*/0`，应在临时工作目录运行。

## 在当前实验集群重新运行

本目录依赖仓库根目录已有的 `ceph.sh`、`rados.sh`，以及 Ceph 构建生成的 Python
`rados` 绑定。默认源码路径 `/home/cyf/ceph-dpu`，可用 `CEPH_SOURCE_DIR` 覆盖；
实验仓库路径默认取本目录的父目录，可用 `CEPH_LAB_DIR` 覆盖。

当前采集布局为 node1 的 OSD 0–3、node2 的 4–7、node3 的 8–11；
SSH 用户为 cyf，端口为 23579，需要免密 SSH 和 `sudo -n`。
首先将采集脚本放到三个 OSD 节点的相同目录：

```bash
for host in node1 node2 node3; do
  ssh "$host" 'mkdir -p /home/cyf/ceph-lab-17.2.7/Exp_SnapshotWriteAmplification'
  rsync -a Exp_SnapshotWriteAmplification/snapwrite_osd_probe.py \
    "$host":/home/cyf/ceph-lab-17.2.7/Exp_SnapshotWriteAmplification/
done

./Exp_SnapshotWriteAmplification/run.sh --label formal
```

默认每池 10,000 个 4 KiB 对象，32 PG、单副本、关闭压缩，并发 32，offset 0
覆盖十轮，做五组成对重复。初始化数据不计入覆盖写结果；snapshot 组在初始化后
只创建一次快照，十轮之间不再创建。奇数次先 baseline，偶数次先 snapshot。
每组前重置计数器，每轮用全部 OSD 的起止差值统计。结束后删除本次新建的临时池，
并恢复临时 `mon_allow_pool_size_one` 配置和 scrub 标志。

测试入口检查实际运行的计数器；补丁应用后的 Git 提交号可以变化。
需要严格固定版本时，可附加 `--expected-osd-commit <SHA>`。

### 大对象追加与周期快照

完整测试报告见 [大对象追加与周期快照测试报告](append-test-report.md)，包含累计和逐轮结果、
每对象平均开销、deferred 与数据分配校验，以及已完成的源码定性分析。

```bash
./Exp_SnapshotWriteAmplification/run.sh append
```

此入口默认初始写入 1,024 个 16 MiB 对象；snapshot 组创建 S0，随后每对象追加
512 KiB，共十轮，每轮完成后创建 S1–S10，因此共有 11 个快照。baseline 组重放
相同的初始数据和十轮追加，并设置近似匹配快照创建时长的空闲窗口。两组串行运行，
初始装载、追加写和快照/空闲窗口分别计数；单副本、32 PG、并发 32、关闭压缩。

数据只在内存中确定性生成，不在客户端保存 16 GiB 输入文件。每阶段检查全部 OSD
计数收敛并记录 deferred 字节，不把该诊断计数直接从提交量中扣除。
全部对象校验最终大小，抽样完整核对 head 和所有快照的内容。每组结束删除其测试池，
等待清理收敛后才开始下一组。命令支持 `--objects`、`--rounds` 等参数。

输出仍在已忽略的 `output/<UTC时间>-append/`，只保留 `results.json`、
`raw-counters.json.gz`、`comparison.csv`、`summary.json`、`report.md`；
原始计数快照集中压缩在一个文件中。
已有结果可用 `run.sh append --summarize <结果目录>` 离线重新生成汇总，不会访问集群。

首次追加实验已完成，一组成对对照的累计 RocksDB 提交量如下（MB=1,000,000 B）：

| 范围 | 无快照 | 有快照 | 有/无 |
|---|---:|---:|---:|
| 初始 16 GiB 装载 | 23.481514 MB | 23.481347 MB | 约 1.00 |
| 十轮共 5 GiB 追加 | 162.447122 MB | 578.733328 MB | 3.56260 |
| 完整流程，含快照创建/匹配空闲窗口 | 185.928636 MB | 603.366171 MB | 3.24515 |

两组的初始装载和全部追加窗口均为零 deferred。快照创建阶段另外出现
1,081,344 B 的集群内部 deferred 写入，已单列；没有直接减去该诊断计数。
追加窗口的物理数据新增分配均恰为 512 MiB/轮。快照组生成 10,240 个 clone，
S0–S10 的三对象抽样完整内容和全部 1,024 个 head 大小均通过校验。
这是一组成对探索性结果，没有重复实验的置信区间。

逐轮高值与 extent-map 重新分片同步：无快照组奇数轮为 0 次、偶数轮为 1,024 次；
有快照组每轮还有新 clone 的映射处理，分别为 1,024/2,048 次。
这些诊断计数也在 CSV 内，不能把逐轮波动简单归因于快照数量。

#### 追加场景的源码归因

以下是对本次固定源码和已有计数的分析。**实测总量、源码推导和待补测的分项字节数
需要区分**；原始计数器尚未记录每类 key/value 的提交量。

每对象每次追加平均提交 56,516.93 B（有快照）和 15,863.98 B（无快照），
差额为 40,652.95 B。此差额包含以下几部分，不能全部理解为 SnapSet 关系表：

| 额外内容 | 写入路径和数据库记录 |
|---|---|
| 旧版本 clone 的对象信息与完整映射 | `PrimaryLogPG::make_writeable()` → `_make_clone()` → `BlueStore::_clone()` → `ExtentMap::dup()`；新增 clone onode 和 extent-map shards，属于 `O` 前缀 |
| 映射中复制的 blob 信息和校验和 | `Blob::dup()` 复制 `bluestore_blob_t`；`ExtentMap::encode_some()` 调用 blob 编码，包含物理 extent、共享 ID 和 `csum_data`。共享原始数据并没有使这些序列化元数据只保存一份 |
| 共享数据的引用计数 | `ExtentMap::dup()` 对每个被克隆的 blob 调用 `get_ref()` / `write_shared_blob()`；`_txc_write_nodes()` 将共享引用映射写入 `X` 前缀。已经共享的旧 blob 在下一次 clone 时仍需增加并提交引用计数 |
| head 的 SnapSet 与新共享标记 | `make_writeable()` 增加 `clones`、`clone_size`、`clone_snaps`、`clone_overlap`；`finish_ctx()` 编码整个 SnapSet 到 head 的 `snapset` 属性，随 onode 提交。首次转为共享的 blob 还会使 head 的相关 extent-map shards 变脏 |
| 快照对象索引和 PG 日志 | `PG::update_snap_map()` → `SnapMapper::add_oid()`，写入 `OBJ_` / `SNA_` 两种 omap 索引；`make_writeable()` 额外生成 `CLONE` 日志记录 |
| key 和编码开销 | 当前总计数包含每次 Put/Delete/Merge 的 key 和编码，以及每个 WriteBatch 的 12 B 批次头。reshard 会删除旧分片 key 并提交新分片 |

已有 perf 增量给出初始装载共创建 262,144 个 blob，即 **256 个/对象**；
每轮追加创建 8,192 个，即 **8 个/对象**。两种情况均对应 64 KiB/blob。
因此第 r 轮 clone 需要遍历 `256 + 8 × (r - 1)` 个 blob，而不只是处理本轮新增的 8 个。
按这一路径推导，十轮共涉及 2,990,080 次共享 blob 的事务内登记/更新；该数不是已经
新增的分类计数，最终提交仍需在 WriteBatch 边界核对。全部 KV 操作数的实测增量则为
`4,125,175 - 544,768 = 3,580,407` 次。

当前运行配置为 `bluestore_clone_cow=true`、`bluestore_csum_type=crc32c`、
SSD 最大 blob 64 KiB，设备块 4 KiB；测试没有设置改变校验粒度的 alloc hint。
结合 `_choose_write_options()`、`_do_alloc_write()` 和 blob 编码，可推导一个新 clone
中，仅校验和字节就从首轮的 `16 MiB / 4 KiB × 4 B = 16,384 B`，增加到第十轮的
20,992 B，平均 **18,688 B/clone**，十轮共 **191,365,120 B**。
这是源码和参数推导的组成项，未计其外层编码、映射与 key，不能当作已实测的分项占比。
首次 clone 还需把原有 256 个 blob 转为共享并重写 head 的相关映射；后续通常只需
将上一轮新增的 8 个 blob 转为共享，但所有被 clone 引用的 blob 仍需更新共享计数。

SnapSet 关系表本身小得多。在本次每个 clone 对应一个快照、重叠范围为一个区间的
场景，按 `SnapSet::encode()` 推导，每增加一个 clone 增长 72 B：clone ID 8 B、
重叠映射 28 B、大小映射 16 B、快照归属映射 20 B。第 r 轮 head 比无快照多
`72 × r` B 的 SnapSet 内容，十轮全体对象合计 4,055,040 B；这仍是编码推导，
并不涵盖 clone 的块映射和共享引用记录。

逐轮奇偶波动还涉及 extent-map 的重新分片。`ExtentMap::update()` 检查编码大小；
`reshard()` 删除旧 key 并写入新分片。本版本 `request_reshard()` 从清零的 begin
取更小值，能将尾部请求扩展到偏移 0，因而追加也可能造成大范围映射重写。
实测无快照组奇数轮 0 次、偶数轮 1,024 次；快照组分别为 1,024 和 2,048 次。
这些路径解释了观测到的波动，但现有计数不足以把每个峰值精确拆成各项字节。

若要给 40.7 KB 的额外提交量做严格分项，应在现有成功提交边界增加完整 WriteBatch
分类统计：`O` 中区分 head/clone 的 onode 和 extent shards，`X` 单独计共享引用，
omap 中再区分快照索引和 PG 日志，并单列分配位图、deferred、其他操作和批次头。
onode 内还可按属性解码细分 SnapSet；blob 编码内可单列校验和。
最终要求各类序列化字节之和逐窗口等于现有 `submitted_bytes`，再串行重放相同对照。
只查看最终 RocksDB/SST 大小会漏掉已经覆盖或删除的历史提交。
现有 `RocksWBHandler` 调试打印在 50 个操作后停止，也不能直接用作完整字节账本。

源码定位（原版 17.2.7，与当前分支这些文件一致）：
[clone 触发与 SnapSet](https://github.com/ceph/ceph/blob/v17.2.7/src/osd/PrimaryLogPG.cc#L8521)、
[extent-map 复制](https://github.com/ceph/ceph/blob/v17.2.7/src/os/bluestore/BlueStore.cc#L2484)、
[blob 编码与校验和](https://github.com/ceph/ceph/blob/v17.2.7/src/os/bluestore/bluestore_types.h#L435)、
[共享引用持久化](https://github.com/ceph/ceph/blob/v17.2.7/src/os/bluestore/BlueStore.cc#L12723)、
[SnapSet 编码](https://github.com/ceph/ceph/blob/v17.2.7/src/osd/osd_types.cc#L5772)。

另外，现有计数仅覆盖 OSD。创建 pool 快照会在 MON 更新 pool/OSDMap 状态，OSD
也会持久化接收到的 map；本实验没有累计 MON 自身的数据库提交量。创建快照阶段的
1.15 MB 是 OSD 侧窗口总提交，含已报告的内部 deferred 数据，不能直接解释为
快照关系表的纯元数据大小。

本地详细结果位于 `output/20261109T184114Z-append-formal/`。
GitHub 提交只需保留一个 `append-results.tar.gz`；解压后可直接读 CSV/报告，
也可以仅用 Python 标准库重新生成汇总：

```bash
mkdir -p Exp_SnapshotWriteAmplification/output/append-saved
tar -xzf Exp_SnapshotWriteAmplification/append-results.tar.gz \
  -C Exp_SnapshotWriteAmplification/output/append-saved
python3 -B Exp_SnapshotWriteAmplification/test_append.py \
  --summarize Exp_SnapshotWriteAmplification/output/append-saved
```

单次输出可直接分析；需要排除受干扰的重复时，先补跑完整对照，再合并：

```bash
python3 -B Exp_SnapshotWriteAmplification/combine_snapwrite.py \
  <正式结果目录> <补跑结果目录> --output <汇总目录> --pairs 5
python3 -B Exp_SnapshotWriteAmplification/analyze_snapwrite.py <汇总目录>
```

## 本次结果和离线复算

下表是每轮元数据提交量的五组均值，MB 为十进制单位：

| 阶段 | 无快照 | 有快照 | 有/无比值 |
|---|---:|---:|---:|
| 第一次覆盖 | 8.951902 MB | 26.934158 MB | 3.00876 |
| 第 2–10 轮平均 | 8.951966 MB | 9.513320 MB | 1.06271 |
| 十轮整体平均 | 8.951959 MB | 11.255404 MB | 1.25731 |

第一次每对象额外约 1,798 B，后续约 56.14 B。原始正式测试第 3 组成对重复的
baseline 第 7–10 轮出现 deferred 数据，整组成对结果被排除并完整补跑。
原记录没有丢弃，均保存在压缩包中。结果不扣除后台噪声，空闲窗口也在原始数据内。
本实验度量逻辑元数据输入，不代表 SSD 实际写放大。

不接触集群即可重现 CSV 和图表；分析脚本需要 Python 3 和 matplotlib：

```bash
mkdir -p Exp_SnapshotWriteAmplification/output/history
tar -xzf Exp_SnapshotWriteAmplification/results.tar.gz \
  -C Exp_SnapshotWriteAmplification/output/history
python3 -B Exp_SnapshotWriteAmplification/combine_snapwrite.py \
  Exp_SnapshotWriteAmplification/output/history/formal \
  Exp_SnapshotWriteAmplification/output/history/recheck \
  --output Exp_SnapshotWriteAmplification/output/reproduced --pairs 5
python3 -B Exp_SnapshotWriteAmplification/analyze_snapwrite.py \
  Exp_SnapshotWriteAmplification/output/reproduced
cmp Exp_SnapshotWriteAmplification/results.csv \
  Exp_SnapshotWriteAmplification/output/reproduced/paired-rounds.csv
```

需要其他图格式时，分析命令附加 `--plot-formats png pdf svg`。
压缩包中 `formal/` 和 `recheck/` 是原始来源，`validated/` 保留本次筛选结果。
原始 JSON 中的绝对路径是历史记录；离线复算按传入的新目录读取，不依赖旧路径。

当前运行的 12 个 OSD 通过 systemd drop-in 使用各节点的
`snapwrite-artifacts/69fbba696bee9762a0025b6b128ddd978120ac78/`。
这些节点上的运行文件仍保留，不属于本目录的 GitHub 提交内容。
