"""Exact-time fixtures: no multi-hour audio allocation or inference is required."""
import copy
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import wave
from types import SimpleNamespace

from audio_transcribe import engine, export, timeline
from audio_transcribe.evaluation import timestamp
from audio_transcribe.execution import ExecutionOwner, OperationContext
from audio_transcribe.thermal import ThermalController


def native(*pairs):
    return {'transcription':[{'offsets':{'from':a,'to':b},'text':'Unedited fixture.'} for a,b in pairs]}


class TimelineTests(unittest.TestCase):
    def test_exact_eof_and_inside_source(self):
        for b in [999000, 1001109]:
            with self.subTest(end=b):
                audit=timeline.require_timeline(native((900000,b)),frames=48053232,rate=48000)
                self.assertEqual(audit['duration_exact_seconds'],'1001109/1000')
                self.assertFalse(audit['normalizations'])

    def test_real_case_and_every_violation_are_preserved_exactly(self):
        n=native((997860,999860),(999860,1001860)); before=copy.deepcopy(n)
        audit=timeline.audit_timeline(n,frames=48053232,rate=48000)
        self.assertEqual(len(audit['violations']),1)
        self.assertEqual(audit['violations'][0]['reason'],'end_after_eof')
        self.assertEqual(audit['violations'][0]['end_minus_eof_exact_seconds'],'751/1000')
        with self.assertRaises(timeline.TimelineError):
            engine.decoder_segments(n,'src',1001.109,frames=48053232,rate=48000)
        self.assertEqual(n,before)

    def test_start_after_eof_is_not_collapsed_to_zero_length(self):
        audit=timeline.audit_timeline(native((1001,1010)),frames=16000,rate=16000)
        self.assertEqual({x['reason'] for x in audit['violations']},{'start_after_eof','end_after_eof'})

    def test_only_final_enclosing_decoder_tick_can_be_normalized(self):
        n=native((1795820,1800180)); original=copy.deepcopy(n)
        seg=engine.decoder_segments(n,'src',1800.170375,frames=86408176,rate=48000)[0]
        self.assertEqual(seg['end_seconds'],86408176/48000)
        self.assertEqual(seg['native_end_milliseconds'],1800180)
        self.assertEqual(seg['timestamp_normalization']['normalized_end_exact_seconds'],'5400511/3000')
        self.assertEqual(n,original)
        # Not a blanket 10 ms allowance: the first enclosing tick is unique.
        for pairs in [((1795820,1800181),),((1795820,1800200),),((1795820,1800180),(1800180,1800180))]:
            with self.subTest(pairs=pairs),self.assertRaises(timeline.TimelineError):
                timeline.require_timeline(native(*pairs),frames=86408176,rate=48000)
        with self.assertRaises(timeline.TimelineError):
            timeline.require_timeline(native((900,1010)),frames=16000,rate=16000)

    def test_intermediate_overrun_and_nonmonotonic_end_are_reported(self):
        audit=timeline.audit_timeline(native((0,1100),(800,900)),frames=16000,rate=16000)
        self.assertEqual([(v['segment'],v['reason']) for v in audit['violations']],[(1,'end_after_eof'),(2,'nonmonotonic_end')])

    def test_nonmonotonic_start_negative_and_reversed(self):
        for pairs in [((500,600),(400,700)),((-1,500),),((500,400),)]:
            with self.subTest(pairs=pairs),self.assertRaises(timeline.TimelineError):
                timeline.require_timeline(native(*pairs),frames=16000,rate=16000)

    def test_integer_millisecond_units_and_offsets(self):
        seg=engine.decoder_segments(native((1250,2250)),'src',3,source_start=90,global_offset=3600)[0]
        self.assertEqual(seg['start_seconds'],91.25)
        self.assertEqual(seg['global_end_seconds'],3602.25)
        self.assertEqual(seg['native_end_milliseconds'],2250)
        for bad in [True,1.5,float('inf'),'1000']:
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                timeline.require_timeline(native((0,bad)),frames=16000,rate=16000)

    def test_source_frame_basis_overrides_rounded_display_seconds(self):
        self.assertTrue(timeline.require_timeline(native((0,1000)),0.99,frames=44101,rate=44100)['valid'])
        # ceil(44101 * 16000 / 44100) = 16001; do not stretch the
        # source timeline to the resampled last frame or rounded milliseconds.
        self.assertEqual((44101*16000+44100-1)//44100,16001)
        with self.assertRaises(timeline.TimelineError):
            timeline.require_timeline(native((0,1001)),1.001,frames=44101,rate=44100)

    def test_long_timeline_has_no_duration_ceiling_and_formats_hours(self):
        for hours in [3,32,100]:
            duration=hours*3600
            segments=engine.decoder_segments(native(((duration-1)*1000,duration*1000)),'src',duration,
                                             frames=duration*48000,rate=48000)
            self.assertEqual(timestamp(duration),f'{hours:02}:00:00.000')
            with tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)
                engine.write_transcripts(path,segments,[{'source_id':'src'}])
                self.assertIn(f'{hours:02}:00:00,000',(path/'transcript.srt').read_text())
                report=export.report_text([{'position':1,'filename':'long.wav','duration_seconds':duration,'state':'completed'}],[segments],'en')
                self.assertIn(f'{hours:02}:00:00.000',report)

    def test_thousands_of_segments_preserve_every_entry(self):
        segments=engine.decoder_segments(native(*[(i*1000,(i+1)*1000) for i in range(11000)]),'src',11000)
        report=export.report_text([{'position':1,'filename':'long.wav','duration_seconds':11000,'state':'completed'}],[segments],'en')
        self.assertEqual(report.count('Unedited fixture.'),11000)
        self.assertIn('03:03:20.000',report)

    def test_default_runtime_deadline_does_not_stop_after_two_hours(self):
        self.assertIsNone(inspect.signature(engine.decode).parameters['timeout'].default)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); wav=root/'input.wav'; out=root/'output';out.mkdir()
            with wave.open(str(wav),'wb') as w:
                w.setparams((1,2,16000,16000,'NONE','not compressed'));w.writeframes(b'\0\0'*16000)
            class Child:
                returncode=0
                def __init__(self):self.calls=0
                def poll(self):
                    self.calls+=1
                    return None if self.calls==1 else 0
            def popen(*args,**kwargs):
                command = args[0]
                Path(command[command.index('--receipt')+1]).write_text(json.dumps({
                    'nonce': command[command.index('--nonce')+1], 'state': 'reaped', 'returncode': 0}))
                (out/'native.json').write_text(json.dumps(native((0,1000))))
                kwargs['stderr'].write(b'main: processing fixture (16000 samples, 1 sec)\n');kwargs['stderr'].flush()
                return Child()
            settings={'roots':{'app':str(root/'app')}}
            thermal=ThermalController(clock=lambda:100,monitor=False)
            thermal.ingest({'state':'nominal','seq':1,'monotonic':100},source='injected')
            # Replace only this module's clock reference. Patching the shared
            # time module would consume the elapsed fixture in owner admission
            # or thermal freshness checks rather than the decoder deadline.
            decoder_time=SimpleNamespace(monotonic=Mock(side_effect=[0,7201,7202]),sleep=Mock())
            with ExecutionOwner(settings,thermal=thermal) as owner, \
                    patch.object(engine.subprocess,'Popen',side_effect=popen) as process, \
                    patch.object(engine,'memory_snapshot',side_effect=AssertionError('No ordinary profiling')), \
                    patch.object(engine,'time',decoder_time), \
                    patch.object(engine,'backend_evidence',return_value={'metal_observed':True}):
                # No real process is spawned: the elapsed-wall-time fixture
                # establishes that audio duration is not used as a cutoff.
                # pid identifies the mocked guardian only; ordinary profiling is disabled.
                Child.pid=123
                result=engine.decode(settings,{'runtime':{'cli':'/fixture','sha256':'runtime'}},{'path':'/fixture-model','sha256':'model'},wav,out,{'language':'en','threads':4,'beam_size':5,'temperature':0.0,'temperature_increment':0.2},{'terms':[]},context=OperationContext(owner))
                process.assert_called_once()
            self.assertEqual(result['elapsed_seconds'],7202)

    def test_interrupted_cache_copy_never_publishes_partial_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); old=root/"old"; logs=old/"logs/src"; logs.mkdir(parents=True)
            (old/"manifest.json").write_text("{}")
            (logs/"native.json").write_text("{}")
            (logs/"complete.json").write_text(json.dumps({"native_sha256": engine.sha256_file(logs/"native.json")}))
            output=root/"new/logs/src"
            copyfile=engine.shutil.copyfile
            calls=0
            def interrupted(a,b):
                nonlocal calls
                calls+=1
                if calls==2:raise KeyboardInterrupt()
                return copyfile(a,b)
            with patch.object(engine.shutil,"copyfile",side_effect=interrupted):
                with self.assertRaises(KeyboardInterrupt):
                    engine.reuse_verified_decoder_logs([old],"src",output)
            self.assertFalse(output.exists())
            self.assertTrue(engine.reuse_verified_decoder_logs([old],"src",output))
            self.assertTrue((output/"complete.json").is_file())
            self.assertEqual((logs/"native.json").read_bytes(),(output/"native.json").read_bytes())

    def test_tail_metrics_reads_bounded_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'tail.wav'
            with wave.open(str(p),'wb') as w:
                w.setparams((1,2,16000,600000,'NONE','not compressed'));w.writeframes(b'\x10\0'*600000)
            original=wave.Wave_read.readframes
            def bounded(w,n):
                self.assertLessEqual(n,262144)
                return original(w,n)
            with patch.object(wave.Wave_read,'readframes',bounded):
                result=engine.tail_metrics(p,0)
            self.assertEqual(result['frames'],600000)
            self.assertAlmostEqual(result['rms_dbfs'],result['sample_peak_dbfs'])
