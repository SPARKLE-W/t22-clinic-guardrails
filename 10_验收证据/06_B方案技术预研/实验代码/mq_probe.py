"""PRE-02 only: real Django/MySQL + RabbitMQ, own UUID fixtures and queues.

Run from this directory with the task venv Python; unittest uses the existing
dedicated database, never Django TestCase's database creation or flush.
"""

import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
import uuid
from datetime import timedelta

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")

PRIVATE = Path("C:/Users/29512/.cache/t22-runtime/private/connections.json")
LIFECYCLE = None


def trace(event, **facts):
    print(json.dumps({"event": event, **facts}, ensure_ascii=False), flush=True)


def rabbit_connection(*, port=None):
    import pika

    config = json.loads(PRIVATE.read_text(encoding="utf-8-sig"))["rabbitmq"]
    return pika.BlockingConnection(pika.ConnectionParameters(
        host=config["host"], port=int(port or config["port"]),
        virtual_host=config.get("virtual_host", config.get("vhost", "/")),
        credentials=pika.PlainCredentials(config["user"], config["password"]),
        connection_attempts=1, socket_timeout=3, stack_timeout=5,
        blocked_connection_timeout=3, heartbeat=15,
    ))


def claim_event(event_id, lease_seconds=10):
    from django.db import transaction
    from django.utils import timezone
    from probe.models import Outbox, Receipt

    with transaction.atomic():
        row = Outbox.objects.select_for_update().get(event_id=event_id)
        if row.state == "delivered" or Receipt.objects.filter(event_id=event_id).exists():
            if row.state != "delivered":
                row.state, row.claim_token, row.lease_until = "delivered", None, None
                row.save(update_fields=["state", "claim_token", "lease_until"])
            return None
        now = timezone.now()
        if row.claim_token and row.lease_until and row.lease_until > now:
            return None
        token = uuid.uuid4()
        row.claim_token = token
        row.lease_until = now + timedelta(seconds=lease_seconds)
        row.save(update_fields=["claim_token", "lease_until"])
        return token


def finish_claim(event_id, token, state):
    from django.utils import timezone
    from probe.models import Outbox

    if token is None or state not in {"pending", "published"}:
        return False
    changes = {"state": state}
    if state == "pending":
        changes.update(claim_token=None, lease_until=None)
    return bool(Outbox.objects.filter(
        event_id=event_id, claim_token=token, lease_until__gt=timezone.now(),
    ).exclude(state="delivered").update(**changes))


def publish_claim(event_id, token, queue, *, fail_after_confirm=False,
                  connection_factory=rabbit_connection):
    import pika
    from django.utils import timezone
    from probe.models import Outbox

    row = Outbox.objects.filter(event_id=event_id, claim_token=token,
                                lease_until__gt=timezone.now()).exclude(state="delivered").first()
    if token is None or row is None:
        return "lease_lost"
    connection = None
    result = "unavailable"
    try:
        connection = connection_factory()
        channel = connection.channel()
        channel.confirm_delivery()
        channel.basic_publish(
            exchange="", routing_key=queue, mandatory=True,
            body=json.dumps({"event_id": str(event_id), "alert_id": str(row.alert_id)}).encode(),
            properties=pika.BasicProperties(delivery_mode=2, content_type="application/json",
                                           message_id=str(event_id)),
        )
        if fail_after_confirm:
            # Explicit experiment fault: broker confirm arrived, app DB write
            # was not made. This is not called a lost network confirm.
            result = "unknown_after_confirm"
            finish_claim(event_id, token, "pending")
        else:
            finish_claim(event_id, token, "published")
            result = "confirmed"
    except pika.exceptions.UnroutableError:
        result = "unroutable"
        finish_claim(event_id, token, "pending")
    except pika.exceptions.NackError:
        result = "nacked"
        finish_claim(event_id, token, "pending")
    except (pika.exceptions.AMQPError, OSError):
        result = "unknown" if connection is not None else "unavailable"
        finish_claim(event_id, token, "pending")
    finally:
        if connection is not None and connection.is_open:
            connection.close()
    trace("publication", event_id=str(event_id), result=result)
    return result


