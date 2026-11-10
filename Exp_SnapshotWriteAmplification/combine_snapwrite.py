#!/usr/bin/env python3
"""Combine complete zero-deferred paired repetitions from saved results.json files."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sources', type=Path, nargs='+')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pairs', type=int, default=5)
    args = parser.parse_args()
    selected = []
    selection = []
    reference = None
    sources = []
    for directory in args.sources:
        path = directory / 'results.json'
        data = json.loads(path.read_text())
        assert data['state'] == 'completed'
        parameters = {k: data['parameters'][k] for k in
                      ('objects', 'rounds', 'pg_num', 'concurrency', 'idle_seconds')}
        if reference is None:
            reference = parameters
        assert parameters == reference
        sources.append({'directory': str(directory.resolve()),
                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        for rep in range(1, data['parameters']['repetitions'] + 1):
            groups = [g for g in data['groups'] if g['repetition'] == rep]
            assert {g['mode'] for g in groups} == {'baseline', 'snapshot'}
            rows = [r for g in groups for r in g['rounds']]
            assert len(rows) == 2 * parameters['rounds']
            contaminated = [{'mode': r['mode'], 'round': r['round'],
                             'deferred_bytes': r['delta']['sum']['issued_deferred_write_bytes']}
                            for r in rows if r['delta']['sum']['issued_deferred_write_bytes']]
            item = {'source_directory': str(directory.resolve()), 'source_repetition': rep,
                    'deferred_rounds': contaminated}
            if contaminated:
                item['decision'] = 'excluded: nonzero deferred bytes during workload windows'
            elif len(selected) < args.pairs:
                item['decision'] = 'included'
                item['selected_repetition'] = len(selected) + 1
                selected.append((directory, data, rep, groups))
            else:
                item['decision'] = 'unused: target already reached'
            selection.append(item)
    assert len(selected) == args.pairs, f'only {len(selected)} clean paired repetitions'
    args.output.mkdir(parents=True, exist_ok=False)
    raw = args.output / 'raw'
    raw.mkdir()
    combined = copy.deepcopy(selected[0][1])
    combined['parameters']['repetitions'] = args.pairs
    combined['parameters']['label'] = 'validated'
    combined['groups'] = []
    combined['round_results'] = []
    combined['created_pools'] = []
    combined['cleanup'] = {}
    combined['selection'] = selection
    combined['sources'] = sources
    combined['final_status'] = selected[-1][1]['final_status']
    combined['final_pools'] = selected[-1][1]['final_pools']
    combined['finished_at_utc'] = selected[-1][1]['finished_at_utc']
    for number, (directory, data, rep, groups) in enumerate(selected, 1):
        combined['cleanup'].update(data['cleanup'])
        combined['created_pools'].extend(data['created_pools'])
        for original in groups:
            group = copy.deepcopy(original)
            group['source_directory'] = str(directory.resolve())
            group['source_repetition'] = rep
            group['repetition'] = number
            for row in group['rounds']:
                row['source_directory'] = str(directory.resolve())
                row['source_repetition'] = rep
                row['repetition'] = number
                combined['round_results'].append(row)
            combined['groups'].append(group)
            prefix = f'rep{rep:02d}-{group["mode"]}'
            for path in (directory / 'raw').glob(f'{prefix}-*.json'):
                target = raw / path.name.replace(prefix, f'rep{number:02d}-{group["mode"]}', 1)
                shutil.copy2(path, target)
    # De-duplicate pool provenance for sources contributing multiple repetitions.
    combined['created_pools'] = list({p['id']: p for p in combined['created_pools']}.values())
    for name, value in [('results.json', combined), ('selection.json', selection),
                        ('sources.json', sources), ('parameters.json', combined['parameters'])]:
        (args.output / name).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    print(json.dumps({'output': str(args.output), 'selection': selection}, indent=2))


if __name__ == '__main__':
    main()
