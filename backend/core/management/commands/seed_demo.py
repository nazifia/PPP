"""Create a hospital, two supplier companies and a catalogue to click through.

    python manage.py seed_demo

Everything logs in with the password below. Safe to re-run: it skips what exists.
"""

from django.core.management.base import BaseCommand

from core.models import (
    Department,
    DispensingUnit,
    Formulation,
    OrgCategory,
    OrgKind,
    Organization,
    Partnership,
    Product,
    Role,
    Unit,
    User,
)

PASSWORD = 'Passw0rd!2026'

CATALOGUE = {
    'VALOUR PHARMACEUTICALS LTD': [
        ('Adrenaline Injection', 'Injection', '1mg/ml', 'AMPOULE', '480.00', 250),
        ('10% Dextrose Water', 'IVF', '500ml', 'CARTON', '720.00', 60),
        ('10% Mannitol', 'IVF', '500ml', 'CARTON', '1650.00', 35),
        ('10mls Syringe', 'Consumable', '', 'PACK', '68.00', 400),
    ],
    'DCL LAB PRODUCTS LTD': [
        ('Glucose Reagent Kit', 'Reagent', '4x50ml', 'KIT', '18500.00', 20),
        ('Widal Antigen Set', 'Reagent', '8x5ml', 'KIT', '9400.00', 12),
        ('EDTA Bottles', 'Consumable', '2ml', 'PACK', '1350.00', 90),
    ],
}


class Command(BaseCommand):
    help = 'Seed a demo hospital, supplier companies and catalogue.'

    def handle(self, *args, **options):
        hospital, _ = Organization.objects.get_or_create(
            phone='08010000001',
            defaults={
                'name': 'Federal Teaching Hospital',
                'kind': OrgKind.HOSPITAL,
                'address': 'Hospital Road',
            },
        )
        self._user(hospital, '08010000002', 'Hafsat Saulawa', Role.ADMIN)
        self._user(hospital, '08010000003', 'Store Officer', Role.STAFF)
        units = {
            'PHARMACY': ['A&E', 'NHIA', 'OUTPATIENT', 'INPATIENT', 'THEATRE'],
            'MAIN LABORATORY': ['HAEMATOLOGY', 'CHEMICAL PATHOLOGY', 'MICROBIOLOGY'],
        }
        for name, unit_names in units.items():
            department, _ = Department.objects.get_or_create(organization=hospital, name=name)
            for unit_name in unit_names:
                Unit.objects.get_or_create(department=department, name=unit_name)

        for index, (company, items) in enumerate(CATALOGUE.items(), start=1):
            supplier, _ = Organization.objects.get_or_create(
                phone=f'0802000000{index}',
                defaults={
                    'name': company,
                    'kind': OrgKind.SUPPLIER,
                    'category': OrgCategory.LABORATORY if 'LAB' in company else OrgCategory.PHARMACY,
                },
            )
            Partnership.objects.get_or_create(hospital=hospital, supplier=supplier)
            self._user(supplier, f'0803000000{index}', f'{company.title()} Contact', Role.ADMIN)
            for generic, formulation, strength, unit, price, stock in items:
                Product.objects.get_or_create(
                    supplier=supplier,
                    generic_name=generic,
                    defaults={
                        'formulation': Formulation.objects.get(
                            supplier=None, name=formulation.upper(),
                        ),
                        'strength': strength,
                        'unit': DispensingUnit.objects.get(supplier=None, name=unit),
                        'unit_price': price,
                        'stock_qty': stock,
                    },
                )

        self.stdout.write(self.style.SUCCESS(f'Seeded. Password for every account: {PASSWORD}'))
        self.stdout.write('Hospital admin 08010000002 | Suppliers 08030000001, 08030000002')

    def _user(self, org, phone, full_name, role):
        user = User.objects.filter(phone=phone).first()
        if user:
            return user
        return User.objects.create_user(
            phone=phone, password=PASSWORD, full_name=full_name, organization=org, role=role,
        )
