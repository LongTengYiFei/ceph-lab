#!/usr/bin/env python3
"""Configure the authorized 6-backend + 6-cache lab; journal each completed step.

plan is read-only against the cluster. apply converts only the six serial-pinned
donor SSDs after evacuating them. Backend OSD IDs, UUIDs and data are retained.
verify is read-only. Existing resources are never blindly formatted on rerun.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
LAB = HERE.parent
sys.path.insert(0, str(LAB))
from lab_common import ceph, ceph_json, remote, save, install_text

COMMIT = '69fbba696bee9762a0025b6b128ddd978120ac78'
DATA_POOL = 'objmeta-data'
CACHE_POOL = 'objmeta-cache'
STATE = HERE / 'deployment.json'
PLAN = HERE / 'layout.json'
JOURNAL = HERE / 'operations.jsonl'


def now():
    return datetime.now(timezone.utc).isoformat()


def record(event, **values):
    with JOURNAL.open('a') as f:
        f.write(json.dumps({'time_utc': now(), 'event': event, **values}) + '\n')


def command(*args):
    record('ceph', argv=list(args))
    return ceph(*args)


def node(host, action, **request):
    code = (HERE / 'node_storage.py').read_text()
    shell = shlex.join(['sudo', '-n', 'python3', '-c', code, action])
    p = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                        '-p', '23579', host, shell], input=json.dumps(request),
                       text=True, capture_output=True, timeout=1800)
    if p.returncode:
        # Request JSON may contain a keyring; never record it.
        record('node_failed', host=host, action=action, returncode=p.returncode,
               stderr=p.stderr[-8000:])
        raise RuntimeError(f'{host}/{action}: {p.returncode}\n{p.stderr[-8000:]}')
    result = json.loads(p.stdout)
    record('node', host=host, action=action, result=result)
    return result['result']


def wait_for(description, predicate, timeout=900):
    print('Waiting: ' + description, flush=True)
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except Exception as error:
            last = str(error)
        time.sleep(2)
    raise RuntimeError(f'timeout: {description}; last error={last}')


def pg_clean(expected_up=None):
    status = ceph_json('status')
    states = status['pgmap'].get('pgs_by_state', [])
    good = all(row['state_name'] == 'active+clean' for row in states)
    if expected_up is not None:
        osds = status['osdmap']
        good = good and osds['num_osds'] == osds['num_up_osds'] == osds['num_in_osds'] == expected_up
    return good and status['mgrmap']['available']


def all_pairs(plan):
    return [(host, pair) for host, value in plan['nodes'].items() for pair in value['pairs']]


def make_plan():
    original = json.loads((LAB / 'manifest.json').read_text())
    assert not original.get('objectmeta_layout'), 'layout already installed; use existing layout.json'
    assert original['fsid'] == ceph_json('fsid')['fsid']
    assert set(ceph_json('osd', 'pool', 'ls')) == {'.mgr'}, 'unexpected user pools'
    assert pg_clean(12)
    plan = {'fsid': original['fsid'], 'osd_commit': COMMIT, 'created_utc': now(),
            'data_pool': DATA_POOL, 'cache_pool': CACHE_POOL,
            'replicas': 3, 'min_size': 2, 'failure_domain': 'host', 'pg_num': 64,
            'cache': {'mode': 'writeback', 'hit_set_type': 'bloom', 'hit_set_count': 12,
                      'hit_set_period': 300, 'min_read_recency_for_promote': 1,
                      'min_write_recency_for_promote': 0, 'cache_target_dirty_ratio': 0.4,
                      'cache_target_dirty_high_ratio': 0.6, 'cache_target_full_ratio': 0.8,
                      'cache_min_flush_age': 60, 'cache_min_evict_age': 120},
            'nodes': {}}
    for host in ('node1', 'node2', 'node3'):
        rows = original['nodes'][host]['osds']
        assert len(rows) == 4
        value = {'disks': rows, 'pairs': []}
        for index in range(2):
            data, shared = rows[index], rows[index + 2]
            vg = f'objmeta_{host}_{index}'
            value['pairs'].append({'backend_id': data['id'], 'cache_id': shared['id'],
                'data': data, 'shared': shared, 'fsid': original['fsid'], 'vg': vg,
                'db_lv': f'/dev/{vg}/backend_db', 'cache_lv': f'/dev/{vg}/cache_data',
                'cache_uuid': str(uuid.uuid4())})
        inventory = node(host, 'inventory', node=value)
        assert COMMIT in inventory['osd_version']
        # The counter-only patch does not change the BlueStore format. Some nodes
        # retain the upstream 17.2.7 tool while their OSD runs the frozen release.
        assert inventory['tool_version'].startswith('ceph version 17.2.7')
        for entry in inventory['disks']:
            assert not entry.get('children') and entry['fstype'] == 'ceph_bluestore'
        assert not inventory['pvs']['report'][0]['pv'], 'unexpected existing LVM'
        plan['nodes'][host] = value
    save(PLAN, plan)
    return plan


def phase(state, key, work):
    if key in state['completed']:
        print('Already complete: ' + key, flush=True)
        return state['completed'][key]
    print('Applying: ' + key, flush=True)
    record('phase_begin', phase=key)
    value = work()
    state['completed'][key] = value if value is not None else True
    state['updated_utc'] = now()
    save(STATE, state)
    record('phase_complete', phase=key)
    return value


def snapshot(plan, state):
    directory = HERE / 'backups' / state['stamp']
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    for filename in ('manifest.json', 'README.md', 'validation.json'):
        target = directory / filename
        if not target.exists():
            shutil.copy2(LAB / filename, target)
    before = {'status': ceph_json('status'), 'osd_dump': ceph_json('osd', 'dump'),
              'crush': ceph_json('osd', 'crush', 'dump'), 'config': ceph_json('config', 'dump'),
              'versions': ceph_json('versions'), 'balancer': ceph_json('balancer', 'status')}
    save(directory / 'cluster-before.json', before)
    for host, value in plan['nodes'].items():
        node(host, 'backup', node=value, stamp=state['stamp'])
    # A small additional backup of the only pre-existing pool; no auth keys.
    p = subprocess.run([str(LAB / 'rados.sh'), '-p', '.mgr', 'export',
                        str(directory / 'mgr-pool.rados')], text=True, capture_output=True, timeout=120)
    assert p.returncode == 0, p.stderr
    return {'directory': str(directory), 'balancer_active': before['balancer']['active'],
            'initial_flags': before['osd_dump']['flags']}


def restrict_backend(plan):
    command('balancer', 'off')
    for _, pair in all_pairs(plan):
        osd = f"osd.{pair['backend_id']}"
        command('osd', 'crush', 'rm-device-class', osd)
        command('osd', 'crush', 'set-device-class', 'backend', osd)
    if 'objmeta-backend' not in ceph_json('osd', 'crush', 'rule', 'ls'):
        command('osd', 'crush', 'rule', 'create-replicated', 'objmeta-backend', 'default', 'host', 'backend')
    command('osd', 'pool', 'set', '.mgr', 'crush_rule', 'objmeta-backend')
    backend = {p['backend_id'] for _, p in all_pairs(plan)}

    def evacuated():
        pgs = ceph_json('pg', 'ls-by-pool', '.mgr')['pg_stats']
        return pg_clean(12) and all(set(p['acting']) <= backend and p['state'] == 'active+clean' for p in pgs)

    wait_for('.mgr fully replicated on backend OSDs', evacuated)
    return {'backend_osds': sorted(backend)}


def retire_cache(plan, state):
    ids = [p['cache_id'] for _, p in all_pairs(plan)]
    current = set(ceph_json('osd', 'ls'))
    present = [osd for osd in ids if osd in current]
    if present:
        command('osd', 'out', *map(str, present))
        wait_for('PGs clean before donor OSD retirement', pg_clean)
        command('osd', 'safe-to-destroy', *map(str, present))
    for host, pair in all_pairs(plan):
        osd = pair['cache_id']
        remote(host, f'sudo -n systemctl stop ceph-quincy-osd@{osd}.service', timeout=180)
        if osd in set(ceph_json('osd', 'ls')):
            command('osd', 'safe-to-destroy', str(osd))
            command('osd', 'purge', str(osd), '--yes-i-really-mean-it')
    wait_for('six backend OSDs up/in and .mgr clean', lambda: pg_clean(6))
    return {'retired_osds': ids}


def migrate_one(host, pair):
    osd = pair['backend_id']
    flags = ceph_json('osd', 'dump')['flags'].split(',')
    added_noout = 'noout' not in flags
    if added_noout:
        command('osd', 'set', 'noout')
    remote(host, f'sudo -n systemctl stop ceph-quincy-osd@{osd}.service', timeout=180)
    try:
        value = node(host, 'migrate-db', pair=pair)
    finally:
        remote(host, f'sudo -n systemctl start ceph-quincy-osd@{osd}.service', timeout=180)
        if added_noout:
            command('osd', 'unset', 'noout')
    wait_for(f'backend osd.{osd} rejoined and PGs clean', lambda: pg_clean(6))
    return value


def create_cache(host, pair, state):
    osd, osd_uuid = pair['cache_id'], pair['cache_uuid']
    current = ceph_json('osd', 'dump')['osds']
    entry = next((row for row in current if row['osd'] == osd), None)
    if entry:
        # If mkfs/start succeeded but the journal write was interrupted, retain it.
        actual_uuid = ceph_json('osd', 'metadata', str(osd)).get('osd_fsid') if entry['up'] else None
        if actual_uuid:
            assert actual_uuid == osd_uuid
    else:
        assert int(command('osd', 'create', osd_uuid, str(osd))) == osd
    key = ceph('auth', 'get-or-create', f'osd.{osd}', 'mon', 'allow profile osd',
               'osd', 'allow *', 'mgr', 'allow profile osd') + '\n'
    # Never put the keyring in argv, output, the journal or versioned files.
    value = node(host, 'cache-mkfs', pair=pair, uuid=osd_uuid, keyring=key, stamp=state['stamp'])
    remote(host, f'sudo -n systemctl enable --now ceph-quincy-osd@{osd}.service', timeout=180)
    wait_for(f'cache osd.{osd} up', lambda: any(row['osd'] == osd and row['up']
             for row in ceph_json('osd', 'dump')['osds']))
    command('osd', 'in', str(osd))
    command('osd', 'crush', 'rm-device-class', f'osd.{osd}')
    command('osd', 'crush', 'set-device-class', 'cache', f'osd.{osd}')
    return value


def pools(plan, state):
    wait_for('all twelve OSDs up/in and existing PGs clean', lambda: pg_clean(12))
    if 'objmeta-cache' not in ceph_json('osd', 'crush', 'rule', 'ls'):
        command('osd', 'crush', 'rule', 'create-replicated', 'objmeta-cache', 'default', 'host', 'cache')
    for pool, rule in [(DATA_POOL, 'objmeta-backend'), (CACHE_POOL, 'objmeta-cache')]:
        if pool not in ceph_json('osd', 'pool', 'ls'):
            command('osd', 'pool', 'create', pool, str(plan['pg_num']), str(plan['pg_num']),
                    'replicated', rule, '--autoscale-mode=off')
        command('osd', 'pool', 'set', pool, 'size', '3')
        command('osd', 'pool', 'set', pool, 'min_size', '2')
        command('osd', 'pool', 'set', pool, 'pg_autoscale_mode', 'off')
        command('osd', 'pool', 'set', pool, 'compression_mode', 'none')
        command('osd', 'pool', 'application', 'enable', pool, 'rados')
    # Quincy permits cache-specific pool options only after this pool is a tier.
    # Keep the overlay disabled until all cache policy and size settings exist.
    command('osd', 'tier', 'add', DATA_POOL, CACHE_POOL)
    for key, value in plan['cache'].items():
        if key != 'mode':
            command('osd', 'pool', 'set', CACHE_POOL, key, str(value))
    cache_raw = sum(state['completed'][f'lvm-{pair["cache_id"]}']['cache_bytes']
                    for _, pair in all_pairs(plan))
    # Pool target is logical/unreplicated bytes; reserve 20% below 3-replica raw capacity.
    target = (cache_raw // 3 * 80 // 100) // (1024 ** 3) * (1024 ** 3)
    command('osd', 'pool', 'set', CACHE_POOL, 'target_max_bytes', str(target))
    command('osd', 'tier', 'cache-mode', CACHE_POOL, 'writeback')
    command('osd', 'tier', 'set-overlay', DATA_POOL, CACHE_POOL)
    wait_for('backend and cache PGs active+clean', lambda: pg_clean(12))
    return {'cache_raw_bytes': cache_raw, 'target_max_bytes': target}


def update_manifest(plan, state):
    m = json.loads((LAB / 'manifest.json').read_text())
    for host, pair in all_pairs(plan):
        rows = {row['id']: row for row in m['nodes'][host]['osds']}
        rows[pair['backend_id']].update(role='backend', db_device=pair['db_lv'],
            shared_device=pair['shared']['device'], paired_cache_osd=pair['cache_id'])
        row = rows[pair['cache_id']]
        row.update(role='cache', device=pair['cache_lv'], physical_device=pair['shared']['device'],
            physical_size=pair['shared']['size'], size=state['completed'][f'lvm-{pair["cache_id"]}']['cache_bytes'],
            uuid=pair['cache_uuid'], previous_uuid=pair['shared']['uuid'], paired_backend_osd=pair['backend_id'])
    m['objectmeta_layout'] = {'directory': str(HERE), 'data_pool': DATA_POOL, 'cache_pool': CACHE_POOL,
                              'configured_at_utc': now(), 'osd_commit': COMMIT}
    save(LAB / 'manifest.json', m)
    save(HERE / 'manifest.json', m)
    return True


def verify(plan, output=True):
    assert ceph_json('fsid')['fsid'] == plan['fsid']
    assert pg_clean(12)
    metadata = ceph_json('osd', 'metadata')
    assert len(metadata) == 12 and all(COMMIT in row['ceph_version'] for row in metadata)
    tree = ceph_json('osd', 'tree')
    by_id = {row['id']: row for row in tree['nodes'] if row['id'] >= 0}
    backend = {p['backend_id'] for _, p in all_pairs(plan)}
    cache = {p['cache_id'] for _, p in all_pairs(plan)}
    assert {i for i, row in by_id.items() if row.get('device_class') == 'backend'} == backend
    assert {i for i, row in by_id.items() if row.get('device_class') == 'cache'} == cache
    host_by_id = {row['id']: row['hostname'] for row in metadata}
    pools_by_name = {p['pool_name']: p for p in ceph_json('osd', 'pool', 'ls', 'detail')}
    data, hot = pools_by_name[DATA_POOL], pools_by_name[CACHE_POOL]
    assert data['read_tier'] == data['write_tier'] == hot['pool_id']
    assert hot['tier_of'] == data['pool_id'] and hot['cache_mode'] == 'writeback'
    placement = {}
    for pool, allowed in [(DATA_POOL, backend), (CACHE_POOL, cache), ('.mgr', backend)]:
        p = pools_by_name[pool]
        assert p['size'] == 3 and p['min_size'] == 2
        # Status aggregates can lag individual PG maps, particularly when the
        # original balancer has just been restored. Wait on the actual PGs too.
        def settled(pool=pool, allowed=allowed):
            rows = ceph_json('pg', 'ls-by-pool', pool)['pg_stats']
            if not rows:
                return False
            for pg in rows:
                if pg['state'] != 'active+clean' or len(pg['acting']) != 3:
                    return False
                if not set(pg['acting']) <= allowed:
                    return False
                if len({host_by_id[osd] for osd in pg['acting']}) != 3:
                    return False
            return rows
        placement[pool] = wait_for(pool + ' PG placement clean on three hosts', settled)
    storage = {f'{host}/{pair["backend_id"]}-{pair["cache_id"]}': node(host, 'verify', pair=pair)
               for host, pair in all_pairs(plan)}
    assert not remote('node4', "systemctl list-units --type=service --state=running --no-legend 'ceph*osd*'").stdout.strip()
    result = {'verified_at_utc': now(), 'status': ceph_json('status'), 'versions': ceph_json('versions'),
              'osd_metadata': metadata, 'osd_tree': tree, 'pools': pools_by_name,
              'placement': placement, 'storage': storage, 'crush': ceph_json('osd', 'crush', 'dump')}
    if output:
        save(HERE / 'validation.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'apply', 'verify'))
    args = parser.parse_args()
    if args.action == 'apply':
        lock = (HERE / '.configure.lock').open('a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if args.action == 'plan':
        plan = json.loads(PLAN.read_text()) if PLAN.exists() else make_plan()
        print(json.dumps(plan, indent=2))
        return
    assert PLAN.exists(), 'run plan first'
    plan = json.loads(PLAN.read_text())
    if args.action == 'verify':
        result = verify(plan)
        print(json.dumps({'verified': True, 'health': result['status']['health']}, indent=2))
        return
    if STATE.exists():
        state = json.loads(STATE.read_text())
    else:
        state = {'stamp': datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
                 'started_utc': now(), 'completed': {}}
        save(STATE, state)
    assert ceph_json('fsid')['fsid'] == plan['fsid']
    allowed_pools = {'.mgr', DATA_POOL, CACHE_POOL}
    assert set(ceph_json('osd', 'pool', 'ls')) <= allowed_pools
    phase(state, 'snapshot', lambda: snapshot(plan, state))
    phase(state, 'restrict-backend', lambda: restrict_backend(plan))
    phase(state, 'retire-donors', lambda: retire_cache(plan, state))
    for host, pair in all_pairs(plan):
        phase(state, f'lvm-{pair["cache_id"]}', lambda h=host, p=pair: node(h, 'lvm', pair=p))
    for host, pair in all_pairs(plan):
        phase(state, f'migrate-db-{pair["backend_id"]}', lambda h=host, p=pair: migrate_one(h, p))
    for host, pair in all_pairs(plan):
        phase(state, f'cache-{pair["cache_id"]}', lambda h=host, p=pair: create_cache(h, p, state))
    phase(state, 'pools', lambda: pools(plan, state))
    phase(state, 'manifest', lambda: update_manifest(plan, state))
    if state['completed']['snapshot']['balancer_active']:
        command('balancer', 'on')
    wait_for('final twelve OSDs and all PGs clean', lambda: pg_clean(12))
    result = verify(plan)
    assert ceph_json('osd', 'dump')['flags'] == state['completed']['snapshot']['initial_flags']
    state['finished_utc'] = now()
    state['verified'] = True
    save(STATE, state)
    print(json.dumps({'configured': True, 'health': result['status']['health'],
                      'cache': state['completed']['pools']}, indent=2))


if __name__ == '__main__':
    main()
