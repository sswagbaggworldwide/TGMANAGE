"""Bounded UI delivery, background persistence, and low-frequency diagnostics."""
from collections import Counter, OrderedDict, deque
import ctypes
import json
from pathlib import Path
import queue
import sys
import threading
import time
import traceback


class EventBuffer:
    """Bound noisy display updates; apply backpressure to lossless data events."""
    def __init__(self, on_event=None, reliable_limit=1024, log_limit=600, state_limit=1024):
        self.on_event = on_event
        self.owner = threading.get_ident()
        self.reliable = queue.Queue(maxsize=reliable_limit)
        self.logs = deque(maxlen=log_limit)
        self.states = OrderedDict()
        self.activity = Counter()
        self.state_limit = state_limit
        self.lock = threading.RLock()
        self.dropped_display_logs = 0
        self.coalesced_states = 0

    def put(self, event, block=True, timeout=None):
        kind, value = event
        if self.on_event:
            self.on_event(kind, value)
        with self.lock:
            if kind == 'dashboard_activity':
                category, action, _key, *extra = value
                self.activity[category, action] += extra[0] if extra else 1
                return
            if kind == 'log' or kind.endswith('_log'):
                if len(self.logs) == self.logs.maxlen:
                    self.dropped_display_logs += 1
                self.logs.append(event)
                return
            if (kind in {'status', 'account_name', 'likes_status', 'parser_status', 'search_status',
                         'parser_snapshot', 'parser_groups'}
                    or kind.endswith('_done')):
                key = value[0] if kind in {'status', 'account_name', 'likes_status'} else ''
                self._state((kind, key), event)
                return
        # These contain records or lifecycle transitions. Do not discard them.
        # Only a background producer can wait: the UI must always be able to drain.
        if threading.get_ident() == self.owner:
            try:
                self.reliable.put_nowait(event)
            except queue.Full:
                with self.lock:
                    self._state((kind, 'ui'), event)
        else:
            self.reliable.put(event, block=block, timeout=timeout)

    def _state(self, key, event):
        if key in self.states:
            self.coalesced_states += 1
        elif len(self.states) >= self.state_limit:
            self.states.popitem(last=False)
        self.states[key] = event

    def get_nowait(self):
        try:
            return self.reliable.get_nowait()
        except queue.Empty:
            pass
        with self.lock:
            if self.states:
                return self.states.popitem(last=False)[1]
            if self.activity:
                (category, action), count = self.activity.popitem()
                return 'dashboard_activity', (category, action, None, count)
            if self.logs:
                return self.logs.popleft()
        raise queue.Empty

    def qsize(self):
        with self.lock:
            return self.reliable.qsize() + len(self.states) + len(self.activity) + len(self.logs)

    def empty(self):
        return self.qsize() == 0

    def statistics(self):
        with self.lock:
            return {'ui_pending': self.qsize(), 'display_logs_skipped': self.dropped_display_logs,
                    'status_updates_merged': self.coalesced_states}


