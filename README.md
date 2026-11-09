# Ceph 17.2.7：三节点、12 OSD 实验集群

本目录独立于 Git 源码分支，主控机是 node4。另一个 session 从这里接手即可。

- 四台源码目录均为 `/home/cyf/ceph-dpu`。
- 指定提交：`b12291d110049b2f35e32e0de30d70e9a4c060d2`，Ceph Quincy 17.2.7。
- 使用 `build/bin` 和 `build/lib`，不通过系统安装的 Ceph 程序启动。
- 统一 `RelWithDebInfo` 构建，开启 `WITH_RDMA`，关闭 `WITH_CEPH_DEBUG_MUTEX`。
- 此提交为原版 Ceph，不包含之前的 DOCA EC／解压缩改动。

## 日常使用

在 node4 上运行：

```bash
python3 /home/cyf/ceph-lab-17.2.7/clusterctl.py status
python3 /home/cyf/ceph-lab-17.2.7/clusterctl.py stop
python3 /home/cyf/ceph-lab-17.2.7/clusterctl.py start
python3 /home/cyf/ceph-lab-17.2.7/clusterctl.py restart
python3 /home/cyf/ceph-lab-17.2.7/clusterctl.py verify
```

这些命令管理现有集群，保留池和对象。`verify` 检查四台源码提交、运行中的 OSD 版本和主机分布、12 OSD up/in、PG clean，以及 node4 没有 OSD 服务。

Ceph 和 RADOS 命令：

```bash
/home/cyf/ceph-lab-17.2.7/ceph.sh -s
/home/cyf/ceph-lab-17.2.7/ceph.sh osd tree
/home/cyf/ceph-lab-17.2.7/rados.sh lspools
```

两个包装脚本设置本次构建的动态库路径，并提供 RDMA 客户端需要的 unlimited memlock。

## 布局

| 主机 | 角色 | OSD | 使用的原实验盘 |
|---|---|---|---|
| node1 | MON node1、MGR m1、4 个 OSD | 0–3 | nvme0n1–nvme3n1 |
| node2 | 4 个 OSD | 4–7 | nvme1n1–nvme4n1 |
| node3 | 4 个 OSD | 8–11 | nvme1n1–nvme4n1 |
| node4 | 主控机、客户端 | 无 | 保留其四块原 OSD 盘，未重新初始化 |

设备序列号、FSID、逐盘映射和源码备份引用以本目录 `manifest.json` 为准。设备名可能随重启变化，重新初始化前必须核对序列号。

MON 地址为 node1 的 `10.26.57.54`；OSD public/cluster network 为 `10.200.0.0/24`。网络沿用原环境的 IB native RDMA，`ms_type=async+rdma`、`ms_cluster_type=async+rdma`，每台使用自己的 `mlx5_1` GID。

## 实际启动入口

本次部署使用的 systemd 模板：

```text
/etc/systemd/system/ceph-quincy-osd@.service
/etc/systemd/system/ceph-quincy-mon@.service    # node1
/etc/systemd/system/ceph-quincy-mgr@.service    # node1
```

例如在 node2 上只重启 osd.4：

```bash
sudo systemctl restart ceph-quincy-osd@4.service
```

OSD 数据目录为 `/var/lib/ceph/osd/ceph-<id>`；MON/MGR 数据目录分别为 `/var/lib/ceph/mon/ceph-node1`、`/var/lib/ceph/mgr/ceph-m1`。配置和密钥在 `/etc/ceph`，日志为 `/var/log/ceph/quincy-{mon,mgr,osd}.<id>.log`，admin socket 在 `/run/ceph/ceph-<name>.asok`。

OSD 服务以 root 访问原始块设备；MON/MGR 以 cyf 运行。服务设置 `LimitMEMLOCK=infinity`。旧的 `ceph-custom-osd@` 实例已停止并禁用。

## 构建与一次性部署

`build.sh` 是实际使用的构建命令，编译 MON、MGR、OSD、RADOS 客户端、EC 插件、压缩插件、BlueStore 工具以及 Python RADOS／CephFS 绑定。每台构建日志为本目录 `build-nodeN.log`。本地 `v17.2.7` 标签指向指定提交，用于生成正确的版本名称。

`mgr-modules/` 通过符号链接引用该提交的 MGR 源码、Python 辅助模块和构建目录中的原生绑定。MGR 使用这个模块目录，确保优先加载匹配的绑定，避免系统自带 CephFS 绑定与本次动态库版本冲突。Ceph 源码没有为此修改。

`prepare_sources.py` 保存原分支和本地修改、备份旧配置及守护进程目录，然后停止旧集群并切换源码。保存引用见各节点的 `source-preservation.json` 和主控 `manifest.json`。

`init_cluster.py` 是本次实际使用的一次性初始化入口：创建新 FSID／密钥、初始化 manifest 指定的 12 块原实验盘，并安装上述服务。它会清除选定 OSD 盘的数据；已初始化的集群会拒绝再次执行。日常操作使用 `clusterctl.py`。

原环境备份位于各节点 `/home/cyf/ceph-lab-17.2.7/backups/`。原自定义代码在 `backup/pre-quincy-17.2.7-*` 分支和 Git stash 中，node1 的 c-ares 子模块改动也单独保存。

最终检查结果为 `validation.json`。默认 CRUSH 按主机放置；若以后在三台主机上建立 8+4 等 EC 池，需要为该实验单独选择适合的 `crush-failure-domain`。

## 验收与归档

`io-validation.json` 记录临时三副本池和 Jerasure 2+1 EC 池的八次完整对象读回校验；测试池、测试 profile 和测试规则已删除。`restart-validation.log` 记录实际使用 `clusterctl.py restart` 的验证。

node1 系统盘空间较紧。旧实验的四份压缩 OSD 日志已校验 SHA256 后归档到 **node2** 的 `archived-node1-logs/`。本次构建的 `libosd.a`、`libmon.a`、`libcommon.a`、`libos.a` 也已校验后归档到 **node2** 的 `archived-node1-static-libraries/`。node1 的运行程序、动态库和编译对象文件均保留；以后执行 `build.sh` 时，Ninja 会按需重新生成这些静态库。归档清单和哈希在 `manifest.json` 中。
