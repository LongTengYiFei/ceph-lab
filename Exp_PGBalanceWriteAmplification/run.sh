#!/usr/bin/env bash
set -euo pipefail
pgbalance_experiment=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
pgbalance_source=${CEPH_SOURCE_DIR:-/home/cyf/ceph-dpu}
pgbalance_lab=${CEPH_LAB_DIR:-$(dirname -- "$pgbalance_experiment")}
exec sudo -n prlimit --memlock=unlimited -- env \
  LD_LIBRARY_PATH="$pgbalance_source/build/lib" \
  PYTHONPATH="$pgbalance_source/build/lib/cython_modules/lib.3" \
  CEPH_LAB_DIR="$pgbalance_lab" PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -B -u "$pgbalance_experiment/test_pgbalance.py" "$@"
