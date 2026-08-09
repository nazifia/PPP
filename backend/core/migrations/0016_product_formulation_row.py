"""Turn Product.formulation from typed text into a row in the formulations table.

The same move migration 0014 made for the dispensing unit: every form already
spelled in the catalogue becomes a row before the old column goes. A spelling
outside the standard list becomes a private row for the supplier that used it,
and an item that named no form at all keeps naming none.
"""

import django.db.models.deletion
from django.db import migrations, models

#: A copy of core.models.FORMULATIONS as it stood here. Frozen on purpose: a
#: migration must keep replaying the same way after the list moves on.
STANDARD_FORMULATIONS = [
    'TABLET', 'CAPSULE', 'SYRUP', 'SUSPENSION', 'SOLUTION', 'INJECTION',
    'INFUSION', 'IVF', 'CREAM', 'OINTMENT', 'GEL', 'LOTION', 'DROPS',
    'INHALER', 'SPRAY', 'POWDER', 'GRANULES', 'PESSARY', 'SUPPOSITORY',
    'PATCH', 'REAGENT', 'CONSUMABLE', 'DEVICE',
]


def to_rows(apps, schema_editor):
    Formulation = apps.get_model('core', 'Formulation')
    Product = apps.get_model('core', 'Product')
    shared = {
        name: Formulation.objects.get_or_create(supplier=None, name=name)[0]
        for name in STANDARD_FORMULATIONS
    }
    for product in Product.objects.all():
        name = (product.legacy_formulation or '').strip().upper()
        if not name:
            continue
        formulation = shared.get(name)
        if formulation is None:
            formulation, _ = Formulation.objects.get_or_create(
                supplier_id=product.supplier_id, name=name,
            )
        Product.objects.filter(pk=product.pk).update(formulation=formulation)


def to_text(apps, schema_editor):
    Product = apps.get_model('core', 'Product')
    for product in Product.objects.select_related('formulation'):
        Product.objects.filter(pk=product.pk).update(
            legacy_formulation='' if product.formulation is None else product.formulation.name,
        )


class Migration(migrations.Migration):

    dependencies = [('core', '0015_catalogue_term')]

    operations = [
        migrations.RenameField(
            model_name='product', old_name='formulation', new_name='legacy_formulation',
        ),
        migrations.AddField(
            model_name='product',
            name='formulation',
            field=models.ForeignKey(
                blank=True,
                help_text='The form it comes in. Picked from the formulations table.',
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='products',
                to='core.formulation',
            ),
        ),
        migrations.RunPython(to_rows, to_text),
        migrations.RemoveField(model_name='product', name='legacy_formulation'),
    ]
