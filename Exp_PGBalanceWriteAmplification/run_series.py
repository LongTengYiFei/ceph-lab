#!/usr/bin/env python3
"""Run independent +1, +2, +3 experiments serially, then create an offline comparison."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from compare_pgbalance import compare

EXPERIMENT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', type=Path, help='Previously completed +4 run to include')
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    directory = EXPERIMENT / 'results' / f'{stamp}-series'
    directory.mkdir(parents=True, exist_ok=False)
    state = {'state': 'running', 'runs': [], 'reference': str(args.reference) if args.reference else None}

    def save():
        temporary = directory / 'series.json.tmp'
        temporary.write_text(json.dumps(state, indent=2) + '\n')
        temporary.replace(directory / 'series.json')

    save()
    try:
        with (directory / 'console.txt').open('w') as console:
            for added in (1, 2, 3):
                row = {'added_osds': added, 'state': 'running'}
                state['runs'].append(row)
                save()
                print(f'=== Independent 8 -> {8+added} test ===', flush=True)
                command = [str(EXPERIMENT / 'run.sh'), '--add-osds', str(added),
                           '--label', f'formal-add{added}']
                with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, bufsize=1) as process:
                    for line in process.stdout:
                        print(line, end='', flush=True)
                        console.write(line)
                        console.flush()
                        if line.startswith('RESULT_DIR='):
                            row['directory'] = line.split('=', 1)[1].strip()
                            save()
                    row['returncode'] = process.wait()
                row['state'] = 'completed' if row['returncode'] == 0 else 'failed'
                save()
                if row['returncode']:
                    raise RuntimeError(f'+{added} test failed; no later test started')
        directories = [Path(row['directory']) for row in state['runs']]
        if args.reference:
            directories.append(args.reference)
        state['comparison'] = compare(directories, EXPERIMENT)
        state['state'] = 'completed'
    except BaseException as error:
        state['state'] = 'failed'
        state['error'] = repr(error)
        raise
    finally:
        save()
        print(f'SERIES_DIR={directory}', flush=True)


if __name__ == '__main__':
    main()
