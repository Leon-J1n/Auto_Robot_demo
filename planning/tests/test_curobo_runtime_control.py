"""Exercise real worker pipes, deadlines and fatal-context replacement without CUDA."""
from pathlib import Path
import sys
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from libraries.curobo.client import CuroboClient
from libraries.curobo.runtime_control import fatal_cuda_error, request_budgets


FAKE_WORKER = '''import json,os,pathlib,sys,time
config=json.loads(sys.argv[1])
pathlib.Path(config['cache_dir'],'worker_pid').write_text(str(os.getpid()),encoding='utf-8')
for line in sys.stdin:
    request=json.loads(line)
    def send(**values):
        print('@@CUROBO@@'+json.dumps(dict(request_id=request['request_id'],**values)),flush=True)
    send(event='stage',stage='conversion')
    behavior=config.get('test_behavior','ok')
    if behavior=='exit_now':
        sys.exit(3)
    elif behavior=='init_stall':
        send(event='stage',stage='robot_model')
        time.sleep(10)
    elif behavior=='solve_stall':
        send(event='stage',stage='planning')
        time.sleep(10)
    elif behavior=='heartbeat_stall':
        for _ in range(100):
            send(event='stage',stage='conversion')
            time.sleep(.03)
    elif behavior in ('fatal_once','ordinary_once'):
        marker=pathlib.Path(config['cache_dir'])/'failed_once'
        if not marker.exists():
            marker.write_text('failed',encoding='utf-8')
            send(ok=False,error={'error_code':'CUROBO_CUDA_FATAL' if behavior=='fatal_once' else 'CUROBO_FAILED',
                'error_message':'injected failure','worker_restart_required':behavior=='fatal_once'})
            continue
    send(ok=True,result={'pid':os.getpid()})
'''


class RuntimeControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root/'worker.py').write_text(FAKE_WORKER, encoding='utf-8')
        self.package = patch('libraries.curobo.client.PACKAGE', self.root)
        self.package.start()
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close(force=True)
        self.package.stop()
        self.temp.cleanup()

    def client(self, **config):
        client = CuroboClient({'cache_dir':str(self.root/'cache'), 'python':sys.executable,
                              'request_timeout':3., 'initialization_timeout':2., **config})
        self.clients.append(client)
        return client

    def test_progress_is_not_returned_as_a_successful_reply(self):
        client = self.client()
        result = client.doctor()
        self.assertIsInstance(result['pid'], int)
        self.assertEqual(client.last_stage, 'conversion')
        self.assertEqual(client.doctor()['pid'], result['pid'])

    def test_initialization_timeout_kills_worker_and_next_call_gets_fresh_process(self):
        client = self.client(test_behavior='init_stall', initialization_timeout=.2)
        started = time.monotonic()
        with self.assertRaises(TimeoutError) as error:
            client.doctor()
        self.assertLess(time.monotonic()-started, 2.)
        self.assertEqual(error.exception.curobo_error['failed_stage'], 'robot_model')
        self.assertIsNone(client.process)
        if sys.platform == 'win32':
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            pid = int((self.root/'cache'/'worker_pid').read_text(encoding='utf-8'))
            handle = kernel.OpenProcess(0x00100000, False, pid)
            if handle:
                try:
                    self.assertNotEqual(kernel.WaitForSingleObject(handle, 0), 258,
                                        'The interpreter child outlived the timed-out venv launcher')
                finally:
                    kernel.CloseHandle(handle)
        client.config['test_behavior'] = 'ok'
        self.assertIn('pid', client.doctor())

    def test_planning_budget_is_enforced_inside_blocking_call(self):
        client = self.client(test_behavior='solve_stall')
        started = time.monotonic()
        with self.assertRaises(TimeoutError) as error:
            client.plan({'planning':{'planning_time':.15}}, {})
        self.assertLess(time.monotonic()-started, 2.)
        self.assertEqual(error.exception.curobo_error['failed_stage'], 'planning')
        self.assertIsNone(client.process)

    def test_progress_cannot_extend_initialization_budget(self):
        client = self.client(test_behavior='heartbeat_stall', initialization_timeout=.2)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            client.doctor()
        self.assertLess(time.monotonic()-started, 2.)

    def test_total_request_deadline_caps_initialization(self):
        self.assertEqual(request_budgets({'request_timeout':120., 'initialization_timeout':60.},
            'plan', {'request':{'constraints':{'timeout':7.}, 'planning':{'planning_time':10.}}}),
            (7., 7., 7.))

    def test_fatal_error_discards_worker_instead_of_serving_next_request(self):
        client = self.client(test_behavior='fatal_once')
        with self.assertRaises(RuntimeError):
            client.doctor()
        self.assertIsNone(client.process)
        self.assertIn('pid', client.doctor())

    def test_ordinary_infeasibility_keeps_healthy_worker(self):
        client = self.client(test_behavior='ordinary_once')
        with self.assertRaises(RuntimeError):
            client.doctor()
        process = client.process
        self.assertIn('pid', client.doctor())
        self.assertIs(client.process, process)

    def test_invalid_budget_does_not_spawn_a_worker(self):
        for value in (0., -1., float('inf'), float('nan'), True):
            with self.subTest(value=value):
                client = self.client(request_timeout=value)
                with self.assertRaises(ValueError):
                    client.doctor()
                self.assertIsNone(client.process)

    def test_fatal_classifier_preserves_ordinary_planning_failures(self):
        for message in ('CUDA error: an illegal memory access was encountered',
                        'Warp CUDA error 700', 'device-side assert triggered'):
            self.assertTrue(fatal_cuda_error(RuntimeError(message)))
        self.assertFalse(fatal_cuda_error(ValueError('Runtime start is infeasible')))

    def test_worker_exit_is_reported_without_waiting_for_timeout(self):
        client = self.client(test_behavior='exit_now')
        with self.assertRaises(RuntimeError) as error:
            client.doctor()
        self.assertEqual(error.exception.curobo_error['error_code'], 'CUROBO_WORKER_EXITED')
        self.assertIsNone(client.process)

    def test_real_worker_fatal_handler_replies_then_exits_without_cuda_cleanup(self):
        code = '''import sys
sys.argv=['worker','{}']
from libraries.curobo import worker
class FailingEngine:
    def __init__(self,progress=None):
        self.progress=progress
        self.stage='initialization'
    def set_stage(self,stage):
        self.stage=stage
        self.progress(stage)
    def convert(self,payload):
        return {}
    def prepare(self,*args,**kwargs):
        self.set_stage('planner_initialization')
        raise RuntimeError('CUDA error: an illegal memory access was encountered')
worker.Engine=FailingEngine
worker.main()
'''
        import json
        result = subprocess.run([sys.executable, '-B', '-c', code],
            input=json.dumps({'op':'plan','request_id':'fatal_test','scene':{}})+'\n',
            capture_output=True, text=True, encoding='utf-8', timeout=5)
        self.assertEqual(result.returncode, 70)
        replies = [json.loads(line[len('@@CUROBO@@'):]) for line in result.stdout.splitlines()
                   if line.startswith('@@CUROBO@@')]
        self.assertEqual(replies[-1]['error']['error_code'], 'CUROBO_CUDA_FATAL')
        self.assertEqual(replies[-1]['error']['failed_stage'], 'planner_initialization')
        self.assertTrue(replies[-1]['error']['worker_restart_required'])


if __name__ == '__main__':
    unittest.main()
