"""只报告允许字段；不回显配置路径、环境、原始异常或凭证。"""
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from archive.services.paths import (open_root, root_identity, open_regular_beneath, UnsafePath,
                                   validate_root_isolation)

VERIFIED_SQLITE_SOURCE = '2026-05-05 10:34:17 c88b22011a54b4f6fbd149e9f8e4de77658ce58143a1af0e3785e4e6475127e9'
PARAMETERS = ('WORKER_CONCURRENCY','MAX_FILE_BYTES','MAX_IMAGE_PIXELS','SCAN_INTERVAL_MS',
              'STABLE_WINDOW_MS','WORKER_MEMORY_MB','WORKER_JOB_SLEEP_MS','WORKER_NICE','RETRY_LIMIT','RETRY_BASE_MS')


def filesystem_status(path):
    # 内核挂载信息只用于匹配；返回值不包含路径或设备名称。
    try:
        actual = str(Path(path).resolve())
        entries=[]
        if sys.platform.startswith('linux'):
            for line in Path('/proc/self/mountinfo').read_text().splitlines():
                left,right=line.split(' - ',1)
                mount=left.split()[4]
                mount=re.sub(r'\\([0-7]{3})',lambda m:chr(int(m[1],8)),mount)
                entries.append((mount,right.split()[0],True))
        elif sys.platform == 'darwin':
            result=subprocess.run(['/sbin/mount'],capture_output=True,text=True,timeout=3,check=True)
            for line in result.stdout.splitlines():
                _,rest=line.split(' on ',1)
                mount,options=rest.rsplit(' (',1)
                flags=options.rstrip(')').split(', ')
                entries.append((mount,flags[0],'local' in flags))
        candidates=[entry for entry in entries if actual==entry[0] or actual.startswith(entry[0].rstrip('/')+'/')]
        mount,kind,local=max(candidates,key=lambda x:len(x[0]))
        local_types={'apfs','hfs','ext4','ext3','ext2','xfs','btrfs','zfs','f2fs','tmpfs'}
        network_types={'nfs','nfs4','cifs','smbfs','afpfs','sshfs','fuse.sshfs'}
        if kind in local_types and local:
            return {'status':'local_observed','type':kind}
        if kind in network_types:
            return {'status':'network_rejected','type':kind}
    except (OSError,ValueError,subprocess.SubprocessError):
        pass
    return {'status':'unknown','type':'unknown'}


def resource_status(*, apply=False):
    import resource
    target=settings.WORKER_MEMORY_MB*1024*1024
    memory='unsupported_unverified'
    limit=None
    if sys.platform.startswith('linux'):
        if apply:
            _,hard=resource.getrlimit(resource.RLIMIT_AS)
            limit=target if hard==resource.RLIM_INFINITY else min(target,hard)
            resource.setrlimit(resource.RLIMIT_AS,(limit,limit))
            memory='enforced'
        else:
            memory='supported_not_applied'
    nice=os.getpriority(os.PRIO_PROCESS,0)
    priority='observed'
    if apply and nice < settings.WORKER_NICE:
        try:
            nice=os.nice(settings.WORKER_NICE-nice)
            priority='enforced'
        except OSError:
            if sys.platform.startswith('linux'):
                raise
            priority='permission_denied_unverified'
    rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {'memory_control':memory,'memory_requested_bytes':target,'address_space_limit_bytes':limit,
            'nice_observed':nice,'nice_target':settings.WORKER_NICE,
            'priority_control':priority,
            'rss_peak_bytes':int(rss) if sys.platform=='darwin' else int(rss)*1024,
            'rss_measurement':'current_process_peak','concurrency':1,
            'job_sleep_ms':settings.WORKER_JOB_SLEEP_MS}


