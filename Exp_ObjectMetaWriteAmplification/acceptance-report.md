# 配置验收记录

验收时间：2026-11-11 16:30:52（北京时间）。

- 12 个 OSD 全部 up/in：后端 0、1、4、5、8、9；缓存 2、3、6、7、10、11。
- 六组共享 SSD 的 DB/cache LV 等大，每个 960,189,431,808 B。
- 六个后端 OSD 均使用独立 DB LV，BlueFS slow_used_bytes 为 0；迁移后 fsck 全部通过。
- 全部 OSD 保留 snapWrite 版本和 RocksDB submitted 计数器，六个后端 UUID 保留。
- .mgr / objmeta-data / objmeta-cache 分别为 1 / 64 / 64 PG，共 129 PG active+clean。
- 两层均为三副本、min_size=2，所有 PG 的三个副本分布在三台主机。
- writeback tier、读写 overlay、缓存容量及热点策略均已配置。
- 全局 flags 和 balancer 启用状态与改造前一致；node4 没有 OSD。
- 唯一健康告警为改造前已有的 MON_DISK_LOW。

I/O 验收使用八个各 256 KiB 的临时对象。先建立已缓存的干净对象，再覆盖为不同内容：
绕过 overlay 后直接核对缓存保存新值、后端仍保存旧值；手动回写后后端匹配新值；
淘汰后缓存对象不存在；通过后端池接口读回正确内容并观察再次提升。八个对象全部通过，
随后删除并回写、淘汰删除记录，直接核对两层均无这些测试对象。

Quincy 的首次冷写可以先代理到后端再提升，不能用首次写入后后端必须不存在作为
writeback 的验收条件；因此本次使用缓存命中后的覆盖写验证脏数据回写。初验记录
io-validation-initial.json 另行保留，最终结果为 io-validation.json。

实际 systemd LV 激活配置和 ceph.conf 副本保存在 configuration/。逐盘和逐 PG 证据
见 validation.json，最终集群状态见 final-state.json，I/O 证据见 io-validation.json。
本次没有执行正式性能或写放大负载。
