import uuid
from django.core.exceptions import ValidationError
from django.db import models, transaction
from governance.models import Account, AuditEvent, Candidate, Store


def _lock_projection_records(record_ids, using):
    if record_ids:
        encounters = ClinicalRecord.objects.using(using).filter(pk__in=record_ids).values_list('encounter_id', flat=True)
        list(Encounter.objects.using(using).select_for_update().filter(
            pk__in=list(encounters)).order_by('pk').values_list('pk', flat=True))


class ProjectionSourceQuerySet(models.QuerySet):
    def _projection_records(self):
        return set(self.filter(**getattr(self.model, 'projection_filter', {})).values_list(
            self.model.projection_record_path, flat=True)) - {None}

    def _invalidate_projection(self):
        source = self.filter(**getattr(self.model, 'projection_filter', {}))
        ClinicalExportProjection.objects.using(self.db).filter(record_id__in=source.values_list(
            self.model.projection_record_path, flat=True)).delete()

    def update(self, **kwargs):
        with transaction.atomic(using=self.db):
            ids = list(self.values_list('pk', flat=True))
            selected = self.model.objects.using(self.db).filter(pk__in=ids)
            records = selected._projection_records()
            incoming = kwargs.get('record_id', kwargs.get('record'))
            if incoming is not None and isinstance(getattr(incoming, 'pk', incoming), (str, uuid.UUID)):
                records.add(getattr(incoming, 'pk', incoming))
            _lock_projection_records(records, self.db)
            selected._invalidate_projection()
            count = super().update(**kwargs)
            selected._invalidate_projection()
            return count

    def bulk_create(self, objs, *args, **kwargs):
        with transaction.atomic(using=self.db):
            objs = list(objs)
            records = self.model.objects.using(self.db).filter(pk__in=[row.pk for row in objs])._projection_records()
            records.update(row.record_id for row in objs if getattr(row, 'record_id', None) is not None)
            _lock_projection_records(records, self.db)
            result = super().bulk_create(objs, *args, **kwargs)
            self.model.objects.using(self.db).filter(pk__in=[row.pk for row in result])._invalidate_projection()
            return result

    def delete(self):
        with transaction.atomic(using=self.db):
            _lock_projection_records(self._projection_records(), self.db)
            self._invalidate_projection()
            return super().delete()

    def bulk_update(self, objs, fields, *args, **kwargs):
        with transaction.atomic(using=self.db):
            objs = list(objs)
            records = self.model.objects.using(self.db).filter(pk__in=[row.pk for row in objs])._projection_records()
            records.update(row.record_id for row in objs if getattr(row, 'record_id', None) is not None)
            _lock_projection_records(records, self.db)
            return super().bulk_update(objs, fields, *args, **kwargs)


class ProjectionSource(models.Model):
    """Source maintenance invalidates derived exports in the same transaction."""
    objects = ProjectionSourceQuerySet.as_manager()
    projection_record_path = 'record_id'

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        using = kwargs.get('using') or self._state.db or 'default'
        selected = type(self).objects.using(using).filter(pk=self.pk)
        with transaction.atomic(using=using, savepoint=False):
            related = self._state.fields_cache.get('record')
            token = getattr(related, '_export_lock_token', None)
            blocks = transaction.get_connection(using).atomic_blocks
            already_locked = (token is not None and token[0] == using and token[1] is blocks[0]
                              and token[2] == related.encounter_id)
            if not already_locked:
                records = selected._projection_records() if not self._state.adding else set()
                if getattr(self, 'record_id', None) is not None:
                    records.add(self.record_id)
                _lock_projection_records(records, using)
            if not self._state.adding:
                selected._invalidate_projection()
            result = super().save(*args, **kwargs)
            selected._invalidate_projection()
            return result

    def delete(self, using=None, keep_parents=False):
        using = using or self._state.db or 'default'
        with transaction.atomic(using=using):
            selected = type(self).objects.using(using).filter(pk=self.pk)
            _lock_projection_records(selected._projection_records(), using)
            selected._invalidate_projection()
            return super().delete(using=using, keep_parents=keep_parents)


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
    encounter = models.ForeignKey(Encounter, on_delete=models.PROTECT, db_index=False)
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
    export_generation = models.PositiveBigIntegerField(default=0)


class ClinicalExportProjection(models.Model):
    record = models.OneToOneField(ClinicalRecord, on_delete=models.CASCADE,
                                  primary_key=True, related_name='export_projection')
    source_signature = models.CharField(max_length=64)
    prefix = models.BinaryField()
    prefix_digest = models.CharField(max_length=64)


class ClinicalRevision(ProjectionSource):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    record = models.ForeignKey(ClinicalRecord, on_delete=models.PROTECT, db_index=False)
    version = models.PositiveIntegerField()
    diagnosis = models.TextField()
    examination = models.TextField(blank=True)
    extra_fields = models.JSONField(default=dict)
    actor = models.ForeignKey(Account, on_delete=models.PROTECT)
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['record', 'version'], name='one_clinical_revision')]


class AuditTarget(models.Model):
    event = models.ForeignKey(AuditEvent, on_delete=models.PROTECT, db_index=False)
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


