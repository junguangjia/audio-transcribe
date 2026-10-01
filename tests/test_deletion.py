"""All destructive operations use synthetic, isolated temporary directories."""
import copy
import fcntl
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

import test_export as fixtures
from audio_transcribe import deletion,direct,export,lifecycle,storage,audio,compute


class DeletionTests(unittest.TestCase):
    setUpBase=fixtures.ExportTests.setUp
    source=fixtures.ExportTests.source
    decode=fixtures.ExportTests.decode

    def setUp(self):
        self.setUpBase()
        # Deletion fixtures keep numerical processing in-process. The owned
        # compute boundary has its own integration tests and no ASR runs here.
        def local_audio(settings, context, operation, source, *, output=None, policy=None):
            return audio.inspect_audio(source) if operation == 'inspect' else audio.prepare_audio(source,output,policy=policy)
        fixture=patch.object(compute,'run_audio_operation',side_effect=local_audio);fixture.start();self.addCleanup(fixture.stop)
        self.settings['roots'].update(cache=str(self.root/'cache'),log=str(self.root/'logs'))
        # No production process scan or data is used by destructive fixtures.
        check=patch.object(deletion,'legacy_barrier',return_value=None);check.start();self.addCleanup(check.stop)

    def result(self,name='錄音 with spaces.wav',sample=10,**kwargs):
        source=self.source(name,sample)
        value=export.build_independent(self.settings,[source],self.resolved,**kwargs)
        self.assertEqual(value['completed'],1)
        return source,value['results'][0]

    def plan(self,ids=(),jobs=()):
        return direct.direct_request(self.settings,{'action':'delete_plan','result_ids':list(ids),'jobs':list(jobs)})

    def commit(self,plan,**kw):
        return direct.direct_request(self.settings,{'action':'delete_commit','operation_id':str(uuid.uuid4()),'selection':plan['selection'],'plan_token':plan['plan_token'],'confirmed':True,'stop_selected':True,**kw})

    def test_exclusive_recording_all_variants_removed_original_export_model_untouched(self):
        source,first=self.result();before=source.read_bytes()
        second=export.build_independent(self.settings,[source],self.resolved,force=True)['results'][0]
        saved=self.root/'saved.txt';direct.direct_request(self.settings,{'action':'export','result_id':first['result_id'],'format':'txt','path':str(saved)})
        model=self.root/'app'/'model.bin';model.write_bytes(b'shared-model');protected=(saved.read_bytes(),model.read_bytes())
        plan=self.plan([first['result_id']]);self.assertEqual(plan['selected'][0]['variant_count'],2);self.assertGreater(plan['remove']['logical_bytes'],0)
        answer=self.commit(plan);self.assertEqual(answer['state'],'completed');self.assertEqual(set(answer['removed_result_ids']),{first['result_id'],second['result_id']})
        self.assertEqual(source.read_bytes(),before);self.assertEqual((saved.read_bytes(),model.read_bytes()),protected)
        self.assertEqual(direct.direct_request(self.settings,{'action':'list'})['results'],[])
        self.assertFalse(any((self.data/'sessions').glob('*/source/*')))
        self.assertEqual(list((self.data/'lifecycle'/'deletions').glob('*.json')),[])
        self.assertEqual(direct.direct_request(self.settings,{'action':'delete_status','operation_id':answer['operation_id']})['state'],'completed')
        self.assertEqual(direct.direct_request(self.settings,{'action':'delete_retry','operation_id':answer['operation_id']}),answer)

    def test_distinct_same_byte_alias_remains_and_shared_session_survives(self):
        source,first=self.result('first.wav');alias=self.root/'different alias.wav';alias.write_bytes(source.read_bytes())
        second=export.build_independent(self.settings,[alias],self.resolved)['results'][0]
        original=direct.read_result(self.settings,second['result_id']);managed=Path(original['audio_path']);bytes_before=managed.read_bytes()
        plan=self.plan([first['result_id']]);self.assertTrue(plan['retained_shared']);answer=self.commit(plan);self.assertEqual(answer['state'],'completed')
        self.assertTrue(managed.exists());self.assertEqual(managed.read_bytes(),bytes_before)
        self.assertEqual([r['result_id'] for r in direct.direct_request(self.settings,{'action':'list'})['results']],[second['result_id']])
        self.assertTrue(direct.read_result(self.settings,second['result_id'])['copy_text'])
        # A later explicit import receives a fresh epoch and does not resurrect
        # the deleted view or let its completed operation remove the new result.
        fresh=export.build_independent(self.settings,[source],self.resolved)['results'][0]
        self.assertNotEqual(fresh['result_id'],first['result_id'])
        direct.direct_request(self.settings,{'action':'delete_retry','operation_id':answer['operation_id']})
        self.assertTrue(Path(fresh['markdown_path']).exists())

    def test_unselected_legacy_report_preserves_its_dependencies(self):
        source=self.source('legacy.wav',15);old=export.build_report(self.settings,[source],self.resolved)
        result=export.build_independent(self.settings,[source],self.resolved)['results'][0]
        before=Path(old['report']).read_bytes();plan=self.plan([result['result_id']]);self.assertTrue(plan['retained_shared'])
        answer=self.commit(plan);self.assertEqual(answer['state'],'completed');self.assertEqual(Path(old['report']).read_bytes(),before)
        rows=direct.direct_request(self.settings,{'action':'list'})['results'];self.assertEqual(len(rows),1);self.assertTrue(rows[0]['result_id'].startswith('legacy-report-'))
        self.assertTrue(direct.read_result(self.settings,rows[0]['result_id'])['copy_allowed'])

    def test_permission_failure_visible_retry_and_completed_journal_removed(self):
        _,result=self.result();plan=self.plan([result['result_id']]);original=deletion._unlink;failed=[]
        def deny(root,target):
            if target['kind']=='managed_audio' and not failed:failed.append(1);raise PermissionError('fixture denial')
            return original(root,target)
        with patch.object(deletion,'_unlink',side_effect=deny):answer=self.commit(plan)
        self.assertEqual(answer['state'],'cleanup_needed');self.assertTrue(answer['remaining']);self.assertTrue((self.data/'lifecycle'/'deletions'/(answer['operation_id']+'.json')).exists())
        self.assertEqual(direct.direct_request(self.settings,{'action':'list'})['results'],[])
        complete=direct.direct_request(self.settings,{'action':'delete_retry','operation_id':answer['operation_id']});self.assertEqual(complete['state'],'completed')
        self.assertFalse(any((self.data/'sessions').glob('*/source/*')))

    def test_changed_plan_or_missing_confirmation_never_purges(self):
        _,result=self.result();plan=self.plan([result['result_id']]);p=Path(result['markdown_path']);p.write_text(p.read_text()+' user edit')
        self.assertEqual(self.commit(plan)['state'],'plan_changed');self.assertTrue(p.exists())
        with self.assertRaises(ValueError):self.commit(self.plan([result['result_id']]),confirmed=False)

    def test_symlink_escape_and_symlink_root_fail_closed(self):
        _,result=self.result();saved=Path(result['markdown_path']);external=self.root/'outside.md';external.write_text('protected');saved.unlink();saved.symlink_to(external)
        with self.assertRaises(ValueError):self.plan([result['result_id']])
        self.assertEqual(external.read_text(),'protected')
        alias=self.root/'linked-data';alias.symlink_to(self.data,target_is_directory=True);settings=copy.deepcopy(self.settings);settings['roots']['data']=str(alias)
        with self.assertRaises(ValueError):deletion.plan(settings,{'result_ids':[result['result_id']]})

    def test_owned_execution_or_legacy_process_blocks_without_journal(self):
        _,result=self.result();plan=self.plan([result['result_id']]);lock=self.root/'app'/'inference.lock'
        with lock.open('a+b') as f:
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);answer=self.commit(plan)
        self.assertEqual((answer['state'],answer['reason']),('busy','active_execution'))
        for reason in ('legacy_writer','process_check_unavailable'):
            with patch.object(deletion,'legacy_barrier',return_value=reason):self.assertEqual(self.commit(plan)['reason'],reason)
        self.assertTrue(Path(result['markdown_path']).exists());self.assertFalse((self.data/'lifecycle'/'deletions').exists())

    def test_queued_task_fence_rejects_late_capture_and_unknown_status(self):
        job={'job_id':str(uuid.uuid4()),'path':str(self.root/'not imported.wav')};plan=self.plan(jobs=[job]);answer=self.commit(plan)
        self.assertEqual(answer['state'],'completed');self.assertEqual(answer['removed_job_ids'],[job['job_id']]);self.assertEqual(answer['removed_logical_bytes'],0)
        with self.assertRaises(ValueError):lifecycle.capture(self.settings,job['job_id'],job['path'])
        self.assertEqual(deletion.request(self.settings,{'action':'delete_status','operation_id':str(uuid.uuid4())})['state'],'unknown')

    def test_sidecar_labels_and_same_basename_different_bytes_are_scoped(self):
        _,a=self.result('same.wav',10);other=self.root/'other';other.mkdir();path=other/'same.wav';path.write_bytes(self.source('other source.wav',20).read_bytes())
        b=export.build_independent(self.settings,[path],self.resolved)['results'][0]
        direct.direct_request(self.settings,{'action':'labels','result_id':a['result_id'],'user_labels':{'course':'private label'}})
        answer=self.commit(self.plan([a['result_id']]));self.assertEqual(answer['state'],'completed')
        self.assertFalse((self.data/'results'/'labels'/(a['result_id']+'.json')).exists());self.assertTrue(Path(b['markdown_path']).exists())

    def test_unrecognized_user_file_retained_in_exclusive_managed_folder(self):
        _,result=self.result();opened=direct.read_result(self.settings,result['result_id']);session=Path(opened['audio_path']).parents[1]
        saved=session/'my own exported notes.txt';saved.write_text('must survive');plan=self.plan([result['result_id']]);self.assertTrue(any(r['kind']=='unclassified' for r in plan['retained_shared']))
        self.assertEqual(self.commit(plan)['state'],'completed');self.assertEqual(saved.read_text(),'must survive')

    def test_shared_source_keeps_referenced_run_but_removes_exclusive_variant(self):
        source,first=self.result('original.wav');alias=self.root/'alias.wav';alias.write_bytes(source.read_bytes())
        kept=export.build_independent(self.settings,[alias],self.resolved)['results'][0]
        latest=export.build_independent(self.settings,[source],self.resolved,force=True)['results'][0]
        record=storage.read_doc(Path(latest['markdown_path']).with_name('result.json'));internal=record['internal'];session=storage.locate_session(self.data,internal['session_id'])
        removed_run=session/'transcript'/internal['run_id'];kept_run=storage.read_doc(Path(kept['markdown_path']).with_name('result.json'))['internal']['run_id']
        answer=self.commit(self.plan([first['result_id']]));self.assertEqual(answer['state'],'completed');self.assertFalse(removed_run.exists())
        self.assertTrue((session/'transcript'/kept_run/'transcript.json').exists());self.assertTrue(Path(direct.read_result(self.settings,kept['result_id'])['audio_path']).exists())
        self.assertNotIn(internal['run_id'],storage.read_doc(session/'session.yaml')['runs']);self.assertEqual(storage.read_doc(session/'transcript'/'current.json')['latest_successful_run_id'],kept_run)

    def test_selected_result_includes_old_job_logs_and_minimal_deleted_tokens(self):
        _,result=self.result();record=storage.read_doc(Path(result['markdown_path']).with_name('result.json'));internal=record['internal']
        scope=lifecycle.scope_key(internal['session_id'],internal['source_id'],result['filename']);jobs=[identity for identity,j in lifecycle.state(self.settings)['jobs'].items() if j.get('scope')==scope]
        log=self.root/'logs'/'app'/'job-synthetic';log.mkdir(parents=True);batch=str(uuid.uuid4());storage.write_json(log/'request.json',{'batch_id':batch,'items':[{'job_id':jobs[0]}]});(log/'backend.log').write_text('synthetic retained diagnostic')
        answer=self.commit(self.plan([result['result_id']]));self.assertEqual(answer['state'],'completed');self.assertFalse(log.exists())
        for identity in jobs:self.assertEqual(set(lifecycle.state(self.settings)['jobs'][identity]),{'generation','state'})
        with self.assertRaises(ValueError):lifecycle.capture(self.settings,jobs[0],str(self.root/result['filename']))

    def test_interrupted_delete_resumes_exact_journal_without_reexpanding(self):
        _,result=self.result();plan=self.plan([result['result_id']]);operation=str(uuid.uuid4());original=deletion._unlink;calls=[]
        def interrupt(root,target):
            calls.append(1)
            if len(calls)==3:raise KeyboardInterrupt()
            return original(root,target)
        with patch.object(deletion,'_unlink',side_effect=interrupt),self.assertRaises(KeyboardInterrupt):self.commit(plan,operation_id=operation)
        self.assertEqual(deletion.request(self.settings,{'action':'delete_status','operation_id':operation})['state'],'purging')
        answer=deletion.request(self.settings,{'action':'delete_retry','operation_id':operation});self.assertEqual(answer['state'],'completed');self.assertFalse(any((self.data/'sessions').glob('*/source/*')))

    def test_known_finalization_and_cache_copy_outputs_are_removed(self):
        _,result=self.result();record=storage.read_doc(Path(result['markdown_path']).with_name('result.json'));internal=record['internal'];session=storage.locate_session(self.data,internal['session_id']);run=session/'transcript'/internal['run_id']
        a=run/'logs'/('interrupted-finalization-'+uuid.uuid4().hex);a.mkdir();(a/'transcript.txt').write_text('synthetic private content')
        b=run/'logs'/('.cache-copy-'+uuid.uuid4().hex);b.mkdir();(b/'native.json').write_text('{}')
        answer=self.commit(self.plan([result['result_id']]));self.assertEqual(answer['state'],'completed');self.assertFalse(a.exists());self.assertFalse(b.exists())

    def test_resume_becomes_active_and_result_plan_returns_exact_active_job(self):
        source,result=self.result();value=lifecycle.state(self.settings);identity=next(iter(value['jobs']));self.assertEqual(value['jobs'][identity]['state'],'finished')
        token=lifecycle.capture(self.settings,identity,str(source));self.assertEqual(lifecycle.state(self.settings)['jobs'][identity]['state'],'active')
        plan=self.plan([result['result_id']]);self.assertTrue(plan['stop_required']);self.assertIn(identity,[j['job_id'] for j in plan['active_jobs']])
        explicit=self.plan([result['result_id']],jobs=plan['active_jobs']);self.assertEqual(plan['plan_token'],explicit['plan_token'])
        lifecycle.finish(self.settings,token)

    def test_late_label_or_export_after_deletion_cannot_recreate_artifact(self):
        _,result=self.result();answer=self.commit(self.plan([result['result_id']]));self.assertEqual(answer['state'],'completed')
        for request in ({'action':'labels','user_labels':{'course':'late'}},{'action':'export','format':'md','path':result['markdown_path']}):
            with self.assertRaises(FileNotFoundError):direct.direct_request(self.settings,{**request,'result_id':result['result_id']})
        self.assertFalse(Path(result['markdown_path']).exists());self.assertFalse((self.data/'results'/'labels'/(result['result_id']+'.json')).exists())

    def test_unbound_partial_run_keeps_session_and_reports_cleanup_needed(self):
        _,result=self.result();internal=storage.read_doc(Path(result['markdown_path']).with_name('result.json'))['internal'];session=storage.locate_session(self.data,internal['session_id']);unknown=session/'transcript'/'run-unbound';unknown.mkdir();(unknown/'transcript.txt').write_text('unbound synthetic content')
        plan=self.plan([result['result_id']]);self.assertTrue(plan['warnings']);answer=self.commit(plan);self.assertEqual(answer['state'],'cleanup_needed');self.assertTrue((session/'session.yaml').exists());self.assertTrue(unknown.exists())

    def test_inflight_labels_hold_fence_until_write_then_can_be_purged(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        _,result=self.result();plan=self.plan([result['result_id']]);ready=threading.Event();release=threading.Event();original=direct.read_result
        def paused(*args,**kwargs):
            opened=original(*args,**kwargs)
            if threading.current_thread().name.startswith('label-test'):
                ready.set();self.assertTrue(release.wait(5))
            return opened
        with ThreadPoolExecutor(1,thread_name_prefix='label-test') as pool,patch.object(direct,'read_result',side_effect=paused):
            future=pool.submit(direct.direct_request,self.settings,{'action':'labels','result_id':result['result_id'],'user_labels':{'course':'fixture'}})
            self.assertTrue(ready.wait(5));self.assertEqual(self.commit(plan)['state'],'busy');release.set();future.result(timeout=5)
        self.assertEqual(self.commit(self.plan([result['result_id']]))['state'],'completed')
        self.assertFalse((self.data/'results'/'labels'/(result['result_id']+'.json')).exists())

    def test_legacy_mixed_session_is_retained_without_scope_expansion(self):
        from audio_transcribe import engine
        paths=[self.source('part one.wav',10),self.source('part two.wav',20)];session,record,_=storage.import_sources(self.data,paths,order_confirmed=True)
        engine.run_session(self.settings,session,self.resolved);before={p:p.read_bytes() for p in session.rglob('*') if p.is_file()}
        rows=direct.direct_request(self.settings,{'action':'list'})['results'];chosen=next(r for r in rows if r['filename']==paths[0].name)
        plan=self.plan([chosen['result_id']]);self.assertTrue(any(r['kind']=='session' for r in plan['retained_shared']));self.assertEqual(self.commit(plan)['state'],'completed')
        for p,content in before.items():self.assertEqual(p.read_bytes(),content)
        remaining=direct.direct_request(self.settings,{'action':'list'})['results'];self.assertEqual([r['filename'] for r in remaining],[paths[1].name])

    def test_decoded_cache_shared_once_then_removed_with_last_consumer(self):
        import hashlib
        source,result=self.result();alias=self.root/'cache alias.wav';alias.write_bytes(source.read_bytes());other=export.build_independent(self.settings,[alias],self.resolved)['results'][0]
        identity={'source_sha256':storage.sha256_file(source),'decoder_sha256':'fixture','version':1};key=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
        cache=self.root/'cache'/'decoded-media'/key;cache.mkdir(parents=True);storage.write_json(cache/'decode.json',{'identity':identity});(cache/'audio.wav').write_bytes(b'fixture cache audio');(cache/'decoder.log').write_text('fixture decoder log')
        first=self.commit(self.plan([result['result_id']]));self.assertEqual(first['state'],'completed');self.assertTrue(cache.exists())
        second=self.commit(self.plan([other['result_id']]));self.assertEqual(second['state'],'completed');self.assertFalse(cache.exists());self.assertTrue(source.exists());self.assertTrue(alias.exists())

    def test_partial_delete_blocks_same_scope_adoption_until_receipt(self):
        source,result=self.result('pending original.wav');alias=self.root/'retained alias.wav';alias.write_bytes(source.read_bytes())
        other=export.build_independent(self.settings,[alias],self.resolved)['results'][0]
        saved=storage.read_doc(Path(result['markdown_path']).with_name('result.json'));internal=saved['internal']
        plan=self.plan([result['result_id']]);original=deletion._unlink
        def deny(root,target):
            if target['path'].endswith('/transcript.md'):raise PermissionError('fixture denial')
            return original(root,target)
        with patch.object(deletion,'_unlink',side_effect=deny):answer=self.commit(plan)
        self.assertEqual(answer['state'],'cleanup_needed')
        token=lifecycle.capture(self.settings,str(uuid.uuid4()),source)
        args=(self.settings,token,internal['session_id'],internal['source_id'],result['filename'])
        with self.assertRaisesRegex(ValueError,'unfinished deletion'):lifecycle.bind_source(*args)
        with self.assertRaisesRegex(ValueError,'unfinished deletion'):
            with lifecycle.publication_guard(*args):pass
        complete=deletion.request(self.settings,{'action':'delete_retry','operation_id':answer['operation_id']})
        self.assertEqual(complete['state'],'completed')
        self.assertEqual(lifecycle.state(self.settings)['jobs'][token['job_id']]['state'],'active')
        bound=lifecycle.bind_source(*args)
        with lifecycle.publication_guard(self.settings,bound,internal['session_id'],internal['source_id'],result['filename']) as epoch:self.assertEqual(epoch,1)
        self.assertTrue(Path(direct.read_result(self.settings,other['result_id'])['audio_path']).exists())

    def test_journal_before_fence_also_blocks_same_scope_publication(self):
        _,result=self.result();internal=storage.read_doc(Path(result['markdown_path']).with_name('result.json'))['internal']
        plan=self.plan([result['result_id']]);operation=str(uuid.uuid4())
        with patch.object(lifecycle,'fence',side_effect=KeyboardInterrupt()),self.assertRaises(KeyboardInterrupt):self.commit(plan,operation_id=operation)
        token=lifecycle.capture(self.settings,str(uuid.uuid4()),self.root/result['filename'])
        with self.assertRaisesRegex(ValueError,'unfinished deletion'):lifecycle.bind_source(self.settings,token,internal['session_id'],internal['source_id'],result['filename'])
        self.assertEqual(deletion.request(self.settings,{'action':'delete_retry','operation_id':operation})['state'],'completed')

    def test_unfinished_source_delete_blocks_renamed_alias_but_not_new_session(self):
        source,result=self.result('original pending.wav');internal=storage.read_doc(Path(result['markdown_path']).with_name('result.json'))['internal']
        operation=str(uuid.uuid4());plan=self.plan([result['result_id']])
        with patch.object(deletion,'_unlink',side_effect=KeyboardInterrupt()),self.assertRaises(KeyboardInterrupt):self.commit(plan,operation_id=operation)
        alias=self.root/'renamed pending.wav';alias.write_bytes(source.read_bytes())
        token=lifecycle.capture(self.settings,str(uuid.uuid4()),alias)
        with self.assertRaisesRegex(ValueError,'unfinished deletion'):lifecycle.bind_source(self.settings,token,internal['session_id'],internal['source_id'],alias.name)
        with self.assertRaisesRegex(ValueError,'unfinished deletion'):
            with lifecycle.publication_guard(self.settings,token,internal['session_id'],internal['source_id'],alias.name):pass
        independent=lifecycle.capture(self.settings,str(uuid.uuid4()),self.root/'independent.wav')
        lifecycle.bind_source(self.settings,independent,'s-independent','src-independent','independent.wav')
        self.assertEqual(deletion.request(self.settings,{'action':'delete_retry','operation_id':operation})['state'],'completed')
        self.assertEqual(lifecycle.state(self.settings)['jobs'][token['job_id']]['state'],'active')

    def test_pending_decoded_cache_cannot_be_adopted_by_new_session(self):
        import hashlib
        from audio_transcribe import media
        source,result=self.result();identity={'source_sha256':storage.sha256_file(source),'decoder_sha256':'fixture','version':media.DECODE_VERSION}
        key=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
        cache=self.root/'cache'/'decoded-media'/key;cache.mkdir(parents=True);storage.write_json(cache/'decode.json',{'identity':identity});(cache/'audio.wav').write_bytes(b'fixture cache')
        operation=str(uuid.uuid4());plan=self.plan([result['result_id']])
        with patch.object(deletion,'_unlink',side_effect=KeyboardInterrupt()),self.assertRaises(KeyboardInterrupt):self.commit(plan,operation_id=operation)
        with patch.object(media,'decoder_identity',return_value={'sha256':'fixture'}),self.assertRaisesRegex(ValueError,'audio cache has unfinished deletion'):
            media.decode_media(self.settings,source)
        lifecycle.assert_decoded_cache_available(self.settings,'0'*64)
        self.assertEqual(deletion.request(self.settings,{'action':'delete_retry','operation_id':operation})['state'],'completed')
        lifecycle.assert_decoded_cache_available(self.settings,key)

if __name__=='__main__':unittest.main()
