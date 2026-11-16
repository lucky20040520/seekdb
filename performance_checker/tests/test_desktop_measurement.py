import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.run_desktop_performance import DesktopMeasurement


def snapshot(io='8:64 rbytes=4096 wbytes=8192\n', populated=0, dirty=0, oom=0):
    return '\x1e'.join((io, '32768\n', f'file_dirty {dirty}\nfile_writeback 0\n',
                        f'oom {oom}\noom_kill 0\nmax 0\n', f'populated {populated}\n',
                        'usage_usec 10\n', ''))


class DesktopMeasurementContracts(unittest.TestCase):
    def measure(self, snapshots):
        measurement = DesktopMeasurement.__new__(DesktopMeasurement)
        measurement.name, measurement.path, measurement.data = 'test', '/cgroup', '/data'
        measurement.device = '8:64'
        with patch('tools.run_desktop_performance.wsl', side_effect=['', *snapshots, '4\n8\n']), \
                patch('tools.run_desktop_performance.time.sleep'):
            return measurement.finish()

    def test_sync_then_unpopulated_clean_stable_samples(self):
        result = self.measure([snapshot(dirty=4096)] + [snapshot()] * 3)
        self.assertEqual(12288, result['io_bytes'])
        self.assertEqual(32768, result['memory_bytes'])
        self.assertEqual([], result['raw']['errors'])
        self.assertEqual(0, result['raw']['sync_exit_code'])
        self.assertEqual(3, result['raw']['settled_samples'])
        self.assertEqual(12, result['raw']['logical_data_bytes'])

    def test_missing_io_live_process_and_oom_never_produce_complete_metrics(self):
        for samples in ([snapshot(io='')] * 4, [snapshot(populated=1)], [snapshot(oom=1)] * 4):
            with self.subTest(samples=samples):
                result = self.measure(samples)
                self.assertIsNone(result['io_bytes'])
                self.assertTrue(result['raw']['errors'])

    def test_reject_unexpected_volume_before_any_wsl_command(self):
        with patch('tools.run_desktop_performance.wsl') as command:
            with self.assertRaises(ValueError):
                DesktopMeasurement(SimpleNamespace(io_device='8:64'), '../foreign-volume')
            command.assert_not_called()

    def test_wrong_device_cannot_create_cgroup(self):
        volume = 'perfcheck-data-' + 'a' * 32
        info = [{'Mountpoint': '/var/lib/docker/volumes/' + volume + '/_data'}]
        with patch('tools.run_desktop_performance.checker.docker',
                   return_value=SimpleNamespace(stdout=json.dumps(info).encode())), \
                patch('tools.run_desktop_performance.wsl', return_value='2112\n') as command:
            with self.assertRaises(ValueError):
                DesktopMeasurement(SimpleNamespace(io_device='8:0'), volume)
            self.assertEqual(1, command.call_count)
            self.assertNotIn('mkdir', command.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
