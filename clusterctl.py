#!/usr/bin/env python3
"""Manage the existing 12-OSD lab. start/restart never format devices."""
import argparse
import json
import shlex

from lab_common import LAB, HOSTS, TARGET, SOURCE, ceph, ceph_json, manifest, remote, save, wait


def start():
    m = manifest()
    remote('node1', 'sudo -n systemctl start ceph-quincy-mon@node1.service')
    wait('monitor quorum', lambda: ceph_json('status', timeout=15).get('quorum_names'), 180)
    remote('node1', 'sudo -n systemctl start ceph-quincy-mgr@m1.service')
    for host, node in m['nodes'].items():
        if node['osds']:
            units = [f"ceph-quincy-osd@{o['id']}.service" for o in node['osds']]
            remote(host, 'sudo -n systemctl start ' + shlex.join(units), timeout=300)
    wait('twelve OSDs up/in and PGs clean', clean, 900)


def clean():
    s = ceph_json('status', timeout=15)
    osds = s['osdmap']
    pgs = s['pgmap']
    clean_count = sum(row['count'] for row in pgs.get('pgs_by_state', []) if row['state_name'] == 'active+clean')
    return (osds['num_osds'] == osds['num_up_osds'] == osds['num_in_osds'] == 12
            and clean_count == pgs.get('num_pgs', 0) and s['mgrmap'].get('available'))


def stop():
    for host, node in manifest()['nodes'].items():
        if node['osds']:
            units = [f"ceph-quincy-osd@{o['id']}.service" for o in node['osds']]
            remote(host, 'sudo -n systemctl stop ' + shlex.join(units), timeout=300)
    remote('node1', 'sudo -n systemctl stop ceph-quincy-mgr@m1.service ceph-quincy-mon@node1.service', timeout=180)


def verify():
    assert clean(), 'cluster is not fully up/in and clean'
    metadata = ceph_json('osd', 'metadata')
    assert len(metadata) == 12
    expected = {row['id']: host for host,node in manifest()['nodes'].items() for row in node['osds']}
    for row in metadata:
        assert row['hostname'] == expected[int(row['id'])], row
        assert TARGET in row['ceph_version'], row
    heads = {}
    for host in HOSTS:
        heads[host] = remote(host, 'git -C ' + str(SOURCE) + ' rev-parse HEAD').stdout.strip()
        assert heads[host] == TARGET
        if host == 'node4':
            active = remote(host, "systemctl list-units --type=service --state=running --no-legend 'ceph*osd*'").stdout.strip()
            assert not active, f'node4 still runs OSD services: {active}'
    result = {'status': ceph_json('status'), 'osd_metadata': metadata, 'source_heads': heads,
              'mon_metadata': ceph_json('mon', 'metadata'), 'mgr_metadata': ceph_json('mgr', 'metadata'),
              'osd_tree': ceph_json('osd', 'tree'), 'config_dump': ceph_json('config', 'dump')}
    for kind in ('mon_metadata','mgr_metadata'):
        entries = result[kind] if isinstance(result[kind],list) else [result[kind]]
        assert entries and all(TARGET in row['ceph_version'] for row in entries), result[kind]
    save(LAB / 'validation.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start','stop','restart','status','verify'))
    args = parser.parse_args()
    if args.action == 'stop':
        stop()
        return
    if args.action == 'restart':
        stop()
    if args.action in ('start','restart'):
        start()
    if args.action == 'verify':
        verify()
    print(ceph('status'))
    print(ceph('osd', 'tree'))


if __name__ == '__main__':
    main()
