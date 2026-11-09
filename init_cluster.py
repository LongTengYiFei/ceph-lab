#!/usr/bin/env python3
"""Initialize the explicitly selected twelve old lab OSD devices once.

This is the destructive creation entry point. For an existing cluster use
clusterctl.py start/stop/restart instead; those operations never erase data.
"""
import json
import shlex
import uuid
from datetime import datetime, timezone

from lab_common import BUILD, HOSTS, LAB, SOURCE, TARGET, ceph, ceph_json, install_text, manifest, python, remote, save, wait
import clusterctl


def systemd_template(kind):
    conflicts = f'ceph-{kind}@%i.service'
    if kind == 'osd':
        conflicts += ' ceph-custom-osd@%i.service'
    common = f'''[Unit]
Description=Ceph 17.2.7 {kind}.%i
Wants=network-online.target
After=network-online.target
Conflicts={conflicts}

[Service]
Type=simple
Environment=LD_LIBRARY_PATH={BUILD}/lib
Environment=PYTHONPATH={BUILD}/lib/cython_modules/lib.3:{SOURCE}/src/pybind:{SOURCE}/src/python-common
LimitNOFILE=1048576
LimitMEMLOCK=infinity
TasksMax=infinity
Restart=on-failure
RestartSec=5
TimeoutStopSec=120
'''
    base = f'{BUILD}/bin/ceph-{kind} -f --conf /etc/ceph/ceph.conf --cluster ceph --id %i'
    if kind == 'osd':
        extra = f' --setuser root --setgroup root --osd-data /var/lib/ceph/osd/ceph-%i --keyring /var/lib/ceph/osd/ceph-%i/keyring --osd-class-dir {BUILD}/lib'
    elif kind == 'mon':
        common += 'User=cyf\nGroup=cyf\n'
        extra = ' --mon-data /var/lib/ceph/mon/ceph-%i'
    else:
        common += 'User=cyf\nGroup=cyf\n'
        extra = f' --mgr-data /var/lib/ceph/mgr/ceph-%i --mgr-module-path {LAB}/mgr-modules'
    return common + f'ExecStart={base}{extra} --admin-socket /run/ceph/ceph-{kind}.%i.asok --pid-file /run/ceph/ceph-{kind}.%i.pid --log-file /var/log/ceph/quincy-{kind}.%i.log\n\n[Install]\nWantedBy=multi-user.target\n'


def prepare_mgr_modules():
    # Quincy prepends mgr_module_path ahead of system site-packages. Keep the
    # matching native bindings there through symlinks, with no source edits.
    code = '''from pathlib import Path
root=Path('/home/cyf/ceph-dpu');out=Path('/home/cyf/ceph-lab-17.2.7/mgr-modules');out.mkdir(exist_ok=True)
items=list((root/'src/pybind/mgr').iterdir())+list((root/'src/pybind').glob('*.py'))+[root/'src/python-common/ceph']+list((root/'build/lib/cython_modules/lib.3').glob('*.so'))
for item in items:
 target=out/item.name
 if target.is_symlink():target.unlink()
 if not target.exists():target.symlink_to(item,target_is_directory=item.is_dir())
'''
    for host in HOSTS:
        python(host,code)