def runtime_report():
    errors=[]
    roots={};identities={}
    for name,path in (('inbox',settings.BIRDS_INBOX_ROOT),('data',settings.BIRDS_DATA_ROOT)):
        fd=None
        try:
            fd=open_root(path);identities[name]=root_identity(fd)
            roots[name]='available'
            if name=='inbox':
                data_fd=None
                try:
                    data_fd=open_root(settings.BIRDS_DATA_ROOT)
                    try:
                        with open_regular_beneath(data_fd,'inbox-root.json') as stream:
                            if json.loads(stream.read(4096)) != identities[name]:
                                roots[name]='identity_changed';errors.append('root_identity_changed')
                    except FileNotFoundError:
                        roots[name]='available_not_registered'
                finally:
                    if data_fd is not None:os.close(data_fd)
        except (OSError,ValueError,UnsafePath):
            roots[name]='unavailable';errors.append('root_unavailable')
        finally:
            if fd is not None:os.close(fd)
    distinct=len(identities)==2 and identities['inbox']!=identities['data']
    if not distinct:errors.append('roots_not_distinct')
    isolated = False
    inbox_fd = data_fd = None
    try:
        inbox_fd = open_root(settings.BIRDS_INBOX_ROOT)
        data_fd = open_root(settings.BIRDS_DATA_ROOT)
        validate_root_isolation(inbox_fd, data_fd, settings.BIRDS_INBOX_ROOT, settings.BIRDS_DATA_ROOT)
        isolated = True
    except (OSError, UnsafePath):
        errors.append('roots_not_isolated')
    finally:
        for fd in (inbox_fd, data_fd):
            if fd is not None:os.close(fd)
    fs=filesystem_status(settings.BIRDS_DATA_ROOT)
    if fs['status']!='local_observed':errors.append('data_filesystem_unverified')
    db={}
    try:
        if not Path(settings.DATABASES['default']['NAME']).is_file():
            raise ValueError('missing')
        with connection.cursor() as cursor:
            source=cursor.execute('SELECT sqlite_source_id()').fetchone()[0]
            db={'version':sqlite3.sqlite_version,'wal_fix':'verified_source_id' if source==VERIFIED_SQLITE_SOURCE else 'unverified',
                'journal_mode':cursor.execute('PRAGMA journal_mode').fetchone()[0],
                'synchronous':cursor.execute('PRAGMA synchronous').fetchone()[0],
                'foreign_keys':cursor.execute('PRAGMA foreign_keys').fetchone()[0]}
        if db['wal_fix']=='unverified':errors.append('wal_fix_unverified')
        if (db['journal_mode'],db['synchronous'],db['foreign_keys'])!=('wal',2,1):errors.append('sqlite_pragmas_invalid')
    except Exception:
        db={'status':'unavailable'};errors.append('database_unavailable')
    return {'python':platform.python_version(),'os':platform.system(),'roots':roots,'roots_distinct':distinct,
            'roots_isolated':isolated,
            'data_filesystem':fs,'sqlite':db,'parameters':{name:getattr(settings,name) for name in PARAMETERS},
            'resources':resource_status(),'recognition_resources':recognition_resource_status(),
            'errors':sorted(set(errors))}


def recognition_resource_status():
    """参数与本进程观测分开；不把报告探针当成子进程已生效。"""
    return {'enabled': settings.BIRDS_RECOGNITION_ENABLED,
            'memory_mb': settings.RECOGNITION_MEMORY_MB,
            'timeout_seconds': settings.RECOGNITION_TIMEOUT_SECONDS,
            'intra_threads': settings.RECOGNITION_INTRA_THREADS,
            'inter_threads': settings.RECOGNITION_INTER_THREADS,
            'nice': settings.RECOGNITION_NICE,
            'memory_control': 'supported_not_applied' if sys.platform.startswith('linux') else 'unsupported_unverified',
            'child_effective': 'not_observed',
            'hard_limit_inheritance': 'supervisor_preserves_hard_children_set_independent_hard'}


class Command(BaseCommand):
    def add_arguments(self, parser):
        modes = parser.add_mutually_exclusive_group()
        modes.add_argument('--deployment-init', action='store_true')
        modes.add_argument('--deployment-start', action='store_true')
        modes.add_argument('--deployment-migrate', action='store_true')
        modes.add_argument('--deployment-finalize', action='store_true')

    def handle(self,*args,**options):
        # 旧开发应用不启用部署契约；容器及显式CLI才要求完整登记。
        if any(options.get(name) for name in ('deployment_init', 'deployment_start', 'deployment_migrate', 'deployment_finalize')) or os.environ.get('BIRDS_DEPLOYMENT_MODE') == '1':
            from archive.services.deployment import (DeploymentError, register_deployment, validate_deployment,
                                                     migrate_deployment, finalize_deployment)
            try:
                if options.get('deployment_init'):
                    deployment = register_deployment()
                elif options.get('deployment_migrate'):
                    deployment = migrate_deployment(stdout=self.stdout)
                elif options.get('deployment_finalize'):
                    deployment = finalize_deployment()
                else:
                    deployment = validate_deployment()
            except (DeploymentError, OSError, ValueError) as error:
                code = error.code if isinstance(error, DeploymentError) else 'deployment_check_failed'
                self.stdout.write(json.dumps({'deployment': {'status': 'rejected'}, 'errors': [code]}))
                raise CommandError('deployment_check_failed：请按错误码检查专用根；未输出路径或凭证。') from None
            if options.get('deployment_init'):
                self.stdout.write(json.dumps({'deployment': deployment, 'errors': []}, sort_keys=True))
                return
        else:
            deployment = {'status': 'not_enabled'}
        report=runtime_report()
        report['deployment'] = deployment
        self.stdout.write(json.dumps(report,ensure_ascii=False,sort_keys=True))
        if report['errors']:
            raise CommandError('runtime_check_failed：请按错误码检查本地配置；未输出路径或凭证。')
