# Local runtime installation and decoder contract

Installation is explicit and networked. Normal audio preparation, inference,
export, profile handling and benchmarks use only local files. This statement concerns the installed application only, not external development
or communication tools.

The application uses whisper.cpp **v1.9.4**, commit
`927cfce34f31707e17f2bff35c349632fb9e2c3a`. Its standard, non-quantized GGML
`large-v3-turbo` is the default model. `large-v3` remains an explicitly
selected alternative. They are multilingual models; the decoder is configured
with `en`, transcription rather than translation.

## Setup and recovery

From CODE_ROOT, create an isolated Python 3.12 environment if one does not exist.
Install the pinned requirements and build requirements there as described in
README.md. No global pip installation, PyTorch or model conversion is needed.
The explicit media-install step provisions the pinned FFmpeg decoder for non-WAV media; app builds do not download dependencies. The private
machine-local manifest records actual dependency versions.

```sh
.venv/bin/python scripts/setup-runtime.py --jobs 4
```

`--app-root PATH` changes APP_ROOT explicitly; the default is
`~/Library/Application Support/AudioTranscribe`. `--build-only` and `--models-only`
resume just those stages. Dependencies and models are outside user sessions.
The installer uses TLS-verified macOS curl, including when a Python installation
has no configured CA bundle. It does not disable TLS verification.

The installer saves `install-checkpoint.json`, `install-logs/build.log`,
`install-logs/whisper-cli-help.txt`, and the schema-versioned `runtime.json` under
APP_ROOT. It resumes `.partial` model files using HTTP byte ranges. Completed
files are rehashed and reused; checksum mismatches stop the affected operation
and preserve the existing file. Build invocations have explicit timeouts. A failed
build can resume from its existing build directory. No inference runs during
installation, and no audio, transcript or profile is read by the installer.

`runtime.json` has `schema_version: 1`, `runtime`, `models`, `hardware`, and
`dependencies`. Paths are machine-local. Do not copy this manifest into portable
session identity records as the only model reference: record the hash and release
alongside any local path. It is separate from the application's approved-roots
settings file.

## Provenance and integrity

Upstream references:

- [Pinned stable release](https://github.com/ggml-org/whisper.cpp/releases/tag/v1.9.4)
- [Pinned model documentation](https://github.com/ggml-org/whisper.cpp/blob/v1.9.4/models/README.md)
- [Upstream documented model repository](https://huggingface.co/ggerganov/whisper.cpp)
- [Pinned decoder source](https://github.com/ggml-org/whisper.cpp/blob/v1.9.4/examples/cli/cli.cpp)

The source archive is addressed by the exact release commit, downloaded through
HTTPS, and its actual SHA-256 is recorded. The release tag was resolved against
the official GitHub API before pinning. Model artifacts are pinned to repository
revision `5359861c739e955e79d9a303bcbc70fb988958b1` and checked against both the
official Hugging Face LFS SHA-256 and the release model documentation's SHA-1.

| Model | Bytes | SHA-256 |
| --- | ---: | --- |
| large-v3 | 3,095,033,483 | `64d182b440b98d5203c4f9bd541544d84c605196c4f7b845dfa11fb23594d1e2` |
| large-v3-turbo | 1,624,555,275 | `1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69` |

The GGML header is also checked for magic and `ftype=1`: the supported standard
F16 representation with mixed F16/F32 tensors, without quantization.

## Installed decoder interface

The captured installed `--help` is authoritative. The following options were
confirmed after compilation:

| Argument | Meaning |
| --- | --- |
| `-m PATH` | Model weights |
| `-f PATH` | Prepared local input WAV |
| `-l en` | English transcription |
| `-t 4 -p 1` | Four threads, one processor |
| `-mc 0` | Clear prior decoded-text context to reduce repetition loops |
| `-bs 5 -tp 0 -tpi 0.2` | Beam size five; temperature starts at zero with the default fallback increment |
| `-nf` | Explicitly disable temperature fallback, if a recipe requests it |
| `-ojf -of PREFIX` | Save full native JSON to PREFIX.json |
| `-otxt -osrt` | Native text and SRT exports, when requested |
| `-ot N -d N` | Input offset/duration in milliseconds |

Translation, diarization and VAD are disabled by default. Decoder failure
thresholds remain at their installed defaults: entropy 2.4, log probability -1.0,
no-speech 0.6. Flash attention is enabled by default in this release. Record the
actual arguments per run; temperature zero does not promise hardware-independent
determinism. `-np` does **not** hide transcript text: it hides diagnostic prints.
Always redirect both stdout and stderr to local run logs.

Full JSON has top-level `model`, `params`, `result`, `systeminfo`, and
`transcription`. Each transcription segment has `text`, `timestamps`, `offsets`,
and `tokens`. `offsets.from` and `offsets.to` are **milliseconds**, while Whisper's
internal timestamps are 10 ms ticks. `timestamps.from/to` are strings in
`HH:MM:SS,mmm` form. Token timestamps can be absent; token `p` is a decoder
heuristic, not a calibrated correctness probability. Token `t_dtw` is not a
segment timestamp. Add each excerpt's original source offset when mapping excerpt
JSON back to a source; add only documented concatenated-audio offsets when
assembling multiple files. Preserve the native output unmodified.

## Hardware and acceleration validation

The installer records the local architecture, memory, OS and compiler versions
in the private runtime manifest. It enables Metal for native arm64 builds.
A compiler flag or successful build is not execution evidence:
`runtime.build_info.metal_execution_verified` starts false. Inference-run logs
must confirm the actual backend separately. Serial mode runs one inference
process; Auto admits up to three when current memory and swap checks allow it.
Each process is released at completion. Use a short pilot to measure elapsed
time and memory pressure before a full recording. Do not upload machine-local manifests
or inference logs when contributing.
