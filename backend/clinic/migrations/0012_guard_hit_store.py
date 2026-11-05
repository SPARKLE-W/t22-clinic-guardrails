import django.db.models.deletion
from django.db import migrations, models


def backfill_stores(apps, schema_editor):
    Hit = apps.get_model('clinic', 'GuardHit')
    Store = apps.get_model('governance', 'Store')
    alias = schema_editor.connection.alias
    for store_id in Store.objects.using(alias).values_list('pk', flat=True).iterator():
        Hit.objects.using(alias).filter(evaluation__encounter__store_id=store_id).update(store_id=store_id)


class Migration(migrations.Migration):
    dependencies = [('clinic', '0011_clinical_export_projection')]
    operations = [
        migrations.AddField(model_name='guardhit', name='store',
            field=models.ForeignKey(null=True, db_index=False, on_delete=django.db.models.deletion.PROTECT,
                                    to='governance.store')),
        migrations.RunPython(backfill_stores, migrations.RunPython.noop),
        migrations.AlterField(model_name='guardhit', name='store',
            field=models.ForeignKey(db_index=False, on_delete=django.db.models.deletion.PROTECT,
                                    to='governance.store')),
        migrations.AddIndex(model_name='guardhit',
            index=models.Index(fields=['store', 'rule_ref'], name='guard_hit_store_rule_idx')),
    ]