def main():
    m = manifest()
    if m.get('cluster_initialized'):
        raise RuntimeError('cluster already initialized; use clusterctl.py to manage it')
    # All daemons, plugins and clients must finish building before deployment.
    for host in HOSTS:
        status = remote(host, f"systemctl show {m['build_units'][host]} --property=ActiveState,ExecMainStatus").stdout
        if 'ExecMainStatus=0' not in status or 'ActiveState=active' in status or 'ActiveState=failed' in status:
            raise RuntimeError(f'{host} build is not complete: {status}')
        version = remote(host, f'env LD_LIBRARY_PATH={BUILD}/lib {BUILD}/bin/ceph-osd --version').stdout
        if TARGET not in version:
            raise RuntimeError(f'{host}: unexpected executable: {version}')
    print('All four upstream builds verified; initializing fresh lab', flush=True)
    prepare_mgr_modules()
    if not m.get('directories_archived'):
        for host in HOSTS:
            code = f'''import os,pathlib,pwd,shutil
backup=pathlib.Path({str(LAB/'backups'/m['stamp']/'old-directories')!r});backup.mkdir(parents=True,exist_ok=True)
for base in ('osd','mon','mgr'):
 p=pathlib.Path('/var/lib/ceph')/base
 if p.exists():
  dest=backup/base;dest.mkdir(exist_ok=True)
  for child in p.iterdir():
   if child.name.startswith('ceph-'):shutil.move(str(child),str(dest/child.name))
for p in ('/run/ceph','/var/log/ceph','/var/lib/ceph/mon/ceph-node1','/var/lib/ceph/mgr/ceph-m1','/var/lib/ceph/osd','/var/lib/ceph/bootstrap-osd'):
 pathlib.Path(p).mkdir(parents=True,exist_ok=True)
u=pwd.getpwnam('cyf')
for p in ('/run/ceph','/var/log/ceph','/var/lib/ceph/mon/ceph-node1','/var/lib/ceph/mgr/ceph-m1'):
 os.chown(p,u.pw_uid,u.pw_gid)
'''
            python(host, code, root=True)
        m['directories_archived'] = True
        save(LAB/'manifest.json', m)
    for host,node in m['nodes'].items():
        conf = f'''[global]
fsid = {m['fsid']}
mon initial members = node1
mon host = [v2:{m['mon_ip']}:3300/0,v1:{m['mon_ip']}:6789/0]
public network = 10.26.57.0/24
auth cluster required = cephx
auth service required = cephx
auth client required = cephx
ms_type = async+rdma
ms_cluster_type = async+rdma
ms_async_rdma_device_name = mlx5_1
ms_async_rdma_polling_us = 0
ms_async_rdma_local_gid = {node['gid']}
erasure code dir = {BUILD}/lib
plugin dir = {BUILD}/lib

[mon]
mon_allow_pool_delete = true
mon data avail warn = 2
mon data avail crit = 1
auth_allow_insecure_global_id_reclaim = false

[mgr]
mgr module path = {LAB}/mgr-modules

[osd]
public network = 10.200.0.0/24
cluster network = 10.200.0.0/24
osd class dir = {BUILD}/lib
'''
        install_text(host, '/etc/ceph/ceph.conf', conf)
        install_text(host, '/etc/systemd/system/ceph-quincy-osd@.service', systemd_template('osd'))
        if host == 'node1':
            for kind in ('mon','mgr'):
                install_text(host, f'/etc/systemd/system/ceph-quincy-{kind}@.service', systemd_template(kind))
        remote(host, 'sudo -n systemctl daemon-reload')
    if not m.get('keys_generated'):
        code = f'''import os,pathlib,pwd,subprocess
b={str(BUILD)!r};env=dict(os.environ,LD_LIBRARY_PATH=b+'/lib')
def run(args):subprocess.run([b+'/bin/'+args[0],*args[1:]],env=env,check=True,stdout=subprocess.DEVNULL)
mon='/etc/ceph/ceph.mon.keyring';admin='/etc/ceph/ceph.client.admin.keyring';bootstrap='/var/lib/ceph/bootstrap-osd/ceph.keyring'
for p in (mon,admin,bootstrap):pathlib.Path(p).unlink(missing_ok=True)
run(['ceph-authtool','--create-keyring',mon,'--gen-key','-n','mon.','--cap','mon','allow *'])
run(['ceph-authtool','--create-keyring',admin,'--gen-key','-n','client.admin','--cap','mon','allow *','--cap','osd','allow *','--cap','mds','allow *','--cap','mgr','allow *'])
run(['ceph-authtool','--create-keyring',bootstrap,'--gen-key','-n','client.bootstrap-osd','--cap','mon','profile bootstrap-osd','--cap','mgr','allow r'])
run(['ceph-authtool',mon,'--import-keyring',admin]);run(['ceph-authtool',mon,'--import-keyring',bootstrap])
u=pwd.getpwnam('cyf')
for p in (mon,admin,bootstrap):os.chown(p,u.pw_uid,u.pw_gid);os.chmod(p,0o600)
pathlib.Path('/etc/ceph/monmap').unlink(missing_ok=True)
run(['monmaptool','--create','--addv','node1','[v2:{m['mon_ip']}:3300/0,v1:{m['mon_ip']}:6789/0]','--fsid',{m['fsid']!r},'/etc/ceph/monmap'])
'''
        python('node1', code, root=True)
        m['keys_generated'] = True
        save(LAB/'manifest.json',m)
    if not m.get('mon_formatted'):
        command = ['sudo','-n','env',f'LD_LIBRARY_PATH={BUILD}/lib',str(BUILD/'bin/ceph-mon'),
                   '--conf','/etc/ceph/ceph.conf','--mkfs','-i','node1','--monmap','/etc/ceph/monmap',
                   '--keyring','/etc/ceph/ceph.mon.keyring','--setuser','cyf','--setgroup','cyf']
        remote('node1',shlex.join(command),timeout=180)
        remote('node1','sudo -n chown -R cyf:cyf /var/lib/ceph/mon/ceph-node1')
        m['mon_formatted'] = True
        save(LAB/'manifest.json',m)
    remote('node1','sudo -n systemctl enable --now ceph-quincy-mon@node1.service')
    wait('new monitor quorum',lambda:ceph_json('status',timeout=15).get('quorum_names'),180)
    # Transfer keyrings through standard input; never print secrets or put them in argv.
    for path in ('/etc/ceph/ceph.client.admin.keyring','/var/lib/ceph/bootstrap-osd/ceph.keyring'):
        content = python('node1',f'from pathlib import Path; print(Path({path!r}).read_text(),end="")',root=True).stdout
        for host in HOSTS:
            if host != 'node1':install_text(host,path,content,0o600,'cyf')
    ceph('auth','get-or-create','mgr.m1','mon','allow profile mgr','osd','allow *','mds','allow *',
         '-o','/var/lib/ceph/mgr/ceph-m1/keyring')
    remote('node1','sudo -n chown -R cyf:cyf /var/lib/ceph/mgr/ceph-m1')
    remote('node1','sudo -n systemctl enable --now ceph-quincy-mgr@m1.service')
    for host,node in m['nodes'].items():
        for row in node['osds']:
            if row.get('initialized'):
                continue
            osd = row['id']
            row.setdefault('uuid',str(uuid.uuid4()))
            save(LAB/'manifest.json',m)
            created = int(ceph('osd','create',row['uuid'],str(osd)).strip())
            if created != osd:
                raise RuntimeError(f'expected osd.{osd}, got {created}')
            # ceph() trims CLI output; the keyring parser requires the final LF.
            keyring = ceph('auth','get-or-create',f'osd.{osd}','mon','allow profile osd','osd','allow *','mgr','allow profile osd') + '\n'
            code = f'''import json,pathlib,subprocess
dev={row['device']!r}
d=json.loads(subprocess.check_output(['lsblk','-J','-b','-o','PATH,SIZE,TYPE,FSTYPE,MOUNTPOINTS,SERIAL',dev],text=True))['blockdevices'][0]
assert d['serial']=={row['serial']!r} and d['size']=={row['size']} and d['type']=='disk' and not any(d['mountpoints']) and not d.get('children') and d['fstype'] in ('ceph_bluestore',None),d
p=pathlib.Path('/var/lib/ceph/osd/ceph-{osd}');p.mkdir(parents=True,exist_ok=True)
if (p/'block').is_symlink():assert (p/'block').resolve()==pathlib.Path(dev)
else:(p/'block').symlink_to(dev)
subprocess.run(['blkdiscard',dev],check=True)
subprocess.run(['wipefs','-a',dev],check=True,stdout=subprocess.DEVNULL)
'''
            python(host,code,root=True,timeout=180)
            install_text(host,f'/var/lib/ceph/osd/ceph-{osd}/keyring',keyring,0o600)
            command = ['sudo','-n','prlimit','--memlock=unlimited','--','env',f'LD_LIBRARY_PATH={BUILD}/lib',
                       str(BUILD/'bin/ceph-osd'),'--conf','/etc/ceph/ceph.conf','--mkfs','-i',str(osd),
                       '--osd-uuid',row['uuid'],'--osd-data',f'/var/lib/ceph/osd/ceph-{osd}',
                       '--keyring',f'/var/lib/ceph/osd/ceph-{osd}/keyring','--setuser','root','--setgroup','root']
            remote(host,shlex.join(command),timeout=300)
            remote(host,f'sudo -n systemctl enable --now ceph-quincy-osd@{osd}.service')
            row['initialized'] = True
            save(LAB/'manifest.json',m)
            print(f'{host}: osd.{osd} initialized on {row["device"]}',flush=True)
    wait('twelve OSDs ready',clusterctl.clean,900)
    m['cluster_initialized'] = True
    m['initialized_at_utc'] = datetime.now(timezone.utc).isoformat()
    save(LAB/'manifest.json',m)
    clusterctl.verify()
    print(ceph('status'))
    print(ceph('osd','tree'))


if __name__ == '__main__':
    main()
