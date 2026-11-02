"""Fixed loopback WSGI serving profile for the isolated formal-load run."""
import argparse
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / 'backend'))

if os.environ.get('T22_FORMAL_LOAD') != '1':
    raise SystemExit('Explicit formal-load mode required')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'vetapp.settings')

from django.core.wsgi import get_wsgi_application
from waitress import serve


local = threading.local()


def serve_profiled(port, threads):
    from waitress.channel import HTTPChannel
    from waitress.server import create_server
    from waitress.task import ThreadedTaskDispatcher
    from django.db import connections

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
            started_utc = datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
            started = time.perf_counter()
            counters = {'sql_ms': 0.0, 'sql_count': 0, 'commit_ms': 0.0}
            database = connections['default']
            original_commit = database._commit

            def sql(execute, statement, params, many, context):
                before = time.perf_counter()
                try:
                    return execute(statement, params, many, context)
                finally:
                    counters['sql_ms'] += (time.perf_counter() - before) * 1000
                    counters['sql_count'] += 1

            def commit():
                before = time.perf_counter()
                try:
                    return original_commit()
                finally:
                    counters['commit_ms'] += (time.perf_counter() - before) * 1000

            def measured_response(status, headers, exc_info=None):
                app_end = time.perf_counter()
                app_end_utc = datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
                timing = (f"app;dur={(app_end - started) * 1000:.4f}, "
                          f"sql;dur={counters['sql_ms']:.4f}, "
                          f"commit;dur={counters['commit_ms']:.4f}, "
                          f"queue;dur={getattr(local, 'queue_ms', 0):.4f}")
                extra = [
                    ('Server-Timing', timing),
                    ('X-T22-Profile-Scope', 'through-start-response; iterable-excluded'),
                    ('X-T22-SQL-Count', str(counters['sql_count'])),
                    ('X-T22-Server-PID', str(os.getpid())),
                    ('X-T22-Process-CPU', str(time.process_time())),
                    ('X-T22-App-Start', str(started)),
                    ('X-T22-App-End', str(app_end)),
                    ('X-T22-App-Start-UTC', started_utc),
                    ('X-T22-App-End-UTC', app_end_utc),
                ]
                return start_response(status, headers + extra, exc_info)

            database._commit = commit
            try:
                with database.execute_wrapper(sql):
                    return self.application(environ, measured_response)
            finally:
                database._commit = original_commit

    dispatcher = TimedDispatcher()
    dispatcher.set_thread_count(threads)
    server = create_server(TimedApplication(get_wsgi_application()), host='127.0.0.1',
                           port=port, threads=threads, connection_limit=512,
                           backlog=1024, channel_timeout=60, _dispatcher=dispatcher)
    server.channel_class = TimedChannel
    server.run()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18830)
    parser.add_argument('--threads', type=int, default=120)
    parser.add_argument('--profile', action='store_true',
                        help='emit per-request queue, WSGI app, SQL and commit timing headers')
    args = parser.parse_args()
    if args.profile:
        serve_profiled(args.port, args.threads)
    else:
        serve(get_wsgi_application(), host='127.0.0.1', port=args.port,
              threads=args.threads, connection_limit=512, backlog=1024,
              channel_timeout=60)