class RunLogWriter:
    """One worker owns disk writes; pending log text is limited to 4 MiB."""
    def __init__(self, path, max_bytes=4 * 1024 * 1024):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.lines = deque()
        self.pending_bytes = 0
        self.json_jobs = {}
        self.condition = threading.Condition()
        self.stopping = False
        self.inflight = False
        self.skipped = 0
        self.last_error = None
        self.state_error = None
        self.thread = threading.Thread(target=self._run, name='runtime-disk-writer', daemon=True)
        self.thread.start()

    def append(self, line):
        line = line.encode('utf-8', errors='replace').decode('utf-8')
        encoded_size = len(line.encode('utf-8'))
        with self.condition:
            if self.stopping:
                return
            while self.lines and self.pending_bytes + encoded_size > self.max_bytes:
                _old, size = self.lines.popleft()
                self.pending_bytes -= size
                self.skipped += 1
            if encoded_size <= self.max_bytes:
                self.lines.append((line, encoded_size))
                self.pending_bytes += encoded_size
            else:
                self.skipped += 1
            self.condition.notify()

    def save_json(self, path, data, wait=True, timeout=5):
        receipt = {'done': threading.Event(), 'error': None} if wait else None
        with self.condition:
            if self.stopping:
                return False
            old = self.json_jobs.get(Path(path))
            receipts = old[1] if old else []
            if receipt:
                receipts.append(receipt)
            self.json_jobs[Path(path)] = data, receipts
            self.condition.notify()
        if receipt:
            return receipt['done'].wait(timeout) and receipt['error'] is None
        return True

    def take_state_error(self):
        with self.condition:
            error, self.state_error = self.state_error, None
            return error

    def _run(self):
        while True:
            with self.condition:
                while not self.lines and not self.json_jobs and not self.stopping:
                    self.condition.wait(.5)
                if self.stopping and not self.lines and not self.json_jobs:
                    return
                lines, self.lines = self.lines, deque()
                jobs, self.json_jobs = self.json_jobs, {}
                skipped, self.skipped = self.skipped, 0
                self.pending_bytes = 0
                self.inflight = True
            try:
                if lines or skipped:
                    with self.path.open('a', encoding='utf-8', errors='replace') as stream:
                        if skipped:
                            stream.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} [log_overflow] '
                                         f'Пропущено строк при переполнении дискового буфера: {skipped}\n')
                        stream.writelines(line for line, _size in lines)
            except OSError as exc:
                self.last_error = type(exc).__name__
            for path, (data, receipts) in jobs.items():
                error = None
                try:
                    temporary = path.with_suffix('.tmp')
                    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), 'utf-8',
                                         errors='replace')
                    temporary.replace(path)
                except (OSError, ValueError, TypeError) as exc:
                    error = type(exc).__name__
                    with self.condition:
                        self.state_error = error
                finally:
                    for receipt in receipts:
                        receipt['error'] = error
                        receipt['done'].set()
            # Release potentially large batches before waiting for the next one.
            lines.clear()
            jobs.clear()
            with self.condition:
                self.inflight = False
                self.condition.notify_all()

    def flush(self, timeout=5):
        deadline = time.monotonic() + timeout
        with self.condition:
            self.condition.notify()
            while self.lines or self.json_jobs or self.inflight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(remaining)
        return self.last_error is None

    def close(self, timeout=3):
        with self.condition:
            self.stopping = True
            self.condition.notify_all()
        self.thread.join(timeout)


def process_memory_mb():
    if sys.platform != 'win32':
        return None
    class Memory(ctypes.Structure):
        _fields_ = [('cb', ctypes.c_ulong), ('faults', ctypes.c_ulong)] + [
            (name, ctypes.c_size_t) for name in
            ('peak_ws', 'ws', 'peak_paged', 'paged', 'peak_nonpaged', 'nonpaged',
             'pagefile', 'peak_pagefile', 'private')]
    counters = Memory()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.windll.kernel32
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    query = ctypes.windll.psapi.GetProcessMemoryInfo
    query.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    if query(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return round(counters.private / 1024 / 1024, 2)
    return None


class RuntimeDiagnostics:
    """Sample Python state without calling Tk from a background thread."""
    def __init__(self, writer, snapshot):
        self.writer, self.snapshot = writer, snapshot
        self.last_heartbeat = time.monotonic()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name='runtime-watchdog', daemon=True)
        self.thread.start()

    def heartbeat(self):
        self.last_heartbeat = time.monotonic()

    def _run(self):
        last_sample = time.monotonic()
        last_dump = float('-inf')
        last_cpu = time.process_time()
        while not self.stopped.wait(5):
            now = time.monotonic()
            lag = now - self.last_heartbeat
            if lag > 10 and now - last_dump > 60:
                last_dump = now
                stacks = ['UI heartbeat delayed %.1f s' % lag]
                for ident, frame in sys._current_frames().items():
                    stacks.append('Thread %s:\n%s' % (ident, ''.join(traceback.format_stack(frame))))
                self.writer.append(f'{time.strftime("%Y-%m-%d %H:%M:%S")} [ui_stall] '
                                   + '\n'.join(stacks) + '\n')
            if now - last_sample < 60:
                continue
            try:
                cpu = time.process_time()
                data = dict(self.snapshot(), private_mb=process_memory_mb(),
                            cpu_one_core_percent=round((cpu - last_cpu) / (now - last_sample) * 100, 1),
                            ui_heartbeat_delay_s=round(lag, 2),
                            disk_buffer_bytes=self.writer.pending_bytes)
                self.writer.append(f'{time.strftime("%Y-%m-%d %H:%M:%S")} [health] '
                                   + json.dumps(data, ensure_ascii=False) + '\n')
                last_sample, last_cpu = now, cpu
            except Exception as exc:
                self.writer.append(f'{time.strftime("%Y-%m-%d %H:%M:%S")} [health_error] '
                                   f'{type(exc).__name__}\n')
                last_sample = now

    def close(self):
        self.stopped.set()
        self.thread.join(1)
