#!/usr/bin/env python3
"""Back up local edits/config, stop the previous lab and switch all sources."""
import concurrent.futures
import json
import shlex
import uuid
from datetime import datetime, timezone

from lab_common import LAB, HOSTS, TARGET, SOURCE, ceph_json, python, remote, save


def main():
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    LAB.mkdir(exist_ok=True)
    pools = ceph_json('osd', 'pool', 'ls')
    if pools != ['.mgr']:
        raise RuntimeError(f'expected the empty benchmark lab, found pools {pools}')
    save(LAB / 'previous_cluster_status.json', ceph_json('status'))
    old_fsid = ceph_json('status')['fsid']
    inventory_code = r'''
import json,pathlib,subprocess
root=pathlib.Path('/home/cyf/ceph-dpu')
devices=[]
for p in pathlib.Path('/var/lib/ceph/osd').glob('ceph-*'):
 if (p/'block').is_symlink():
  dev=(p/'block').resolve()
  data=json.loads(subprocess.check_output(['lsblk','-J','-b','-o','PATH,SIZE,TYPE,FSTYPE,MOUNTPOINTS,MODEL,SERIAL',str(dev)],text=True))['blockdevices'][0]
  if data['fstype']!='ceph_bluestore' or data['type']!='disk' or any(data['mountpoints']):
   raise RuntimeError(f'unexpected old OSD device {data}')
  devices.append({'old_id':int(p.name.split('-')[-1]),'device':str(dev),'serial':data['serial'],'size':data['size']})
print(json.dumps({'devices':sorted(devices,key=lambda x:x['old_id']), 'ceph_conf':pathlib.Path('/etc/ceph/ceph.conf').read_text(),
 'git_head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),
 'git_branch':subprocess.check_output(['git','branch','--show-current'],cwd=root,text=True).strip(),
 'git_status':subprocess.check_output(['git','status','--short'],cwd=root,text=True)}))
'''
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        inventory = dict(zip(HOSTS, ex.map(lambda h: json.loads(python(h, inventory_code, root=True).stdout), HOSTS)))
    save(LAB / 'previous_inventory.json', inventory)
    data = {'commit': TARGET, 'fsid': str(uuid.uuid4()), 'previous_fsid': old_fsid,
            'created_at_utc': datetime.now(timezone.utc).isoformat(), 'stamp': stamp,
            'source': str(SOURCE), 'network': 'native RDMA, IB 10.200.0.0/24',
            'mon_host': 'node1', 'mon_ip': '10.26.57.54', 'mgr_id': 'm1', 'nodes': {}}
    for n, host in enumerate(HOSTS):
        rows = inventory[host]['devices']
        if len(rows) != 4:
            raise RuntimeError(f'{host}: expected four old lab OSD devices')
        gid = next(line.split('=',1)[1].strip() for line in inventory[host]['ceph_conf'].splitlines()
                   if line.startswith('ms_async_rdma_local_gid'))
        data['nodes'][host] = {'old_osds': [r['old_id'] for r in rows], 'gid': gid,
                               'osds': [{**r, 'id': n * 4 + i} for i,r in enumerate(rows)] if n < 3 else []}
    save(LAB / 'manifest.json', data)
    # Preserve auth/config and service definitions before changing the lab.
    for host in HOSTS:
        remote(host, 'mkdir -p ' + str(LAB))
        backup = str(LAB / 'backups' / stamp)
        code = f'''import pathlib,shutil
p=pathlib.Path({backup!r});p.mkdir(parents=True,exist_ok=False);p.chmod(0o700)
shutil.copytree('/etc/ceph',p/'etc-ceph',symlinks=True)
for name in ['ceph-custom-osd@.service','ceph-mon@.service','ceph-mgr@.service']:
 src=pathlib.Path('/etc/systemd/system')/name
 if src.exists():shutil.copy2(src,p/name)
shutil.copy2('/home/cyf/ceph-dpu/build/CMakeCache.txt',p/'CMakeCache.txt')
'''
        python(host, code, root=True)
        units = [f'ceph-custom-osd@{n}.service' for n in data['nodes'][host]['old_osds']]
        remote(host, 'sudo -n systemctl disable --now ' + shlex.join(units), timeout=300)
    stop = r'''import os,pathlib,signal,time
targets=[]
for p in pathlib.Path('/proc').iterdir():
 if not p.name.isdigit():continue
 try:
  exe=(p/'exe').resolve()
  if exe.name in ('ceph-mon','ceph-mgr') and str(exe).startswith('/home/cyf/ceph-dpu/build/'):
   targets.append(int(p.name));os.kill(int(p.name),signal.SIGTERM)
 except (FileNotFoundError,PermissionError,ProcessLookupError):pass
for _ in range(60):
 if all(not pathlib.Path(f'/proc/{pid}').exists() for pid in targets):break
 time.sleep(1)
else:raise RuntimeError('old mon/mgr did not stop')
'''
    python('node1', stop, root=True, timeout=90)
    for host in HOSTS:
        backup = str(LAB / 'backups' / stamp)
        python(host, f'''import pathlib,subprocess
p=pathlib.Path({backup!r})
subprocess.run(['tar','-czf',str(p/'old-daemon-directories.tgz'),'-C','/var/lib/ceph','.'],check=True)
(p/'old-daemon-directories.tgz').chmod(0o600)
''', root=True, timeout=180)
    # Stash each dirty submodule separately, then save the main worktree.
    switch_code = r'''
import json,pathlib,subprocess
root=pathlib.Path('/home/cyf/ceph-dpu')
stamp=STAMP
def git(*args,cwd=root):return subprocess.check_output(['git',*args],cwd=cwd,text=True).strip()
branch='backup/pre-quincy-17.2.7-'+stamp
git('branch',branch,'HEAD')
saved={'backup_branch':branch,'old_head':git('rev-parse','HEAD'),'old_branch':git('branch','--show-current'),'submodule_stashes':{}}
for line in git('config','--file','.gitmodules','--get-regexp','path').splitlines():
 name=line.split(None,1)[1];p=root/name
 if not p.exists():continue
 if git('status','--porcelain',cwd=p):
  git('stash','push','--include-untracked','-m','before-quincy-17.2.7-'+stamp,cwd=p)
  saved['submodule_stashes'][name]=git('rev-parse','refs/stash',cwd=p)
if git('status','--porcelain'):
 git('stash','push','--include-untracked','-m','before-quincy-17.2.7-'+stamp)
 saved['stash']=git('rev-parse','refs/stash')
git('switch','--detach',TARGET)
git('submodule','update','--init','--recursive')
assert git('rev-parse','HEAD')==TARGET
git('diff','--exit-code',TARGET,'--')
p=pathlib.Path('/home/cyf/ceph-lab-17.2.7/source-preservation.json');p.write_text(json.dumps(saved,indent=2)+'\n')
print(json.dumps(saved))
'''
    for host in HOSTS:
        remote(host, 'mkdir -p ' + str(LAB))
        code = switch_code.replace('STAMP', repr(stamp)).replace('TARGET', repr(TARGET))
        result = json.loads(python(host, code, timeout=300).stdout)
        data['nodes'][host]['source_preservation'] = result
        save(LAB / 'manifest.json', data)
        print(host, 'source switched; preserved:', json.dumps(result), flush=True)
    print('All four source trees switched; old lab stopped; manifest:', LAB/'manifest.json', flush=True)


if __name__ == '__main__':
    main()
