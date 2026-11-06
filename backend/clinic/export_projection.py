"""Transactional derived CSV prefixes; clinical history remains authoritative."""
import hashlib

from django.db import connection, transaction
from governance.models import Candidate
from .models import ClinicalExportProjection, ClinicalRecord, _lock_projection_records
from .services import _template


def source_signature(row, template_digest):
    fields = ('id', 'encounter_id', 'template_id', 'current_version', 'export_generation',
              'encounter__store_id', 'encounter__template_id', 'encounter__doctor_id',
              'encounter__receptionist_id')
    parts = ['clinical-prefix-v1', *(str(row[field]) for field in fields),
             row['encounter__created_at'].isoformat(), template_digest]
    return hashlib.sha256('\0'.join(parts).encode('utf-8')).hexdigest()


def validated_prefix(row, template_digest):
    prefix = row.get('export_projection__prefix')
    if prefix is None or row.get('export_projection__source_signature') != source_signature(row, template_digest):
        return None
    prefix = bytes(prefix)
    if hashlib.sha256(prefix).hexdigest() != row.get('export_projection__prefix_digest'):
        return None
    return prefix


def _write_projections(rows, candidates, templates):
    from .exports import _clinical_rows, _csv_line, _load_histories

    histories = _load_histories([row['id'] for row in rows])
    projections = []
    for row, cells in zip(rows, _clinical_rows(rows, templates, histories)):
        prefix = _csv_line(cells[:11])[:-2]
        projections.append(ClinicalExportProjection(record_id=row['id'], prefix=prefix,
            prefix_digest=hashlib.sha256(prefix).hexdigest(),
            source_signature=source_signature(row, candidates[row['template_id']].digest)))
    options = {'unique_fields': ['record']} if connection.features.supports_update_conflicts_with_target else {}
    ClinicalExportProjection.objects.bulk_create(projections, update_conflicts=True,
        update_fields=['source_signature', 'prefix', 'prefix_digest'], **options)
    return projections


def rebuild_projections(record_ids):
    from .exports import _RECORD_FIELDS

    with transaction.atomic():
        _lock_projection_records(record_ids, connection.alias)
        rows = list(ClinicalRecord.objects.filter(pk__in=record_ids).values(*_RECORD_FIELDS).order_by('pk'))
        candidates = Candidate.objects.in_bulk({row['template_id'] for row in rows})
        templates = {}
        for row in rows:
            template_id = row['template_id']
            if template_id not in templates:
                templates[template_id] = _template(candidates[template_id], row['encounter__store_id'])
        return len(_write_projections(rows, candidates, templates))


def note_encounter_lock(record, encounter):
    """Reuse a lock obtained by _own(..., lock=True), only in this transaction."""
    record._export_lock_token = (connection.alias, connection.atomic_blocks[0], encounter.pk)


def refresh_projection(record, encounter, candidate, template, *, save_version=False):
    """Persist the generation and any diagnosis version already advanced by the caller."""
    if not connection.in_atomic_block:
        raise RuntimeError('Refresh the export projection inside the clinical write transaction')
    record.export_generation += 1
    record.save(update_fields=['current_version', 'export_generation'] if save_version else ['export_generation'])
    row = {'id': record.pk, 'encounter_id': encounter.pk, 'template_id': record.template_id,
           'current_version': record.current_version, 'export_generation': record.export_generation,
           'encounter__store_id': encounter.store_id, 'encounter__template_id': encounter.template_id,
           'encounter__created_at': encounter.created_at, 'encounter__doctor_id': encounter.doctor_id,
           'encounter__receptionist_id': encounter.receptionist_id,
           'encounter__error_mark': encounter.error_mark, 'encounter__rebuilt_id': encounter.rebuilt_id}
    return _write_projections([row], {candidate.pk: candidate}, {candidate.pk: template})[0].prefix
