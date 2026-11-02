"""Local exclusive broker lifecycle checks; never stop the shared MySQL or Web app."""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

stage=Path(__file__).resolve().parents[1]
code=stage.parents[2]/'backend'
runtime=Path.home()/'.cache/t22-runtime'
output=stage/'原始结果/lifecycle_02.json'
if output.exists():
    raise RuntimeError('Refuse to overwrite evidence')
os.environ['T22_APP_CONFIG']=str(runtime/'private/app.json')
os.environ['T22_MQ_CONFIG']=str(runtime/'private/app-mq.json')
os.environ['DJANGO_SETTINGS_MODULE']='vetapp.settings'
sys.path.insert(0,str(code))
import django
django.setup()
from django.conf import settings
settings.DATABASES['default']['TEST']['NAME']='t22_app_mq_test'
from django.test.runner import DiscoverRunner
from clinic.tests import ClinicFixture
from clinic.models import Alert, AlertOutbox, ClinicalRecord
from clinic.writes import diagnosis
from clinic import delivery

actions=[]


class LifecycleTests(ClinicFixture):
    def setUp(self):
        super().setUp()
        self.config={**delivery.load_config(),'queue':'t22.app.lifecycle.'+uuid.uuid4().hex}
        self.ref=self.create()['encounter_ref']

    def lifecycle(self,action):
        start=time.perf_counter()
        result=subprocess.run(['C:/Python314/python.exe',str(runtime/'scripts/runtime.py'),action],capture_output=True,timeout=60)
        actions.append({'action':action,'exit_code':result.returncode,'seconds':round(time.perf_counter()-start,3)})
        self.assertEqual(0,result.returncode,'Local broker lifecycle command failed; private runtime logs preserved.')

    def create_alert(self):
        result=diagnosis(self.doctor.pk,'record',str(uuid.uuid4()),{},self.ref)
        self.assertFalse(result['ok'])
        self.assertEqual('GUARD_BLOCKED',result['error']['code'])
        return AlertOutbox.objects.get()

    def receive(self):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            result=delivery.consume_one(self.config)
            if result:
                return result
            time.sleep(.05)
        self.fail('Restarted broker did not supply the durable event.')

    def tearDown(self):
        self.lifecycle('start_rabbit')
        conn=delivery.connect(self.config)
        try:
            conn.channel().queue_delete(queue=self.config['queue'])
        finally:
            delivery.close(conn)
        super().tearDown()

    def test_offline_guard_retains_outbox_and_real_recovery_resends(self):
        self.lifecycle('stop_rabbit')
        event=self.create_alert()
        token=delivery.claim_event(event.pk)
        self.assertEqual('unavailable',delivery.publish_event(event.pk,token,self.config))
        event.refresh_from_db()
        self.assertEqual('pending',event.state)
        self.assertFalse(ClinicalRecord.objects.exists())
        self.assertEqual(1,Alert.objects.count())
        self.lifecycle('start_rabbit')
        from django.utils import timezone
        while True:
            delay=(event.next_attempt_at-timezone.now()).total_seconds()
            if delay<=0:
                break
            time.sleep(min(.5,delay))
        token=delivery.claim_event(event.pk)
        self.assertIsNotNone(token)
        self.assertEqual('published',delivery.publish_event(event.pk,token,self.config))
        self.assertTrue(self.receive()['receipt_created'])
        self.assertEqual('delivered',AlertOutbox.objects.get().state)
        self.assertEqual(1,Alert.objects.count())

    def test_persistent_confirmed_message_survives_actual_broker_restart(self):
        event=self.create_alert();token=delivery.claim_event(event.pk)
        self.assertEqual('published',delivery.publish_event(event.pk,token,self.config))
        self.assertFalse(delivery.DeliveryReceipt.objects.exists())
        self.lifecycle('stop_rabbit');self.lifecycle('start_rabbit')
        self.assertTrue(self.receive()['receipt_created'])
        self.assertEqual('delivered',AlertOutbox.objects.get().state)
        self.assertEqual(1,Alert.objects.count())
        self.assertIsNone(Alert.objects.get().handled_at)


if __name__=='__main__':
    failures=DiscoverRunner(verbosity=2,interactive=False).run_tests(['__main__.LifecycleTests'])
    report={'passed':failures==0,'tests':2,'failures':failures,'actions':actions,
            'test_database':'t22_app_mq_test','note':'实际停止/启动本任务Rabbit节点；不停止共享MySQL或前端Web服务；不清空历史队列。'}
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'tests':2}))
    raise SystemExit(0 if report['passed'] else 1)
