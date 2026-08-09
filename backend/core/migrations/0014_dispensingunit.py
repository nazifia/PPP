"""Turn Product.unit from typed text into a row in a dispensing units table.

Every unit already spelled in the catalogue becomes a row before the old column
goes, so no item loses the unit it was sold in. A spelling outside the standard
list becomes a private row for the supplier that used it.
"""

import django.db.models.deletion
from django.db import migrations, models

#: A copy of core.models.DISPENSING_UNITS as it stood here. Frozen on purpose:
#: a migration must keep replaying the same way after the list moves on.
STANDARD_UNITS = [
    'UNIT', 'TABLET', 'CAPSULE', 'SACHET', 'BOTTLE', 'VIAL', 'AMPOULE',
    'SYRINGE', 'SUPPOSITORY', 'TUBE', 'STRIP', 'PACK', 'CARTON', 'BOX',
    'KIT', 'ROLL', 'PAIR', 'PIECE', 'ML', 'L', 'MG', 'G', 'KG', 'DROP',
]


def to_rows(apps, schema_editor):
    DispensingUnit = apps.get_model('core', 'DispensingUnit')
    Product = apps.get_model('core', 'Product')
    shared = {
        name: DispensingUnit.objects.get_or_create(supplier=None, name=name)[0]
        for name in STANDARD_UNITS
    }
    for product in Product.objects.all():
        name = (product.legacy_unit or '').strip().upper() or 'UNIT'
        unit = shared.get(name)
        if unit is None:
            unit, _ = DispensingUnit.objects.get_or_create(
                supplier_id=product.supplier_id, name=name,
            )
        Product.objects.filter(pk=product.pk).update(unit=unit)


def to_text(apps, schema_editor):
    Product = apps.get_model('core', 'Product')
    for product in Product.objects.select_related('unit'):
        Product.objects.filter(pk=product.pk).update(legacy_unit=product.unit.name)


class Migration(migrations.Migration):

    dependencies = [('core', '0013_alter_product_unit')]

    operations = [
        migrations.CreateModel(
            name='DispensingUnit',
            fields=[
                ('id', models.BigAutoField(
                    auto_created=True, primary_key=True, serialize=False, verbose_name='ID',
                )),
                ('name', models.CharField(max_length=40)),
                ('is_active', models.BooleanField(default=True)),
                ('supplier', models.ForeignKey(
                    blank=True,
                    help_text='Blank for a standard unit shared by every company.',
                    null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='dispensing_units',
                    to='core.organization',
                )),
            ],
            options={'ordering': ['name']},
        ),
        migrations.AddConstraint(
            model_name='dispensingunit',
            constraint=models.UniqueConstraint(
                fields=('supplier', 'name'), name='uniq_unit_per_supplier',
            ),
        ),
        migrations.AddConstraint(
            model_name='dispensingunit',
            constraint=models.UniqueConstraint(
                condition=models.Q(('supplier__isnull', True)),
                fields=('name',),
                name='uniq_shared_dispensing_unit',
            ),
        ),
        migrations.RenameField(
            model_name='product', old_name='unit', new_name='legacy_unit',
        ),
        migrations.AddField(
            model_name='product',
            name='unit',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='products',
                to='core.dispensingunit',
            ),
        ),
        migrations.RunPython(to_rows, to_text),
        migrations.AlterField(
            model_name='product',
            name='unit',
            field=models.ForeignKey(
                help_text='How the item is dispensed. Picked from the units table.',
                on_delete=django.db.models.deletion.PROTECT,
                related_name='products',
                to='core.dispensingunit',
            ),
        ),
        migrations.RemoveField(model_name='product', name='legacy_unit'),
    ]
