#!/usr/bin/env python3
"""Readback checks for disposable replicated/zlib and Jerasure EC pools."""
import hashlib
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import clusterctl
from lab_common import LAB, ceph, ceph_json, run, save, wait


def main():
    assert clusterctl.clean()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    pools = [f'lab-smoke-rep-{stamp}', f'lab-smoke-ec-{stamp}']
    profile = f'lab-smoke-profile-{stamp}'
    checks = []
    try:
        ceph('osd','pool','create',pools[0],'8','8','--autoscale-mode=off')
        ceph('osd','pool','set',pools[0],'compression_mode','force')
        ceph('osd','pool','set',pools[0],'compression_algorithm','zlib')
        ceph('osd','erasure-code-profile','set',profile,'plugin=jerasure','technique=reed_sol_van',
             'k=2','m=1','w=8','crush-failure-domain=host')
        ceph('osd','pool','create',pools[1],'8','8','erasure',profile,'--autoscale-mode=off')
        for pool in pools:
            ceph('osd','pool','application','enable',pool,'rados')
        wait('temporary pools clean',clusterctl.clean,300)
        with tempfile.TemporaryDirectory(prefix='ceph-quincy-smoke-') as directory:
            payload = Path(directory)/'payload'
            payload.write_bytes(b'Q'*1024**2 + os.urandom(1024**2))
            expected = hashlib.sha256(payload.read_bytes()).hexdigest()
            for pool in pools:
                for index in range(4):
                    name = f'check-{index}'
                    run([LAB/'rados.sh','-p',pool,'put',name,payload],timeout=90)
                    target = Path(directory)/'readback'
                    run([LAB/'rados.sh','-p',pool,'get',name,target],timeout=90)
                    assert target.stat().st_size == payload.stat().st_size
                    actual = hashlib.sha256(target.read_bytes()).hexdigest()
                    assert actual == expected
                    checks.append({'pool':pool,'object':name,'bytes':target.stat().st_size,'sha256':actual})
                    print(pool,name,'SHA256 OK',flush=True)
        result = {'checks':checks,'pool_stats_before_cleanup':ceph_json('df','detail')}
    finally:
        for pool in pools:
            if pool in ceph_json('osd','pool','ls'):
                ceph('osd','pool','delete',pool,pool,'--yes-i-really-really-mean-it')
        if profile in ceph_json('osd','erasure-code-profile','ls'):
            ceph('osd','erasure-code-profile','rm',profile)
        rules = ceph_json('osd','crush','rule','ls')
        for name in (profile,pools[1]):
            if name in rules:
                ceph('osd','crush','rule','rm',name)
    wait('cluster clean after test cleanup',clusterctl.clean,300)
    result['final_pools'] = ceph_json('osd','pool','ls')
    result['final_status'] = ceph_json('status')
    save(LAB/'io-validation.json',result)
    print('all eight object readback checks passed; temporary pools/profile removed',flush=True)


if __name__ == '__main__':
    main()
