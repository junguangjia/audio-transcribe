"""Real FFmpeg format decoding; mocked ASR only for bounded batch regression."""
import copy
import json
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

import numpy as np

from audio_transcribe import media, audio, config, engine, export, storage


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = {"roots": {name: str(self.root / name) for name in ("data", "app", "cache", "code", "log")}, "storage_approved": True}
        self.ffmpeg = media.decoder_identity()["path"]
        self.original = self.root / "测试 lecture 2.wav"
        samples = np.rint(np.sin(np.arange(48000) * 2*np.pi*440/48000) * 4000).astype('<i2')
        with wave.open(str(self.original), 'wb') as f:
            f.setparams((1, 2, 48000, len(samples), 'NONE', 'not compressed'))
            f.writeframes(samples.tobytes())
        self.before = storage.sha256_file(self.original)

    def encode(self, extension, codec):
        out = self.root / ('测试 lecture 10.' + extension)
        subprocess.run([self.ffmpeg, '-v', 'error', '-nostdin', '-i', str(self.original), '-c:a', codec, str(out)], check=True, capture_output=True)
        return out

    def test_real_common_formats_preserve_originals_and_measured_frames(self):
        for ext, codec in [('m4a','aac'),('mp3','libmp3lame'),('flac','flac'),('aac','aac'),('aiff','pcm_s16be'),('aif','pcm_s16be'),('ogg','libvorbis'),('opus','libopus'),('mp4','aac'),('mov','aac')]:
            with self.subTest(extension=ext):
                p = self.encode(ext, codec); before = storage.sha256_file(p)
                info = media.inspect_media(self.settings, p)
                working, meta = media.working_source(self.settings, p)
                actual = audio.inspect_audio(working)
                self.assertEqual(info['sha256'], before)
                self.assertEqual(info['complete_frames'], actual['complete_frames'])
                self.assertEqual(info['duration_seconds'], actual['complete_frames']/actual['sample_rate'])
                self.assertTrue(0.9 < info['duration_seconds'] < 1.2)
                self.assertFalse(meta['resampled']); self.assertFalse(meta['video_decoded'])
                self.assertNotEqual(meta['detected_audio_codec'], 'unavailable')
                self.assertEqual(storage.sha256_file(p), before)
        self.assertEqual(storage.sha256_file(self.original), self.before)

    def test_native_wav_uses_existing_exact_path_without_ffmpeg(self):
        with patch.object(media, 'decoder_identity', side_effect=AssertionError('WAV needs no format conversion')):
            working, provenance = media.working_source(self.settings, self.original)
            self.assertEqual(working, self.original); self.assertIsNone(provenance)
            self.assertEqual(media.inspect_media(self.settings, self.original)['sha256'], self.before)

    def test_cached_decode_and_corrupted_cache(self):
        p = self.encode('flac','flac')
        wav, meta = media.working_source(self.settings, p)
        with patch.object(media.subprocess, 'Popen', side_effect=AssertionError('Do not decode twice')):
            self.assertEqual(media.working_source(self.settings, p), (wav, meta))
        wav.write_bytes(b'fixture corruption')
        with self.assertRaisesRegex(media.MediaError, 'integrity'):
            media.working_source(self.settings, p)

    def test_corrupt_unsupported_and_video_without_audio(self):
        corrupt = self.root/'corrupt.mp3'; corrupt.write_bytes(b'not audio')
        unsupported = self.root/'unsupported.txt'; unsupported.write_text('fixture')
        video = self.root/'silent-video.mp4'
        subprocess.run([self.ffmpeg,'-v','error','-f','lavfi','-i','color=size=16x16:rate=1','-t','1','-an','-c:v','mpeg4',str(video)],capture_output=True,check=True)
        for path in [corrupt, unsupported, video, self.root/'missing.m4a']:
            with self.subTest(path=path.name), self.assertRaises(media.MediaError):
                media.inspect_media(self.settings, path)

    def test_video_audio_track_selected_without_video_decoding(self):
        video = self.root/'audio-video.mp4'
        subprocess.run([self.ffmpeg,'-v','error','-f','lavfi','-i','color=size=16x16:rate=1','-i',str(self.original),'-t','1','-c:v','mpeg4','-c:a','aac',str(video)],capture_output=True,check=True)
        info = media.inspect_media(self.settings, video)
        self.assertGreater(info['complete_frames'],0)
        self.assertFalse(info['media_decode']['video_decoded'])

    def test_cancellation_terminates_owned_decoder_and_keeps_source(self):
        p = self.encode('flac','flac'); before = storage.sha256_file(p)
        real_popen = subprocess.Popen; children = []
        def delayed(*args, **kwargs):
            child = real_popen(['/bin/sleep','30'], start_new_session=True)
            children.append(child); return child
        old = signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        try:
            signal.setitimer(signal.ITIMER_REAL, 0.15)
            with patch.object(media.subprocess,'Popen',side_effect=delayed), self.assertRaises(KeyboardInterrupt):
                media.working_source(self.settings,p)
        finally:
            signal.setitimer(signal.ITIMER_REAL,0); signal.signal(signal.SIGALRM,old)
        self.assertTrue(children); self.assertIsNotNone(children[0].poll())
        self.assertEqual(storage.sha256_file(p),before)

    def test_mixed_batch_duplicate_partial_reuse_and_complete_text(self):
        m4a=self.encode('m4a','aac'); mp3=self.encode('mp3','libmp3lame'); flac=self.encode('flac','flac')
        bad=self.root/'broken.mp3';bad.write_bytes(b'fixture corruption')
        paths=[mp3,self.original,bad,m4a,flac,mp3]
        before={str(p):storage.sha256_file(p) for p in paths}
        runtime={'runtime':{'cli':'/fixture/whisper','sha256':'fixture-runtime','release':'fixture'}}
        model={'name':'large-v3','path':'/fixture/model','sha256':'fixture-model','precision':'f16'}
        calls=[]
        def decode(settings,runtime,model,wav,output,asr,glossary,**kwargs):
            calls.append(str(wav));output.mkdir(parents=True)
            with wave.open(str(wav),'rb') as f: frames,rate=f.getnframes(),f.getframerate()
            native=output/'native.json'
            storage.write_json(native,{'transcription':[{'offsets':{'from':100,'to':500},'text':'Complete <fixture> repeated words words.'}]})
            return {'input_frames':frames,'input_duration_seconds':frames/rate,'elapsed_seconds':.01,'native_sha256':storage.sha256_file(native)}
        patches=[]
        for module in (engine,export):
            for name,value in [('load_runtime',runtime),('model_identity',model)]:
                patches.append(patch.object(module,name,return_value=value))
        for p in patches:p.start();self.addCleanup(p.stop)
        resolved=config.resolve_config(self.settings['roots']['data']);events=[]
        with patch.object(engine,'decode',side_effect=decode):
            first=export.build_report(self.settings,paths,resolved,events=events.append)
        self.assertEqual((first['completed'],first['failed']),(5,1));self.assertEqual(len(calls),4)
        manifest=storage.read_doc(Path(first['report']).with_name('manifest.json'))
        self.assertEqual([s['selected_path'] for s in manifest['ordered_sources']],[str(p) for p in paths])
        self.assertEqual(manifest['ordered_sources'][-1]['duplicate_of'],1)
        self.assertEqual(Path(first['report']).read_text().count('Complete \\<fixture\\> repeated words words.'),5)
        # Shared aliases and independent workers can complete out of order;
        # every original selection must still have one terminal event.
        terminal=[e for e in events if e.get('type')=='file' and e.get('state') in ('completed','failed')]
        self.assertEqual(sorted(e['index'] for e in terminal),list(range(6)))
        drained=[e for e in events if e.get('type')=='drained']
        self.assertEqual(sorted(e['index'] for e in drained),list(range(6)))
        terminal_by_index={e['index']:e for e in terminal}
        for event in drained:
            self.assertEqual(event['state'],terminal_by_index[event['index']]['state'])
            self.assertGreater(event['seq'],terminal_by_index[event['index']]['seq'])
        self.assertEqual([e['seq'] for e in events], list(range(1, len(events) + 1)))
        with patch.object(engine,'decode',side_effect=AssertionError('Cached ASR must not rerun')):
            second=export.build_report(self.settings,paths,resolved)
        self.assertEqual(second['reused_transcripts'],5)
        self.assertEqual(before,{str(p):storage.sha256_file(p) for p in paths})


if __name__ == '__main__':unittest.main()
