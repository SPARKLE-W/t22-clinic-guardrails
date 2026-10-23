"""Durable outbox transport. Publication, receipt and doctor handling are separate."""
import hashlib
import json
import os
import uuid
from datetime import timedelta
from pathlib import Path
import pika
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.utils import timezone
from governance.services import DomainError, _uuid
from vetcontrols.compiler import parse_text
from .models import AlertOutbox, DeliveryAttempt, DeliveryReceipt, DeliveryRejection
from .mq_transport import connect, close, publish_message


class DeliveryError(Exception):
    def __init__(self,code):
        self.code=code
        super().__init__(code)


class InvalidMessage(DeliveryError):
    pass


def load_config():
    try:
        config=json.loads(Path(os.environ['T22_MQ_CONFIG']).read_text(encoding='utf-8-sig'))
        required={'host','port','virtual_host','user','password','queue'}
        valid=type(config) is dict and set(config)==required
        valid=valid and all(type(config[key]) is str and config[key] for key in required-{'port'})
        valid=valid and config['host'] in ['127.0.0.1','localhost'] and type(config['port']) is int and 1<=config['port']<=65535
        valid=valid and config['queue'].startswith('t22.app.') and len(config['queue'])<=200
        if not valid:
            raise ValueError()
        return config
    except (KeyError,OSError,ValueError,TypeError):
        raise DeliveryError('MQ_CONFIG_UNAVAILABLE') from None


def _delivered(row):
    row.state,row.claim_token,row.lease_until,row.next_attempt_at='delivered',None,None,None
    row.last_error_code=''
    row.save(update_fields=['state','claim_token','lease_until','next_attempt_at','last_error_code'])


def claim_event(event_id):
    with transaction.atomic():
        row=AlertOutbox.objects.select_for_update().get(pk=event_id)
        if DeliveryReceipt.objects.filter(event=row).exists():
            _delivered(row)
            return None
        now=timezone.now()
        if row.state=='delivered' or (row.claim_token and row.lease_until and row.lease_until>now) or (row.next_attempt_at and row.next_attempt_at>now):
            return None
        token=uuid.uuid4()
        row.claim_token,row.lease_until=token,now+timedelta(seconds=30)
        row.attempt_count+=1
        row.save(update_fields=['claim_token','lease_until','attempt_count'])
        DeliveryAttempt.objects.create(pk=token,event=row)
        return token


def finish_claim(event_id,token,outcome):
    with transaction.atomic():
        row=AlertOutbox.objects.select_for_update().get(pk=event_id)
        now=timezone.now()
        DeliveryAttempt.objects.filter(pk=token,event=row,finished_at__isnull=True).update(finished_at=now,outcome=outcome)
        if DeliveryReceipt.objects.filter(event=row).exists():
            _delivered(row)
            return False
        if row.state=='delivered' or row.claim_token!=token or not row.lease_until or row.lease_until<=now:
            return False
        row.state='published' if outcome=='published' else 'uncertain' if outcome=='uncertain' else 'pending'
        row.claim_token,row.lease_until=None,None
        row.last_error_code='' if outcome=='published' else outcome.upper()
        row.next_attempt_at=now+timedelta(seconds=30 if outcome=='published' else min(60,5*2**min(row.attempt_count-1,4)))
        if outcome=='published':
            row.publisher_confirmed_at=now
        row.save(update_fields=['state','claim_token','lease_until','last_error_code','next_attempt_at','publisher_confirmed_at'])
        return True


def publish_event(event_id,token,config):
    row=AlertOutbox.objects.filter(pk=event_id,claim_token=token,lease_until__gt=timezone.now()).exclude(state='delivered').first()
    if not row or token is None:
        return 'lease_lost'
    body=json.dumps({'event_ref':str(row.pk),'alert_ref':str(row.alert_id)}).encode()
    outcome=publish_message(config,body,str(row.pk))
    try:
        updated=finish_claim(event_id,token,outcome)
    except DatabaseError:
        raise DeliveryError('PERSISTENCE_UNCONFIRMED') from None
    return outcome if updated or DeliveryReceipt.objects.filter(event_id=event_id).exists() else 'lease_lost'


