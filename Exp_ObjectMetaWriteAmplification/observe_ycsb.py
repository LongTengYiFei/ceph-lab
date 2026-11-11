#!/usr/bin/env python3
"""Read-only pool occupancy observer for an active saved YCSB run."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lab_common import ceph_json


def main():
    directory = Path(sys.argv[1]).resolve()
    while True:
        run = json.loads((directory / 'results.json').read_text())
        if run.get('finished_utc'):
            break
        details = ceph_json('df', 'detail')
        pools = {p['name']: p['stats'] for p in details['pools']}
        item = {'time_utc': datetime.now(timezone.utc).isoformat(),
                'data': pools.get(run['data_pool']), 'cache': pools.get(run['cache_pool'])}
        with (directory / 'pool-samples.jsonl').open('a') as stream:
            stream.write(json.dumps(item) + '\n')
        time.sleep(10)


if __name__ == '__main__':
    main()
