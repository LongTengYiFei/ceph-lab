#!/usr/bin/env bash
set -euo pipefail
exec sudo -n prlimit --memlock=unlimited -- env \
  LD_LIBRARY_PATH=/home/cyf/ceph-dpu/build/lib \
  /home/cyf/ceph-dpu/build/bin/ceph --conf /etc/ceph/ceph.conf "$@"
