#!/usr/bin/env python3
"""Run actual YCSB A on disposable tiered pools and measure six whole SSD Bs.

Uses the upstream 0.17.0 CoreWorkload with a fixed-4KiB overwrite RADOS binding.
Records are loaded directly to the backend; warmup and run use normal overlay.
All operations/counters are retained, including failed or smoke runs.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
LAB = HERE.parent
sys.path.insert(0, str(LAB))
from lab_common import ceph, ceph_json, save

JAVA_CP = ':'.join([str(HERE / 'ycsb/build/classes'),
                   str(HERE / 'tools/YCSB-0.17.0/core/target/core-0.17.0.jar'),
                   str(HERE / 'tools/YCSB-0.17.0/core/target/dependency/*')])
TIER_KEYS = ('tier_promote', 'tier_flush', 'tier_try_flush', 'tier_evict',
             'agent_flush', 'agent_evict', 'tier_dirty', 'tier_clean',
             'tier_proxy_read', 'tier_proxy_write', 'op_cache_hit')


def stamp():
    return datetime.now(timezone.utc).isoformat()


def clean():
    status = ceph_json('status')
    osds = status['osdmap']
    return (osds['num_osds'] == osds['num_up_osds'] == osds['num_in_osds'] == 12
            and all({'active', 'clean'} <= set(p['state_name'].split('+'))
                    for p in status['pgmap']['pgs_by_state']))


class Experiment:
    def __init__(self, args):
        self.args = args
        self.layout = json.loads((HERE / 'layout.json').read_text())
        self.tag = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        self.directory = HERE / 'results' / (self.tag + ('-smoke-a' if args.smoke else '-ycsb-a'))
        self.directory.mkdir(parents=True)
        self.data = 'objmeta-ya-data-' + self.tag.lower()
        self.cache = 'objmeta-ya-cache-' + self.tag.lower()
        self.raw = {}
        self.periodic = []
        self.metrics = {}
        self.created = []
        self.original = {}
        self.record = {'started_utc': stamp(), 'parameters': vars(args),
                       'data_pool': self.data, 'cache_pool': self.cache,
                       'binding': 'official YCSB 0.17.0 CoreWorkload + custom raw 4096-byte RADOS overwrite',
                       'record_header': '8 bytes within payload: OMWA marker and CRC32',
                       'counter': '/sys/class/block/<physical SSD B>/stat field 7 * 512',
                       'commands': [], 'phases': {}, 'passed': False}
        save(self.directory / 'results.json', self.record)

    def command(self, *args):
        self.record['commands'].append({'time_utc': stamp(), 'argv': list(args)})
        save(self.directory / 'results.json', self.record)
        return ceph(*args)

    def wait(self, description, test, timeout=600):
        print('Waiting: ' + description, flush=True)
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if test():
                return
            time.sleep(2)
        raise RuntimeError('timeout: ' + description)

    def sample(self, name, smart=False, periodic=False):
        source = (HERE / 'ycsb_probe.py').read_text()
        def read(host, value):
            command = shlex.join(['sudo', '-n', 'python3', '-c', source])
            p = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                                '-p', '23579', host, command],
                               input=json.dumps({'pairs': value['pairs'], 'smart': smart}),
                               text=True, capture_output=True, timeout=60)
            if p.returncode:
                raise RuntimeError(host + ': ' + p.stderr[-3000:])
            return host, json.loads(p.stdout)
        started = time.time_ns()
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(read, host, value) for host, value in self.layout['nodes'].items()]
            hosts = dict(f.result() for f in futures)
        result = {'name': name, 'started_ns': started, 'finished_ns': time.time_ns(), 'hosts': hosts}
        if self.raw:
            first = self.raw['initial']['hosts']
            for host, value in hosts.items():
                assert value['boot_id'] == first[host]['boot_id']
                for osd, item in value['osds'].items():
                    original = first[host]['osds'][osd]
                    assert (item['pid'], item['start_ticks']) == (original['pid'], original['start_ticks'])
                for device, item in value['disks'].items():
                    assert item['written_sectors'] >= first[host]['disks'][device]['written_sectors']
        if periodic:
            self.periodic.append(result)
        else:
            self.raw[name] = result
        return result

    def prepare(self):
        assert clean()
        assert ceph_json('fsid')['fsid'] == self.layout['fsid']
        existing = set(ceph_json('osd', 'pool', 'ls'))
        assert existing == {'.mgr', 'objmeta-data', 'objmeta-cache'}, existing
        self.original = {'pools': ceph_json('osd', 'pool', 'ls', 'detail'),
                         'flags': ceph_json('osd', 'dump')['flags'],
                         'balancer': ceph_json('balancer', 'status'),
                         'status': ceph_json('status'), 'versions': ceph_json('versions')}
        self.record['before'] = self.original
        self.command('balancer', 'off')
        for pool, rule in ((self.data, 'objmeta-backend'), (self.cache, 'objmeta-cache')):
            self.command('osd', 'pool', 'create', pool, '64', '64', 'replicated', rule, '--autoscale-mode=off')
            self.created.append(pool)
            for key, value in [('size', '3'), ('min_size', '2'), ('compression_mode', 'none'),
                               ('noscrub', 'true'), ('nodeep-scrub', 'true')]:
                self.command('osd', 'pool', 'set', pool, key, value)
            self.command('osd', 'pool', 'application', 'enable', pool, 'rados')
        self.command('osd', 'tier', 'add', self.data, self.cache)
        for key, value in self.layout['cache'].items():
            if key != 'mode':
                self.command('osd', 'pool', 'set', self.cache, key, str(value))
        self.command('osd', 'pool', 'set', self.cache, 'target_max_bytes', str(self.args.cache_bytes))
        self.command('osd', 'tier', 'cache-mode', self.cache, 'writeback')
        self.command('osd', 'tier', 'set-overlay', self.data, self.cache)
        self.wait('temporary pools clean', clean)
        self.record['test_pools'] = [p for p in ceph_json('osd', 'pool', 'ls', 'detail')
                                     if p['pool_name'] in (self.data, self.cache)]
        self.sample('initial', smart=True)
        self.command_record_sources()

    def command_record_sources(self):
        sources = {}
        for file in list((HERE / 'ycsb').glob('*.java')) + list((HERE / 'ycsb').glob('*.c')) + [
                HERE / 'ycsb/workloada.properties', HERE / 'run_ycsb.py', HERE / 'ycsb_probe.py',
                HERE / 'tools/ycsb-0.17.0-source.tar.gz',
                HERE / 'tools/YCSB-0.17.0/core/target/core-0.17.0.jar']:
            sources[str(file.relative_to(HERE))] = hashlib.sha256(file.read_bytes()).hexdigest()
        self.record['source_sha256'] = sources
        save(self.directory / 'results.json', self.record)

    def java(self, clazz, *args):
        return ['sudo', '-n', 'prlimit', '--memlock=unlimited', '--', 'env',
                'LD_LIBRARY_PATH=/home/cyf/ceph-dpu/build/lib:' + str(HERE / 'ycsb/build'),
                'java', '-Xms512m', '-Xmx2g', '-Djava.library.path=' + str(HERE / 'ycsb/build'),
                '-cp', JAVA_CP, clazz, *map(str, args)]

    def run_phase(self, name, seconds=0, load=False):
        self.sample(name + '-start', smart=True)
        path = self.directory / (name + '.log')
        stats = self.directory / (name + '-client.json')
        command = self.java('site.ycsb.Client', '-load' if load else '-t', '-s',
                            '-db', 'lab.RadosBlockDB', '-P', HERE / 'ycsb/workloada.properties',
                            '-threads', self.args.threads,
                            '-p', 'recordcount=' + str(self.args.records),
                            '-p', 'rados.pool=' + self.data,
                            '-p', 'rados.stats=' + str(stats), '-p', 'rados.phase=' + name,
                            '-p', 'rados.bypass=' + str(load).lower())
        if not load:
            command += ['-target', str(self.args.target_ops), '-p', 'operationcount=1000000000',
                        '-p', 'maxexecutiontime=' + str(seconds)]
        print('Starting YCSB ' + name + ': ' + str(path), flush=True)
        began = time.time()
        with path.open('w') as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            last = time.monotonic()
            timeout = 1800 if load else seconds + 660
            try:
                while process.poll() is None:
                    time.sleep(1)
                    if time.monotonic() - last >= 10:
                        sample = self.sample(name + '-periodic', periodic=True)
                        print(name, f'{time.time()-began:.0f}s', self.tier_totals(sample), flush=True)
                        last = time.monotonic()
                    if time.time() - began > timeout:
                        raise TimeoutError('YCSB ' + name)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=30)
        self.sample(name + '-end', smart=True)
        assert process.returncode == 0, path.read_text()[-4000:]
        assert stats.exists(), path.read_text()[-4000:]
        metric = json.loads(stats.read_text())
        assert metric['failures'] == metric['crc_failures'] == 0, metric
        if load:
            assert metric['insert_success'] == self.args.records
            assert metric['successful_write_bytes'] == self.args.records * 4096
            initial_tier = self.tier_totals(self.raw[name + '-start'])
            loaded_tier = self.tier_totals(self.raw[name + '-end'])
            assert loaded_tier['tier_promote'] == initial_tier['tier_promote'], 'load did not bypass cache'
        else:
            assert metric['read_success'] > 0 and metric['update_success'] > 0
            assert metric['successful_write_bytes'] == metric['update_success'] * 4096
        metric['wall_seconds'] = time.time() - began
        self.metrics[name] = metric
        self.record['phases'][name] = metric
        save(self.directory / 'results.json', self.record)

    def tier_totals(self, sample):
        values = {key: 0 for key in TIER_KEYS}
        cache_ids = {str(p['cache_id']) for value in self.layout['nodes'].values() for p in value['pairs']}
        for host in sample['hosts'].values():
            for osd, item in host['osds'].items():
                if osd in cache_ids:
                    for key in values:
                        values[key] += item['perf']['osd'].get(key, 0)
        return values

    def dirty(self):
        for pool in ceph_json('df', 'detail')['pools']:
            if pool['name'] == self.cache:
                return pool['stats']['dirty']
        return 0

    def drain(self):
        print('Draining dirty cache after measurement workload', flush=True)
        for key in ('cache_min_flush_age', 'cache_target_dirty_ratio', 'cache_target_dirty_high_ratio'):
            self.command('osd', 'pool', 'set', self.cache, key, '0')
        self.wait('cache dirty objects zero', lambda: self.dirty() == 0, timeout=900)
        # Require a quiet disk-writing window, with an explicit limit. No forced compaction.
        previous = self.sample('drain-dirty-zero', smart=True)
        quiet = 0
        for attempt in range(60):
            time.sleep(2)
            current = self.sample('drain-periodic', periodic=True)
            delta = self.written(current) - self.written(previous)
            if self.dirty() == 0 and delta <= 1024 * 1024:
                quiet += 1
            else:
                quiet = 0
            previous = current
            if quiet >= 3:
                break
        else:
            raise RuntimeError('SSD B writes did not settle within drain limit')
        self.sample('tail-end', smart=True)
        assert self.dirty() == 0

    def written(self, sample):
        return sum(d['written_bytes'] for h in sample['hosts'].values() for d in h['disks'].values())

    def validate(self):
        path = self.directory / 'readback.json'
        command = self.java('lab.CheckRecords', self.data, self.args.records, 4096,
                            self.args.threads, path)
        with (self.directory / 'readback.log').open('w') as log:
            p = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=1800)
        assert p.returncode == 0, (self.directory / 'readback.log').read_text()[-4000:]
        self.record['readback'] = json.loads(path.read_text())
        assert self.record['readback']['records_checked'] == self.args.records

    def idle_control(self):
        if not self.args.idle_seconds:
            return
        self.sample('idle-start', smart=True)
        for _ in range(self.args.idle_seconds // 10):
            time.sleep(10)
            self.sample('idle-periodic', periodic=True)
        self.sample('idle-end', smart=True)

    def summarize(self):
        begin, end = self.raw['formal-start'], self.raw['tail-end']
        client = self.metrics['formal']
        d = client['successful_write_bytes']
        rows = []
        for host, sample in end['hosts'].items():
            for device, entry in sample['disks'].items():
                start = begin['hosts'][host]['disks'][device]
                bytes_written = entry['written_bytes'] - start['written_bytes']
                assert bytes_written >= 0
                rows.append({'host': host, 'device': device, 'serial': entry['serial'],
                             'written_bytes': bytes_written, 'written_GiB': bytes_written / 1024**3})
        total = sum(row['written_bytes'] for row in rows)
        tier_begin = self.tier_totals(begin)
        tier_end = self.tier_totals(self.raw['formal-end'])
        tier_delta = {key: tier_end[key] - tier_begin[key] for key in TIER_KEYS}
        if not self.args.smoke:
            assert tier_delta['agent_evict'] > 0, 'no natural eviction in formal interval'
            assert tier_delta['tier_promote'] > 0, 'no cache promotion in formal interval'
        summary = {'client': client, 'ssd_b_written_bytes': total, 'ssd_b_written_GiB': total / 1024**3,
                   'host_write_amplification': total / d, 'three_replica_normalized': total / (3*d),
                   'active_written_bytes': self.written(self.raw['formal-end']) - self.written(begin),
                   'tail_written_bytes': self.written(end) - self.written(self.raw['formal-end']),
                   'formal_cache_events': tier_delta, 'per_disk': rows,
                   'main_window_seconds': (end['finished_ns'] - begin['started_ns']) / 1e9,
                   'warmup_excluded': True, 'cleanup_excluded': True, 'background_subtracted': False,
                   'nand_waf_measured': False}
        if 'idle-end' in self.raw:
            idle_bytes = self.written(self.raw['idle-end']) - self.written(self.raw['idle-start'])
            idle_seconds = (self.raw['idle-end']['finished_ns'] - self.raw['idle-start']['started_ns']) / 1e9
            summary['idle_control'] = {'bytes': idle_bytes, 'seconds': idle_seconds,
                                       'bytes_per_second': idle_bytes / idle_seconds}
        save(self.directory / 'summary.json', summary)
        with (self.directory / 'per-ssd.csv').open('w') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader(); writer.writerows(rows)
        self.record['summary'] = summary

    def cleanup(self):
        print('Cleaning only this run\'s temporary pools', flush=True)
        existing = set(ceph_json('osd', 'pool', 'ls'))
        if self.data in existing and self.cache in existing:
            details = {p['pool_name']: p for p in ceph_json('osd', 'pool', 'ls', 'detail')}
            if details[self.cache]['tier_of'] == details[self.data]['pool_id']:
                for key in ('cache_min_flush_age', 'cache_target_dirty_ratio', 'cache_target_dirty_high_ratio'):
                    self.command('osd', 'pool', 'set', self.cache, key, '0')
                self.wait('dirty cache drained before resource removal', lambda: self.dirty() == 0, 900)
                if details[self.data]['read_tier'] == details[self.cache]['pool_id']:
                    self.command('osd', 'tier', 'remove-overlay', self.data)
                if details[self.cache]['cache_mode'] == 'writeback':
                    self.command('osd', 'tier', 'cache-mode', self.cache, 'readproxy')
                self.command('osd', 'tier', 'cache-mode', self.cache, 'none')
                self.command('osd', 'tier', 'remove', self.data, self.cache)
        for pool in reversed(self.created):
            if pool in set(ceph_json('osd', 'pool', 'ls')):
                self.command('osd', 'pool', 'delete', pool, pool, '--yes-i-really-really-mean-it')
        if self.original.get('balancer', {}).get('active'):
            self.command('balancer', 'on')
        self.wait('original cluster clean after cleanup', clean, 900)
        if self.original:
            assert ceph_json('osd', 'dump')['flags'] == self.original['flags']
            after = ceph_json('osd', 'pool', 'ls', 'detail')
            before_by_name = {p['pool_name']: p for p in self.original['pools']}
            assert {p['pool_name'] for p in after} == set(before_by_name)
            for pool in after:
                assert pool == before_by_name[pool['pool_name']], 'existing pool configuration changed'
        self.record['cleanup_complete'] = True
        self.record['after'] = ceph_json('status')

    def run(self):
        try:
            self.prepare()
            self.run_phase('load', load=True)
            self.run_phase('warmup', self.args.warmup_seconds)
            self.run_phase('formal', self.args.seconds)
            self.drain()
            self.idle_control()
            self.summarize()
            self.validate()
            self.record['passed'] = True
        except Exception as error:
            self.record['error'] = repr(error)
            raise
        finally:
            try:
                self.cleanup()
            except Exception as error:
                self.record['cleanup_error'] = repr(error)
                self.record['passed'] = False
                raise
            finally:
                self.record['finished_utc'] = stamp()
                save(self.directory / 'results.json', self.record)
                with gzip.open(self.directory / 'raw-counters.json.gz', 'wt') as stream:
                    json.dump({'boundaries': self.raw, 'periodic': self.periodic}, stream)
                print('Results: ' + str(self.directory), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--records', type=int, default=1048576)
    parser.add_argument('--cache-bytes', type=int, default=1024**3)
    parser.add_argument('--warmup-seconds', type=int, default=300)
    parser.add_argument('--seconds', type=int, default=300)
    parser.add_argument('--target-ops', type=int, default=10000)
    parser.add_argument('--threads', type=int, default=32)
    parser.add_argument('--idle-seconds', type=int, default=30)
    parser.add_argument('--cleanup-result', type=Path)
    args = parser.parse_args()
    if args.smoke:
        args.records = 256; args.cache_bytes = 8*1024**2
        args.warmup_seconds = 2; args.seconds = 3; args.target_ops = 500; args.threads = 4
        args.idle_seconds = 0
    lock = (HERE / '.ycsb.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if args.cleanup_result:
        directory = args.cleanup_result.resolve()
        assert directory.is_relative_to(HERE / 'results')
        record = json.loads((directory / 'results.json').read_text())
        recovery = Experiment.__new__(Experiment)
        recovery.directory = directory; recovery.record = record
        recovery.data = record['data_pool']; recovery.cache = record['cache_pool']
        assert recovery.data.startswith('objmeta-ya-data-') and recovery.cache.startswith('objmeta-ya-cache-')
        recovery.created = [recovery.data, recovery.cache]; recovery.original = record['before']
        recovery.cleanup()
        record['recovery_cleanup_utc'] = stamp()
        save(directory / 'results.json', record)
        return
    Experiment(args).run()


if __name__ == '__main__':
    main()
