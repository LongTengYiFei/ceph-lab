#!/usr/bin/env python3
"""Read local admin sockets without resetting counters; also runnable over SSH stdin."""
import json
from pathlib import Path
import socket
import struct
import sys
import time


def request(osd_id, command):
    def exact(sock, size):
        result = bytearray()
        while len(result) < size:
            data = sock.recv(size - len(result))
            if not data:
                raise RuntimeError('admin socket closed early')
            result.extend(data)
        return bytes(result)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(20)
        sock.connect(f'/run/ceph/ceph-osd.{osd_id}.asok')
        sock.sendall(json.dumps(command).encode() + b'\0')
        size, = struct.unpack('>I', exact(sock, 4))
        return json.loads(exact(sock, size))


def main():
    result = {}
    for osd_id in map(int, sys.argv[1:]):
        start = time.time_ns()
        pid = Path(f'/run/ceph/ceph-osd.{osd_id}.pid').read_text().strip()
        # Field 22, after the parenthesized comm field, identifies the process lifetime.
        process_start = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
        perf = request(osd_id, {'prefix': 'perf dump', 'format': 'json'})
        result[str(osd_id)] = {'start_ns': start, 'end_ns': time.time_ns(),
                               'pid': pid, 'process_start_ticks': process_start, 'perf': perf}
    print(json.dumps(result, separators=(',', ':')))


if __name__ == '__main__':
    main()
