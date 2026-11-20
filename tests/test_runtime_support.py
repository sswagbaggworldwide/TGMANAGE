import json
from pathlib import Path
import queue
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from runtime_support import EventBuffer, RunLogWriter


class EventBufferTests(unittest.TestCase):
    def test_burst_is_bounded_and_delivery_counts_are_exact(self):
        recorded = 0
        def persist(kind, value):
            nonlocal recorded
            recorded += 1
        events = EventBuffer(on_event=persist, log_limit=20)
        for index in range(20000):
            events.put(('log', f'sent {index}'))
            events.put(('status', ('account', str(index))))
            events.put(('dashboard_activity', ('mailing', 'sent', 'account')))
        self.assertLessEqual(events.qsize(), 22)
        self.assertEqual(recorded, 60000)
        consumed = []
        while not events.empty():
            consumed.append(events.get_nowait())
        self.assertIn(('status', ('account', '19999')), consumed)
        self.assertIn(('dashboard_activity', ('mailing', 'sent', None, 20000)), consumed)
        self.assertEqual(events.statistics()['display_logs_skipped'], 19980)

    def test_data_records_are_not_dropped_when_producer_is_faster(self):
        events = EventBuffer(reliable_limit=2)
        full = threading.Event()
        def produce():
            events.put(('failed_chat', 1))
            events.put(('failed_chat', 2))
            full.set()
            events.put(('failed_chat', 3))
        worker = threading.Thread(target=produce, daemon=True)
        worker.start()
        self.assertTrue(full.wait(1))
        self.assertEqual(events.get_nowait(), ('failed_chat', 1))
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual([events.get_nowait(), events.get_nowait()],
                         [('failed_chat', 2), ('failed_chat', 3)])

    def test_ui_done_events_never_wait_for_a_full_data_queue(self):
        events = EventBuffer(reliable_limit=1)
        events.put(('account', 'first'))
        events.put(('scan_done', None))
        self.assertEqual(events.get_nowait(), ('account', 'first'))
        self.assertEqual(events.get_nowait(), ('scan_done', None))
        with self.assertRaises(queue.Empty):
            events.get_nowait()


class DiskWriterTests(unittest.TestCase):
    def test_logs_and_json_are_flushed_before_close(self):
        with tempfile.TemporaryDirectory() as folder:
            log, state = Path(folder) / 'run.log', Path(folder) / 'state.json'
            writer = RunLogWriter(log)
            try:
                for number in range(100):
                    writer.append(f'{number}\n')
                self.assertTrue(writer.save_json(state, {'items': [1, 2, 3]}))
                self.assertTrue(writer.flush())
            finally:
                writer.close()
            self.assertEqual(log.read_text('utf-8').splitlines(), [str(i) for i in range(100)])
            self.assertEqual(json.loads(state.read_text('utf-8')), {'items': [1, 2, 3]})

    def test_slow_disk_does_not_make_the_memory_buffer_unbounded(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / 'run.log'
            started, unblock = threading.Event(), threading.Event()
            original_open = Path.open
            def slow_open(path, *args, **kwargs):
                if path == log:
                    started.set()
                    unblock.wait(2)
                return original_open(path, *args, **kwargs)
            with patch.object(Path, 'open', slow_open):
                writer = RunLogWriter(log, max_bytes=1024)
                try:
                    writer.append('first\n')
                    self.assertTrue(started.wait(1))
                    for index in range(1000):
                        writer.append(f'{index:05d}' + 'x' * 100 + '\n')
                    self.assertLessEqual(writer.pending_bytes, 1024)
                    self.assertGreater(writer.skipped, 0)
                finally:
                    unblock.set()
                    writer.close()
            self.assertIn('[log_overflow]', log.read_text('utf-8'))

    def test_background_state_failure_is_reported_for_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            writer = RunLogWriter(Path(folder) / 'run.log')
            try:
                target = Path(folder) / 'missing' / 'state.json'
                self.assertFalse(writer.save_json(target, {'items': [1]}))
                self.assertEqual(writer.take_state_error(), 'FileNotFoundError')
                self.assertIsNone(writer.take_state_error())
            finally:
                writer.close()


class EventPumpRecoveryTests(unittest.TestCase):
    def test_one_broken_event_does_not_stop_future_polling(self):
        import app
        scheduled, failures, processed = [], [], []
        def process(kind, value):
            if value == 'bad':
                raise RuntimeError('bad event')
            processed.append(value)
        state = types.SimpleNamespace(
            events=queue.Queue(), parser_event=process,
            report_callback_exception=lambda *args: failures.append(args[0]),
            after=lambda delay, callback: scheduled.append(delay), poll=lambda: None)
        state.events.put(('parser_test', 'bad'))
        state.events.put(('parser_test', 'good'))
        app.App.poll(state)
        self.assertEqual(failures, [RuntimeError])
        self.assertEqual(scheduled[-1], 10)
        app.App.poll(state)
        self.assertEqual(processed, ['good'])
        self.assertEqual(scheduled[-1], 100)


if __name__ == '__main__':
    unittest.main()
