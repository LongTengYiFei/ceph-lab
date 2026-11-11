#!/usr/bin/env bash
set -euo pipefail
objmeta_binding=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
objmeta_java=${JAVA_HOME:-/usr/lib/jvm/java-11-openjdk-amd64}
objmeta_core="$objmeta_binding/../tools/YCSB-0.17.0/core/target/core-0.17.0.jar"
test -f "$objmeta_core"
mkdir -p "$objmeta_binding/build/classes"
javac -cp "$objmeta_core" -d "$objmeta_binding/build/classes" \
  "$objmeta_binding/RadosBlockDB.java" "$objmeta_binding/CheckRecords.java"
gcc -std=gnu11 -O2 -fPIC -shared -Wall -Wextra -Wno-unused-parameter \
  -I"$objmeta_java/include" -I"$objmeta_java/include/linux" \
  -I/home/cyf/ceph-dpu/src/include \
  "$objmeta_binding/rados_jni.c" -L/home/cyf/ceph-dpu/build/lib -lrados \
  -o "$objmeta_binding/build/libobjmeta_rados.so"
