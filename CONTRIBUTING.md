# Contributing

Use Python 3.12 in a project-local `.venv`. On macOS, install the dependencies
from requirements-dev.txt, explicitly provision the pinned media decoder,
then build the native shell and run the checks documented in README.md. No model download is needed
for synthetic tests.

Changes must preserve source audio, explicit recording order, immutable raw
decoder evidence, cache provenance and cancellation. Do not fix repetition by
deleting words or make a quality badge imply verified transcription accuracy.
Use synthetic recordings and text in tests. Never submit personal recordings,
transcripts, local validation reports, account credentials or machine paths.

Explain the behavior being changed, its validation and remaining limitations in
the pull request. UI tests and mocked decoder tests are distinct from live
inference or listening-based accuracy review. Keep these claims separate.
