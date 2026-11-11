#!/usr/bin/env python3
"""Root-only, read-only SSD-B and OSD sampler; JSON request on stdin over SSH."""
import json
from pathlib import Path
import os
import shutil
import socket
import struct
import subprocess
import sys
import time


def admin(osd):
    def exact(sock, size):
        value = bytearray()
        while len(value) < size:
            chunk = sock.recv(size - len(value))
            if not chunk:
                raise RuntimeError('short admin socket response')
            value.extend(chunk)
        return bytes(value)
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(20)
        sock.connect(f'/run/ceph/ceph-osd.{osd}.asok')
        sock.sendall(b'{"prefix":"perf dump","format":"json"}\0')
        length, = struct.unpack('>I', exact(sock, 4))
        return json.loads(exact(sock, length))


def main():
    assert os.geteuid() == 0
    request = json.load(sys.stdin)
    started = time.time_ns()
    result = {'started_ns': started,
              'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
              'disks': {}, 'osds': {}}
    for pair in request['pairs']:
        row = pair['shared']
        device = row['device']
        name = Path(device).name
        serial = subprocess.check_output(['lsblk', '-dn', '-o', 'SERIAL', device], text=True).strip()
        size = int(subprocess.check_output(['blockdev', '--getsize64', device], text=True))
        assert serial == row['serial'] and size == row['size']
        stat = [int(x) for x in (Path('/sys/class/block') / name / 'stat').read_text().split()]
        item = {'time_ns': time.time_ns(), 'serial': serial, 'size': size,
                'stat': stat, 'written_sectors': stat[6], 'written_bytes': stat[6] * 512}
        item['lvs'] = {}
        for role, volume in (('backend_db', pair['db_lv']), ('cache_data', pair['cache_lv'])):
            kernel_device = Path(volume).resolve().name
            lv_stat = [int(x) for x in (Path('/sys/class/block') / kernel_device / 'stat').read_text().split()]
            item['lvs'][role] = {'device': volume, 'kernel_device': kernel_device,
                                'time_ns': time.time_ns(), 'stat': lv_stat,
                                'written_bytes': lv_stat[6] * 512}
        if request.get('smart') and shutil.which('nvme'):
            p = subprocess.run(['nvme', 'smart-log', '-o', 'json', device],
                               text=True, capture_output=True, timeout=20)
            item['smart'] = json.loads(p.stdout) if p.returncode == 0 else {'error': p.stderr}
        elif request.get('smart') and shutil.which('smartctl'):
            p = subprocess.run(['smartctl', '-a', '-j', device], text=True, capture_output=True, timeout=20)
            try:
                item['smart'] = json.loads(p.stdout).get('nvme_smart_health_information_log', {})
                item['smart_source'] = 'smartctl'
            except json.JSONDecodeError:
                item['smart'] = {'error': p.stderr}
        result['disks'][device] = item
    for pair in request['pairs']:
        for osd in (pair['backend_id'], pair['cache_id']):
            pid = Path(f'/run/ceph/ceph-osd.{osd}.pid').read_text().strip()
            ticks = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
            result['osds'][str(osd)] = {'pid': pid, 'start_ticks': ticks, 'perf': admin(osd)}
    result['finished_ns'] = time.time_ns()
    print(json.dumps(result, separators=(',', ':')))


if __name__ == '__main__':
    main()
