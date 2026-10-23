import uuid
from django.db import models
from governance.models import Account, AuditEvent, Candidate, Store


class PetIdentity(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.PROTECT)


class Encounter(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.PROTECT)
    pet = models.ForeignKey(PetIdentity, on_delete=models.PROTECT)
    receptionist = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='received_encounters')
    doctor = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='assigned_encounters')
    template = models.ForeignKey(Candidate, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    species = models.CharField(max_length=100)
    complaint = models.TextField()
    pet_name = models.CharField(max_length=100, blank=True)
    version = models.PositiveIntegerField(default=1)
    error_mark = models.BooleanField(default=False)
    error_reason = models.TextField(blank=True)
    error_actor = models.ForeignKey(Account, on_delete=models.PROTECT, null=True, related_name='+')
    error_at = models.DateTimeField(null=True)
    rebuilt = models.ForeignKey('self', on_delete=models.PROTECT, null=True)

    class Meta:
        indexes = [models.Index(fields=['store', 'receptionist', 'created_at']), models.Index(fields=['store', 'doctor', 'created_at'])]


class ReceptionRevision(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    encounter = models.ForeignKey(Encounter, on_delete=models.PROTECT)
    version = models.PositiveIntegerField()
    species = models.CharField(max_length=100)
    complaint = models.TextField()
    pet_name = models.CharField(max_length=100, blank=True)
    actor = models.ForeignKey(Account, on_delete=models.PROTECT)
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['encounter', 'version'], name='one_reception_revision')]


class ClinicalRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    encounter = models.OneToOneField(Encounter, on_delete=models.PROTECT)
    template = models.ForeignKey(Candidate, on_delete=models.PROTECT)
    current_version = models.PositiveIntegerField(default=1)


class ClinicalRevision(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    record = models.ForeignKey(ClinicalRecord, on_delete=models.PROTECT)
    version = models.PositiveIntegerField()
    diagnosis = models.TextField()
    examination = models.TextField(blank=True)
    actor = models.ForeignKey(Account, on_delete=models.PROTECT)
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['record', 'version'], name='one_clinical_revision')]


class AuditTarget(models.Model):
    event = models.ForeignKey(AuditEvent, on_delete=models.PROTECT)
    target = models.UUIDField(db_index=True)
    kind = models.CharField(max_length=12)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['event', 'target', 'kind'], name='one_event_target')]


class ClinicalCheck(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    encounter = models.ForeignKey(Encounter, on_delete=models.PROTECT)
    record = models.ForeignKey(ClinicalRecord, on_delete=models.PROTECT, null=True)
    original_version = models.ForeignKey(ClinicalRevision, on_delete=models.PROTECT, null=True, related_name='+')
    result_version = models.ForeignKey(ClinicalRevision, on_delete=models.PROTECT, null=True, related_name='+')
    actor = models.ForeignKey(Account, on_delete=models.PROTECT)
    mode = models.CharField(max_length=20)
    template = models.ForeignKey(Candidate, on_delete=models.PROTECT, related_name='+')
    policy = models.ForeignKey(Candidate, on_delete=models.PROTECT, related_name='+')
    policy_digest = models.CharField(max_length=64)
    status = models.CharField(max_length=12)
    checks = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)


class GuardHit(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    evaluation = models.ForeignKey(ClinicalCheck, on_delete=models.PROTECT)
    rule_ref = models.CharField(max_length=100)
    rule_version = models.CharField(max_length=100)
    field = models.CharField(max_length=100)
    value = models.JSONField(null=True)


class Alert(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    hit = models.OneToOneField(GuardHit, on_delete=models.PROTECT)
    handler = models.ForeignKey(Account, on_delete=models.PROTECT, null=True)
    handled_at = models.DateTimeField(null=True)
    handling_result = models.TextField(blank=True)


class AlertOutbox(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    alert = models.OneToOneField(Alert, on_delete=models.PROTECT)
    state = models.CharField(max_length=16, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)


class MedicationEntry(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    record = models.ForeignKey(ClinicalRecord, on_delete=models.PROTECT)
    original = models.ForeignKey('self', null=True, on_delete=models.PROTECT)
    evaluation = models.OneToOneField(ClinicalCheck, on_delete=models.PROTECT)
    drug = models.TextField()
    amount = models.CharField(max_length=256)
    unit = models.CharField(max_length=100)
    actor = models.ForeignKey(Account, on_delete=models.PROTECT)
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Suggestion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    encounter = models.ForeignKey(Encounter, on_delete=models.PROTECT)
    source_version = models.CharField(max_length=100)
    sample_key = models.CharField(max_length=100)
    source = models.JSONField()
    content = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['encounter', 'source_version', 'sample_key'], name='one_demo_suggestion')]


class SuggestionDecision(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    suggestion = models.OneToOneField(Suggestion, on_delete=models.PROTECT, related_name='decision')
    outcome = models.CharField(max_length=12)
    actor = models.ForeignKey(Account, on_delete=models.PROTECT)
    reason = models.TextField(blank=True)
    adopted_content = models.JSONField(default=dict)
    record = models.ForeignKey(ClinicalRecord, on_delete=models.PROTECT, null=True, related_name='suggestion_decisions')
    version = models.ForeignKey(ClinicalRevision, on_delete=models.PROTECT, null=True)
    medication = models.OneToOneField(MedicationEntry, on_delete=models.PROTECT, null=True)
    evaluation = models.OneToOneField(ClinicalCheck, on_delete=models.PROTECT, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(outcome__in=['adopted', 'declined']), name='suggestion_terminal_outcome')]
