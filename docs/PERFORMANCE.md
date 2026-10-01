# Execution and measurement

The native app submits a batch to a short-lived Python coordinator. Each admitted ASR job runs in an independent whisper.cpp process. Auto requests up to three ASR workers (bounded by the number of files); Serial requests one. Both use one preparation worker and at most one prepared item ahead by default. The settings allow explicit worker overrides.

Admission considers current memory pressure, available memory, swapping and measured decoder RSS. The first worker can progress while additional slots wait. Requested capacity and currently admitted workers are displayed separately. The application does not throttle or cancel work based on thermal state; macOS hardware and driver protections remain in place. Parallel work does not change the model, beam size, timestamps or quality rules. Throughput depends on hardware and recordings and is not a universal speedup guarantee.

## Ownership and publication

A cross-instance execution lease prevents duplicate ownership. Batch, job and attempt IDs plus monotonically increasing event sequences reject stale UI updates. Restoring the queue never automatically restarts incomplete work. Cancellation is scoped to owned work, and guardians retain ownership while decoder children are still active.

Preparation uses bounded buffers and passes paths/metadata to the decoder. Identical verified sources can share work; cancelling an alias does not cancel surviving consumers. Successful source results publish independently, so a slower neighbor does not hide a completed result. Exports are atomic and preserve source provenance. Deletion waits for owned work and keeps external originals, saved exports, shared dependencies, models and unselected items.

## Measurement limits

Normal progress is transcript-free. Stage timing uses monotonic clocks and reports preparation, ownership/admission wait, ASR, verification and publication separately. Worker stages can overlap and must not be summed into wall time. A decoder reaching 100 percent does not mean the result is verified and published.

Detailed resource sampling is opt-in. Sampled process RSS is not a true peak or total unified-memory footprint, and shared mappings must not be added as independent allocations. Models on disk are not a persistent in-process model cache. Cache-warm timings must be distinguished from fresh inference.

Tests use synthetic/fake decoders and local fixtures; they verify control flow and integrity without downloading models or claiming listening accuracy. Keep private benchmark inputs, transcripts, logs and machine reports outside Git.