def record_receipt(body,message_id):
    try:
        if len(body)>512:
            raise ValueError()
        payload=parse_text(body)
        if type(payload) is not dict or set(payload)!={'event_ref','alert_ref'}:
            raise ValueError()
        event_ref,alert_ref=_uuid(payload['event_ref']),_uuid(payload['alert_ref'])
        if _uuid(message_id)!=event_ref:
            raise ValueError()
    except (ValueError,TypeError,DomainError,UnicodeError,RecursionError,OverflowError):
        raise InvalidMessage('MESSAGE_INVALID') from None
    with transaction.atomic():
        row=AlertOutbox.objects.select_for_update().filter(pk=event_ref).first()
        if not row:
            raise InvalidMessage('EVENT_UNKNOWN')
        if row.alert_id!=alert_ref:
            raise InvalidMessage('ALERT_MISMATCH')
        receipt,created=DeliveryReceipt.objects.get_or_create(event=row)
        if not created:
            receipt.received_count+=1
            receipt.last_received_at=timezone.now()
            receipt.save(update_fields=['received_count','last_received_at'])
        _delivered(row)
        return {'event_ref':str(row.pk),'delivery_state':'delivered','receipt_created':created}


def consume_one(config):
    connection=None
    committed=False
    try:
        connection=connect(config)
        channel=connection.channel()
        channel.queue_declare(queue=config['queue'],durable=True)
        method,properties,body=channel.basic_get(queue=config['queue'],auto_ack=False)
        if method is None:
            return None
        try:
            result=record_receipt(body,properties.message_id)
        except InvalidMessage as error:
            DeliveryRejection.objects.create(normalized_reason=error.code,body_digest=hashlib.sha256(body).hexdigest())
            committed=True
            channel.basic_reject(delivery_tag=method.delivery_tag,requeue=False)
            return {'delivery_state':'rejected','reason':error.code}
        committed=True
        channel.basic_ack(delivery_tag=method.delivery_tag)
        return result
    except DatabaseError:
        raise DeliveryError('PERSISTENCE_UNCONFIRMED') from None
    except (pika.exceptions.AMQPError,OSError):
        raise DeliveryError('MQ_ACK_UNCONFIRMED' if committed else 'MQ_UNAVAILABLE') from None
    finally:
        close(connection)


def inspect_event(ref):
    try:
        event_id=_uuid(ref)
    except DomainError:
        raise DeliveryError('INVALID_REFERENCE') from None
    row=AlertOutbox.objects.filter(pk=event_id).first()
    if not row:
        raise DeliveryError('EVENT_UNKNOWN')
    receipt=DeliveryReceipt.objects.filter(event=row).first()
    return {'event_ref':str(row.pk),'alert_ref':str(row.alert_id),'state':row.state,'attempt_count':row.attempt_count,
        'lease_until':row.lease_until.isoformat() if row.lease_until else None,
        'next_attempt_at':row.next_attempt_at.isoformat() if row.next_attempt_at else None,
        'publisher_confirmed_at':row.publisher_confirmed_at.isoformat() if row.publisher_confirmed_at else None,
        'last_error_code':row.last_error_code,'first_received_at':receipt.first_received_at.isoformat() if receipt else None,
        'last_received_at':receipt.last_received_at.isoformat() if receipt else None,'received_count':receipt.received_count if receipt else 0}


def run_round(config,phase='both',batch_size=100):
    result={'published':0,'received':0,'rejected':0,'deferred':0}
    if phase in ['both','publish']:
        now=timezone.now()
        rows=AlertOutbox.objects.exclude(state='delivered').filter(Q(next_attempt_at__isnull=True)|Q(next_attempt_at__lte=now))
        rows=rows.filter(Q(claim_token__isnull=True)|Q(lease_until__lte=now)).order_by('created_at','id')
        for event_id in list(rows.values_list('id',flat=True)[:batch_size]):
            token=claim_event(event_id)
            if token:
                outcome=publish_event(event_id,token,config)
                result['published' if outcome=='published' else 'deferred']+=1
    if phase in ['both','consume']:
        for _ in range(batch_size):
            item=consume_one(config)
            if item is None:
                break
            result['received' if item['delivery_state']=='delivered' else 'rejected']+=1
    return result
