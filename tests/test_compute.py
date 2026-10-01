"""Owned numerical worker keeps exact DSP results and removes scratch files."""
from pathlib import Path
import tempfile
import unittest
import wave
import numpy as np

from audio_transcribe.audio import inspect_audio, prepare_audio
from audio_transcribe.compute import run_audio_operation
from audio_transcribe.execution import ExecutionOwner, OperationContext
from audio_transcribe.thermal import ThermalController


class ComputeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = {'roots': {k: str(self.root/k) for k in ('data', 'app', 'cache')}}
        self.source = self.root/'input 空间.wav'
        with wave.open(str(self.source), 'wb') as f:
            f.setparams((2,2,48000,48000,'NONE','not compressed'))
            x = (np.sin(np.arange(96000)*.021)*1000).astype('<i2')
            f.writeframes(x.tobytes())

    def test_child_inspection_and_preparation_equal_original_numerical_code(self):
        baseline = self.root/'baseline.wav'; output = self.root/'owned.wav'
        expected = prepare_audio(self.source, baseline)
        with ExecutionOwner(self.settings) as owner:
            context = OperationContext(owner)
            self.assertEqual(run_audio_operation(self.settings, context, 'inspect', self.source), inspect_audio(self.source))
            self.assertEqual(run_audio_operation(self.settings, context, 'prepare', self.source, output=output), expected)
            self.assertEqual(output.read_bytes(), baseline.read_bytes())
            self.assertFalse(owner._children)
        self.assertEqual(list((self.root/'cache/compute').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
