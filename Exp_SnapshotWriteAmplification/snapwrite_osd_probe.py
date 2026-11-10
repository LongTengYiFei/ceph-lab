#!/usr/bin/env python3
"""Read/reset local OSD counters; deploy this file at the same experiment path on each host."""
import argparse
import json
import socket
import struct
import time
from pathlib import Path


def request(osd_id, command):
    def recv_exact(sock, length):
        data = bytearray()
        while len(data) < length:
            chunk = sock.recv(length - len(data))
            if not chunk:
                raise RuntimeError('admin socket closed before the full response')
            data.extend(chunk)
        return bytes(data)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(15)
        sock.connect(f'/run/ceph/ceph-osd.{osd_id}.asok')
        sock.sendall(json.dumps(command).encode() + b'\0')
        length, = struct.unpack('>I', recv_exact(sock, 4))
        return json.loads(recv_exact(sock, length))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['dump', 'reset'])
    parser.add_argument('ids', type=int, nargs='+')
    args = parser.parse_args()
    output = {}
    for osd_id in args.ids:
        if args.action == 'reset':
            output[str(osd_id)] = request(osd_id, {
                'prefix': 'perf reset', 'var': 'rocksdb', 'format': 'json'})
        else:
            start_ns = time.time_ns()
            perf = request(osd_id, {'prefix': 'perf dump', 'format': 'json'})
            for key in ('submitted_txns', 'submitted_ops', 'submitted_bytes'):
                if key not in perf.get('rocksdb', {}):
                    raise RuntimeError(f'osd.{osd_id}: missing rocksdb.{key}')
            pid_path = Path(f'/run/ceph/ceph-osd.{osd_id}.pid')
            output[str(osd_id)] = {
                'start_ns': start_ns, 'end_ns': time.time_ns(),
                'pid': pid_path.read_text().strip() if pid_path.exists() else None,
                'perf': {k: perf[k] for k in ('rocksdb', 'bluestore', 'bluefs', 'osd')
                         if k in perf}}
    print(json.dumps(output))


if __name__ == '__main__':
    main()
