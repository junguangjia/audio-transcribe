"""Synthetic direct-result provenance/export tests. No live inference or GUI."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

import yaml

import test_export as fixtures
from audio_transcribe import direct, direct_metadata, storage, audio, compute
from audio_transcribe.product import APP_VERSION, APP_BUILD


class DirectTests(unittest.TestCase):
    def setUp(self):
        fixtures.ExportTests.setUp(self)
        # This suite tests result metadata, not the separately tested guardian.
        def local_audio(settings, context, operation, source, *, output=None, policy=None):
            return audio.inspect_audio(source) if operation == 'inspect' else audio.prepare_audio(source, output, policy=policy)
        fixture=patch.object(compute,'run_audio_operation',side_effect=local_audio)
        fixture.start();self.addCleanup(fixture.stop)
    source = fixtures.ExportTests.source
    decode = fixtures.ExportTests.decode
    report = fixtures.ExportTests.report

    def fixture(self, name='音声 lecture one.wav', sample=25):
        source = self.source(name, sample)
        report = self.report([source])
        manifest = storage.read_doc(Path(report['report']).with_name('manifest.json'))
        entry = manifest['ordered_sources'][0]
        session = storage.locate_session(self.data, entry['transcript']['session_id'])
        run = session/'transcript'/entry['transcript']['run_id']
        document = storage.read_doc(run/'transcript.json')
        info = storage.read_doc(run/'diagnostics.json')['sources'][0]['transform']['source']
        return source, entry, session, run, document['segments'], info

    def publish(self, item):
        _, entry, _, _, segments, info = item
        return direct.publish_result(self.settings, entry, segments, self.resolved, self.runtime, self.model, info)

    def test_four_exports_provenance_unicode_unknown_time_and_safe_clipboard(self):
        item=self.fixture(); source=item[0]; before=source.read_bytes(); result=self.publish(item)
        opened=direct.direct_request(self.settings,{'action':'read','result_id':result['result_id']})
        self.assertEqual(set(opened['available_formats']), {'md','txt','srt','json'})
        self.assertEqual(opened['provenance']['recording']['started_at'],None)
        self.assertEqual(opened['provenance']['recording']['ended_at'],None)
        self.assertNotIn('source_sha256',opened['readable_text'])
        self.assertNotIn('session_id',opened['plain_text'])
        self.assertIn(self.raw[0],opened['plain_text'])
        for format in ('md','txt','srt','json'):
            with self.subTest(format=format):
                out=self.root/'Export space 空间'/f'音声_transcript.{format}'
                direct.direct_request(self.settings,{'action':'export','result_id':result['result_id'],'format':format,'path':str(out)})
                text=out.read_text()
                if format=='md':
                    header=yaml.safe_load(text.split('---\n')[1]);self.assertEqual(header['schema'],'audiotranscribe/v1');self.assertEqual(header['source_filename'],source.name);self.assertEqual(header['recording_started_at'],None);self.assertEqual(header['timezone'],None);self.assertEqual(header['transcription']['app_version'],APP_VERSION)
                elif format=='txt':
                    self.assertIn('Recorded: unknown',text);self.assertIn('Duration:',text);self.assertNotIn(item[1]['sha256'],text)
                elif format=='srt':
                    self.assertTrue(text.startswith('1\n00:00:00,100 --> 00:00:00,400\n'));self.assertNotIn('schema:',text)
                else:
                    exported=json.loads(text);self.assertEqual(exported['segments'],item[4]);self.assertEqual(exported['source']['sha256'],storage.sha256_file(source));self.assertNotIn('internal',exported)
        self.assertEqual(source.read_bytes(),before)

    def test_cache_reuse_stable_identity_and_retains_factual_import_time(self):
        item=self.fixture();item[1]['imported_at']='2024-01-02T09:00:00+00:00';first=self.publish(item)
        item[1].pop('imported_at');second=self.publish(item)
        self.assertEqual(first['result_id'],second['result_id'])
        opened=direct.read_result(self.settings,first['result_id']);self.assertEqual(opened['provenance']['source']['imported_at'],'2024-01-02T09:00:00+00:00')
        self.assertEqual(len(list((self.data/'results').glob('r-*'))),1)

    def test_filename_only_remains_unknown_and_legacy_inference_is_separate(self):
        item=self.fixture('audio_240102_090000_32bit_orig.wav')
        result=self.publish(item);record=direct.read_result(self.settings,result['result_id'])['provenance']['recording']
        self.assertIsNone(record['started_at']);self.assertIsNone(record['timezone']);self.assertEqual(record['legacy_filename_inference']['status'],'unverified_naming_convention');self.assertFalse(record['legacy_filename_inference']['used_as_recording_time'])

    def test_explicit_time_plus_duration_preserves_timezone_and_unknown_import(self):
        item=self.fixture();session=storage.read_doc(item[2]/'session.yaml');session['recorded_at']='2024-02-29T23:59:59.250-04:00';storage.write_yaml(item[2]/'session.yaml',session,overwrite=True)
        opened=direct.read_result(self.settings,self.publish(item)['result_id']);clock=opened['provenance']['recording']
        self.assertEqual(clock['started_at'],'2024-02-29T23:59:59.250000-04:00');self.assertEqual(clock['ended_at'],'2024-03-01T00:00:00.250000-04:00');self.assertEqual(clock['time_confidence'],'user_supplied');self.assertEqual(clock['timezone_status'],'explicit_offset');self.assertIsNone(opened['provenance']['source']['imported_at'])

    def test_labels_post_transcription_update_future_exports_only_user_values(self):
        item=self.fixture();result=self.publish(item);saved=Path(result['markdown_path']).with_name('result.json');original=saved.read_bytes()
        labels={'course':' 概率 学 ','speaker':'','event':'Topic A'}
        opened=direct.direct_request(self.settings,{'action':'labels','result_id':result['result_id'],'user_labels':labels})
        self.assertEqual(opened['user_labels'],{'course':'概率 学','speaker':None,'event':'Topic A'});self.assertEqual(saved.read_bytes(),original)
        self.assertIn('概率 学',opened['markdown']);self.assertNotIn('概率 学',Path(result['markdown_path']).read_text())
        with self.assertRaises(ValueError): direct.direct_request(self.settings,{'action':'labels','result_id':result['result_id'],'user_labels':{'nationality':'anything'}})

    def test_exact_duplicate_hash_only_and_array_lookup_is_scoped(self):
        item=self.fixture();result=self.publish(item);alias=self.root/'different-name.wav';alias.write_bytes(item[0].read_bytes());different=self.source('same-looking-name.wav',35)
        matches=direct.direct_request(self.settings,{'action':'lookup','paths':[str(alias),str(different),str(self.root/'missing.wav')]})['matches']
        self.assertEqual(matches[0]['existing'][0]['result_id'],result['result_id']);self.assertEqual(matches[1]['existing'],[]);self.assertEqual(matches[2]['existing'],[]);self.assertIn('error',matches[2]);self.assertEqual([m['index'] for m in matches],[0,1,2])

    def test_export_never_overwrites_under_two_competing_writers(self):
        result=self.publish(self.fixture());out=self.root/'space folder'/'export.md';request={'action':'export','result_id':result['result_id'],'format':'md','path':str(out)}
        def attempt(_):
            try: direct.direct_request(self.settings,request);return 'created'
            except FileExistsError:return 'exists'
        with ThreadPoolExecutor(max_workers=2) as pool: self.assertEqual(sorted(pool.map(attempt,range(2))),['created','exists'])
        before=out.read_bytes()
        with self.assertRaises(FileExistsError):direct.direct_request(self.settings,request)
        self.assertEqual(out.read_bytes(),before)

    def test_invalid_timestamps_raw_retained_but_no_srt(self):
        item=self.fixture();item[4][0]['start_seconds']=-.01;item[1]['state']='review_required';item[1]['transcript']['quality'].update(status='review_required',timestamp_valid=False)
        with self.assertRaises(ValueError): self.publish(item)
        item=self.fixture('valid-again.wav');opened=direct.read_result(self.settings,self.publish(item)['result_id']);document={**opened['provenance'],'segments':copy.deepcopy(opened['segments'])};document['segments'][0]['start_seconds']=-.01;document['transcription']['quality']['timestamp_valid']=False
        self.assertIn('[-00:00:00.010]',direct.render_export(document,'md'));self.assertEqual(document['segments'][0]['start_seconds'],-.01)
        with self.assertRaises(ValueError): direct.render_export(document,'srt')

    def test_old_source_and_whole_report_remain_readable_without_migration(self):
        item=self.fixture();before={p:p.read_bytes() for p in self.data.rglob('*') if p.is_file()}
        rows=direct.direct_request(self.settings,{'action':'list'})['results'];self.assertTrue(any(r['result_id'].startswith('legacy-source-') for r in rows));self.assertTrue(any(r['result_id'].startswith('legacy-report-') for r in rows))
        for row in rows:
            opened=direct.read_result(self.settings,row['result_id']);self.assertIn('Same repeated text',opened['readable_text'])
        self.assertFalse((self.data/'results').exists())
        for p,content in before.items(): self.assertEqual(p.read_bytes(),content)

    def test_compressed_source_does_not_claim_decoded_float_as_original_format(self):
        item=self.fixture();item[5].update(format='IEEE_FLOAT32',media_decode={'detected_audio_codec':'aac','detected_container':'mov','resampled':False,'gain_applied':False})
        doc=direct.read_result(self.settings,self.publish(item)['result_id'])['provenance'];self.assertEqual(doc['audio']['codec'],'aac');self.assertIsNone(doc['audio']['sample_format']);self.assertEqual(doc['audio']['decoded_sample_format'],'float32')

    def test_tampered_result_metadata_does_not_offer_existing_result(self):
        result=self.publish(self.fixture());p=Path(result['markdown_path']).with_name('result.json');d=storage.read_doc(p);d['document']['source']['filename']='forged.wav';storage.write_json(p,d,overwrite=True)
        with self.assertRaises(FileNotFoundError): direct.read_result(self.settings,result['result_id'])

    def test_bwf_embedded_time_strict_calendar_and_no_timezone_guess(self):
        path=self.source('bwf.wav',12);base=path.read_bytes();payload=bytearray(602);payload[320:330]=b'2024-02-29';payload[330:338]=b'23:59:59';chunk=b'bext'+struct.pack('<I',len(payload))+payload;raw=base[:12]+chunk+base[12:];raw=raw[:4]+struct.pack('<I',len(raw)-8)+raw[8:];path.write_bytes(raw)
        metadata=direct_metadata.recording_metadata(path,1.25);self.assertEqual(metadata['started_at'],'2024-02-29T23:59:59');self.assertEqual(metadata['ended_at'],'2024-03-01T00:00:00.250000');self.assertIsNone(metadata['timezone']);self.assertEqual(metadata['time_source'],'embedded_bwf_origination')
        path.write_bytes(raw.replace(b'2024-02-29',b'2023-02-29'));self.assertIsNone(direct_metadata.recording_metadata(path,1.25)['started_at'])
        malformed=raw+b'xx';malformed=malformed[:4]+struct.pack('<I',len(malformed)-8)+malformed[8:];path.write_bytes(malformed);self.assertIsNone(direct_metadata.recording_metadata(path,1.25)['started_at'])


    def test_source_change_disables_playback_and_duplicate_offer_but_keeps_text(self):
        item=self.fixture();result=self.publish(item);managed=item[2]/'source'/item[0].name
        content=managed.read_bytes();managed.write_bytes(content[:-2]+b'xx')
        opened=direct.read_result(self.settings,result['result_id']);self.assertIsNone(opened['audio_path']);self.assertEqual(opened['source_integrity'],'missing_or_changed');self.assertIn('Same repeated text',opened['plain_text'])
        self.assertEqual(direct.direct_request(self.settings,{'action':'lookup','path':str(item[0])})['existing'],[])

    def test_archived_managed_source_is_resolved_without_changing_stored_result(self):
        item=self.fixture();result=self.publish(item);saved=Path(result['markdown_path']).with_name('result.json');before=saved.read_bytes()
        destination=self.data/'archive'/'2024'/item[2].name;destination.parent.mkdir(parents=True);item[2].rename(destination)
        opened=direct.read_result(self.settings,result['result_id']);self.assertEqual(opened['source_integrity'],'verified');self.assertTrue(opened['audio_path'].startswith(str(destination)));self.assertEqual(saved.read_bytes(),before)

    def test_whitespace_cue_no_srt_and_labels_do_not_replace_edited_markdown(self):
        item=self.fixture();result=self.publish(item);path=Path(result['markdown_path']);path.write_text('User edit must survive.');direct.direct_request(self.settings,{'action':'labels','result_id':result['result_id'],'user_labels':{'course':'Human label'}});self.assertEqual(path.read_text(),'User edit must survive.')
        opened=direct.read_result(self.settings,result['result_id']);document={**opened['provenance'],'segments':copy.deepcopy(opened['segments'])};document['segments'][0]['text']='  \n '
        with self.assertRaises(ValueError): direct.render_export(document,'srt')

    def test_legacy_copy_is_literal_text_without_diagnostics_and_opaque_edits_disable_copy(self):
        self.fixture();row=next(r for r in direct.direct_request(self.settings,{'action':'list'})['results'] if r['result_id'].startswith('legacy-report-'))
        opened=direct.read_result(self.settings,row['result_id']);self.assertTrue(opened['copy_allowed']);self.assertNotIn('Selected files:',opened['copy_text']);self.assertNotIn('source_sha256',opened['copy_text']);self.assertIn(self.raw[0],opened['copy_text'])
        Path(row['markdown_path']).write_text('User-owned edited old report.');opened=direct.read_result(self.settings,row['result_id']);self.assertFalse(opened['copy_allowed']);self.assertEqual(opened['readable_text'],'User-owned edited old report.');self.assertEqual(opened['copy_text'],'')

    def test_fresh_shared_inference_and_historical_cache_generation_versions(self):
        item=self.fixture();item[1]['transcript'].update(reused_transcript=True,generated_in_current_invocation=True)
        fresh=direct.read_result(self.settings,self.publish(item)['result_id'])['provenance']['transcription']
        self.assertEqual((fresh['app_version'],fresh['app_build']),(APP_VERSION,APP_BUILD))
        old=self.fixture('historical.wav',35);old[1]['transcript'].update(reused_transcript=True,generated_in_current_invocation=False)
        adopted=direct.read_result(self.settings,self.publish(old)['result_id'])['provenance']['transcription']
        self.assertEqual(adopted['app_version'],storage.read_doc(old[3]/'manifest.json')['application_version']);self.assertIsNone(adopted['app_build']);self.assertEqual(adopted['export_application'],{'version':APP_VERSION,'build':APP_BUILD})

    def test_later_renamed_cached_alias_retains_verified_generation_app(self):
        from audio_transcribe import export
        original=self.source('original.wav',10)
        first=export.build_independent(self.settings,[original],self.resolved)['results'][0]
        saved=Path(first['markdown_path']).with_name('result.json');before=saved.read_bytes()
        alias=self.root/'later renamed 空间.wav';alias.write_bytes(original.read_bytes())
        second=export.build_independent(self.settings,[alias],self.resolved)['results'][0]
        self.assertEqual(self.calls,1);self.assertNotEqual(first['result_id'],second['result_id'])
        generation=direct.read_result(self.settings,second['result_id'])['provenance']['transcription']
        self.assertEqual((generation['app_version'],generation['app_build']),(APP_VERSION,APP_BUILD))
        self.assertEqual(saved.read_bytes(),before)

    def test_invalid_direct_record_does_not_supply_cached_generation_version(self):
        item=self.fixture();first=self.publish(item);path=Path(first['markdown_path']).with_name('result.json')
        invalid=storage.read_doc(path);invalid['document']['transcription']['app_version']='invalid';storage.write_json(path,invalid,overwrite=True)
        item[1].pop('lifecycle_token',None);item[1]['filename']='later alias.wav';item[1]['transcript'].update(reused_transcript=True,generated_in_current_invocation=False)
        generation=direct.read_result(self.settings,self.publish(item)['result_id'])['provenance']['transcription']
        self.assertEqual(generation['app_version'],storage.read_doc(item[3]/'manifest.json')['application_version']);self.assertIsNone(generation['app_build'])

    def test_new_forced_run_has_independent_result_identity(self):
        from audio_transcribe import engine
        item=self.fixture();first=self.publish(item);fresh=engine.run_session(self.settings,item[2],self.resolved,force=True);run=Path(fresh['path']);entry=copy.deepcopy(item[1]);entry['transcript'].update(run_id=run.name,run_manifest_sha256=storage.sha256_file(run/'manifest.json'),transcript_json_sha256=storage.sha256_file(run/'transcript.json'));segments=storage.read_doc(run/'transcript.json')['segments'];second=direct.publish_result(self.settings,entry,segments,self.resolved,self.runtime,self.model,item[5]);self.assertNotEqual(first['result_id'],second['result_id']);self.assertTrue(Path(first['markdown_path']).exists())

if __name__=='__main__': unittest.main()
