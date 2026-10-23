import json
import time
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, close_old_connections
from clinic.delivery import DeliveryError, inspect_event, load_config, run_round


class Command(BaseCommand):
    help='Publish/consume durable guard events, or inspect one opaque transport event. No clinical content.'

    def add_arguments(self,parser):
        parser.add_argument('--once',action='store_true')
        parser.add_argument('--phase',choices=['both','publish','consume'],default='both')
        parser.add_argument('--batch-size',type=int,default=100)
        parser.add_argument('--interval',type=int,default=5)
        parser.add_argument('--inspect')

    def handle(self,*args,**options):
        if not 1<=options['batch_size']<=100 or not 1<=options['interval']<=60:
            raise CommandError('INPUT_INVALID')
        try:
            if options['inspect']:
                self.stdout.write(json.dumps(inspect_event(options['inspect'])))
                return
            config=load_config()
        except DeliveryError as error:
            raise CommandError(error.code) from None
        except DatabaseError:
            raise CommandError('PERSISTENCE_UNCONFIRMED') from None
        try:
            while True:
                close_old_connections()
                try:
                    result=run_round(config,options['phase'],options['batch_size'])
                    self.stdout.write(json.dumps(result))
                except (DeliveryError,DatabaseError) as error:
                    code=error.code if isinstance(error,DeliveryError) else 'PERSISTENCE_UNCONFIRMED'
                    if options['once']:
                        raise CommandError(code) from None
                    self.stderr.write(json.dumps({'error':code}))
                if options['once']:
                    return
                time.sleep(options['interval'])
        except KeyboardInterrupt:
            return
