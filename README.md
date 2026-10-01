# AudioTranscribe 2.5.1

The native three-pane app watches the selected local recording folder while it runs, then starts ready new files only when **开始全部新录音** is clicked. Dragged/chosen recordings remain managed-copy imports. Each file becomes an independent, readable result; the app does not infer course membership or combine recordings. The default model is the standard-precision `large-v3-turbo`. `large-v3` remains an explicit alternative.

The default **Auto** mode uses a measured, bounded number of independent whisper.cpp processes for a multi-file batch. Admission checks current memory and swapping, and the app displays the requested/admitted count. **Serial** always requests one. The preparation lane is bounded; completed files publish without waiting for slower neighbors. Both modes retain cross-instance ownership, cancellation, result verification, exact-source cache reuse, and crash recovery. The application does not gate or cancel jobs based on macOS thermal state. macOS hardware and driver protections remain in place.

Open a result to play and seek its audio, read its complete timestamped transcript, or use Copy All. Export supports Markdown, TXT, SRT, and JSON and refuses to overwrite an existing file. Markdown and JSON carry `audiotranscribe/v1` provenance. Optional Course, Speaker, and Event / Topic labels may be added after transcription. Unknown recording clocks and timezones remain unknown.

Watched-folder originals stay in place as external references; the app stores derived audio and results without a second full-size original copy. The saved text remains available if the source moves, while playback and retranscription require full-byte source verification or an explicit Locate File action. Deleting a watched result never deletes its external original; its current source version remains ignored until explicitly re-added or changed.

Selected items can be permanently deleted from the list or detail view after one confirmation. The deletion coordinator waits for owned work and preserves external originals, saved exports, shared dependencies, models, and unselected items. Old incomplete results require an explicit retry; an upgrade never restarts them automatically.

## Setup on macOS

Requires macOS 13 or later, Xcode Command Line Tools, and Python 3.12. In a checkout, create an isolated environment and explicitly install the pinned dependencies:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt -r requirements-build.txt
.venv/bin/python -m pip install --only-binary=:all: --no-deps --target native/vendor imageio-ffmpeg==0.6.0
.venv/bin/python scripts/setup-runtime.py --jobs 4
```

Dependency and model installation downloads from their upstream sources. Transcription stays local. Models live outside the checkout; see [RUNTIME.md](RUNTIME.md) for pinned versions and recovery commands.

Choose a permanent local data directory before importing recordings. Do not assume Documents or Desktop is outside iCloud sync. For a deliberately chosen local directory:

```sh
.venv/bin/python -m audio_transcribe configure --data-root "$HOME/AudioTranscription" --storage-mode local_alternative
.venv/bin/python scripts/build-app.py --install
./Transcribe.command
```

Existing settings and installed apps are preserved: neither command silently overwrites them. To try another build alongside an installed app, choose a different `--name` and `--bundle-id`, and open the reported app path. Keep the checkout, its `.venv`, and `native/vendor` available while an app refers to them. This is a source-linked local application, not a self-contained redistributable bundle.

The default icon is generated waveform artwork. An optional `--icon /path/to/image.png` is local customization; personal images, recordings, transcripts, settings, models and runtime manifests are excluded from this repository.

## Development and validation

```sh
.venv/bin/python -m pytest -q
sh scripts/test-native.sh
bash scripts/test-layout.sh
.venv/bin/python scripts/build-app.py
```

The build produces `native/build/AudioTranscribe.app`, verifies its ad-hoc signature, and writes an ignored `runtime-manifest.json` recording source/runtime linkage. Building does not install or replace an app. `--settings-path` and `--install-dir` support isolated validation. Layout tests instantiate production AppKit views with synthetic data and private preferences, without starting transcription or monitoring production folders; watcher restoration uses an empty test-owned temporary folder.

The engine's cache compatibility version is separate from the application version in `audio_transcribe/product.py`. Scheduling settings do not change ASR cache identity. Automated checks and repetition flags do not establish word accuracy; review uncertain passages against the original audio. Legacy combined-report CLI commands remain available but are not the normal GUI workflow.
