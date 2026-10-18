import unittest
from unittest.mock import Mock

from queenbee.gpu import AdaptiveGPU
from queenbee.agents.host_client import HostUnavailable


class ClientTests(unittest.TestCase):
    def make(self, directives, role="secondary", release=None):
        client = Mock()
        client._call.side_effect = [{"id": "job", "token": "secret"}, *directives]
        release = release or Mock()
        sdk = AdaptiveGPU(role=role, max_batch=8, release=release, client=client, sleep=Mock())
        return sdk, client, release

    def test_wait_unloads_before_acknowledging_and_resumes_small(self):
        sdk, client, release = self.make([
            {"action": "wait", "revision": 1},
            {"action": "run", "revision": 2, "batch": 1, "delay_s": .25},
        ])
        self.assertEqual(sdk.before_batch(), 1)
        release.assert_called_once()
        self.assertEqual(client._call.call_args_list[2].kwargs, {"ack": 1, "released": True})
        sdk.sleep.assert_any_call(.25)

    def test_release_failure_never_acknowledged(self):
        release = Mock(side_effect=RuntimeError("unload failed"))
        sdk, client, _ = self.make([{"action": "wait", "revision": 1}], release=release)
        with self.assertRaisesRegex(RuntimeError, "unload failed"):
            sdk.before_batch()
        self.assertEqual(sdk.ack, -1)
        self.assertEqual(client._call.call_count, 2)

    def test_preempt_saves_then_releases_then_exits(self):
        order = []
        client = Mock()
        client._call.side_effect = lambda method, *a, **k: (
            {"id": "job", "token": "t"} if method == "gpu_register"
            else {"action": "preempt", "revision": 3} if method == "gpu_heartbeat" and "released" in k and not k.get("released")
            else {"ok": True})
        from queenbee.gpu import Preempted, PREEMPTED_EXIT
        sdk = AdaptiveGPU(role="secondary", max_batch=8, client=client, sleep=Mock(),
                          release=lambda: order.append("release"), save=lambda: order.append("save"))
        with self.assertRaises(Preempted) as ctx:
            sdk.before_batch()
        self.assertEqual(ctx.exception.code, PREEMPTED_EXIT)
        self.assertEqual(order, ["save", "release"])
        methods = [c.args[0] for c in client._call.call_args_list]
        self.assertEqual(methods[-1], "gpu_unregister")
        self.assertTrue(client._call.call_args_list[0].kwargs["resumable"])

    def test_save_failure_never_releases_or_exits(self):
        client = Mock()
        client._call.side_effect = [{"id": "job", "token": "t"}, {"action": "preempt", "revision": 1}]
        release = Mock()
        sdk = AdaptiveGPU(role="secondary", client=client, sleep=Mock(), release=release,
                          save=Mock(side_effect=OSError("disk full")))
        with self.assertRaisesRegex(OSError, "disk full"):
            sdk.before_batch()
        release.assert_not_called()
        self.assertEqual(sdk.ack, -1)

    def test_host_loss_releases_secondary_and_fails_closed(self):
        sdk, _, release = self.make([HostUnavailable("offline")])
        with self.assertRaisesRegex(RuntimeError, "secondary memory released"):
            sdk.before_batch()
        release.assert_called_once()

    def test_host_loss_does_not_stop_primary(self):
        sdk, _, release = self.make([HostUnavailable("offline")], role="primary")
        self.assertEqual(sdk.before_batch(), 8)
        release.assert_not_called()

    def test_variable_batches_preserve_all_input_items(self):
        sdk, _, _ = self.make([
            {"action": "run", "revision": n, "batch": size, "delay_s": 0}
            for n, size in enumerate([1, 3, 2])
        ])
        self.assertEqual(list(sdk.batches(range(6))), [[0], [1, 2, 3], [4, 5]])

    def test_secondary_requires_release_callback(self):
        with self.assertRaises(ValueError):
            AdaptiveGPU(role="secondary")

    def test_smaller_batch_trims_allocator_cache(self):
        sdk, _, release = self.make([
            {"action": "run", "revision": 1, "batch": 4, "delay_s": 0},
            {"action": "run", "revision": 2, "batch": 2, "delay_s": .25},
        ])
        sdk.trim = Mock()
        self.assertEqual(sdk.before_batch(), 4)
        self.assertEqual(sdk.before_batch(), 2)
        sdk.trim.assert_called_once()
        release.assert_not_called()

    def test_smaller_batch_unloads_when_no_trim_adapter(self):
        sdk, _, release = self.make([
            {"action": "run", "revision": 1, "batch": 4, "delay_s": 0},
            {"action": "run", "revision": 2, "batch": 2, "delay_s": .25},
        ])
        sdk.before_batch()
        sdk.before_batch()
        release.assert_called_once()


if __name__ == "__main__":
    unittest.main()