def consume_one(queue, *, ack=True):
    from django.db import transaction
    from probe.models import Outbox, Receipt

    connection = rabbit_connection()
    try:
        channel = connection.channel()
        deadline = time.monotonic() + 5
        while True:
            method, _, body = channel.basic_get(queue, auto_ack=False)
            if method is not None:
                break
            if time.monotonic() >= deadline:
                return None
            connection.process_data_events(time_limit=0.05)
        payload = json.loads(body)
        if set(payload) != {"event_id", "alert_id"}:
            raise ValueError("unexpected message field set")
        event_id, alert_id = uuid.UUID(payload["event_id"]), uuid.UUID(payload["alert_id"])
        with transaction.atomic():
            row = Outbox.objects.select_for_update().get(event_id=event_id)
            if row.alert_id != alert_id:
                raise ValueError("event-alert association mismatch")
            _, created = Receipt.objects.get_or_create(event_id=event_id)
            row.state, row.claim_token, row.lease_until = "delivered", None, None
            row.save(update_fields=["state", "claim_token", "lease_until"])
        # atomic has exited: DB receipt is durable before broker ack.
        if ack:
            channel.basic_ack(method.delivery_tag)
        result = {"event_id": str(event_id), "receipt_created": created,
                  "redelivered": bool(method.redelivered), "acked": ack}
        trace("consumption", **result)
        return result
    finally:
        # ack=False intentionally closes an AMQP connection with an unacked
        # delivery; the real broker must requeue, no fake redelivery flag.
        if connection.is_open:
            connection.close()


class ConfirmLossProxy:
    """Test transport: forwards real AMQP, drops Basic.Ack and disconnects.

    This is a connection fault injection, not a stopped RabbitMQ process.
    Its only parser identifies server method frames; it never logs bodies.
    """

    def __enter__(self):
        self.dropped = threading.Event()
        self.sockets = []
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen(1)
        self.listener.settimeout(10)
        self.thread = threading.Thread(target=self.relay, daemon=True)
        self.thread.start()
        return self

    @staticmethod
    def read_exact(sock, count):
        result = b""
        while len(result) < count:
            block = sock.recv(count - len(result))
            if not block:
                raise EOFError
            result += block
        return result

    def relay(self):
        try:
            client, _ = self.listener.accept()
            config = json.loads(PRIVATE.read_text(encoding="utf-8-sig"))["rabbitmq"]
            upstream = socket.create_connection((config["host"], int(config["port"])), 5)
            client.settimeout(10)
            upstream.settimeout(10)
            self.sockets.extend([client, upstream])

            def forward_client():
                try:
                    while block := client.recv(65536):
                        upstream.sendall(block)
                except OSError:
                    pass

            threading.Thread(target=forward_client, daemon=True).start()
            while True:
                header = self.read_exact(upstream, 7)
                frame_type, channel, size = struct.unpack("!BHI", header)
                payload = self.read_exact(upstream, size)
                end = self.read_exact(upstream, 1)
                if frame_type == 1 and channel and payload[:4] == b"\x00\x3c\x00\x50":
                    self.dropped.set()
                    trace("transport_fault", injected="drop_real_basic_ack_then_disconnect")
                    break
                client.sendall(header + payload + end)
        except (OSError, EOFError):
            pass
        finally:
            for sock in self.sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()

    def __exit__(self, *_):
        self.listener.close()
        self.thread.join(timeout=12)


