#!/usr/bin/env bash
set -euo pipefail
cd /home/cyf/ceph-dpu
test "$(git rev-parse HEAD)" = b12291d110049b2f35e32e0de30d70e9a4c060d2
cmake -S . -B build -DCMAKE_BUILD_TYPE=RelWithDebInfo -DWITH_RDMA=ON -DWITH_CEPH_DEBUG_MUTEX=OFF
cmake --build build --parallel "${CEPH_LAB_BUILD_JOBS:-8}" --target \
  ceph-osd ceph-mon ceph-mgr ceph-authtool monmaptool osdmaptool crushtool \
  ceph-bluestore-tool rados erasure_code_plugins compressor_plugins cython_rados cython_cephfs
env LD_LIBRARY_PATH=/home/cyf/ceph-dpu/build/lib build/bin/ceph-osd --version
