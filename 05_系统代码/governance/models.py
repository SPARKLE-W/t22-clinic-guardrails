import uuid
from django.contrib.auth.hashers import identify_hasher, is_password_usable
from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models


class Store(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=80)


def _reject_password_write(reason, store_id=None):
    AuditEvent.objects.create(store_id=store_id, action='identity.credential.persist',
                              outcome='refused', normalized_reason=reason)
    raise ValueError('Direct credential persistence rejected')


class AccountQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if 'password' in kwargs:
            _reject_password_write('CREDENTIAL_WRITE_BYPASS_REJECTED')
        return super().update(**kwargs)

    def bulk_create(self, objs, **kwargs):
        if objs:
            _reject_password_write('CREDENTIAL_WRITE_BYPASS_REJECTED')
        return super().bulk_create(objs, **kwargs)

    def bulk_update(self, objs, fields, **kwargs):
        if 'password' in fields:
            _reject_password_write('CREDENTIAL_WRITE_BYPASS_REJECTED')
        return super().bulk_update(objs, fields, **kwargs)


class AccountManager(UserManager.from_queryset(AccountQuerySet)):
    pass


class Account(AbstractUser):
    objects = AccountManager()
    store = models.ForeignKey(Store, on_delete=models.PROTECT)
    role = models.CharField(max_length=12, choices=[(x, x) for x in ['frontdesk', 'doctor', 'manager']])

    def save(self, *args, **kwargs):
        if is_password_usable(self.password):
            try:
                identify_hasher(self.password)
            except ValueError:
                store_id = self.store_id if self.store_id and Store.objects.filter(pk=self.store_id).exists() else None
                _reject_password_write('PLAINTEXT_PASSWORD_REJECTED', store_id)
        return super().save(*args, **kwargs)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(role__in=['frontdesk', 'doctor', 'manager']), name='account_three_roles')]


class Demand(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.PROTECT)
    actor = models.ForeignKey(Account, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    reason = models.TextField()
    input_digest = models.CharField(max_length=64)
    definition = models.JSONField(null=True)
    report = models.JSONField()


class Candidate(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    demand = models.OneToOneField(Demand, on_delete=models.PROTECT)
    store = models.ForeignKey(Store, on_delete=models.PROTECT)
    envelope = models.JSONField()
    digest = models.CharField(max_length=64)
    state = models.CharField(max_length=16, default='draft')
    submitted_by = models.ForeignKey(Account, on_delete=models.PROTECT, null=True)
    submitted_at = models.DateTimeField(null=True)
    submit_reason = models.TextField(blank=True)


class Review(models.Model):
    candidate = models.OneToOneField(Candidate, on_delete=models.PROTECT, primary_key=True)
    actor = models.ForeignKey(Account, on_delete=models.PROTECT)
    decision = models.CharField(max_length=8)
    digest = models.CharField(max_length=64)
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)


class PolicyHead(models.Model):
    store = models.OneToOneField(Store, on_delete=models.PROTECT, primary_key=True)
    candidate = models.ForeignKey(Candidate, on_delete=models.PROTECT, null=True)
    generation = models.PositiveIntegerField(default=0)


class Activation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.PROTECT)
    candidate = models.ForeignKey(Candidate, on_delete=models.PROTECT)
    actor = models.ForeignKey(Account, on_delete=models.PROTECT)
    digest = models.CharField(max_length=64)
    generation = models.PositiveIntegerField()
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['store', 'generation'], name='one_policy_generation')]


class Operation(models.Model):
    actor = models.ForeignKey(Account, on_delete=models.PROTECT, db_index=False)
    permission = models.CharField(max_length=80)
    key = models.UUIDField()
    digest = models.CharField(max_length=64)
    result = models.JSONField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['actor', 'permission', 'key'], name='one_account_operation')]


class AuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.PROTECT, null=True)
    actor = models.ForeignKey(Account, on_delete=models.PROTECT, null=True)
    actor_role = models.CharField(max_length=12, blank=True)
    action = models.CharField(max_length=80)
    outcome = models.CharField(max_length=24)
    normalized_reason = models.CharField(max_length=48)
    target = models.UUIDField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['store', 'created_at'])]
