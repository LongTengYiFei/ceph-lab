#!/usr/bin/env python3
"""Apply/restore explicit promotion limits for this short eviction experiment.

Zero disables both rate limits in OSDService::promote_throttle_recalibrate().
Only the six cache OSDs are touched; every prior explicit entry is recorded.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lab_common import ceph, ceph_json, save

KEYS = ('osd_tier_promote_max_objects_sec', 'osd_tier_promote_max_bytes_sec')


def now():
    return datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('apply', 'restore'))
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    path = args.directory.resolve() / 'promotion-overrides.json'
    if args.action == 'apply':
        assert not path.exists()
        layout = json.loads((HERE / 'layout.json').read_text())
        osds = [pair['cache_id'] for value in layout['nodes'].values() for pair in value['pairs']]
        config = ceph_json('config', 'dump')
        state = {'started_utc': now(), 'before_dump': config, 'entries': []}
        save(path, state)
        for osd in osds:
            section = f'osd.{osd}'
            for key in KEYS:
                existing = [r for r in config if r['section'] == section and r['name'] == key and not r.get('mask')]
                assert len(existing) <= 1
                entry = {'section': section, 'key': key,
                         'previous_explicit': existing[0]['value'] if existing else None,
                         'previous_effective': ceph('config', 'get', section, key),
                         'new_value': '0', 'applied': False}
                state['entries'].append(entry)
                save(path, state)
                ceph('config', 'set', section, key, '0')
                entry['applied'] = True
                entry['applied_utc'] = now()
                save(path, state)
                print(section, key, '0', flush=True)
        state['applied_utc'] = now()
        save(path, state)
    else:
        state = json.loads(path.read_text())
        for entry in state['entries']:
            if entry.get('restored'):
                continue
            if entry['previous_explicit'] is None:
                ceph('config', 'rm', entry['section'], entry['key'])
            else:
                ceph('config', 'set', entry['section'], entry['key'], entry['previous_explicit'])
            assert ceph('config', 'get', entry['section'], entry['key']) == entry['previous_effective']
            entry['restored'] = True
            save(path, state)
            print('Restored', entry['section'], entry['key'], entry['previous_effective'], flush=True)
        state['restored_utc'] = now()
        state['restore_complete'] = True
        save(path, state)


if __name__ == '__main__':
    main()
