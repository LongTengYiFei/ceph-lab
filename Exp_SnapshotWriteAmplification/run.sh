#!/usr/bin/env bash
set -euo pipefail
snapwrite_experiment=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
snapwrite_source=${CEPH_SOURCE_DIR:-/home/cyf/ceph-dpu}
snapwrite_lab=${CEPH_LAB_DIR:-$(dirname -- "$snapwrite_experiment")}
snapwrite_script=test_snapwrite.py
if [[ ${1:-} == append ]]; then
  snapwrite_script=test_append.py
  shift
fi
exec sudo -n prlimit --memlock=unlimited -- env \
  LD_LIBRARY_PATH="$snapwrite_source/build/lib" \
  PYTHONPATH="$snapwrite_source/build/lib/cython_modules/lib.3" \
  CEPH_LAB_DIR="$snapwrite_lab" PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -B -u "$snapwrite_experiment/$snapwrite_script" "$@"
