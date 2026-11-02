"""Exercise durable outbox recovery on an isolated synthetic database."""
import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['prepare', 'down', 'recover'])
    parser.add_argument('--event-ref')
    args = parser.parse_args()
    if os.environ.get('T22_FORMAL_LOAD') != '1':
        parser.error('Isolated formal-load mode is required')
    sys.path.insert(0, str(Path(__file__).resolve().parents[4] / 'backend'))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'vetapp.settings')
    import django
    django.setup()
    from django.conf import settings
    from django.utils import timezone
    from clinic import delivery, mq_transport
    from clinic.models import Alert, AlertOutbox, ClinicalCheck, DeliveryReceipt, GuardHit

    if not settings.DATABASES['default']['NAME'].startswith('t22_perf_'):
        parser.error('Only an isolated synthetic performance database is allowed')
    if args.phase == 'prepare':
        check = ClinicalCheck.objects.order_by('created_at', 'id').first()
        if not check:
            parser.error('Synthetic clinical check is missing')
        hit = GuardHit.objects.create(evaluation=check, rule_ref='synthetic.mq_recovery',
                                      rule_version='test-1', field='diagnosis', value=None)
        alert = Alert.objects.create(hit=hit)
        event = AlertOutbox.objects.create(alert=alert)
        print(json.dumps({'event_ref': str(event.pk), 'initial_state': event.state}))
        return

    if not args.event_ref:
        parser.error('--event-ref is required')
    event_ref = uuid.UUID(args.event_ref)
    event = AlertOutbox.objects.get(pk=event_ref)
    if event.alert.hit.rule_ref != 'synthetic.mq_recovery':
        parser.error('Event was not created by this synthetic probe')
    config = {**delivery.load_config(), 'queue': 't22.app.recovery.' + event_ref.hex}
    if args.phase == 'down':
        token = delivery.claim_event(event_ref)
        if token is None:
            raise RuntimeError('Outbox claim was unavailable')
        result = delivery.publish_event(event_ref, token, config)
        event.refresh_from_db()
        if result != 'unavailable' or event.state != 'pending' or DeliveryReceipt.objects.filter(event=event).exists():
            raise RuntimeError('Broker outage was not retained as a pending outbox event')
        print(json.dumps({'event_ref': str(event_ref), 'publish_result': result,
                          'outbox_state': event.state, 'receipt_count': 0}))
        return

    event.refresh_from_db()
    if event.next_attempt_at and event.next_attempt_at > timezone.now():
        time.sleep((event.next_attempt_at - timezone.now()).total_seconds() + 0.1)
    token = delivery.claim_event(event_ref)
    if token is None or delivery.publish_event(event_ref, token, config) != 'published':
        raise RuntimeError('Outbox was not published after broker recovery')
    first = delivery.consume_one(config)
    body = json.dumps({'event_ref': str(event_ref), 'alert_ref': str(event.alert_id)}).encode()
    if mq_transport.publish_one(config, body, str(event_ref)) != 'published':
        raise RuntimeError('Synthetic duplicate was not published')
    second = delivery.consume_one(config)
    event.refresh_from_db()
    receipt = DeliveryReceipt.objects.get(event=event)
    if not first['receipt_created'] or second['receipt_created'] or receipt.received_count != 2:
        raise RuntimeError('Redelivery created a second receipt or changed the first result')
    if event.state != 'delivered' or event.alert.handled_at is not None:
        raise RuntimeError('Delivery state was confused with clinical alert handling')
    print(json.dumps({'event_ref': str(event_ref), 'outbox_state': event.state,
                      'durable_receipts': DeliveryReceipt.objects.filter(event=event).count(),
                      'message_receives': receipt.received_count, 'doctor_handled': False}))


if __name__ == '__main__':
    main()
