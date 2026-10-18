import unittest
from unittest.mock import Mock, patch

from queenbee.collectors.gpu import NvidiaSmiDevices, _BACKGROUND_FLAGS, select_process_backend


class CollectorTests(unittest.TestCase):
    def test_measurement_runs_without_a_console_and_preserves_device_uuid(self):
        monitor = NvidiaSmiDevices()
        monitor.available = True
        result = Mock(returncode=0, stdout="0, GPU, 10, 1000, 8192, 50, 20, 100, GPU-test\n")
        with patch("queenbee.collectors.gpu.subprocess.run", return_value=result) as run:
            devices = monitor.sample()
        self.assertEqual(run.call_args.kwargs["creationflags"], _BACKGROUND_FLAGS)
        self.assertEqual(devices[0].uuid, "GPU-test")
        self.assertEqual(devices[0].mem_used_mb, 1000)

    def test_windows_prefers_pdh_even_when_nvidia_smi_is_present(self):
        pdh = Mock(attributable=True)
        with patch("queenbee.collectors.gpu.platform.system", return_value="Windows"), \
             patch("queenbee.collectors.gpu.shutil.which", return_value="nvidia-smi"), \
             patch("queenbee.collectors.gpu.WindowsPdhGpu", return_value=pdh), \
             patch("queenbee.collectors.gpu.NvidiaComputeApps") as smi:
            backend, notes = select_process_backend(5.0)

        self.assertIs(backend, pdh)
        self.assertIn("Windows PDH", notes[0])
        smi.assert_not_called()


if __name__ == "__main__":
    unittest.main()
