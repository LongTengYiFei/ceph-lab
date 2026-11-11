#!/usr/bin/env python3
"""Root-side, serial-checked storage operations for the object-metadata lab.

Invoked by configure_cluster.py through SSH, with a JSON request on stdin.
Never format an entire backend data disk. Each shared SSD has its own VG.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

SOURCE = Path('/home/cyf/ceph-dpu')
LAB = Path('/home/cyf/ceph-lab-17.2.7')
COMMIT = '69fbba696bee9762a0025b6b128ddd978120ac78'
RELEASE = LAB / 'snapwrite-artifacts' / COMMIT
ENV = dict(os.environ, LD_LIBRARY_PATH=str(SOURCE / 'build/lib'))
HISTORY = []


def run(*args, timeout=300, check=True):
    p = subprocess.run([str(a) for a in args], env=ENV, text=True,
                       capture_output=True, timeout=timeout)
    HISTORY.append({'argv': [str(a) for a in args], 'returncode': p.returncode,
                    'stdout': p.stdout[-12000:], 'stderr': p.stderr[-12000:]})
    if check and p.returncode:
        raise RuntimeError(f'{args}: {p.returncode}\n{p.stdout[-3000:]}\n{p.stderr[-3000:]}')
    return p.stdout


def disk(row, *, pristine=False):
    value = json.loads(run('lsblk', '--tree', '-J', '-b', '-o',
                          'PATH,SIZE,TYPE,FSTYPE,MOUNTPOINTS,SERIAL', row['device']))['blockdevices'][0]
    assert value['serial'] == row['serial'], value
    assert value['size'] == row['size'] and value['type'] == 'disk', value
    assert not any(value['mountpoints']), value
    if pristine:
        assert not value.get('children') and value['fstype'] in ('ceph_bluestore', None), value
    return value


def service_stopped(osd):
    state = run('systemctl', 'is-active', f'ceph-quincy-osd@{osd}.service', check=False).strip()
    assert state in ('inactive', 'failed', 'unknown'), (osd, state)


def stable_link(path, target):
    path = Path(path)
    temporary = path.with_name(path.name + '.objmeta-new')
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    temporary.replace(path)


def tool(action, osd, *args):
    return run(SOURCE / 'build/bin/ceph-bluestore-tool', action,
               '--path', f'/var/lib/ceph/osd/ceph-{osd}', '--no-mon-config',
               '--log-level', '1', *args, timeout=900)


def install_activation(osd, volume):
    directory = Path(f'/etc/systemd/system/ceph-quincy-osd@{osd}.service.d')
    directory.mkdir(parents=True, exist_ok=True)
    (directory / '60-objectmeta-lvm.conf').write_text(
        '[Service]\nExecStartPre=/usr/sbin/lvchange -ay ' + volume + '\n'
        'ExecStartPre=/usr/bin/test -b ' + volume + '\n')
    run('systemctl', 'daemon-reload')


def inventory(node):
    return {
        'disks': [disk(row) for row in node['disks']],
        'pvs': json.loads(run('pvs', '--reportformat', 'json')),
        'vgs': json.loads(run('vgs', '--reportformat', 'json')),
        'lvs': json.loads(run('lvs', '--reportformat', 'json', '--units', 'b', '--nosuffix',
                              '-o', 'vg_name,lv_name,lv_size,lv_uuid,devices')),
        'services': {str(row['id']): run('systemctl', 'show',
                     f"ceph-quincy-osd@{row['id']}.service", '-p', 'ActiveState,MainPID,ExecStart')
                     for row in node['disks']},
        'tool_version': run(SOURCE / 'build/bin/ceph-bluestore-tool', '--version').strip(),
        'osd_version': run(RELEASE / 'run-ceph-osd', '--version').strip(),
    }


def backup(node, stamp):
    directory = LAB / 'Exp_ObjectMetaWriteAmplification/backups' / stamp
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    for row in node['disks']:
        source = Path(f"/var/lib/ceph/osd/ceph-{row['id']}")
        destination = directory / source.name
        if not destination.exists():
            shutil.copytree(source, destination, symlinks=True)
    if not (directory / 'ceph.conf').exists():
        shutil.copy2('/etc/ceph/ceph.conf', directory / 'ceph.conf')
    return {'directory': str(directory)}


def make_lvm(pair):
    service_stopped(pair['cache_id'])
    physical = disk(pair['shared'])
    vg = pair['vg']
    pvs = json.loads(run('pvs', '--reportformat', 'json', '-o', 'pv_name,vg_name'))['report'][0]['pv']
    existing = [p for p in pvs if Path(p['pv_name']).resolve() == Path(pair['shared']['device']).resolve()]
    if not existing:
        disk(pair['shared'], pristine=True)
        label = json.loads(run(SOURCE / 'build/bin/ceph-bluestore-tool', 'show-label',
                               '--dev', pair['shared']['device']))
        original = label[pair['shared']['device']]
        assert original['osd_uuid'] == pair['shared']['uuid']
        assert original['ceph_fsid'] == pair['fsid']
        assert original['whoami'] == str(pair['cache_id'])
        run('wipefs', '--all', pair['shared']['device'])
        run('pvcreate', '--yes', pair['shared']['device'])
        run('vgcreate', vg, pair['shared']['device'])
    else:
        assert existing[0]['vg_name'].strip() == vg, existing
    members = json.loads(run('pvs', '--reportformat', 'json', '-o', 'pv_name,vg_name'))['report'][0]['pv']
    members = [p for p in members if p['vg_name'].strip() == vg]
    assert len(members) == 1 and Path(members[0]['pv_name']).resolve() == Path(pair['shared']['device']).resolve()
    extents = int(run('vgs', '--noheadings', '-o', 'vg_extent_count', vg).strip())
    half = extents // 2
    for name in ('backend_db', 'cache_data'):
        path = Path('/dev') / vg / name
        if not path.exists():
            run('lvcreate', '--yes', '--zero', 'y', '--wipesignatures', 'y',
                '-l', str(half), '-n', name, vg)
        size = int(run('blockdev', '--getsize64', path).strip())
        extent_size = int(float(run('vgs', '--units', 'b', '--nosuffix', '--noheadings',
                                   '-o', 'vg_extent_size', vg).strip()))
        assert size == half * extent_size
    db_bytes = int(run('blockdev', '--getsize64', pair['db_lv']).strip())
    cache_bytes = int(run('blockdev', '--getsize64', pair['cache_lv']).strip())
    assert db_bytes == cache_bytes
    run('lvchange', '--addtag', f"objmeta.backend_{pair['backend_id']}", pair['db_lv'])
    run('lvchange', '--addtag', f"objmeta.cache_{pair['cache_id']}", pair['cache_lv'])
    run('vgcfgbackup', vg)
    return {'vg': vg, 'db_bytes': db_bytes, 'cache_bytes': cache_bytes,
            'physical_bytes': physical['size'], 'unallocated_extents': extents % 2}


def migrate_db(pair):
    osd = pair['backend_id']
    service_stopped(osd)
    disk(pair['data'], pristine=True)
    disk(pair['shared'])
    directory = Path(f'/var/lib/ceph/osd/ceph-{osd}')
    assert (directory / 'block').resolve() == Path(pair['data']['device']).resolve()
    assert (directory / 'fsid').read_text().strip() == pair['data']['uuid']
    db = directory / 'block.db'
    if not db.exists():
        tool('bluefs-bdev-new-db', osd, '--dev-target', pair['db_lv'])
    assert db.resolve() == Path(pair['db_lv']).resolve()
    stable_link(db, pair['db_lv'])
    # Use the OSD symlink paths: bluestore-tool infers its device keys from these.
    tool('bluefs-bdev-migrate', osd, '--devs-source', str(directory / 'block'),
         '--dev-target', str(db))
    stable_link(db, pair['db_lv'])
    tool('fsck', osd)
    install_activation(osd, pair['db_lv'])
    return {'osd': osd, 'block': str((directory / 'block').resolve()),
            'block_db': str(db.readlink()), 'fsck_passed': True}


def cache_mkfs(pair, request):
    osd = pair['cache_id']
    service_stopped(osd)
    disk(pair['shared'])
    path = Path(f'/var/lib/ceph/osd/ceph-{osd}')
    if path.exists() and (path / 'fsid').exists() and (path / 'fsid').read_text().strip() != request['uuid']:
        destination = LAB / 'Exp_ObjectMetaWriteAmplification/backups' / request['stamp'] / f'replaced-ceph-{osd}'
        assert not destination.exists()
        shutil.move(str(path), destination)
    path.mkdir(parents=True, exist_ok=True)
    stable_link(path / 'block', pair['cache_lv'])
    key = path / 'keyring'
    key.write_text(request['keyring'])
    key.chmod(0o600)
    if not (path / 'mkfs_done').exists():
        run('prlimit', '--memlock=unlimited', '--', RELEASE / 'run-ceph-osd',
            '--conf', '/etc/ceph/ceph.conf', '--mkfs',
            '-i', str(osd), '--osd-uuid', request['uuid'], '--osd-data', str(path),
            '--keyring', str(key), '--setuser', 'root', '--setgroup', 'root', timeout=900)
    assert (path / 'fsid').read_text().strip() == request['uuid']
    install_activation(osd, pair['cache_lv'])
    return {'osd': osd, 'uuid': request['uuid'], 'block': str((path / 'block').readlink())}


def verify_pair(pair):
    disk(pair['data'], pristine=True)
    disk(pair['shared'])
    paths = {}
    for osd, name, volume in [(pair['backend_id'], 'block', pair['data']['device']),
                              (pair['backend_id'], 'block.db', pair['db_lv']),
                              (pair['cache_id'], 'block', pair['cache_lv'])]:
        p = Path(f'/var/lib/ceph/osd/ceph-{osd}') / name
        assert p.resolve() == Path(volume).resolve(), (str(p), str(p.resolve()), volume)
        paths[f'{osd}/{name}'] = str(p.readlink())
    db_bytes = int(run('blockdev', '--getsize64', pair['db_lv']).strip())
    cache_bytes = int(run('blockdev', '--getsize64', pair['cache_lv']).strip())
    assert db_bytes == cache_bytes
    labels = json.loads(run(SOURCE / 'build/bin/ceph-bluestore-tool', 'show-label',
                            '--dev', pair['db_lv'], '--dev', pair['cache_lv']))
    assert labels[pair['db_lv']]['osd_uuid'] == pair['data']['uuid']
    assert labels[pair['db_lv']]['description'] == 'bluefs db'
    assert labels[pair['cache_lv']]['osd_uuid'] == pair['cache_uuid']
    assert labels[pair['cache_lv']]['description'] == 'main'
    volumes = json.loads(run('lvs', '--reportformat', 'json', '--units', 'b', '--nosuffix',
                             '-o', 'vg_name,lv_name,lv_size,devices', pair['vg']))
    pvs = json.loads(run('pvs', '--reportformat', 'json', '-o', 'pv_name,vg_name'))['report'][0]['pv']
    members = [p for p in pvs if p['vg_name'].strip() == pair['vg']]
    assert len(members) == 1 and Path(members[0]['pv_name']).resolve() == Path(pair['shared']['device']).resolve()
    bluefs = {}
    for osd in (pair['backend_id'], pair['cache_id']):
        socket = f'/run/ceph/ceph-osd.{osd}.asok'
        counters = json.loads(run(SOURCE / 'build/bin/ceph', 'daemon', socket,
                                  'perf', 'dump', 'bluefs'))['bluefs']
        bluefs[str(osd)] = counters
        rocksdb = json.loads(run(SOURCE / 'build/bin/ceph', 'daemon', socket,
                                 'perf', 'dump', 'rocksdb'))['rocksdb']
        assert all(key in rocksdb for key in ('submitted_txns', 'submitted_ops', 'submitted_bytes'))
    backend_bluefs = bluefs[str(pair['backend_id'])]
    assert db_bytes - 1024 ** 2 <= backend_bluefs['db_total_bytes'] <= db_bytes
    assert backend_bluefs['slow_used_bytes'] == 0, backend_bluefs
    return {'paths': paths, 'db_bytes': db_bytes, 'cache_bytes': cache_bytes,
            'labels': labels, 'lvs': volumes, 'bluefs': bluefs,
            'rocksdb_submission_counters_present': True}


def main():
    assert os.geteuid() == 0
    request = json.load(sys.stdin)
    action = sys.argv[1]
    if action == 'inventory':
        result = inventory(request['node'])
    elif action == 'backup':
        result = backup(request['node'], request['stamp'])
    elif action == 'lvm':
        result = make_lvm(request['pair'])
    elif action == 'migrate-db':
        result = migrate_db(request['pair'])
    elif action == 'cache-mkfs':
        result = cache_mkfs(request['pair'], request)
    elif action == 'verify':
        result = verify_pair(request['pair'])
    else:
        raise ValueError(action)
    print(json.dumps({'result': result, 'commands': HISTORY}))


if __name__ == '__main__':
    main()
