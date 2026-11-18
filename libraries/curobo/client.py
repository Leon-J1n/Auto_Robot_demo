"""Persistent optional GPU worker, isolated from the application's Python imports."""
from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid

from .runtime_control import request_budgets


PACKAGE = Path(__file__).resolve().parent
PROJECT = PACKAGE.parents[1]
PREFIX = '@@CUROBO@@'


class CuroboClient:
    def __init__(self, config=None):
        self.config = dict(config or {})
        self.cache = Path(self.config.get('cache_dir', PROJECT / '.cache/curobo')).resolve()
        local = self.cache / 'runtime.json'
        if local.is_file():
            self.config = {**json.loads(local.read_text('utf-8')), **self.config}
        self.config['cache_dir'] = str(self.cache)
        self.process = None
        self._lock = threading.RLock()
        self._responses = queue.Queue()
        self._log = None
        self.last_stage = None
        atexit.register(self.close)

    def _start(self):
        if self.process is not None and self.process.poll() is None:
            return
        if self.process is not None or self._log is not None:
            self.close(force=True)
        self.cache.mkdir(parents=True, exist_ok=True)
        self._responses = queue.Queue()
        self._log = (self.cache / 'worker.log').open('a', encoding='utf-8')
        env = os.environ.copy()
        env.update(PYTHONUTF8='1', PYTHONUNBUFFERED='1', PYTHONDONTWRITEBYTECODE='1')
        if 'cuda_visible_devices' in self.config:
            env['CUDA_VISIBLE_DEVICES']=str(self.config['cuda_visible_devices'])
        # The worker alone sees cuRobo dependencies; the parent process keeps its own imports.
        env.pop('PYTHONPATH', None)
        python = self.config.get('python') or os.environ.get('CUROBO_PYTHON') or sys.executable
        command = [str(python), '-B', str(PACKAGE / 'worker.py'), json.dumps(self.config)]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._log, text=True, encoding='utf-8', bufsize=1, cwd=str(PROJECT), env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        process = self.process
        responses = self._responses
        log = self._log
        def read():
            try:
                for line in process.stdout:
                    if line.startswith(PREFIX):
                        responses.put(json.loads(line[len(PREFIX):]))
                    else:
                        log.write(line)
                        log.flush()
            finally:
                responses.put({'ok':False,'error':{'error_code':'CUROBO_WORKER_EXITED',
                    'error_message':f'cuRobo worker exited; inspect {self.cache / "worker.log"}',
                    'failed_stage':'initialization','recoverable':True}})
        self._reader = threading.Thread(target=read, daemon=True)
        self._reader.start()

    def call(self, op, **payload):
        with self._lock:
            total_budget, initialization_budget, solve_budget = request_budgets(self.config, op, payload)
            started = time.monotonic()
            total_deadline = started + total_budget
            initialization_deadline = started + initialization_budget
            solve_deadline = None
            request_id = uuid.uuid4().hex
            self.last_stage = 'worker_start'
            try:
                # Encode before writing so malformed requests cannot leave a
                # partial JSON line in an otherwise reusable worker.
                message = json.dumps({'op':op, 'request_id':request_id, **payload}, allow_nan=False)+'\n'
                self._start()
                self.process.stdin.write(message)
                self.process.stdin.flush()
                while True:
                    deadline = min(total_deadline, solve_deadline if solve_deadline is not None
                                   else initialization_deadline)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise queue.Empty
                    reply = self._responses.get(timeout=remaining)
                    if time.monotonic() > deadline:
                        raise queue.Empty
                    if reply.get('request_id') not in (None, request_id):
                        continue
                    if reply.get('event') == 'stage':
                        self.last_stage = reply['stage']
                        if self.last_stage == 'planning' and solve_deadline is None:
                            solve_deadline = time.monotonic() + solve_budget
                        continue
                    break
            except queue.Empty as exc:
                stage = self.last_stage
                self.close(force=True)
                error = {'error_code':'CUROBO_TIMEOUT', 'failed_stage':stage,
                    'error_message':f'cuRobo timed out during {stage}; worker was stopped',
                    'recoverable':True, 'worker_restart_required':True,
                    'details':{'elapsed_s':time.monotonic()-started, 'total_budget_s':total_budget,
                               'initialization_budget_s':initialization_budget, 'solve_budget_s':solve_budget}}
                exception = TimeoutError(error['error_message'])
                exception.curobo_error = error
                raise exception from exc
            except BaseException:
                self.close(force=True)
                raise
            if not reply.get('ok'):
                error=reply.get('error',{})
                if error.get('worker_restart_required') or error.get('error_code') == 'CUROBO_WORKER_EXITED':
                    self.close(force=True)
                exception=RuntimeError(error.get('error_message','cuRobo worker failed'))
                exception.curobo_error=error
                raise exception
            return reply['result']

    def prepare(self, scene, *, robot_object_id=None, tool=None, request=None):
        return self.call('prepare',scene=scene,robot_object_id=robot_object_id,tool=tool,request=request)

    def doctor(self):
        return self.call('doctor')

    def plan(self, request, scene, *, robot_object_id=None, tool=None):
        return self.call('plan',request=request,scene=scene,robot_object_id=robot_object_id,tool=tool)

    def inspect_scene(self, scene, *, robot_object_id=None, tool=None):
        return self.call('inspect',scene=scene,robot_object_id=robot_object_id,tool=tool)

    def refresh_assets(self):
        return self.call('refresh')

    @staticmethod
    def _kill_worker(process):
        # Windows venv python.exe can be a launcher with an interpreter child.
        # Killing just Popen.pid leaves that child (and its CUDA context) alive.
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW, check=False)
        if process.poll() is None:
            process.kill()

    def close(self, *, force=False):
        with self._lock:
            process=self.process
            self.process=None
            if process is not None:
                if process.poll() is None:
                    try:
                        if force:
                            self._kill_worker(process)
                        else:
                            process.stdin.close()
                        process.wait(timeout=3 if not force else 5)
                    except (OSError, subprocess.TimeoutExpired):
                        self._kill_worker(process)
                        process.wait(timeout=5)
                for stream in (process.stdin,process.stdout):
                    if stream and not stream.closed:
                        stream.close()
                if hasattr(self,'_reader'):
                    self._reader.join(timeout=2)
            if self._log is not None:
                self._log.close()
                self._log=None

    def __enter__(self):
        return self

    def __exit__(self,*args):
        self.close()
