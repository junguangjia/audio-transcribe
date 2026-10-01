"""Confirmed, scoped permanent removal of explicitly owned local artifacts.

Plans contain identities and file-stat evidence, never audio/transcript bodies.
Every unlink is relative to an opened, no-follow directory. Shared dependencies
and unknown files are retained; no recursive remover or filename glob deletes.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import uuid

from . import lifecycle
from .storage import read_doc, validate_id, write_json, write_yaml

_RUN_FILES={'manifest.json','resolved-config.yaml','transcript.json','transcript.md','transcript.txt','transcript.srt','diagnostics.json','review.md'}
_DECODE_FILES={'invocation.json','native.json','stdout.log','stderr.log','complete.json','provisional.json','timeline-audit.json','guardian.json','cache-provenance.json','decoder.log','audio.wav','decode.json'}


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def _roots(settings):
    roots={name:Path(settings['roots'][name]).expanduser().absolute() for name in ('data','app','cache','log') if settings['roots'].get(name)}
    for path in roots.values():
        for parent in (path,*path.parents):
            if parent.is_symlink():
                raise ValueError('Managed root contains a symbolic link; deletion is disabled.')
    return roots


def _safe_relative(root,path):
    root=Path(root).absolute();path=Path(path).absolute()
    try:relative=path.relative_to(root)
    except ValueError:raise ValueError('Deletion target is outside its managed root.') from None
    if not relative.parts or any(p in ('','.','..') for p in relative.parts):
        raise ValueError('Invalid deletion target.')
    for count in range(1,len(relative.parts)+1):
        component=root.joinpath(*relative.parts[:count])
        if component.is_symlink():
            raise ValueError('Symbolic-link deletion target rejected.')
    return relative


def _signature(path):
    value=path.lstat()
    if not stat.S_ISREG(value.st_mode):
        raise ValueError('Deletion target is not a regular file.')
    return {'device':value.st_dev,'inode':value.st_ino,'size':value.st_size,
            'mtime_ns':value.st_mtime_ns,'allocated':getattr(value,'st_blocks',0)*512,'links':value.st_nlink}


def _selection(request):
    supplied=request.get('selection',request)
    ids=supplied.get('result_ids',[]);jobs=supplied.get('jobs',[])
    if not isinstance(ids,list) or not isinstance(jobs,list) or len(ids)+len(jobs)>1000:
        raise ValueError('Select a bounded list of recordings.')
    ids=list(dict.fromkeys(validate_id(v,'result ID') for v in ids))
    clean=[]
    for job in jobs:
        if not isinstance(job,dict):raise ValueError('Invalid selected task.')
        item={'job_id':str(uuid.UUID(str(job.get('job_id'))))}
        for key in ('attempt_id','batch_id'):
            if job.get(key):item[key]=str(uuid.UUID(str(job[key])))
        if job.get('path') is not None:
            if not isinstance(job['path'],str) or not Path(job['path']).is_absolute():raise ValueError('Selected task path must be absolute.')
            item['path']=str(Path(job['path']).absolute())
        clean.append(item)
    if not ids and not clean:raise ValueError('Select at least one recording or task.')
    return {'result_ids':ids,'jobs':clean}


def _source_ref(record):
    if record['kind']=='new':
        x=record['saved']['internal'];return x['session_id'],x['source_id']
    if record['kind']=='source':return record['session_path'].name,record['source']['id']
    return None


def _run_ref(record):
    if record['kind']=='new':
        internal=record['saved']['internal'];return internal['session_id'],internal['run_id']
    if record['kind']=='source':return record['session_path'].name,record['run_path'].name
    return None


def _run_owned_files(run):
    """Known engine artifacts; user-created extras never become purge targets."""
    files=[]
    for child in run.iterdir():
        if child.name in _RUN_FILES and child.is_file():files.append(child)
    logs=run/'logs'
    if logs.is_dir():
        for source in logs.iterdir():
            if source.is_dir() and re.fullmatch(r'interrupted-finalization-[0-9a-f]+',source.name):
                files.extend(p for p in source.iterdir() if p.name in _RUN_FILES and p.is_file())
                continue
            if source.is_dir() and re.fullmatch(r'\.cache-copy-[0-9a-f]+',source.name):
                files.extend(p for p in source.iterdir() if p.name in _DECODE_FILES and p.is_file())
                continue
            if not source.is_dir() or not re.fullmatch(r'src-[A-Za-z0-9_-]+',source.name):continue
            for child in source.iterdir():
                if child.name in _DECODE_FILES and child.is_file():files.append(child)
                elif child.name=='attempts' and child.is_dir():
                    for attempt in child.iterdir():
                        if not attempt.is_dir() or not re.fullmatch(r'attempt-[A-Za-z0-9_-]+',attempt.name):continue
                        files.extend(p for p in attempt.iterdir() if p.name in _DECODE_FILES and p.is_file())
    return files


def plan(settings,request):
    from .direct import _records
    roots=_roots(settings);data=roots['data'];selection=_selection(request);value=lifecycle.state(settings)
    records=_records(settings,include_deleted=True)
    selected_ids=set(selection['result_ids']);scopes=set();refs=set();selected=[];warnings=[];retained=[];active=[];unresolved=[]
    for identity in selected_ids:
        record=records.get(identity)
        if record is None:raise ValueError('Selected result is unavailable; refresh the list.')
        scope=lifecycle.record_scope(record)
        if scope:scopes.add(scope);refs.add(_source_ref(record))
    job_ids={j['job_id'] for j in selection['jobs']}
    for job in selection['jobs']:
        known=value['jobs'].get(job['job_id'])
        if known and known.get('scope'):
            scopes.add(known['scope']);refs.add((known['session_id'],known['source_id']))
        if known and known['state']=='active':active.append(job)
        elif not known:
            # A queued native task may not yet have created any managed files.
            selected.append({'recording_ref':job['job_id'],'filename':Path(job.get('path','Queued recording')).name,'result_ids':[],'variant_count':0})
    for identity,record in records.items():
        if lifecycle.record_scope(record) in scopes:selected_ids.add(identity)
    job_ids.update(identity for identity,job in value['jobs'].items() if job.get('scope') in scopes)
    active_ids={j['job_id'] for j in active}
    for identity,job in value['jobs'].items():
        if job.get('scope') in scopes and job['state']=='active' and identity not in active_ids:
            active.append({'job_id':identity});active_ids.add(identity)
    for key in sorted(scopes):
        items=[(i,r) for i,r in records.items() if lifecycle.record_scope(r)==key]
        name=items[0][1]['summary']['filename'] if items else next((j['filename'] for j in value['jobs'].values() if j.get('scope')==key),'Recording')
        selected.append({'recording_ref':key,'filename':name,'result_ids':[i for i,_ in items],'variant_count':len(items)})
    targets={};owned_dirs=set();fingerprints={}
    def add(path,kind,root_name='data'):
        root=roots[root_name];relative=_safe_relative(root,path)
        if not path.exists():return
        signature=_signature(path);key=(root_name,relative.as_posix())
        targets[key]={'root':root_name,'path':relative.as_posix(),'kind':kind,'stat':signature}
        parent=relative.parent
        while parent.parts and parent.as_posix() not in {'results','results/labels','sessions','archive','exports','library','decoded-media','app','performance'}:
            owned_dirs.add((root_name,parent.as_posix()));parent=parent.parent
    retained_refs=set();retained_runs=set();session_updates=[]
    for identity,record in records.items():
        if identity not in selected_ids:
            if not lifecycle.visible(record,identity,value):continue
            ref=_source_ref(record)
            if ref:retained_refs.add(ref);retained_runs.add(_run_ref(record))
            elif record['kind']=='report':
                for entry in record['manifest'].get('ordered_sources',[]):
                    t=entry.get('transcript',{})
                    if t.get('session_id') and t.get('source_id'):
                        retained_refs.add((t['session_id'],t['source_id']))
                        if t.get('run_id'):retained_runs.add((t['session_id'],t['run_id']))
        else:
            if record['kind']=='new':
                base=data/'results'/identity
                for name in ('result.json','transcript.md'):add(base/name,'result')
                add(data/'results'/'labels'/(identity+'.json'),'labels')
                fingerprints[identity]=record['saved']['document_sha256']
            elif record['kind']=='report':
                base=record['path'].parent
                add(base/'manifest.json','legacy_report');add(base/'transcript-report.md','legacy_report')
                selected.append({'recording_ref':identity,'filename':record['summary']['filename'],'result_ids':[identity],'variant_count':1})
                retained.append({'kind':'historical_report_sources','item_count':len(record['manifest'].get('ordered_sources',[])),
                                 'reason':'Only this historical report document is selected. Its source recordings and transcription runs remain available as separate recordings.'})
                fingerprints[identity]=record['manifest'].get('report_sha256')
            else:
                add(data/'results'/'labels'/(identity+'.json'),'labels')
    # Tombstoned legacy views are not retained consumers. Real unselected direct
    # aliases/reports are, even if they share identical bytes or an archived run.
    for job_id,job in value['jobs'].items():
        if job_id not in job_ids and job.get('state')=='active' and job.get('scope') not in scopes and job.get('source_id'):
            retained_refs.add((job['session_id'],job['source_id']))
    from .storage import locate_session,validate_session
    selected_hashes=set();retained_hashes=set();sessions={};ignored_versions=set()
    for session_id,source_id in sorted(refs):
        try:
            session_path=locate_session(data,session_id);session=validate_session(session_path)
        except (OSError,ValueError):
            warnings.append('A selected managed session could not be validated; its files are retained.');continue
        sessions[session_id]=(session_path,session)
        source=next(s for s in session['sources'] if s['id']==source_id);selected_hashes.add(source['sha256'])
        if source.get('ownership')=='external_referenced':
            from .watch import version_key
            ignored_versions.add(version_key(source['external_path'],source['sha256']))
        if (session_id,source_id) in retained_refs:
            retained.append({'kind':'session','item_count':1,'reason':'An unselected recording alias or historical report still depends on this source and its runs.'})
    # Metadata-only scan supplies reference counts; it never hashes audio.
    session_files=list((data/'sessions').glob('*/session.yaml'))+list((data/'archive').glob('*/*/session.yaml'))
    for path in session_files:
        _safe_relative(data,path)
        session=read_doc(path)
        for source in session.get('sources',[]):
            if (session.get('id'),source.get('id')) not in refs or (session.get('id'),source.get('id')) in retained_refs:
                retained_hashes.add(source.get('sha256'))
    for session_id,(base,session) in sessions.items():
        allrefs={(session_id,s['id']) for s in session['sources']}
        if not allrefs.issubset(refs):
            if len(session['sources'])>1:retained.append({'kind':'session','item_count':1,'reason':'Mixed legacy session retained intact for its unselected sources/results.'})
            continue
        if allrefs.intersection(retained_refs):
            # Source sharing does not automatically make every model variant
            # shared. Remove only runs explicitly owned by selected views/jobs.
            selected_runs={_run_ref(records[i]) for i in selected_ids}
            removed_runs=[]
            for run in (base/'transcript').iterdir() if (base/'transcript').is_dir() else []:
                if not run.is_dir() or not re.fullmatch(r'run-[A-Za-z0-9_-]+',run.name):continue
                if (session_id,run.name) in retained_runs:continue
                manifest=read_doc(run/'manifest.json') if (run/'manifest.json').is_file() else None
                execution=(manifest or {}).get('execution',{})
                owners={item.get('job_id') for item in execution.get('producer_jobs',[]) if isinstance(item,dict) and item.get('job_id')}
                if execution.get('job_id'):owners.add(execution['job_id'])
                retained_owners={owner for owner in owners if owner not in job_ids and value['jobs'].get(owner,{}).get('state')!='deleted'}
                if retained_owners:
                    retained.append({'kind':'transcript_variant','item_count':1,'reason':'An unselected task also produced this shared transcription attempt.'});continue
                if (session_id,run.name) not in selected_runs and not owners.intersection(job_ids):continue
                if not manifest or manifest.get('session_id')!=session_id:
                    unresolved.append({'kind':'transcript_variant','reason':'unbound_partial_run_requires_review','session_id':session_id});continue
                for path in _run_owned_files(run):add(path,'transcript_variant')
                removed_runs.append(run.name)
            if removed_runs:
                session_updates.append({'session_id':session_id,'path':str(base.relative_to(data)),'removed_runs':sorted(removed_runs)})
                fingerprints['session:'+session_id]=_digest(session)
            continue
        transcript=base/'transcript'
        unbound=[]
        for run in transcript.iterdir() if transcript.is_dir() else []:
            if run.is_dir() and re.fullmatch(r'run-[A-Za-z0-9_-]+',run.name):
                manifest=read_doc(run/'manifest.json') if (run/'manifest.json').is_file() else None
                if not manifest or manifest.get('session_id')!=session_id:unbound.append(run)
        if unbound:
            retained_hashes.update(source['sha256'] for source in session['sources'])
            unresolved.append({'kind':'transcript_variant','reason':'unbound_partial_run_requires_review','session_id':session_id})
            warnings.append('An unbound partial run requires investigation. Its session and dependent caches will remain in cleanup-needed state.')
            continue
        fingerprints['session:'+session_id]=_digest(session)
        add(base/'session.yaml','session_metadata')
        for source in session['sources']:
            if source.get('ownership','managed')=='managed':
                add(base/source['path'],'managed_audio')
        for recipe in (base/'derived').iterdir() if (base/'derived').is_dir() else []:
            if not recipe.is_dir() or not re.fullmatch(r'recipe-[0-9a-f]+',recipe.name):continue
            for child in recipe.iterdir():
                if child.name in {'audio.wav','transform.json'} or re.fullmatch(r'interrupted-[0-9a-f]+\.wav',child.name):
                    add(child,'derived_audio')
        transcript=base/'transcript'
        for run in transcript.iterdir() if transcript.is_dir() else []:
            if run.name=='current.json':add(run,'recovery_state');continue
            if not run.is_dir() or not re.fullmatch(r'run-[A-Za-z0-9_-]+',run.name):continue
            manifest=read_doc(run/'manifest.json') if (run/'manifest.json').is_file() else None
            if not manifest or manifest.get('session_id')!=session_id:
                warnings.append('An unbound partial run is retained for separate review.');continue
            for path in _run_owned_files(run):add(path,'transcript_variant')
        # The session-local lock is removable only while global/import ownership
        # excludes all legacy writers. Global/shared lock files are never removed.
        add(base/'.session.lock','session_lock')
        planned={str(roots[name]/relative) for name,relative in targets}
        unknown=[p for p in base.rglob('*') if p.is_file() and str(p) not in planned]
        if unknown:
            retained.append({'kind':'unclassified','item_count':len(unknown),'reason':'Unrecognized files are retained; app ownership was not established.'})
    cache=roots.get('cache')
    if cache and (cache/'decoded-media').is_dir():
        for directory in (cache/'decoded-media').iterdir():
            if not directory.is_dir():continue
            _safe_relative(cache,directory/'decode.json')
            receipt=directory/'decode.json' if (directory/'decode.json').is_file() else directory/'invocation.json'
            if not receipt.is_file():continue
            record=read_doc(receipt);sha=(record.get('identity') or {}).get('source_sha256')
            if sha not in selected_hashes:continue
            key=hashlib.sha256(json.dumps(record.get('identity'),sort_keys=True).encode()).hexdigest()
            if directory.name!=key and not re.fullmatch(r'\.'+key+r'-[0-9a-f]{32}\.pending',directory.name):
                unresolved.append({'kind':'decoded_media','reason':'cache_identity_requires_review'});continue
            if sha in retained_hashes:
                retained.append({'kind':'decoded_media','item_count':1,'reason':'Decoded audio is shared by a retained recording.'});continue
            for path in directory.iterdir():
                if path.name in _DECODE_FILES and path.is_file():add(path,'decoded_media','cache')
    if cache and (cache/'compute').is_dir():
        for directory in (cache/'compute').iterdir():
            receipt=directory/'request.json'
            if not directory.is_dir() or not receipt.is_file():continue
            _safe_relative(cache,receipt)
            record=read_doc(receipt)
            source=record.get('source')
            owned_paths={str(roots[name]/relative) for (name,relative),item in targets.items() if item['kind'] in {'managed_audio','derived_audio'}}
            selected_path_keys={j.get('path_key') for job_id,j in value['jobs'].items() if job_id in job_ids}
            source_key=hashlib.sha256(source.encode()).hexdigest() if isinstance(source,str) else None
            if (record.get('job_id') in job_ids or source in owned_paths
                    or (source_key is not None and source_key in selected_path_keys)):
                for name in ('request.json','result.json','guardian.json','worker.log'):
                    add(directory/name,'compute_recovery','cache')
    # Bind native logs by explicit job IDs, not basenames or matching hashes.
    logs=roots.get('log');batches=set()
    if logs and (logs/'app').is_dir():
        for directory in (logs/'app').iterdir():
            request_path=directory/'request.json'
            if not directory.is_dir() or not request_path.is_file():continue
            _safe_relative(logs,request_path)
            request_doc=read_doc(request_path);items=request_doc.get('items',[])
            request_ids={str(i.get('job_id')) for i in items if isinstance(i,dict)}
            if request_ids and request_ids.issubset(job_ids):
                for name in ('request.json','backend.log'):add(directory/name,'job_log','log')
                if request_doc.get('batch_id'):batches.add(request_doc['batch_id'])
            elif request_ids.intersection(job_ids):
                retained.append({'kind':'job_log','item_count':1,'reason':'Batch log also belongs to unselected tasks.'})
        for batch in batches:
            validate_id(batch,'batch ID')
            for suffix in ('.json','-resources.json'):add(logs/'performance'/(batch+suffix),'performance','log')
    ordered=sorted(targets.values(),key=lambda x:(x['root'],x['path']))
    inodes={}
    for item in ordered:inodes.setdefault((item['stat']['device'],item['stat']['inode']),item['stat'])
    totals={'item_count':len(ordered),'logical_bytes':sum(s['size'] for s in inodes.values()),'allocated_bytes':sum(s['allocated'] for s in inodes.values())}
    facts={'selection':selection,'scopes':sorted(scopes),'source_refs':[list(ref) for ref in sorted(refs)],'result_ids':sorted(selected_ids),'targets':ordered,'unresolved':unresolved,'session_updates':session_updates,
           'ignored_source_versions':sorted(ignored_versions),
           'job_ids':sorted(job_ids),
           'report_ids':[records[i]['manifest']['batch_id'] for i in selected_ids if records[i]['kind']=='report'],
           'fingerprints':fingerprints,'epochs':{key:value['epochs'].get(key,0) for key in scopes}}
    # Equivalent selection of an existing recording's active job does not
    # change its plan; native may add these returned stop targets explicitly.
    token=_digest({k:v for k,v in facts.items() if k!='selection'})
    return {'schema_version':1,'plan_id':'delete-'+token[:24],'plan_token':token,'selection':selection,
            'selected':selected,'remove':totals,'retained_shared':retained,'active_jobs':active,
            'stop_required':bool(active),'warnings':warnings,
            'protected':['external_originals','user_exports','models','code','unrelated_preferences'],
            '_facts':facts,'_directories':[list(x) for x in sorted(owned_dirs,key=lambda x:len(Path(x[1]).parts),reverse=True)]}


def public_plan(value):
    return {k:v for k,v in value.items() if not k.startswith('_')}


def legacy_barrier(settings):
    """Fail closed if any installed older app/backend can still publish writes."""
    code=str(Path(__file__).resolve().parents[1])
    try:
        result=subprocess.run(['/bin/ps','-axo','pid=,args='],capture_output=True,text=True,timeout=3,check=True)
    except (OSError,subprocess.SubprocessError):return 'process_check_unavailable'
    for line in result.stdout.splitlines():
        fields=line.strip().split(None,1)
        if len(fields)!=2 or not fields[0].isdigit():continue
        pid,command=int(fields[0]),fields[1]
        if pid==os.getpid():continue
        # Full installed app paths do not contain transcript content. Output is
        # never logged, retained, or returned to the caller.
        if '.app/Contents/MacOS/AudioTranscribe' in command:
            try:
                import plistlib
                app=Path(command.split('/Contents/MacOS/',1)[0])/'Contents/Info.plist'
                info=plistlib.loads(app.read_bytes())
                if info.get('AudioTranscribeCodeRoot')!=code:return 'legacy_writer'
            except (OSError,ValueError):return 'process_check_unavailable'
        if (' -m audio_transcribe ' in command or '/audio_transcribe/process_guardian.py' in command) and code not in command:
            # Python -m uses cwd, so its command need not contain the runtime.
            try:
                found=subprocess.run(['/usr/sbin/lsof','-a','-p',str(pid),'-d','cwd','-Fn'],capture_output=True,text=True,timeout=2,check=True)
                cwd=next((x[1:] for x in found.stdout.splitlines() if x.startswith('n')),None)
            except (OSError,subprocess.SubprocessError):return 'process_check_unavailable'
            if cwd!=code:return 'legacy_writer'
    return None


def _try_lock(path, *, create_parent=True):
    if create_parent:path.parent.mkdir(parents=True,exist_ok=True)
    elif not path.parent.is_dir():raise DeletionBusy('storage_busy')
    if path.is_symlink():raise ValueError('Lock path is a symbolic link.')
    handle=path.open('a+b')
    try:fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BaseException:handle.close();raise
    return handle


@contextlib.contextmanager
def _purge_locks(settings):
    roots=_roots(settings);handles=[]
    try:
        handles.append(_try_lock(roots['app']/'inference.lock'))
        reason=legacy_barrier(settings)
        if reason:raise DeletionBusy(reason)
        with lifecycle.lock(settings,blocking=False):
            handles.append(_try_lock(roots['data']/'.import.lock'))
            yield roots
    except BlockingIOError:raise DeletionBusy('active_execution') from None
    except RuntimeError as error:
        if isinstance(error.__cause__,BlockingIOError):raise DeletionBusy('storage_busy') from None
        raise
    finally:
        # Preserve inherited legacy ownership semantics; never delete or unlock
        # the shared global lock inode. Closing our exclusive handle releases it.
        for handle in reversed(handles):handle.close()


class DeletionBusy(Exception):
    pass


@contextlib.contextmanager
def _target_locks(roots, targets, session_updates=()):
    paths=set()
    for name,leaf in (('results','.publish.lock'),('results','.labels.lock'),('library','.library.lock')):
        if (roots['data']/name).is_dir():paths.add(roots['data']/name/leaf)
    for target in targets:
        relative=Path(target['path'])
        if target['kind']=='session_metadata' and (roots['data']/relative).is_file():paths.add(roots['data']/relative.parent/'.session.lock')
        elif target['root']=='cache' and relative.parts[0]=='decoded-media':
            # Finished cache identity is hexadecimal; pending names carry its
            # exact hash before the UUID, and use the same cache lock.
            name=relative.parts[1].lstrip('.').split('-',1)[0]
            if re.fullmatch('[0-9a-f]{64}',name):paths.add(roots['cache']/'decoded-media'/(name+'.lock'))
    for update in session_updates:paths.add(roots['data']/update['path']/'.session.lock')
    handles=[]
    try:
        for path in sorted(paths):
            _safe_relative(roots['cache'] if path.is_relative_to(roots.get('cache',Path('/nonexistent'))) else roots['data'],path)
            handles.append(_try_lock(path,create_parent=False))
        yield
    except BlockingIOError:raise DeletionBusy('storage_busy') from None
    finally:
        for handle in reversed(handles):handle.close()


def _journal(settings,operation):
    operation=str(uuid.UUID(str(operation)))
    path=lifecycle.root(settings)/'deletions'/(operation+'.json')
    if path.parent.is_symlink() or path.is_symlink():raise ValueError('Deletion journal cannot be a symbolic link.')
    return path


def _unlink(root,item):
    relative=_safe_relative(root,root/item['path']);fds=[]
    try:
        fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);fds.append(fd)
        for part in relative.parts[:-1]:
            fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);fds.append(fd)
        try:now=os.stat(relative.name,dir_fd=fd,follow_symlinks=False)
        except FileNotFoundError:return False
        expected=item['stat']
        if (not stat.S_ISREG(now.st_mode) or (now.st_dev,now.st_ino,now.st_size,now.st_mtime_ns)
                != (expected['device'],expected['inode'],expected['size'],expected['mtime_ns'])):
            raise ValueError('Target changed after confirmation; preserved for a new plan.')
        os.unlink(relative.name,dir_fd=fd)
        os.fsync(fd)
        try:os.stat(relative.name,dir_fd=fd,follow_symlinks=False)
        except FileNotFoundError:return True
        raise OSError('Target still exists after unlink.')
    except FileNotFoundError:
        return False
    finally:
        for fd in reversed(fds):os.close(fd)


def _prune(root,relative):
    relative=_safe_relative(root,root/relative);fds=[]
    try:
        fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);fds.append(fd)
        for part in relative.parts[:-1]:
            fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);fds.append(fd)
        current=os.stat(relative.name,dir_fd=fd,follow_symlinks=False)
        if stat.S_ISDIR(current.st_mode):os.rmdir(relative.name,dir_fd=fd);os.fsync(fd)
    except OSError:pass
    finally:
        for fd in reversed(fds):os.close(fd)


def _perform(settings,journal,path,roots):
    removed=journal.setdefault('removed',[]);remaining=list(journal.get('unresolved',[]))
    for index,item in enumerate(journal['targets']):
        if index in removed:continue
        try:
            _unlink(roots[item['root']],item)
            removed.append(index)
            write_json(path,journal,overwrite=True)
        except (OSError,ValueError) as error:
            remaining.append({'kind':item['kind'],'reason':'permission_denied' if isinstance(error,PermissionError) else 'target_changed_or_unavailable','target_index':index})
    for name,relative in journal.get('directories',[]):_prune(roots[name],relative)
    removed_inodes={}
    for i in removed:
        signature=journal['targets'][i]['stat'];removed_inodes.setdefault((signature['device'],signature['inode']),signature)
    if not remaining:
        try:
            for update in journal.get('session_updates',[]):
                base=roots['data']/update['path'];_safe_relative(roots['data'],base/'session.yaml')
                session=read_doc(base/'session.yaml')
                if session.get('id')!=update['session_id']:raise ValueError('Session moved or changed identity.')
                removed_runs=set(update['removed_runs'])
                session['runs']=[run for run in session.get('runs',[]) if (run.get('run_id') if isinstance(run,dict) else run) not in removed_runs]
                from .engine import verify_completed_run
                retained=[read_doc(p) for p in (base/'transcript').glob('*/manifest.json') if p.parent.name not in removed_runs and verify_completed_run(p.parent)]
                session['processing_status']='completed' if retained else 'imported'
                write_yaml(base/'session.yaml',session,overwrite=True)
                pointer=base/'transcript'/'current.json'
                if pointer.exists():
                    _safe_relative(roots['data'],pointer);current=read_doc(pointer)
                    for key in ('accepted_run_id','latest_successful_run_id'):
                        if current.get(key) in removed_runs:current[key]=None
                    if not current.get('latest_successful_run_id') and retained:
                        current['latest_successful_run_id']=max(retained,key=lambda m:m.get('completed_at',''))['run_id']
                    if not current.get('accepted_run_id'):current['accuracy_acceptance']='pending'
                    write_json(pointer,current,overwrite=True)
            library=roots['data']/'library'
            if library.is_dir():
                annotation=library/'annotations.json'
                if annotation.is_file() and not annotation.is_symlink():
                    record=read_doc(annotation)
                    for report_id in journal.get('report_ids',[]):record.get('reports',{}).pop(report_id,None)
                    write_json(annotation,record,overwrite=True)
                if (library/'index.json').is_file() and not (library/'index.json').is_symlink():
                    from .library import rebuild_index
                    rebuild_index(settings,locked=True)
        except (OSError,ValueError):
            remaining.append({'kind':'index','reason':'metadata_update_failed'})
    answer={'operation_id':journal['operation_id'],'state':'cleanup_needed' if remaining else 'completed',
            'removed_result_ids':journal['result_ids'],'removed_job_ids':journal['job_ids'],
            'removed_logical_bytes':sum(s['size'] for s in removed_inodes.values()),
            'removed_allocated_bytes':sum(s['allocated'] for s in removed_inodes.values()),
            'retained_shared':journal['retained_shared'],'remaining':remaining,'retryable':bool(remaining),
            'reclamation_note':'File sizes are not a promise of equal free-space change; other links, open handles and snapshots may retain blocks.'}
    if remaining:
        journal['state']='cleanup_needed';journal['remaining']=remaining;write_json(path,journal,overwrite=True)
    else:
        # A tiny result-ID tombstone is the durable idempotency receipt; no
        # recoverable transcript, audio or path list survives in a journal.
        value=lifecycle.state(settings);value.setdefault('completed_deletions',{})[journal['operation_id']]=answer
        for job in value['jobs'].values():
            if job.get('state')=='deleted':
                generation=job['generation'];job.clear();job.update(generation=generation,state='deleted')
        lifecycle.save(settings,value);path.unlink()
        descriptor=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
    return answer


def request(settings,value):
    action=value.get('action')
    if action=='delete_plan':return public_plan(plan(settings,value))
    operation=str(uuid.UUID(str(value.get('operation_id'))));path=_journal(settings,operation)
    if action=='delete_status':
        if path.is_file():
            saved=read_doc(path);return {'state':saved['state'],'operation_id':operation,'remaining':saved.get('remaining',[]),'retryable':True}
        return lifecycle.state(settings).get('completed_deletions',{}).get(operation,{'state':'unknown','operation_id':operation})
    if action not in {'delete_commit','delete_retry'}:raise ValueError('Unknown deletion action.')
    if action=='delete_commit' and value.get('confirmed') is not True:raise ValueError('Permanent deletion requires explicit confirmation.')
    try:
        with _purge_locks(settings) as roots:
            prior=lifecycle.state(settings).get('completed_deletions',{}).get(operation)
            if prior:return prior
            is_new=not path.is_file()
            if not is_new:
                journal=read_doc(path)
            else:
                if action=='delete_retry':raise ValueError('Deletion operation was not found.')
                current=plan(settings,value)
                if current['plan_token']!=value.get('plan_token'):
                    return {'state':'plan_changed','operation_id':operation,'plan':public_plan(current),'retryable':False}
                if current['stop_required'] and value.get('stop_selected') is not True:
                    return {'state':'busy','reason':'stop_required','operation_id':operation,'retryable':True}
                facts=current['_facts'];jobs=facts['job_ids']
                journal={'schema_version':1,'operation_id':operation,'state':'purging','targets':facts['targets'],
                         'directories':current['_directories'],'scopes':facts['scopes'],'source_refs':facts['source_refs'],'result_ids':facts['result_ids'],
                         'job_ids':jobs,'report_ids':facts['report_ids'],'retained_shared':current['retained_shared'],'unresolved':facts['unresolved'],'session_updates':facts['session_updates'],
                         'ignored_source_versions':facts['ignored_source_versions'],'removed':[]}
            # Retry after interruption before the initial fence is idempotent.
            pending=[target for i,target in enumerate(journal['targets']) if i not in journal['removed']]
            with _target_locks(roots,pending,journal.get('session_updates',[])):
                if is_new:
                    verified=plan(settings,value)
                    if verified['plan_token']!=value.get('plan_token'):
                        return {'state':'plan_changed','operation_id':operation,'plan':public_plan(verified),'retryable':False}
                    # Journal before fence, fence before the first unlink.
                    write_json(path,journal)
                lifecycle.save(settings,lifecycle.fence(lifecycle.state(settings),journal['scopes'],journal['result_ids'],journal['job_ids'],operation,
                                                        journal.get('ignored_source_versions',())))
                return _perform(settings,journal,path,roots)
    except DeletionBusy as error:
        reason=str(error)
        messages={'legacy_writer':'Close the older AudioTranscribe app or let its active operation finish, then retry.',
                  'process_check_unavailable':'Could not verify older writer processes; no files were removed.',
                  'active_execution':'Waiting for active transcription ownership to be released. Other selected jobs may finish.',
                  'storage_busy':'Managed storage is busy; retry after its current operation finishes.',
                  'stop_required':'Stop the selected tasks before permanent deletion.'}
        return {'state':'busy','reason':reason,'message':messages.get(reason,'Deletion is waiting for storage.'),'operation_id':operation,'retryable':True}
