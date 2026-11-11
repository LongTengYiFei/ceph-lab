#!/usr/bin/env bash
set -euo pipefail
objmeta_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec sudo -n prlimit --memlock=unlimited -- env \
  LD_LIBRARY_PATH=/home/cyf/ceph-dpu/build/lib \
  PYTHONPATH=/home/cyf/ceph-dpu/build/lib/cython_modules/lib.3 \
  PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -B -u "$objmeta_dir/validate_io.py" "$@"
