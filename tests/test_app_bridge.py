"""Native protocol contract: no raw transcript is sent to the UI."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import sys
import types
import uuid
import unittest
from unittest.mock import patch

from audio_transcribe import cli, config, storage


class AppBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = {'roots':{'data':str(self.root)},'storage_approved':True}
        self.request = self.root/'request.json'

    def test_explicit_order_duplicates_and_unicode_round_trip(self):
        paths = ['/fixtures/part10 中文.m4a','/fixtures/part2.wav','/fixtures/part2.wav']
        storage.write_json(self.request,{'files':paths,'retry_failed':True})
        def build(settings, received, resolved, *, events, retry_failed, coordinator):
            self.assertEqual(received, paths);self.assertTrue(retry_failed)
            coordinator.emit({'type':'file','index':0,'total':3,'state':'completed'})
            return {'state':'partial','report':'/fixture/transcript-report.md','selected':3,'completed':2,'failed':1}
        stream=io.StringIO()
        with patch.object(cli,'build_report',side_effect=build), contextlib.redirect_stdout(stream):
            code=cli.app_report_command(self.settings,str(self.request))
        self.assertEqual(code,1)
        events=[json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual([e['type'] for e in events],['file','result'])
        self.assertFalse(any('text' in e for e in events))

    def test_error_and_cancel_are_json_without_private_exception_payload(self):
        storage.write_json(self.request,{'files':['/fixture/a.wav']})
        for error, expected, code in [(ValueError('private payload must not be emitted'),'error',2),(KeyboardInterrupt(),'cancelled',130)]:
            stream=io.StringIO()
            with patch.object(cli,'build_report',side_effect=error),contextlib.redirect_stdout(stream):
                self.assertEqual(cli.app_report_command(self.settings,str(self.request)),code)
            lines = [json.loads(line) for line in stream.getvalue().splitlines()]
            self.assertEqual(lines[-1]['type'],expected)
            self.assertEqual([v['seq'] for v in lines], list(range(1, len(lines) + 1)))
            self.assertNotIn('private payload',stream.getvalue())

    def test_invalid_request_never_imports(self):
        for paths in ([],['relative.wav'],[None],'/fixture/a.wav'):
            storage.write_json(self.request,{'files':paths},overwrite=self.request.exists())
            with patch.object(cli,'build_report') as build,contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.app_report_command(self.settings,str(self.request)),2)
            build.assert_not_called()

    def test_confirmed_groups_are_forwarded_without_changing_file_order(self):
        paths = ['/fixtures/audio_240102_090000.wav', '/fixtures/audio_240103_110000.wav']
        groups = [{'indices':[0], 'title':'Class A', 'date':'2024-01-02', 'confirmed':True},
                  {'indices':[1], 'title':'Class B', 'date':None, 'confirmed':True}]
        storage.write_json(self.request, {'files': paths, 'groups': groups})
        result = {'state':'completed', 'report':'/fixtures/master-report.md', 'selected':2, 'completed':2, 'failed':0}
        with patch.object(cli, 'build_report', return_value=result) as build, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.app_report_command(self.settings, str(self.request)), 0)
        self.assertEqual(build.call_args.args[1], paths)
        self.assertEqual(build.call_args.kwargs['groups'], groups)

    def test_library_bridge_emits_only_safe_error_for_invalid_request(self):
        storage.write_json(self.request, {'action':'read', 'report_id':'../private'})
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.assertEqual(cli.app_library_command(self.settings, str(self.request)), 2)
        self.assertEqual(json.loads(stream.getvalue())['type'], 'error')
        self.assertNotIn('../private', stream.getvalue())

    def test_direct_request_honors_explicit_model_force_and_protocol_controls(self):
        paths = ['/fixtures/a 中文.wav', '/fixtures/b space.wav']
        batch_id = str(uuid.uuid4())
        items = [{'job_id': str(uuid.uuid4()), 'path': path, 'index': i} for i, path in enumerate(paths)]
        storage.write_json(self.request, {'protocol_version': 2, 'batch_id': batch_id, 'files': paths,
            'items': items, 'model': 'large-v3', 'force': True, 'execution': {'mode': 'serial'}})
        seen = []
        def build(settings, received, resolved, **kwargs):
            self.assertEqual(received, paths)
            self.assertEqual(resolved['asr']['model'], 'large-v3')
            self.assertTrue(kwargs['force'])
            self.assertEqual(kwargs['coordinator'].policy['effective_asr_workers'], 1)
            seen.extend(job.job_id for job in kwargs['coordinator'].jobs)
            return {'state': 'completed', 'selected': 2, 'completed': 2, 'failed': 0,
                    'cancelled': 0, 'review_required': 0, 'results': []}
        stream = io.StringIO()
        with patch.object(cli, 'build_independent', side_effect=build), patch.object(cli, 'ControlReader') as reader, \
             contextlib.redirect_stdout(stream):
            self.assertEqual(cli.app_transcribe_command(self.settings, str(self.request)), 0)
        reader.return_value.start.assert_called_once()
        reader.return_value.close.assert_called_once()
        self.assertEqual(seen, [item['job_id'] for item in items])
        events = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual([e['seq'] for e in events], list(range(1, len(events) + 1)))
        self.assertTrue(all(e['batch_id'] == batch_id for e in events))
        self.assertEqual(events[-1]['type'], 'result')
        self.assertNotIn('report_id', events[-1])

    def test_direct_single_file_keeps_one_effective_worker(self):
        storage.write_json(self.request, {'files': ['/fixtures/a.wav']})
        self.settings['execution'] = {'mode': 'pipeline', 'asr_workers': 2}
        result = {'state': 'completed', 'selected': 1, 'completed': 1, 'failed': 0, 'cancelled': 0, 'results': []}
        with patch.object(cli, 'build_independent', return_value=result) as build, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.app_transcribe_command(self.settings, str(self.request)), 0)
        self.assertEqual(build.call_args.args[2]['asr']['model'], config.defaults()['asr']['model'])
        self.assertEqual(build.call_args.kwargs['coordinator'].policy['asr_workers'], 2)
        self.assertEqual(build.call_args.kwargs['coordinator'].policy['effective_asr_workers'], 1)
        self.assertFalse(build.call_args.kwargs['force'])

    def test_watched_version_token_is_validated_and_forwarded(self):
        path = '/fixtures/watched.wav'
        token = 'a' * 64
        item = {'job_id': str(uuid.uuid4()), 'path': path, 'index': 0,
                'input_mode': 'referenced', 'watched_version_key': token}
        result = {'state': 'completed', 'selected': 1, 'completed': 1,
                  'failed': 0, 'cancelled': 0, 'results': []}
        storage.write_json(self.request, {'files': [path], 'items': [item]})
        with patch.object(cli, 'build_independent', return_value=result) as build, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.app_transcribe_command(self.settings, str(self.request)), 0)
        self.assertEqual(build.call_args.kwargs['input_modes'], ['referenced'])
        self.assertEqual(build.call_args.kwargs['expected_version_keys'], [token])
        for invalid in ({**item, 'watched_version_key': 'unverified'},
                        {**item, 'input_mode': 'managed'}):
            storage.write_json(self.request, {'files': [path], 'items': [invalid]}, overwrite=True)
            with patch.object(cli, 'build_independent') as build, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.app_transcribe_command(self.settings, str(self.request)), 2)
            build.assert_not_called()

    def test_direct_measured_auto_preset_and_serial_fallback_without_experimental_flag(self):
        paths = [f'/fixtures/{name}.wav' for name in ('a', 'b', 'c', 'd')]
        result = {'state': 'completed', 'selected': 4, 'completed': 4, 'failed': 0, 'cancelled': 0, 'results': []}
        for mode, count, expected in [('auto', 4, 3), ('auto', 2, 2), ('serial', 4, 1)]:
            with self.subTest(mode=mode, count=count):
                storage.write_json(self.request, {'files': paths[:count], 'execution': {'mode': mode}},
                                   overwrite=self.request.exists())
                with patch.object(cli, 'build_independent', return_value=result) as build, contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.app_transcribe_command(self.settings, str(self.request)), 0)
                policy = build.call_args.kwargs['coordinator'].policy
                self.assertEqual(policy['asr_workers'], 3)
                self.assertEqual(policy['effective_asr_workers'], expected)
                self.assertEqual(policy['effective_prepare_workers'], 1)
                self.assertEqual(policy['effective_prepared_ahead'], 1)
                self.assertFalse(build.call_args.kwargs['experimental_parallel'])

    def test_direct_rejects_grouping_profiles_and_invalid_flags_before_processing(self):
        cases = [{'groups': []}, {'speaker': 'sample'}, {'force': 'yes'}, {'retry_failed': 1},
                 {'model': 'unsupported'}, {'execution': {'asr_workers': 10}}, {'files': ['relative.wav']}]
        for extra in cases:
            with self.subTest(extra=extra):
                storage.write_json(self.request, {'files': ['/fixtures/a.wav'], **extra}, overwrite=self.request.exists())
                with patch.object(cli, 'build_independent') as build, contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.app_transcribe_command(self.settings, str(self.request)), 2)
                build.assert_not_called()

    def test_direct_partial_cancel_has_preserved_results_and_cancel_exit(self):
        storage.write_json(self.request, {'files': ['/fixtures/a.wav', '/fixtures/b.wav']})
        result = {'state': 'partial', 'selected': 2, 'completed': 1, 'failed': 0, 'cancelled': 1,
                  'results': [{'result_id': 'ready', 'filename': 'a.wav'}]}
        stream = io.StringIO()
        with patch.object(cli, 'build_independent', return_value=result), contextlib.redirect_stdout(stream):
            self.assertEqual(cli.app_transcribe_command(self.settings, str(self.request)), 130)
        event = json.loads(stream.getvalue().splitlines()[-1])
        self.assertEqual(event['results'], result['results'])
        self.assertNotIn('report', event)

    def test_explicit_legacy_experimental_parallel_remains_compatible(self):
        storage.write_json(self.request, {'files': ['/fixtures/a.wav', '/fixtures/b.wav'],
                                         'experimental_parallel': True})
        result = {'state': 'completed', 'selected': 2, 'completed': 2, 'failed': 0, 'cancelled': 0, 'results': []}
        with patch.object(cli, 'build_independent', return_value=result) as build, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.app_transcribe_command(self.settings, str(self.request)), 0)
        self.assertTrue(build.call_args.kwargs['experimental_parallel'])
        self.assertEqual(build.call_args.kwargs['coordinator'].policy['effective_asr_workers'], 2)

    def test_direct_processing_errors_remain_safe_and_reader_closes(self):
        storage.write_json(self.request, {'protocol_version': 2, 'files': ['/fixtures/a.wav']})
        for error, code in ((RuntimeError('private raw transcript or path'), 2), (KeyboardInterrupt(), 130)):
            with self.subTest(code=code):
                stream = io.StringIO()
                with patch.object(cli, 'build_independent', side_effect=error), patch.object(cli, 'ControlReader') as reader, \
                     contextlib.redirect_stdout(stream):
                    self.assertEqual(cli.app_transcribe_command(self.settings, str(self.request)), code)
                reader.return_value.close.assert_called_once()
                self.assertNotIn('private raw transcript', stream.getvalue())
                self.assertIn(json.loads(stream.getvalue().splitlines()[-1])['type'], ('error', 'cancelled'))

    def test_direct_result_channel_forwards_only_requested_action(self):
        storage.write_json(self.request, {'action': 'read', 'result_id': 'result-fixture'})
        def request(settings, value):
            self.assertEqual(value, {'action': 'read', 'result_id': 'result-fixture'})
            return {'result_id': 'result-fixture', 'readable_text': 'Synthetic explicit result text.'}
        stream = io.StringIO()
        with patch.dict(sys.modules, {'audio_transcribe.direct': types.SimpleNamespace(direct_request=request)}), \
             contextlib.redirect_stdout(stream):
            self.assertEqual(cli.app_results_command(self.settings, str(self.request)), 0)
        self.assertEqual(json.loads(stream.getvalue())['readable_text'], 'Synthetic explicit result text.')

    def test_direct_result_channel_does_not_echo_internal_error(self):
        storage.write_json(self.request, {'action': 'read', 'result_id': '../private'})
        def request(*args):
            raise ValueError('private transcript payload')
        stream = io.StringIO()
        with patch.dict(sys.modules, {'audio_transcribe.direct': types.SimpleNamespace(direct_request=request)}), \
             contextlib.redirect_stdout(stream):
            self.assertEqual(cli.app_results_command(self.settings, str(self.request)), 2)
        self.assertEqual(json.loads(stream.getvalue())['type'], 'error')
        self.assertNotIn('private', stream.getvalue())
