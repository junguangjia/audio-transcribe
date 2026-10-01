"""Deterministic resource-admission tests; no real system-pressure dependency."""
import unittest

from audio_transcribe.memory_admission import MemoryAdmission


def sample(level="warning", *, free=1, inactive=9, speculative=0, swapouts=10):
    return {"memory_pressure": level, "page_size_bytes": 1_000_000_000,
            "vm_pages": {"pages_free": free, "pages_inactive": inactive,
                         "pages_speculative": speculative, "swapouts": swapouts}}


class AdmissionTests(unittest.TestCase):
    def policy(self, snapshots, *, model_bytes=1_000_000_000):
        current = [0]
        def read():
            result = snapshots[min(current[0], len(snapshots) - 1)]
            current[0] += 1
            return result
        policy = MemoryAdmission(model_bytes=model_bytes, system_reader=read,
                                 process_reader=lambda _: {"available": True, "root_present": True,
                                                            "processes": [{"pid": -1, "rss_bytes": 1_500_000_000}]},
                                 clock=lambda: current[0], sample_interval=0)
        return policy

    def test_warning_with_verified_headroom_and_no_new_swap_admits_two(self):
        policy = self.policy([sample(), sample()])
        self.assertEqual(policy.decide(active=1, requested=2)[1], "awaiting_swap_trend")
        allowed, reason, evidence = policy.decide(active=1, requested=2)
        self.assertTrue(allowed)
        self.assertEqual(reason, "headroom_verified")
        self.assertEqual(evidence["new_swapout_pages"], 0)

    def test_each_new_concurrency_high_requires_fresh_swap_observations(self):
        policy = self.policy([sample(), sample(), sample(), sample(swapouts=11),
                              sample(swapouts=11)])
        self.assertEqual(policy.decide(active=1, requested=9)[1], "awaiting_swap_trend")
        self.assertTrue(policy.decide(active=1, requested=9)[0])
        self.assertEqual(policy.decide(active=2, requested=9)[1], "awaiting_swap_trend")
        self.assertEqual(policy.decide(active=2, requested=9)[1], "active_swapouts")
        self.assertTrue(policy.decide(active=2, requested=9)[0])

    def test_critical_unknown_swap_growth_and_shortage_hold_extra_slot(self):
        cases = [([sample("critical")], "pressure_critical"),
                 ([sample("unknown")], "pressure_unknown"),
                 ([sample(), sample(swapouts=11)], "active_swapouts"),
                 ([sample(free=0, inactive=1), sample(free=0, inactive=1)],
                  "insufficient_reclaimable_estimate")]
        for snapshots, expected in cases:
            with self.subTest(expected=expected):
                policy = self.policy(snapshots)
                policy.decide(active=1, requested=2)
                self.assertEqual(policy.decide(active=1, requested=2)[1], expected)
                self.assertTrue(policy.decide(active=0, requested=2)[0])

    def test_missing_sample_or_model_never_invents_headroom(self):
        policy = self.policy([{}, {}])
        self.assertEqual(policy.decide(active=1, requested=2)[1], "pressure_unknown")
        policy = self.policy([sample(), sample()], model_bytes=None)
        policy.decide(active=1, requested=2)
        self.assertEqual(policy.decide(active=1, requested=2)[1], "model_cost_unknown")

    def test_allocation_failure_suppresses_extra_slot_but_not_first_worker(self):
        policy = self.policy([sample(), sample(), sample()])
        policy.decide(active=1, requested=2)
        self.assertTrue(policy.decide(active=1, requested=2)[0])
        policy.note_allocation_failure()
        self.assertEqual(policy.decide(active=1, requested=2)[1], "prior_allocation_failure")
        self.assertTrue(policy.decide(active=0, requested=2)[0])

    def test_missing_process_observation_is_not_zero_rss(self):
        policy = self.policy([sample(), sample()])
        policy.process_reader = lambda _: {"available": False, "root_present": None, "processes": []}
        self.assertEqual(policy.decide(active=1, requested=2)[1], "process_telemetry_unavailable")
        policy = self.policy([sample(), sample()])
        policy.process_reader = lambda _: {"available": True, "root_present": True, "processes": []}
        policy.decide(active=1, requested=2)
        self.assertEqual(policy.decide(active=1, requested=2)[1], "decoder_rss_unavailable")


if __name__ == "__main__":
    unittest.main()
