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
        atexit.register(self.close)

    def _start(self):
        if self.process is not None and self.process.poll() is None:
            return
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
            self._start()
            try:
                self.process.stdin.write(json.dumps({'op':op, **payload}, allow_nan=False)+'\n')
                self.process.stdin.flush()
                reply=self._responses.get(timeout=float(self.config.get('request_timeout',1800)))
            except queue.Empty as exc:
                self.close()
                raise TimeoutError('cuRobo worker timed out and was stopped') from exc
            if not reply.get('ok'):
                error=reply.get('error',{})
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

    def close(self):
        with self._lock:
            process=self.process
            self.process=None
            if process is not None:
                if process.poll() is None:
                    try:
                        process.stdin.close()
                        process.wait(timeout=3)
                    except (OSError, subprocess.TimeoutExpired):
                        process.kill()
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
