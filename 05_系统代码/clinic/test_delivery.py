import json
import uuid
from contextlib import contextmanager
from datetime import timedelta
from importlib import import_module
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pika
from django.core.management import call_command
from django.db import DatabaseError, connection
from django.utils import timezone
from .tests import ClinicFixture
from .models import Alert, AlertOutbox, ClinicalRecord
from .writes import diagnosis, resolve
from . import mq_transport


class DeliveryTests(ClinicFixture):
    def setUp(self):
        super().setUp()
        self.ref=self.create()['encounter_ref']
        diagnosis(self.doctor.pk,'record',str(uuid.uuid4()),{},self.ref)
        self.event=AlertOutbox.objects.get()

    def service(self):
        return import_module('clinic.delivery')

    def config(self):
        return {'host':'127.0.0.1','port':5672,'virtual_host':'test','user':'test','password':'synthetic','queue':'t22.app.tests.unit'}

    def wire(self,alert=None,event=None):
        return json.dumps({'event_ref':str(event or self.event.pk),'alert_ref':str(alert or self.event.alert_id)}).encode()

    def transport(self):
        conn=MagicMock();channel=conn.channel.return_value
        channel.basic_get.return_value=(SimpleNamespace(delivery_tag=7),SimpleNamespace(message_id=str(self.event.pk)),self.wire())
        return conn,channel

    @contextmanager
    def publishing(self,conn):
        # Unit tests run the real AMQP adapter with controlled IO; integration
        # tests below exercise the spawned deadline process against RabbitMQ.
        with patch('clinic.delivery.publish_message',side_effect=mq_transport.publish_one),patch('clinic.mq_transport.connect',return_value=conn):
            yield

    def test_claim_expires_and_old_token_cannot_overwrite_new_owner(self):
        delivery=self.service();first=delivery.claim_event(self.event.pk)
        self.assertIsNotNone(first)
        self.assertIsNone(delivery.claim_event(self.event.pk))
        AlertOutbox.objects.filter(pk=self.event.pk).update(lease_until=timezone.now()-timedelta(seconds=1))
        second=delivery.claim_event(self.event.pk)
        self.assertNotEqual(first,second)
        self.assertFalse(delivery.finish_claim(self.event.pk,first,'published'))
        self.event.refresh_from_db()
        self.assertEqual(second,self.event.claim_token)
        self.assertEqual('pending',self.event.state)

    def test_confirm_is_not_delivery_and_wire_has_no_clinical_fields(self):
        delivery=self.service();token=delivery.claim_event(self.event.pk);conn,channel=self.transport()
        with self.publishing(conn):
            result=delivery.publish_event(self.event.pk,token,self.config())
        self.assertEqual('published',result)
        self.event.refresh_from_db()
        self.assertEqual('published',self.event.state)
        self.assertFalse(delivery.DeliveryReceipt.objects.exists())
        kwargs=channel.basic_publish.call_args.kwargs
        self.assertEqual({'event_ref','alert_ref'},set(json.loads(kwargs['body'])))
        self.assertTrue(kwargs['mandatory'])
        self.assertEqual(2,kwargs['properties'].delivery_mode)
        self.assertEqual(str(self.event.pk),kwargs['properties'].message_id)
        channel.queue_declare.assert_called_once_with(queue=self.config()['queue'],durable=True)
        self.assertIsNone(Alert.objects.get().handled_at)

    def test_unroutable_is_recoverable_and_not_a_new_clinical_alert(self):
        delivery=self.service();token=delivery.claim_event(self.event.pk);conn,channel=self.transport()
        channel.basic_publish.side_effect=pika.exceptions.UnroutableError([])
        with self.publishing(conn):
            self.assertEqual('unroutable',delivery.publish_event(self.event.pk,token,self.config()))
        self.event.refresh_from_db()
        self.assertEqual('pending',self.event.state)
        self.assertEqual('UNROUTABLE',self.event.last_error_code)
        self.assertIsNone(self.event.claim_token)
        self.assertEqual(1,Alert.objects.count())

    def test_connection_loss_after_send_remains_uncertain(self):
        delivery=self.service();token=delivery.claim_event(self.event.pk);conn,channel=self.transport()
        channel.basic_publish.side_effect=pika.exceptions.AMQPConnectionError()
        with self.publishing(conn):
            self.assertEqual('uncertain',delivery.publish_event(self.event.pk,token,self.config()))
        self.event.refresh_from_db()
        self.assertEqual('uncertain',self.event.state)
        self.assertGreater(self.event.next_attempt_at,timezone.now())
        self.assertIsNone(delivery.claim_event(self.event.pk))
        self.assertFalse(ClinicalRecord.objects.exists())

    def test_connection_failure_keeps_original_event_pending(self):
        delivery=self.service();token=delivery.claim_event(self.event.pk)
        with patch('clinic.delivery.publish_message',side_effect=mq_transport.publish_one),patch('clinic.mq_transport.connect',side_effect=OSError('synthetic transport failure')):
            self.assertEqual('unavailable',delivery.publish_event(self.event.pk,token,self.config()))
        self.event.refresh_from_db()
        self.assertEqual('pending',self.event.state)
        self.assertEqual(1,AlertOutbox.objects.count())

    def test_consumer_commits_receipt_before_acknowledging(self):
        delivery=self.service();conn,channel=self.transport()
        def acknowledge(*args,**kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertTrue(delivery.DeliveryReceipt.objects.filter(event=self.event).exists())
            self.assertEqual('delivered',AlertOutbox.objects.get(pk=self.event.pk).state)
        channel.basic_ack.side_effect=acknowledge
        with patch('clinic.delivery.connect',return_value=conn):
            result=delivery.consume_one(self.config())
        self.assertTrue(result['receipt_created'])
        channel.basic_ack.assert_called_once_with(delivery_tag=7)

    def test_receipt_db_failure_does_not_ack_or_mark_delivered(self):
        delivery=self.service();conn,channel=self.transport()
        with patch('clinic.delivery.connect',return_value=conn),patch.object(delivery.DeliveryReceipt.objects,'get_or_create',side_effect=DatabaseError('injected')):
            with self.assertRaises(delivery.DeliveryError) as caught:
                delivery.consume_one(self.config())
        self.assertEqual('PERSISTENCE_UNCONFIRMED',caught.exception.code)
        channel.basic_ack.assert_not_called()
        self.event.refresh_from_db()
        self.assertEqual('pending',self.event.state)
        self.assertFalse(delivery.DeliveryReceipt.objects.exists())

    def test_duplicate_consumption_reuses_receipt_without_handling_alert(self):
        delivery=self.service()
        first=delivery.record_receipt(self.wire(),str(self.event.pk))
        second=delivery.record_receipt(self.wire(),str(self.event.pk))
        self.assertTrue(first['receipt_created']);self.assertFalse(second['receipt_created'])
        self.assertEqual(1,delivery.DeliveryReceipt.objects.count())
        self.assertEqual(2,delivery.DeliveryReceipt.objects.get().received_count)
        self.assertIsNone(Alert.objects.get().handled_at)
        self.assertEqual(1,Alert.objects.count())

    def test_delivered_receipt_wins_over_late_publisher_failure(self):
        delivery=self.service();token=delivery.claim_event(self.event.pk)
        delivery.record_receipt(self.wire(),str(self.event.pk))
        self.assertFalse(delivery.finish_claim(self.event.pk,token,'uncertain'))
        self.assertIsNone(delivery.claim_event(self.event.pk))
        self.assertEqual('delivered',AlertOutbox.objects.get(pk=self.event.pk).state)

    def test_unknown_event_and_mismatched_alert_never_create_receipt(self):
        delivery=self.service()
        for body,message_id in [(self.wire(alert=uuid.uuid4()),str(self.event.pk)),(self.wire(event=uuid.uuid4()),str(uuid.uuid4()))]:
            with self.assertRaises(delivery.InvalidMessage):
                delivery.record_receipt(body,message_id)
        self.assertFalse(delivery.DeliveryReceipt.objects.exists())

    def test_invalid_message_is_recorded_without_retaining_raw_body(self):
        delivery=self.service();conn,channel=self.transport()
        body=json.dumps({'event_ref':str(self.event.pk),'alert_ref':str(self.event.alert_id),'clinical':'PRIVATE_SYNTHETIC_BODY'}).encode()
        channel.basic_get.return_value=(SimpleNamespace(delivery_tag=7),SimpleNamespace(message_id=str(self.event.pk)),body)
        with patch('clinic.delivery.connect',return_value=conn):
            result=delivery.consume_one(self.config())
        self.assertEqual('rejected',result['delivery_state'])
        self.assertEqual(1,delivery.DeliveryRejection.objects.count())
        self.assertNotIn('PRIVATE_SYNTHETIC_BODY',str(list(delivery.DeliveryRejection.objects.values())))
        channel.basic_reject.assert_called_once_with(delivery_tag=7,requeue=False)
        self.assertEqual('pending',AlertOutbox.objects.get(pk=self.event.pk).state)

    def test_concurrent_claims_have_one_current_owner(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections
        delivery=self.service();barrier=Barrier(2)
        def task(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return delivery.claim_event(self.event.pk)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            values=list(pool.map(task,[0,1]))
        self.assertEqual(1,sum(value is not None for value in values))
        self.assertEqual(1,delivery.DeliveryAttempt.objects.count())

    def test_publisher_db_failure_leaves_lease_for_recovery(self):
        delivery=self.service();token=delivery.claim_event(self.event.pk);conn,_=self.transport()
        with self.publishing(conn),patch('clinic.delivery.finish_claim',side_effect=DatabaseError('injected')):
            with self.assertRaises(delivery.DeliveryError):
                delivery.publish_event(self.event.pk,token,self.config())
        self.event.refresh_from_db()
        self.assertEqual(token,self.event.claim_token)
        self.assertEqual('pending',self.event.state)

    def test_inspection_command_has_only_transport_metadata(self):
        output=StringIO()
        call_command('alert_delivery',inspect=str(self.event.pk),stdout=output)
        result=json.loads(output.getvalue())
        self.assertEqual('pending',result['state'])
        self.assertEqual(str(self.event.pk),result['event_ref'])
        self.assertNotIn('diagnosis',result)
        self.assertNotIn('password',result)

    def test_delivery_does_not_prevent_independent_doctor_handling(self):
        delivery=self.service();delivery.record_receipt(self.wire(),str(self.event.pk))
        resolve(self.doctor.pk,str(uuid.uuid4()),{'alert_ref':str(self.event.alert_id),'handling_result':'合成医生处理'},str(self.event.alert_id))
        self.assertIsNotNone(Alert.objects.get().handled_at)
        self.assertEqual('delivered',AlertOutbox.objects.get(pk=self.event.pk).state)
