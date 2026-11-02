"""Minimal experiment schema; not the complete product data model."""
import uuid
from django.conf import settings
from django.db import models


class Store(models.Model):
    code = models.CharField(max_length=64, unique=True)


class Profile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    store = models.ForeignKey(Store, on_delete=models.PROTECT)
    role = models.CharField(max_length=16)


class PolicyHead(models.Model):
    store = models.OneToOneField(Store, on_delete=models.CASCADE)
    version = models.PositiveIntegerField(default=1)
    source = models.CharField(max_length=40, default='PRE_FIXTURE_NOT_APPROVED')


class Encounter(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.PROTECT)
    receptionist = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    doctor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    pet_id = models.CharField(max_length=64)
    species = models.CharField(max_length=32, default='dog')
    complaint = models.TextField(default='PRE fixture')
    pet_name = models.CharField(max_length=64, blank=True)
    misregistered = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['store','doctor']),models.Index(fields=['store','receptionist'])]


class Record(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    encounter = models.OneToOneField(Encounter, on_delete=models.PROTECT)
    diagnosis = models.TextField()
    examination = models.TextField(blank=True)
    adopted_text = models.TextField(blank=True)
    version = models.PositiveIntegerField(default=1)
    template_version = models.CharField(max_length=32, default='PRE-template-1')


class Revision(models.Model):
    record = models.ForeignKey(Record, on_delete=models.PROTECT)
    version = models.PositiveIntegerField()
    content = models.JSONField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['record','version'], name='pre_record_version')]


class Medication(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    record = models.ForeignKey(Record, on_delete=models.PROTECT)
    original = models.ForeignKey('self', null=True, on_delete=models.PROTECT)
    drug = models.TextField()
    amount = models.CharField(max_length=64)
    unit = models.CharField(max_length=64)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Suggestion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    encounter = models.ForeignKey(Encounter, on_delete=models.PROTECT)
    text = models.TextField(default='PRE synthetic suggestion')
    source = models.CharField(max_length=32, default='PRE_FIXTURE')


class Decision(models.Model):
    suggestion = models.OneToOneField(Suggestion, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    state = models.CharField(max_length=16)
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Operation(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    permission = models.CharField(max_length=80)
    operation_key = models.CharField(max_length=64)
    fingerprint = models.CharField(max_length=64)
    outcome = models.JSONField(default=dict)
    status = models.IntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['actor','permission','operation_key'], name='pre_operation_once')]


class Audit(models.Model):
    event_store = models.ForeignKey(Store, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    role = models.CharField(max_length=16)
    action = models.CharField(max_length=64)
    result = models.CharField(max_length=32)
    reason_code = models.CharField(max_length=40)
    target_ref = models.CharField(max_length=64)
    private_details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['event_store','actor','created_at'])]


class AuditTarget(models.Model):
    audit = models.ForeignKey(Audit, on_delete=models.CASCADE)
    record = models.ForeignKey(Record, on_delete=models.PROTECT)


class Alert(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    encounter = models.ForeignKey(Encounter, on_delete=models.PROTECT)
    attempt = models.ForeignKey(Operation, on_delete=models.PROTECT)
    rule_id = models.CharField(max_length=32)
    rule_version = models.PositiveIntegerField(default=1)
    field = models.CharField(max_length=64)
    handled = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['attempt','rule_id','rule_version','field'], name='pre_hit_once')]


class Outbox(models.Model):
    event_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    alert = models.ForeignKey(Alert, on_delete=models.PROTECT)
    state = models.CharField(max_length=16, default='pending')
    claim_token = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)


class Receipt(models.Model):
    event_id = models.UUIDField(primary_key=True)
    received_at = models.DateTimeField(auto_now_add=True)
