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
