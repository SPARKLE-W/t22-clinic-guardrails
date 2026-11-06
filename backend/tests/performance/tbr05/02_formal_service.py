"""Fixed WSGI serving profile for the isolated formal-load run."""
import argparse
import ipaddress
import math
import os
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

if os.environ.get('T22_FORMAL_LOAD') != '1':
    raise SystemExit('Explicit formal-load mode required')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'vetapp.settings')

from django.core.wsgi import get_wsgi_application
from waitress import serve


local = threading.local()


@contextmanager
def gil_switch_interval(milliseconds):
    original = sys.getswitchinterval()
    if milliseconds is not None:
        if not math.isfinite(milliseconds) or milliseconds <= 0:
            raise ValueError('GIL switch interval must be finite and positive')
        sys.setswitchinterval(milliseconds / 1000)
    try:
        yield sys.getswitchinterval() * 1000
    finally:
        if milliseconds is not None:
            sys.setswitchinterval(original)


@contextmanager
def measure_connections(database, counters):
    """Observe real connection attempts without changing retry or timeout policy."""
    original = database.get_new_connection
    had_override = 'get_new_connection' in database.__dict__
    counters.update(db_connect_ms=0.0, db_connect_count=0)

    def connect(params):
        before = time.perf_counter()
        counters['db_connect_count'] += 1
        try:
            return original(params)
        except Exception as exc:
            if exc.args and type(exc.args[0]) is int:
                counters['db_connect_error_code'] = exc.args[0]
            raise
        finally:
            counters['db_connect_ms'] += (time.perf_counter() - before) * 1000

    database.get_new_connection = connect
    try:
        yield
    finally:
        if had_override:
            database.get_new_connection = original
        else:
            del database.get_new_connection


def normalize_bind_host(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise ValueError('--host must be loopback or a private IPv4 address') from error
    vpc_networks = (ipaddress.ip_network('10.0.0.0/8'), ipaddress.ip_network('172.16.0.0/12'),
                    ipaddress.ip_network('192.168.0.0/16'))
    if address.version != 4 or not (
            address.is_loopback or any(address in network for network in vpc_networks)):
        raise ValueError('--host must be loopback or a private IPv4 address')
    return str(address)


def serve_profiled(port, threads, host):
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
            export_timings = environ['t22.export_timings'] = {}
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
                          f"db-connect;dur={counters['db_connect_ms']:.4f}, "
                          f"queue;dur={getattr(local, 'queue_ms', 0):.4f}")
                for name, duration in export_timings.items():
                    timing += f", {name};dur={duration:.4f}"
                extra = [
                    ('Server-Timing', timing),
                    ('X-T22-Profile-Scope', 'through-start-response; iterable-excluded'),
                    ('X-T22-SQL-Count', str(counters['sql_count'])),
                    ('X-T22-DB-Connect-Count', str(counters['db_connect_count'])),
                    ('X-T22-Server-PID', str(os.getpid())),
                    ('X-T22-Process-CPU', str(time.process_time())),
                    ('X-T22-App-Start', str(started)),
                    ('X-T22-App-End', str(app_end)),
                    ('X-T22-App-Start-UTC', started_utc),
                    ('X-T22-App-End-UTC', app_end_utc),
                ]
                if 'db_connect_error_code' in counters:
                    extra.append(('X-T22-DB-Connect-Error-Code', str(counters['db_connect_error_code'])))
                return start_response(status, headers + extra, exc_info)

            database._commit = commit
            try:
                with measure_connections(database, counters), database.execute_wrapper(sql):
                    return self.application(environ, measured_response)
            finally:
                database._commit = original_commit

    dispatcher = TimedDispatcher()
    dispatcher.set_thread_count(threads)
    server = create_server(TimedApplication(get_wsgi_application()), host=host,
                           port=port, threads=threads, connection_limit=512,
                           backlog=1024, channel_timeout=60, _dispatcher=dispatcher)
    server.channel_class = TimedChannel
    server.run()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18830)
    parser.add_argument('--threads', type=int, default=120)
    parser.add_argument('--host', default='127.0.0.1',
                        help='loopback or a private IPv4 address for an isolated VPC load sender')
    parser.add_argument('--gil-switch-interval-ms', type=float,
                        default=os.environ.get('T22_SERVER_GIL_SWITCH_INTERVAL_MS'),
                        help='optional process-local scheduling candidate; default preserves interpreter setting')
    parser.add_argument('--profile', action='store_true',
                        help='emit per-request queue, WSGI app, SQL and commit timing headers')
    args = parser.parse_args()
    try:
        host = normalize_bind_host(args.host)
    except ValueError as error:
        parser.error(str(error))
    with gil_switch_interval(args.gil_switch_interval_ms) as active:
        print(f'GIL_SWITCH_INTERVAL_MS={active}', flush=True)
        if args.profile:
            serve_profiled(args.port, args.threads, host)
        else:
            serve(get_wsgi_application(), host=host, port=args.port,
                  threads=args.threads, connection_limit=512, backlog=1024,
                  channel_timeout=60)
