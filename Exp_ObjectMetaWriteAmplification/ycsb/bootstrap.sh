#!/usr/bin/env bash
set -euo pipefail
objmeta_ycsb=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
objmeta_tools="$objmeta_ycsb/../tools"
mkdir -p "$objmeta_tools"
if [[ ! -f "$objmeta_tools/apache-maven-3.9.9-bin.tar.gz" ]]; then
  curl --fail --location --retry 2 \
    --output "$objmeta_tools/apache-maven-3.9.9-bin.tar.gz" \
    https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/3.9.9/apache-maven-3.9.9-bin.tar.gz
fi
if [[ ! -f "$objmeta_tools/ycsb-0.17.0-source.tar.gz" ]]; then
  curl --fail --location --retry 2 \
    --output "$objmeta_tools/ycsb-0.17.0-source.tar.gz" \
    https://codeload.github.com/brianfrankcooper/YCSB/tar.gz/refs/tags/0.17.0
fi
python3 - "$objmeta_tools" <<'PY'
import hashlib,sys
from pathlib import Path
root=Path(sys.argv[1])
expected={
 'apache-maven-3.9.9-bin.tar.gz':'7a9cdf674fc1703d6382f5f330b3d110ea1b512b51f1652846d9e4e8a588d766',
 'ycsb-0.17.0-source.tar.gz':'5dd1a3d4dd7ac336eadccc83b097c811e142cfe1b23fc278f247054a1892c0e0',
}
for name,digest in expected.items():
 assert hashlib.sha256((root/name).read_bytes()).hexdigest()==digest,name
PY
if [[ ! -d "$objmeta_tools/apache-maven-3.9.9" ]]; then
  tar -xzf "$objmeta_tools/apache-maven-3.9.9-bin.tar.gz" -C "$objmeta_tools"
fi
if [[ ! -d "$objmeta_tools/YCSB-0.17.0" ]]; then
  tar -xzf "$objmeta_tools/ycsb-0.17.0-source.tar.gz" -C "$objmeta_tools"
fi
if [[ ! -f "$objmeta_tools/YCSB-0.17.0/core/target/core-0.17.0.jar" \
   || ! -d "$objmeta_tools/YCSB-0.17.0/core/target/dependency" ]]; then
  (cd "$objmeta_tools/YCSB-0.17.0" && \
    "$objmeta_tools/apache-maven-3.9.9/bin/mvn" -B -ntp -pl core -am package \
      -Psource-run -DskipTests -Dcheckstyle.skip)
fi
exec bash "$objmeta_ycsb/build_binding.sh"
