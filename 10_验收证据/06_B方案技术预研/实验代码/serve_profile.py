"""Loopback diagnostic entry; identical Django application, timing only."""
import os
import argparse
import threading
import time
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'settings')
from django.core.wsgi import get_wsgi_application
from django.db import connections
from waitress.channel import HTTPChannel
from waitress.server import create_server
from waitress.task import ThreadedTaskDispatcher

local = threading.local()


class TimedDispatcher(ThreadedTaskDispatcher):
    def add_task(self, task):
        task.pre_queued_at = time.perf_counter()
        return super().add_task(task)


class TimedChannel(HTTPChannel):
    def service(self):
        local.queue_ms = (time.perf_counter() - self.pre_queued_at) * 1000
        try:
            return super().service()
        finally:
            local.queue_ms = 0


class TimedApplication:
    def __init__(self, application):
        self.application = application

    def __call__(self, environ, start_response):
        started = time.perf_counter()
        counters = {'sql_ms': 0.0, 'sql_n': 0, 'commit_ms': 0.0}
        database = connections['default']  # real thread-local wrapper
        original_commit = database._commit

        def sql(execute, statement, params, many, context):
            before = time.perf_counter()
            try:
                return execute(statement, params, many, context)
            finally:
                counters['sql_ms'] += (time.perf_counter() - before) * 1000
                counters['sql_n'] += 1

        def commit():
            before = time.perf_counter()
            try:
                return original_commit()
            finally:
                counters['commit_ms'] += (time.perf_counter() - before) * 1000

        def measured_response(status, headers, exc_info=None):
            app_ms = (time.perf_counter() - started) * 1000
            # WSGI call through start_response, including Django render/commit;
            # response iterable delivery occurs later and is not called app_ms.
            timing = f"app;dur={app_ms:.4f}, sql;dur={counters['sql_ms']:.4f}, commit;dur={counters['commit_ms']:.4f}, queue;dur={getattr(local, 'queue_ms', 0):.4f}"
            return start_response(status, headers + [('Server-Timing', timing),
                                  ('X-PRE-SQL-Count', str(counters['sql_n'])),
                                  ('X-PRE-Server-PID', str(os.getpid())),
                                  ('X-PRE-Process-CPU', str(time.process_time())),
                                  ('X-PRE-App-Start', str(started)),
                                  ('X-PRE-App-End', str(time.perf_counter()))], exc_info)

        database._commit = commit
        try:
            with database.execute_wrapper(sql):
                return self.application(environ, measured_response)
        finally:
            database._commit = original_commit


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18722)
    parser.add_argument('--threads', type=int, default=32)
    args = parser.parse_args()
    dispatcher = TimedDispatcher()
    dispatcher.set_thread_count(args.threads)
    server = create_server(TimedApplication(get_wsgi_application()), host='127.0.0.1',
                           port=args.port, threads=args.threads, connection_limit=512,
                           channel_timeout=45, _dispatcher=dispatcher)
    server.channel_class = TimedChannel
    server.run()
