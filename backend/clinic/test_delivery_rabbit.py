import json
import os
import time
import uuid
from datetime import timedelta
from io import StringIO
from unittest import skipUnless
from unittest.mock import patch
import pika
from django.core.management import call_command
from django.db import DatabaseError
from django.utils import timezone
from .tests import ClinicFixture
from .models import Alert, AlertOutbox, ClinicalRecord
from .writes import diagnosis
from . import delivery
from . import mq_transport
from .mq_test_tools import ConfirmLossProxy


@skipUnless(os.environ.get('T22_MQ_CONFIG'),'Real RabbitMQ configuration not provided; not a passing transport test.')
class RabbitDeliveryTests(ClinicFixture):
    def setUp(self):
        super().setUp()
        self.config={**delivery.load_config(),'queue':'t22.app.tests.'+uuid.uuid4().hex}
        self.ref=self.create()['encounter_ref']
        diagnosis(self.doctor.pk,'record',str(uuid.uuid4()),{},self.ref)
        self.event=AlertOutbox.objects.get()

    def tearDown(self):
        conn=delivery.connect(self.config)
        try:
            conn.channel().queue_delete(queue=self.config['queue'])
        except pika.exceptions.AMQPError:
            pass
        finally:
            delivery.close(conn)
        super().tearDown()

    def publish(self):
        token=delivery.claim_event(self.event.pk)
        self.assertIsNotNone(token)
        result=delivery.publish_event(self.event.pk,token,self.config)
        self.assertEqual('published',result)

    def receive(self):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            item=delivery.consume_one(self.config)
            if item is not None:
                return item
            time.sleep(.05)
        self.fail('Real broker did not supply the persisted event within five seconds.')

    def raw_send(self,body,message_id):
        conn=delivery.connect(self.config)
        try:
            channel=conn.channel();channel.queue_declare(queue=self.config['queue'],durable=True);channel.confirm_delivery()
            channel.basic_publish(exchange='',routing_key=self.config['queue'],mandatory=True,body=body,
                properties=pika.BasicProperties(delivery_mode=2,message_id=message_id))
        finally:
            delivery.close(conn)

    def test_real_guard_outbox_publish_consume_does_not_handle_doctor_alert(self):
        self.publish()
        self.assertEqual('published',AlertOutbox.objects.get().state)
        self.assertFalse(delivery.DeliveryReceipt.objects.exists())
        result=self.receive()
        self.assertTrue(result['receipt_created'])
        self.assertEqual('delivered',AlertOutbox.objects.get().state)
        self.assertIsNone(Alert.objects.get().handled_at)
        self.assertFalse(ClinicalRecord.objects.exists())

    def test_real_duplicate_message_reuses_one_durable_receipt(self):
        self.publish()
        body=json.dumps({'event_ref':str(self.event.pk),'alert_ref':str(self.event.alert_id)}).encode()
        self.raw_send(body,str(self.event.pk))
        first,second=self.receive(),self.receive()
        self.assertTrue(first['receipt_created']);self.assertFalse(second['receipt_created'])
        self.assertEqual(1,delivery.DeliveryReceipt.objects.count())
        self.assertEqual(2,delivery.DeliveryReceipt.objects.get().received_count)
        self.assertEqual(1,Alert.objects.count())

    def test_real_db_failure_requeues_unacked_message(self):
        self.publish()
        with patch.object(delivery.DeliveryReceipt.objects,'get_or_create',side_effect=DatabaseError('injected')):
            with self.assertRaises(delivery.DeliveryError):
                delivery.consume_one(self.config)
        self.assertFalse(delivery.DeliveryReceipt.objects.exists())
        conn=delivery.connect(self.config)
        try:
            method,_,_=conn.channel().basic_get(queue=self.config['queue'],auto_ack=False)
            self.assertIsNotNone(method)
            self.assertTrue(method.redelivered)
        finally:
            delivery.close(conn)
        self.assertTrue(self.receive()['receipt_created'])
        self.assertEqual(1,Alert.objects.count())

    def test_real_ack_loss_keeps_receipt_and_redelivery_is_idempotent(self):
        self.publish()
        with patch('pika.adapters.blocking_connection.BlockingChannel.basic_ack',side_effect=pika.exceptions.AMQPConnectionError()):
            with self.assertRaises(delivery.DeliveryError) as caught:
                delivery.consume_one(self.config)
        self.assertEqual('MQ_ACK_UNCONFIRMED',caught.exception.code)
        self.assertEqual('delivered',AlertOutbox.objects.get().state)
        self.assertFalse(self.receive()['receipt_created'])
        self.assertEqual(2,delivery.DeliveryReceipt.objects.get().received_count)

    def test_real_confirm_then_db_failure_replays_same_event_after_lease(self):
        token=delivery.claim_event(self.event.pk)
        with patch('clinic.delivery.finish_claim',side_effect=DatabaseError('injected')):
            with self.assertRaises(delivery.DeliveryError):
                delivery.publish_event(self.event.pk,token,self.config)
        self.assertEqual('pending',AlertOutbox.objects.get().state)
        AlertOutbox.objects.filter(pk=self.event.pk).update(lease_until=timezone.now()-timedelta(seconds=1))
        self.publish()
        self.receive();self.receive()
        self.assertEqual(2,delivery.DeliveryReceipt.objects.get().received_count)
        self.assertEqual(1,Alert.objects.count())

    def test_real_unroutable_return_retains_recovery_state(self):
        conn=delivery.connect(self.config);channel=conn.channel();declare=channel.queue_declare
        def declare_then_remove(**kwargs):
            result=declare(**kwargs)
            channel.queue_delete(queue=self.config['queue'])
            return result
        channel.queue_declare=declare_then_remove
        token=delivery.claim_event(self.event.pk)
        with patch('clinic.delivery.publish_message',side_effect=mq_transport.publish_one),patch('clinic.mq_transport.connect',return_value=conn),patch.object(conn,'channel',return_value=channel):
            self.assertEqual('unroutable',delivery.publish_event(self.event.pk,token,self.config))
        self.assertEqual('pending',AlertOutbox.objects.get().state)
        self.assertFalse(delivery.DeliveryReceipt.objects.exists())

    def test_real_broker_confirm_dropped_by_proxy_is_not_claimed_delivered(self):
        token=delivery.claim_event(self.event.pk)
        with ConfirmLossProxy(self.config) as proxy:
            result=delivery.publish_event(self.event.pk,token,{**self.config,'port':proxy.port})
            self.assertTrue(proxy.dropped.is_set())
        self.assertEqual('uncertain',result)
        self.assertEqual('uncertain',AlertOutbox.objects.get().state)
        self.assertFalse(delivery.DeliveryReceipt.objects.exists())
        self.assertTrue(self.receive()['receipt_created'])
        self.assertEqual('delivered',AlertOutbox.objects.get().state)

    def test_real_worker_command_completes_one_round(self):
        output=StringIO()
        with patch('clinic.management.commands.alert_delivery.load_config',return_value=self.config):
            call_command('alert_delivery',once=True,batch_size=1,stdout=output)
        result=json.loads(output.getvalue())
        self.assertEqual(1,result['published'])
        self.assertEqual(1,result['received'])
        self.assertEqual('delivered',AlertOutbox.objects.get().state)

    def test_live_connection_without_confirm_has_bounded_publish_deadline(self):
        from concurrent.futures import ThreadPoolExecutor, TimeoutError
        from django.db import close_old_connections, connections
        token=delivery.claim_event(self.event.pk)
        with ConfirmLossProxy(self.config,disconnect_on_confirm=False) as proxy:
            def task():
                close_old_connections()
                try:
                    return delivery.publish_event(self.event.pk,token,{**self.config,'port':proxy.port})
                finally:
                    connections.close_all()
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(task)
                try:
                    result=future.result(timeout=16)
                except TimeoutError:
                    proxy.disconnect()
                    future.result(timeout=5)
                    self.fail('Live connection with missing confirm blocked beyond the publication deadline.')
            self.assertTrue(proxy.dropped.is_set())
        self.assertEqual('uncertain',result)
        self.assertEqual('uncertain',AlertOutbox.objects.get().state)
        self.assertTrue(self.receive()['receipt_created'])
        self.assertEqual('delivered',AlertOutbox.objects.get().state)

    def test_real_invalid_wire_is_rejected_without_clinical_body_storage(self):
        self.raw_send(b'{"clinical":"SYNTHETIC_PRIVATE_TEXT"}',str(uuid.uuid4()))
        self.assertEqual('rejected',self.receive()['delivery_state'])
        self.assertEqual('MESSAGE_INVALID',delivery.DeliveryRejection.objects.get().normalized_reason)
        self.assertNotIn('SYNTHETIC_PRIVATE_TEXT',str(list(delivery.DeliveryRejection.objects.values())))
        self.assertEqual('pending',AlertOutbox.objects.get().state)
        self.assertFalse(delivery.DeliveryReceipt.objects.exists())
