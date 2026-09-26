import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = load('gateway_under_test', ROOT / 'gateway.py')

    def valid(self):
        g = self.g
        return {
            'Id': g.CONTAINER_ID, 'Name': '/agent-sandbox', 'Image': g.IMAGE_ID,
            'Config': {'User': '1020:1020', 'Labels': {'owner': 'wangcj', 'purpose': 'agent-learning'}},
            'State': {'Running': True, 'Paused': False, 'Restarting': False},
            'HostConfig': {
                'Privileged': False, 'ReadonlyRootfs': True, 'NetworkMode': 'none',
                'PidMode': '', 'IpcMode': 'private', 'UTSMode': '',
                'CapAdd': None, 'CapDrop': ['ALL'],
                'SecurityOpt': ['no-new-privileges:true'],
                'Memory': 34359738368, 'MemorySwap': 34359738368,
                'NanoCpus': 4000000000, 'PidsLimit': 512,
                'RestartPolicy': {'Name': 'no'}, 'Init': True,
                'Devices': [], 'DeviceRequests': [], 'PortBindings': {},
                'Tmpfs': g.TMPFS.copy(),
            },
            'Mounts': [{'Type': 'bind', 'Source': g.WORKSPACE, 'Destination': '/workspace', 'RW': True}],
        }

    def test_valid_policy(self):
        self.g.validate_container(self.valid())

    def test_rejects_other_target_or_image(self):
        for key in ['Id', 'Name', 'Image']:
            data = self.valid()
            data[key] = 'other'
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.g.validate_container(data)

    def test_rejects_socket_and_extra_mounts(self):
        data = self.valid()
        data['Mounts'].append({'Type': 'bind', 'Source': '/var/run/docker.sock', 'Destination': '/var/run/docker.sock', 'RW': True})
        with self.assertRaises(ValueError):
            self.g.validate_container(data)

    def test_rejects_relaxed_security(self):
        cases = {'Privileged': True, 'ReadonlyRootfs': False, 'NetworkMode': 'host',
                 'PidMode': 'host', 'IpcMode': 'host', 'CapAdd': ['SYS_ADMIN'],
                 'SecurityOpt': ['seccomp=unconfined'], 'Memory': 0, 'MemorySwap': -1,
                 'NanoCpus': 0, 'PidsLimit': -1, 'RestartPolicy': {'Name': 'always'}}
        for key, value in cases.items():
            data = self.valid()
            data['HostConfig'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.g.validate_container(data)

    def test_rejects_invalid_requests(self):
        cases = [[], {'command': 'id', 'container': 'ok3576-build'},
                 {'command': 'id', 'timeout': True}, {'command': 'id', 'timeout': 121},
                 {'command': 'id', 'cwd': '/workspace/../../root'},
                 {'command': 'x' * 8193}, {'command': 'x\x00y'}]
        for data in cases:
            with self.subTest(data=str(data)[:90]), self.assertRaises(ValueError):
                self.g.validate_request(data)

    def test_shell_text_is_single_container_argument(self):
        command = 'echo "$(id)"; touch /tmp/only-in-container'
        request = self.g.validate_request({'command': command})
        argv = self.g.exec_argv(request)
        self.assertEqual(argv[-1], command)
        self.assertIn(self.g.CONTAINER_ID, argv)
        self.assertNotIn('ok3576-build', argv)
        self.assertNotIn('ok3576-v2-build', argv)

    def test_bounded_execution_captures_nonzero(self):
        result = self.g.run_bounded([sys.executable, '-c', 'print("error");raise SystemExit(7)'], 3, 1000)
        self.assertEqual(result[0], 7)
        self.assertIn('error', result[1])

    def test_bounded_execution_timeout(self):
        with self.assertRaises(self.g.ExecutionFault):
            self.g.run_bounded([sys.executable, '-c', 'import time;time.sleep(5)'], 0.1, 1000)

    def test_bounded_execution_output_limit(self):
        with self.assertRaises(self.g.ExecutionFault):
            self.g.run_bounded([sys.executable, '-c', 'print("x" * 100000)'], 3, 1000)

    def test_stop_targets_pinned_id_and_checks_state(self):
        stopped = self.valid()
        stopped['State']['Running'] = False
        with patch.object(self.g, 'control', return_value='') as ctl, patch.object(self.g, 'inspect_container', return_value=stopped):
            self.g.stop_sandbox()
        self.assertEqual(ctl.call_args.args[0], ['stop', '--time', '2', self.g.CONTAINER_ID])

    def test_failed_stop_is_reported(self):
        with patch.object(self.g, 'control', return_value=''), patch.object(self.g, 'inspect_container', return_value=self.valid()):
            with self.assertRaises(self.g.ExecutionFault):
                self.g.stop_sandbox()

    def test_normal_compile_failure_is_returned_without_stopping(self):
        with tempfile.TemporaryDirectory() as folder:
            active = Path(folder) / 'active'
            with patch.object(self.g, 'inspect_container', return_value=self.valid()), \
                 patch.object(self.g, 'process_ids', return_value={100, 101}), \
                 patch.object(self.g, 'run_bounded', return_value=(1, 'compiler error')), \
                 patch.object(self.g, 'stop_sandbox') as stop:
                result = self.g.execute_checked(self.g.validate_request({'command': 'g++ bad.cpp'}), active)
            self.assertEqual(result['returncode'], 1)
            self.assertFalse(active.exists())
            stop.assert_not_called()

    def test_timeout_stops_only_sandbox_and_preserves_lockout(self):
        with tempfile.TemporaryDirectory() as folder:
            active = Path(folder) / 'active'
            with patch.object(self.g, 'inspect_container', return_value=self.valid()), \
                 patch.object(self.g, 'process_ids', return_value={100, 101}), \
                 patch.object(self.g, 'run_bounded', side_effect=self.g.ExecutionFault('timeout')), \
                 patch.object(self.g, 'stop_sandbox') as stop, \
                 patch.object(self.g.signal, 'signal'):
                with self.assertRaises(self.g.ExecutionFault):
                    self.g.execute_checked(self.g.validate_request({'command': 'sleep 999'}), active)
            stop.assert_called_once_with()
            self.assertTrue(active.exists())

    def test_background_process_triggers_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            active = Path(folder) / 'active'
            with patch.object(self.g, 'inspect_container', return_value=self.valid()), \
                 patch.object(self.g, 'process_ids', side_effect=[{100, 101}, {100, 101, 102}]), \
                 patch.object(self.g, 'run_bounded', return_value=(0, '')), \
                 patch.object(self.g, 'stop_sandbox') as stop, \
                 patch.object(self.g.signal, 'signal'):
                with self.assertRaises(self.g.ExecutionFault):
                    self.g.execute_checked(self.g.validate_request({'command': 'sleep 999 &'}), active)
            stop.assert_called_once_with()
            self.assertTrue(active.exists())

    def test_invalid_container_never_runs_or_stops(self):
        wrong = self.valid()
        wrong['Id'] = 'protected-other-id'
        with patch.object(self.g, 'inspect_container', return_value=wrong), \
             patch.object(self.g, 'run_bounded') as run, patch.object(self.g, 'stop_sandbox') as stop:
            with self.assertRaises(ValueError):
                self.g.execute_checked(self.g.validate_request({'command': 'id'}), Path('unused'))
        run.assert_not_called()
        stop.assert_not_called()


class AdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load('adapter_under_test', ROOT / 'fixed_sandbox.py')

    def test_execute_serializes_without_host_shell(self):
        env = self.m.FixedSandboxEnvironment(timeout=10)
        reply = json.dumps({'output': 'hello', 'returncode': 0, 'exception_info': '', 'fatal': False})
        with patch.object(self.m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, reply, '')) as run:
            result = env.execute({'command': 'echo hello'})
        self.assertEqual(result['output'], 'hello')
        args, kw = run.call_args
        self.assertEqual(args[0], ['/usr/bin/sudo', '-n', '--', '/usr/local/libexec/wangcj-agent-exec'])
        self.assertFalse(kw.get('shell', False))
        self.assertEqual(json.loads(kw['input'])['command'], 'echo hello')

    def test_fatal_error_never_falls_back(self):
        env = self.m.FixedSandboxEnvironment()
        reply = json.dumps({'output': '', 'returncode': -1, 'exception_info': 'stopped', 'fatal': True})
        with patch.object(self.m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 2, reply, '')) as run:
            with self.assertRaises(RuntimeError):
                env.execute({'command': 'id'})
            self.assertEqual(run.call_count, 1)

    def test_template_vars_exclude_host_secrets(self):
        with patch.dict('os.environ', {'DEEPSEEK_API_KEY': 'test-secret'}):
            data = self.m.FixedSandboxEnvironment().get_template_vars()
        self.assertNotIn('DEEPSEEK_API_KEY', data)
        self.assertNotIn('test-secret', str(data))


if __name__ == '__main__':
    unittest.main()
