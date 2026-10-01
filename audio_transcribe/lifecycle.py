"""Small local deletion fences; no transcript/audio content is retained here.

Deletion lock order: existing global execution owner, lifecycle, import/session/cache.
Session/cache acquisitions under lifecycle must be nonblocking, since cache
reuse can hold those locks while binding its lifecycle scope.
The lifecycle lock never waits for execution ownership. Old backends do not
understand this protocol; deletion separately checks their process barrier.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import uuid
from pathlib import Path

from .storage import _file_lock, read_doc, write_json, validate_id


def root(settings):
    return Path(settings['roots']['data']).expanduser().resolve() / 'lifecycle'


def empty():
    return {'schema_version': 1, 'jobs': {}, 'epochs': {}, 'deleted_scopes': {},
            'deleted_results': {}, 'ignored_source_versions': {}}


def state(settings):
    path = root(settings) / 'state.json'
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError('Lifecycle metadata cannot be a symbolic link.')
    if not path.exists():
        return empty()
    value = read_doc(path)
    if value.get('schema_version') != 1 or any(not isinstance(value.get(k), dict) for k in ('jobs','epochs','deleted_scopes','deleted_results')):
        raise ValueError('Lifecycle state is invalid; managed files were preserved.')
    if not isinstance(value.get('ignored_source_versions', {}), dict):
        raise ValueError('Ignored source versions are invalid; managed files were preserved.')
    value.setdefault('ignored_source_versions', {})
    return value


def save(settings, value):
    write_json(root(settings) / 'state.json', value, overwrite=True)


@contextlib.contextmanager
def lock(settings, *, blocking=True):
    path = root(settings)
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path / '.lock').is_symlink():
        raise ValueError('Lifecycle lock cannot be a symbolic link.')
    with _file_lock(path / '.lock', blocking=blocking):
        yield


def scope_key(session_id, source_id, filename):
    validate_id(session_id, 'session ID'); validate_id(source_id, 'source ID')
    if not isinstance(filename, str) or Path(filename).name != filename or filename in ('','.','..'):
        raise ValueError('Recording name must be one filename.')
    return hashlib.sha256(json.dumps([session_id, source_id, filename], ensure_ascii=False).encode()).hexdigest()


def record_scope(record):
    if record['kind'] == 'new':
        internal = record['saved']['internal']
        return scope_key(internal['session_id'], internal['source_id'], record['saved']['document']['source']['filename'])
    if record['kind'] == 'source':
        return scope_key(record['session_path'].name, record['source']['id'], record['source']['original_basename'])
    return None


def visible(record, identity, value):
    if identity in value['deleted_results']:
        return False
    key = record_scope(record)
    if key not in value['deleted_scopes']:
        return True
    # A later explicitly submitted job can create a new generation. Old raw
    # runs remain shared dependencies but never reappear as legacy list rows.
    return (record['kind'] == 'new' and record['saved']['internal'].get('lifecycle_epoch', 0)
            >= value['epochs'].get(key, 0))


def capture(settings, job_id, path):
    job_id = str(uuid.UUID(str(job_id)))
    path = str(Path(path).expanduser().absolute())
    with lock(settings):
        value = state(settings)
        old = value['jobs'].get(job_id)
        digest = hashlib.sha256(path.encode()).hexdigest()
        if old:
            if old['state'] == 'deleted' or old.get('path_key') != digest:
                raise ValueError('This task was deleted or its input changed; explicitly import it as a new task.')
            old['state']='active';save(settings,value)
            return {'job_id':job_id,'generation':old['generation']}
        value['jobs'][job_id] = {'generation':uuid.uuid4().hex,'path_key':digest,
                                'filename':Path(path).name,'state':'active'}
        save(settings,value)
        return {'job_id':job_id,'generation':value['jobs'][job_id]['generation']}


def _job(value, token):
    if not isinstance(token,dict):
        raise ValueError('Missing task lifecycle identity.')
    job = value['jobs'].get(token.get('job_id'))
    if not job or job.get('generation') != token.get('generation') or job['state'] == 'deleted':
        raise ValueError('This task was deleted; stale completion was rejected.')
    return job


def assert_live(settings, token):
    _job(state(settings),token)


def _unfinished_journals(settings, value):
    # Inspect journals even before the first fence: a process may exit between
    # those writes. A completed receipt is authoritative if journal unlink lags.
    pending = root(settings) / 'deletions'
    if pending.is_symlink():
        raise ValueError('Deletion recovery metadata cannot be a symbolic link.')
    completed = value.get('completed_deletions', {})
    for path in pending.glob('*.json'):
        if path.is_symlink():
            raise ValueError('Deletion recovery metadata cannot be a symbolic link.')
        journal = read_doc(path)
        if journal.get('operation_id') not in completed:
            yield journal


def _assert_scope_available(settings, value, key, session_id, source_id):
    """Unfinished deletion owns exact source dependencies until its receipt."""
    for journal in _unfinished_journals(settings, value):
        if not isinstance(journal.get('scopes'), list) or not isinstance(journal.get('source_refs'), list):
            raise ValueError('Deletion recovery metadata is invalid; import was preserved.')
        if key in journal['scopes'] or [session_id,source_id] in journal['source_refs']:
            raise ValueError('This recording has unfinished deletion cleanup; retry that deletion before importing it again.')


def assert_decoded_cache_available(settings, key):
    """Do not adopt a cache identity already owned by unfinished deletion.

    Call while holding the existing decoded-cache lock. Purge tries that lock
    nonblocking, so its lifecycle lock cannot form a wait cycle with this check.
    """
    with lock(settings):
        for journal in _unfinished_journals(settings, state(settings)):
            targets=journal.get('targets')
            if not isinstance(targets,list):
                raise ValueError('Deletion recovery metadata is invalid; audio cache was preserved.')
            for target in targets:
                parts=Path(target.get('path','')).parts
                if target.get('root')!='cache' or len(parts)<2 or parts[0]!='decoded-media':continue
                name=parts[1]
                if name==key or name.startswith('.'+key+'-'):
                    raise ValueError('This audio cache has unfinished deletion cleanup; retry that deletion before importing it again.')


def bind_source(settings, token, session_id, source_id, filename):
    with lock(settings):
        value = state(settings); job = _job(value,token)
        key = scope_key(session_id,source_id,filename)
        _assert_scope_available(settings,value,key,session_id,source_id)
        if job.get('scope') and job['scope'] != key:
            raise ValueError('Task recording identity changed.')
        job.update(scope=key,session_id=session_id,source_id=source_id,filename=filename)
        # This capture happened before a delete only if its job was explicitly
        # fenced by that delete. New explicit jobs may reuse retained ASR bytes.
        job.setdefault('epoch',value['epochs'].get(key,0))
        save(settings,value)
        return {**token,'scope':key,'epoch':job['epoch']}


def finish(settings, token):
    with lock(settings):
        value=state(settings);job=_job(value,token);job['state']='finished';save(settings,value)


@contextlib.contextmanager
def publication_guard(settings, token, session_id, source_id, filename):
    """Hold the fence through publication; deletion cannot win between checks."""
    with lock(settings):
        value=state(settings);key=scope_key(session_id,source_id,filename)
        _assert_scope_available(settings,value,key,session_id,source_id)
        if token:
            job=_job(value,token)
            if job.get('scope') != key or job.get('epoch',0) != value['epochs'].get(key,0):
                raise ValueError('Recording generation was deleted; stale publication rejected.')
            yield job.get('epoch',0)
        else:
            if key in value['deleted_scopes']:
                raise ValueError('Recording was deleted; explicitly import it as a new task.')
            yield 0


def fence(value, scopes, result_ids, job_ids, operation_id, ignored_source_versions=()):
    for key in scopes:
        if value['deleted_scopes'].get(key) != operation_id:
            value['epochs'][key]=value['epochs'].get(key,0)+1
        value['deleted_scopes'][key]=operation_id
    for identity in result_ids:
        value['deleted_results'][identity]=operation_id
    for identity,job in value['jobs'].items():
        if identity in job_ids:
            job['state']='deleted'
    for identity in job_ids:
        value['jobs'].setdefault(identity, {'generation':uuid.uuid4().hex,'path_key':None,'filename':None,'state':'deleted'})
    for key in ignored_source_versions:
        value.setdefault('ignored_source_versions', {})[key] = operation_id
    return value