class MQRecoveryTests(unittest.TestCase):
    """Each assertion catches a durable-state/ownership regression, not a mock."""

    def setUp(self):
        if self._testMethodName.startswith(("test_07_", "test_08_")) and not LIFECYCLE:
            self.skipTest("task broker lifecycle not enabled")
        from probe.fixtures import create_case
        from probe.models import Alert, Operation, Outbox

        self.tag = "mq-" + uuid.uuid4().hex
        self.case = create_case(self.tag)
        self.encounter = self.case["encounter"]
        self.operation = Operation.objects.create(
            actor=self.case["doctor"], permission="clinical.diagnosis.write",
            operation_key=self.tag, fingerprint="a" * 64, outcome={}, status=422,
        )
        self.alert = Alert.objects.create(
            id=uuid.uuid4(), encounter=self.encounter, attempt=self.operation,
            rule_id="PRE_FIXTURE_MQ", rule_version=1, field="diagnosis",
        )
        self.event = uuid.uuid4()
        Outbox.objects.create(event_id=self.event, alert=self.alert, state="pending")
        self.queue = "t22.probe." + self.tag
        self.queues = [self.queue]
        conn = rabbit_connection()
        try:
            conn.channel().queue_declare(queue=self.queue, durable=True)
        finally:
            conn.close()
        trace("fixture", tag=self.tag, event_id=str(self.event), queue=self.queue)

    def tearDown(self):
        # Preserve synthetic DB rows for evidence; only remove this test's MQ
        # queue. Never flush or delete the parent's medical/load records.
        try:
            conn = rabbit_connection()
            try:
                ch = conn.channel()
                for queue in self.queues:
                    ch.queue_delete(queue=queue)
            finally:
                conn.close()
        except Exception as exc:
            trace("cleanup_queue_failed", exception_type=type(exc).__name__)

    def row(self):
        from probe.models import Outbox
        return Outbox.objects.get(event_id=self.event)

    def assert_business_once(self):
        from probe.models import Alert, Receipt
        self.assertEqual(1, Alert.objects.filter(encounter=self.encounter).count())
        self.assertEqual(1, Receipt.objects.filter(event_id=self.event).count())
        self.alert.refresh_from_db()
        self.assertFalse(self.alert.handled)
        self.assertEqual("delivered", self.row().state)
        trace("business_once", event_id=str(self.event), alert_id=str(self.alert.id),
              alerts=1, receipts=1, outbox="delivered", doctor_handled=False)

    def test_01_only_one_worker_claims_live_lease(self):
        # Removing the live-lease predicate lets both real DB workers claim.
        from django.db import close_old_connections
        barrier = threading.Barrier(2)

        def worker():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return claim_event(self.event)
            finally:
                close_old_connections()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda _: worker(), range(2)))
        self.assertEqual(1, sum(value is not None for value in values))
        self.assertEqual("pending", self.row().state)

    def test_02_expired_worker_cannot_revert_delivered(self):
        # Omitting token/state predicates permits an expired worker to regress.
        from django.utils import timezone
        from probe.models import Outbox
        first = claim_event(self.event)
        self.assertIsNotNone(first)
        Outbox.objects.filter(event_id=self.event).update(
            lease_until=timezone.now() - timedelta(seconds=1))
        second = claim_event(self.event)
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)
        self.assertFalse(finish_claim(self.event, first, "pending"))
        self.assertEqual("confirmed", publish_claim(self.event, second, self.queue))
        self.assertIsNotNone(consume_one(self.queue))
        self.assertFalse(finish_claim(self.event, first, "pending"))
        self.assertFalse(finish_claim(self.event, second, "published"))
        self.assert_business_once()

    def test_03_confirm_is_publication_not_doctor_handling(self):
        # Treating publisher confirm as consumption or handling breaks this.
        token = claim_event(self.event)
        self.assertEqual("confirmed", publish_claim(self.event, token, self.queue))
        self.assertEqual("published", self.row().state)
        conn = rabbit_connection()
        try:
            ch = conn.channel()
            method, props, body = ch.basic_get(self.queue, auto_ack=False)
            self.assertIsNotNone(method)
            payload = json.loads(body)
            self.assertEqual({"event_id", "alert_id"}, set(payload))
            self.assertEqual(str(self.event), payload["event_id"])
            self.assertEqual(2, props.delivery_mode)
            ch.basic_nack(method.delivery_tag, requeue=True)
        finally:
            conn.close()
        self.assertIsNotNone(consume_one(self.queue))
        self.assert_business_once()

    def test_04_mandatory_unroutable_retains_pending(self):
        # Ignoring Basic.Return would falsely mark an unroutable message sent.
        absent = "t22.probe.absent." + uuid.uuid4().hex
        token = claim_event(self.event)
        self.assertEqual("unroutable", publish_claim(self.event, token, absent))
        self.assertEqual("pending", self.row().state)

    def test_05_commit_before_ack_survives_real_redelivery(self):
        # Missing receipt de-dup creates repeated effects after commit/no-ack.
        token = claim_event(self.event)
        self.assertEqual("confirmed", publish_claim(self.event, token, self.queue))
        first = consume_one(self.queue, ack=False)
        self.assertIsNotNone(first)
        second = consume_one(self.queue)
        self.assertIsNotNone(second)
        self.assertTrue(second["redelivered"])
        self.assertFalse(second["receipt_created"])
        self.assert_business_once()

    def test_06_confirm_then_database_gap_retries_same_event(self):
        # A lost application state write must not create a second business alert.
        first = claim_event(self.event)
        self.assertEqual("unknown_after_confirm", publish_claim(
            self.event, first, self.queue, fail_after_confirm=True))
        self.assertEqual("pending", self.row().state)
        second = claim_event(self.event)
        self.assertEqual("confirmed", publish_claim(self.event, second, self.queue))
        self.assertIsNotNone(consume_one(self.queue))
        duplicate = consume_one(self.queue)
        self.assertIsNotNone(duplicate)
        self.assertFalse(duplicate["receipt_created"])
        self.assert_business_once()

    def test_06b_real_confirm_connection_loss_keeps_retryable_event(self):
        # Ignoring an unobserved broker ack would wrongly report publication.
        token = claim_event(self.event)
        with ConfirmLossProxy() as proxy:
            result = publish_claim(self.event, token, self.queue,
                                   connection_factory=lambda: rabbit_connection(port=proxy.port))
            self.assertEqual("unknown", result)
            self.assertTrue(proxy.dropped.wait(timeout=5))
        self.assertEqual("pending", self.row().state)
        token = claim_event(self.event)
        self.assertEqual("confirmed", publish_claim(self.event, token, self.queue))
        self.assertIsNotNone(consume_one(self.queue))
        duplicate = consume_one(self.queue)
        self.assertIsNotNone(duplicate)
        self.assertFalse(duplicate["receipt_created"])
        self.assert_business_once()

    def lifecycle(self, operation):
        if not LIFECYCLE:
            self.skipTest("requires explicit task-only start/stop script paths")
        subprocess.run(["pwsh", "-NoProfile", "-File", str(LIFECYCLE[operation])],
                       check=True, timeout=90, capture_output=True, text=True)
        trace("broker_process", operation=operation)

    def test_07_real_broker_stop_preserves_outbox_then_recovers(self):
        if not LIFECYCLE:
            self.skipTest("task broker lifecycle not enabled")
        try:
            self.lifecycle("stop")
            token = claim_event(self.event)
            self.assertEqual("unavailable", publish_claim(self.event, token, self.queue))
            self.assertEqual("pending", self.row().state)
        finally:
            self.lifecycle("start")
        token = claim_event(self.event)
        self.assertEqual("confirmed", publish_claim(self.event, token, self.queue))
        self.assertIsNotNone(consume_one(self.queue))
        self.assert_business_once()

    def test_08_persistent_confirmed_message_survives_broker_restart(self):
        if not LIFECYCLE:
            self.skipTest("task broker lifecycle not enabled")
        token = claim_event(self.event)
        self.assertEqual("confirmed", publish_claim(self.event, token, self.queue))
        try:
            self.lifecycle("stop")
            self.assertEqual("published", self.row().state)
        finally:
            self.lifecycle("start")
        self.assertIsNotNone(consume_one(self.queue))
        self.assert_business_once()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-script", type=Path)
    parser.add_argument("--stop-script", type=Path)
    args, unittest_args = parser.parse_known_args()
    if bool(args.start_script) != bool(args.stop_script):
        parser.error("supply both task-only lifecycle scripts")
    if args.start_script:
        LIFECYCLE = {"start": args.start_script, "stop": args.stop_script}
    import django
    django.setup()
    unittest.main(argv=[sys.argv[0], *unittest_args], verbosity=2)
