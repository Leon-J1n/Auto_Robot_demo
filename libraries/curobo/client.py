"""Persistent optional GPU worker, isolated from the application's Python imports."""
from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading
import time
import uuid

from .runtime_control import request_budgets


PACKAGE = Path(__file__).resolve().parent
PROJECT = PACKAGE.parents[1]
PREFIX = '@@CUROBO@@'


# Keep one worker per runtime configuration for callers that execute several
# tasks in the same Python process.  The worker itself still receives every
# task's scene through ``prepare`` and decides whether its planner can be
# reused or must be rebuilt.
_SHARED_CLIENTS = {}


def shared_client(config=None):
    """Return a process-wide cuRobo client for the supplied configuration."""
    config = dict(config or {})
    key = json.dumps(config, sort_keys=True, default=str)
    client = _SHARED_CLIENTS.get(key)
    if client is None:
        client = CuroboClient(config)
        _SHARED_CLIENTS[key] = client
    return client


def close_shared_clients():
    """Close all process-wide workers, normally only at interpreter exit."""
    clients = list(_SHARED_CLIENTS.values())
    _SHARED_CLIENTS.clear()
    for client in clients:
        client.close()


def stop_persistent_daemon(config=None):
    """Ask the local persistent worker to exit and release its GPU context."""
    config = dict(config or {})
    cache = Path(config.get('cache_dir', PROJECT / '.cache/curobo')).resolve()
    path = cache / 'worker.sock'
    if not path.exists():
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(5.0)
        sock.connect(str(path))
        request_id = uuid.uuid4().hex
        sock.sendall((json.dumps({'op': 'shutdown', 'request_id': request_id}) + '\n').encode('utf-8'))
        while True:
            line = sock.recv(65536)
            if not line:
                break
            if PREFIX.encode('ascii') in line and b'"ok":true' in line:
                break
        return True
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return False
    finally:
        sock.close()


def start_persistent_daemon(config=None):
    """Start the local persistent worker if needed, leaving it detached."""
    config = {**dict(config or {}), 'persistent_worker': True,
              'persistent_worker_autostart': True}
    client = CuroboClient(config)
    client._start_persistent()
    client.close()
    return client.socket_path


atexit.register(close_shared_clients)


class CuroboClient:
    def __init__(self, config=None):
        self.config = dict(config or {})
        self.cache = Path(self.config.get('cache_dir', PROJECT / '.cache/curobo')).resolve()
        local = self.cache / 'runtime.json'
        if local.is_file():
            self.config = {**json.loads(local.read_text('utf-8')), **self.config}
        self.config['cache_dir'] = str(self.cache)
        self.process = None
        self.socket = None
        self._socket_reader = None
        self.persistent = bool(self.config.get('persistent_worker', False))
        self.persistent_autostart = bool(self.config.get('persistent_worker_autostart', True))
        self.socket_path = self.cache / 'worker.sock'
        self._lock = threading.RLock()
        self._responses = queue.Queue()
        self._log = None
        self.last_stage = None
        atexit.register(self.close)

    def _start(self):
        if self.persistent:
            self._start_persistent()
            return
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

    def _start_persistent(self):
        if self.socket is not None:
            try:
                self.socket.getpeername()
                return
            except OSError:
                self.socket = None
        self.cache.mkdir(parents=True, exist_ok=True)
        def connect():
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            sock.connect(str(self.socket_path))
            self._socket_reader = sock.makefile('rb')
            return sock
        try:
            self.socket = connect()
            return
        except (FileNotFoundError, ConnectionRefusedError, OSError):
            pass
        if not self.persistent_autostart:
            raise FileNotFoundError(f'cuRobo daemon is not running at {self.socket_path}')
        # A stale socket belongs to a dead daemon. Remove it before spawning.
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        self._log = (self.cache / 'worker.log').open('a', encoding='utf-8')
        env = os.environ.copy()
        env.update(PYTHONUTF8='1', PYTHONUNBUFFERED='1', PYTHONDONTWRITEBYTECODE='1')
        if 'cuda_visible_devices' in self.config:
            env['CUDA_VISIBLE_DEVICES']=str(self.config['cuda_visible_devices'])
        env.pop('PYTHONPATH', None)
        python = self.config.get('python') or os.environ.get('CUROBO_PYTHON') or sys.executable
        command = [str(python), '-B', str(PACKAGE / 'worker.py'), '--daemon-socket',
                   str(self.socket_path), json.dumps(self.config)]
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=self._log, cwd=str(PROJECT), env=env,
            start_new_session=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        deadline=time.monotonic()+10.0
        last_error=None
        while time.monotonic() < deadline:
            try:
                self.socket=connect()
                return
            except OSError as exc:
                last_error=exc
                time.sleep(.05)
        if self._log is not None:
            self._log.close()
            self._log=None
        raise RuntimeError(f'cuRobo daemon did not start at {self.socket_path}: {last_error}')

    def _call_persistent(self, message, *, total_deadline, initialization_deadline,
                         solve_budget):
        self.socket.sendall(message.encode('utf-8'))
        solve_deadline=None
        while True:
            deadline=min(total_deadline, solve_deadline if solve_deadline is not None else initialization_deadline)
            remaining=deadline-time.monotonic()
            if remaining <= 0:
                raise queue.Empty
            self.socket.settimeout(remaining)
            line=self._socket_reader.readline()
            if not line:
                raise RuntimeError('cuRobo daemon connection closed')
            if not line.startswith(PREFIX.encode('ascii')):
                continue
            reply=json.loads(line[len(PREFIX):])
            if reply.get('event')=='stage':
                self.last_stage=reply.get('stage')
                if self.last_stage=='planning' and solve_deadline is None:
                    solve_deadline=time.monotonic()+solve_budget
                continue
            return reply

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
                try:
                    self._start()
                except FileNotFoundError:
                    # Manual-daemon mode deliberately falls back to the
                    # original per-process worker when no daemon is running.
                    self.persistent=False
                    self._start()
                if self.persistent:
                    reply=self._call_persistent(message, total_deadline=total_deadline,
                                                initialization_deadline=initialization_deadline,
                                                solve_budget=solve_budget)
                    # Stage events are handled below by the normal pipe path;
                    # the daemon client receives one response per request.
                    if not reply.get('ok'):
                        error=reply.get('error',{})
                        exception=RuntimeError(error.get('error_message','cuRobo daemon failed'))
                        exception.curobo_error=error
                        if error.get('worker_restart_required'):
                            self.close(force=True)
                        raise exception
                    return reply['result']
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
            if self.persistent:
                if self.socket is not None:
                    try:
                        if self._socket_reader is not None:
                            self._socket_reader.close()
                        self.socket.close()
                    except OSError:
                        pass
                    self.socket=None
                    self._socket_reader=None
                if self._log is not None:
                    self._log.close()
                    self._log=None
                return
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
