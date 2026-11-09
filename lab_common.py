#!/usr/bin/env python3
"""Helpers for the dedicated Ceph 17.2.7 lab, independent of the source branch."""
import json
import os
import shlex
import socket
import subprocess
import time
from pathlib import Path

LAB = Path('/home/cyf/ceph-lab-17.2.7')
SOURCE = Path('/home/cyf/ceph-dpu')
BUILD = SOURCE / 'build'
TARGET = 'b12291d110049b2f35e32e0de30d70e9a4c060d2'
HOSTS = ('node1', 'node2', 'node3', 'node4')
LOCAL_HOST = socket.gethostname().split('.')[0]
SSH = ('ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-p', '23579')


def run(args, *, timeout=180, check=True, input=None):
    p = subprocess.run([str(a) for a in args], input=input, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if check and p.returncode:
        raise RuntimeError(f'{args}: exit={p.returncode}\n{p.stdout[-3000:]}\n{p.stderr[-3000:]}')
    return p


def remote(host, command, *, timeout=180, check=True):
    args = ('bash', '-lc', command) if host == LOCAL_HOST else (*SSH, host, command)
    return run(args, timeout=timeout, check=check)


def python(host, code, *, root=False, timeout=180):
    return remote(host, ('sudo -n ' if root else '') + 'python3 -c ' + shlex.quote(code), timeout=timeout)


def save(path, data):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
    temporary.replace(path)


def manifest():
    return json.loads((LAB / 'manifest.json').read_text())


def ceph(*args, timeout=180):
    return remote('node1', 'sudo -n prlimit --memlock=unlimited -- env LD_LIBRARY_PATH=' + str(BUILD / 'lib') + ' ' +
                  shlex.join([str(BUILD / 'bin/ceph'), '--conf', '/etc/ceph/ceph.conf', *args]), timeout=timeout).stdout.strip()


def ceph_json(*args, timeout=180):
    return json.loads(ceph(*args, '-f', 'json', timeout=timeout))


def wait(description, predicate, timeout=300):
    deadline = time.monotonic() + timeout
    error = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except Exception as e:
            error = e
        time.sleep(1)
    raise RuntimeError(f'timeout: {description}; last error: {error}')


def install_text(host, path, text, mode=0o644, owner=None):
    code = ('import os,pathlib,pwd,sys; p=pathlib.Path(' + repr(str(path)) + '); '
            'p.parent.mkdir(parents=True,exist_ok=True); p.write_text(sys.stdin.read()); '
            f'p.chmod({mode})')
    if owner:
        code += '; u=pwd.getpwnam(' + repr(owner) + '); os.chown(p,u.pw_uid,u.pw_gid)'
    command = ('sudo', '-n', 'python3', '-c', code)
    args = command if host == LOCAL_HOST else (*SSH, host, shlex.join(command))
    return run(args, input=text)