def _derive_hit_stores(rows, using):
    # This is an index key derived from immutable encounter ownership, not authorization.
    stores = dict(ClinicalCheck.objects.using(using).filter(
        pk__in={row.evaluation_id for row in rows}).values_list('pk', 'encounter__store_id'))
    for row in rows:
        store_id = stores[uuid.UUID(str(row.evaluation_id))]
        if row.store_id is not None and uuid.UUID(str(row.store_id)) != store_id:
            raise ValidationError('Guard hit store must match its evaluation encounter.')
        row.store_id = store_id


class GuardHitQuerySet(models.QuerySet):
    def bulk_create(self, objs, batch_size=None, ignore_conflicts=False, update_conflicts=False,
                    update_fields=None, unique_fields=None):
        rows = list(objs)
        if set(update_fields or ()) & {'evaluation', 'evaluation_id', 'store', 'store_id'}:
            raise ValidationError('Change guard hit lineage with source-aware save().')
        with transaction.atomic(using=self.db):
            _derive_hit_stores(rows, self.db)
            return super().bulk_create(rows, batch_size=batch_size, ignore_conflicts=ignore_conflicts,
                update_conflicts=update_conflicts, update_fields=update_fields, unique_fields=unique_fields)

    def update(self, **kwargs):
        if set(kwargs) & {'evaluation', 'evaluation_id', 'store', 'store_id'}:
            raise ValidationError('Change guard hit lineage with source-aware save().')
        return super().update(**kwargs)


class GuardHit(models.Model):
    objects = GuardHitQuerySet.as_manager()
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.PROTECT, db_index=False)
    evaluation = models.ForeignKey(ClinicalCheck, on_delete=models.PROTECT)
    rule_ref = models.CharField(max_length=100)
    rule_version = models.CharField(max_length=100)
    field = models.CharField(max_length=100)
    value = models.JSONField(null=True)

    class Meta:
        indexes = [models.Index(fields=['store', 'rule_ref'], name='guard_hit_store_rule_idx')]

    def save(self, *args, **kwargs):
        using = kwargs.get('using') or self._state.db or 'default'
        with transaction.atomic(using=using):
            _derive_hit_stores([self], using)
            if kwargs.get('update_fields') is not None:
                kwargs['update_fields'] = set(kwargs['update_fields']) | {'store'}
            return super().save(*args, **kwargs)



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
    claim_token = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    next_attempt_at = models.DateTimeField(null=True)
    attempt_count = models.PositiveIntegerField(default=0)
    last_error_code = models.CharField(max_length=40, blank=True)
    publisher_confirmed_at = models.DateTimeField(null=True)

    class Meta:
        indexes = [models.Index(fields=['state','next_attempt_at'])]
        constraints = [models.CheckConstraint(condition=models.Q(state__in=['pending','uncertain','published','delivered']),name='outbox_delivery_state')]


class DeliveryAttempt(models.Model):
    id = models.UUIDField(primary_key=True, editable=False)
    event = models.ForeignKey(AlertOutbox, on_delete=models.PROTECT)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)
    outcome = models.CharField(max_length=20, default='claimed')


class DeliveryReceipt(models.Model):
    event = models.OneToOneField(AlertOutbox, on_delete=models.PROTECT, primary_key=True)
    first_received_at = models.DateTimeField(auto_now_add=True)
    last_received_at = models.DateTimeField(auto_now_add=True)
    received_count = models.PositiveIntegerField(default=1)


class DeliveryRejection(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    normalized_reason = models.CharField(max_length=40)
    body_digest = models.CharField(max_length=64)


class MedicationEntry(ProjectionSource):
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


class Suggestion(ProjectionSource):
    projection_record_path = 'decision__record_id'
    projection_filter = {'decision__outcome': 'adopted'}
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    encounter = models.ForeignKey(Encounter, on_delete=models.PROTECT)
    source_version = models.CharField(max_length=100)
    sample_key = models.CharField(max_length=100)
    source = models.JSONField()
    content = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['encounter', 'source_version', 'sample_key'], name='one_demo_suggestion')]


class SuggestionDecision(ProjectionSource):
    projection_filter = {'outcome': 'adopted'}
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


class WorkDivision(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.PROTECT)
    work_item_ref = models.UUIDField()
    stage_ref = models.CharField(max_length=32)
    current_version = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['store', 'work_item_ref', 'stage_ref'], name='one_work_stage')]


class WorkDivisionRevision(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    division = models.ForeignKey(WorkDivision, on_delete=models.PROTECT, related_name='revisions')
    version = models.PositiveIntegerField()
    plan_version_ref = models.UUIDField(default=uuid.uuid4, unique=True)
    actual_version_ref = models.UUIDField(default=uuid.uuid4, unique=True)
    snapshot = models.JSONField()
    actor = models.ForeignKey(Account, on_delete=models.PROTECT)
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['division', 'version'], name='one_work_revision')]


class WorkDivisionReview(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    revision = models.ForeignKey(WorkDivisionRevision, on_delete=models.PROTECT, related_name='reviews')
    actor = models.ForeignKey(Account, on_delete=models.PROTECT)
    conclusion = models.CharField(max_length=32)
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)


class ExportRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.PROTECT)
    actor = models.ForeignKey(Account, on_delete=models.PROTECT)
    permission = models.CharField(max_length=80)
    scope = models.JSONField()
    purpose = models.TextField(blank=True)
    record_count = models.PositiveIntegerField(default=0)
    row_count = models.PositiveIntegerField(default=0)
    byte_count = models.PositiveIntegerField(default=0)
    state = models.CharField(max_length=16, default='ready')
    created_at = models.DateTimeField(auto_now_add=True)
