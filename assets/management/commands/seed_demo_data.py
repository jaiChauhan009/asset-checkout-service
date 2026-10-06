from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from rest_framework.authtoken.models import Token

from assets.models import Asset, CheckOut, Employee

ASSETS = [
    ("CAM-001", "Canon EOS R6", "CAMERA"),
    ("CAM-002", "Sony A7 IV", "CAMERA"),
    ("LAP-001", "ThinkPad X1 Carbon", "LAPTOP"),
    ("LAP-002", "MacBook Pro 14", "LAPTOP"),
    ("SEN-001", "Air Quality Sensor", "SENSOR"),
    ("SEN-002", "Vibration Sensor", "SENSOR"),
    ("VEH-001", "Mahindra Bolero", "VEHICLE"),
    ("VEH-002", "Tata Ace", "VEHICLE"),
]

EMPLOYEES = [
    ("E001", "Asha Verma", True),
    ("E002", "Rohan Mehta", True),
    ("E003", "Priya Nair", True),
    ("E004", "Karan Singh", True),
    ("E005", "Old Contractor", False),  # inactive
]

# (asset_tag, employee_code, checked_out_days_ago, due_days_from_now, returned_days_ago)
CHECKOUTS = [
    ("CAM-001", "E001", 10, -3, None),   # open, overdue 3 days
    ("LAP-001", "E002", 20, -7, None),   # open, overdue 7 days
    ("SEN-001", "E003", 1, 5, None),     # open, not yet due
    ("LAP-002", "E001", 30, -20, 22),    # returned on time
    ("SEN-002", "E002", 15, -8, 9),      # returned on time
    ("VEH-001", "E003", 25, -18, 14),    # returned 4 days late
]


class Command(BaseCommand):
    help = "Populate demo data. Safe to re-run: it resets the demo rows it owns."

    @transaction.atomic
    def handle(self, *args, **options):
        now = timezone.now()

        assets = {}
        for tag, name, category in ASSETS:
            assets[tag], _ = Asset.objects.update_or_create(
                asset_tag=tag,
                defaults={
                    "name": name,
                    "category": category,
                    "status": Asset.Status.AVAILABLE,
                    "purchase_date": date(2024, 1, 15),
                },
            )
        Asset.objects.filter(asset_tag="VEH-002").update(status=Asset.Status.MAINTENANCE)

        employees = {}
        for code, name, active in EMPLOYEES:
            employees[code], _ = Employee.objects.update_or_create(
                employee_code=code,
                defaults={
                    "full_name": name,
                    "email": f"{code.lower()}@example.com",
                    "is_active": active,
                },
            )

        # Rebuild demo check-outs so re-runs don't pile up duplicates.
        CheckOut.objects.filter(asset__asset_tag__in=assets).delete()
        for tag, code, out_ago, due_in, ret_ago in CHECKOUTS:
            co = CheckOut.objects.create(
                asset=assets[tag], employee=employees[code], due_at=now + timedelta(days=due_in)
            )
            # checked_out_at is auto_now_add, so backdate with an UPDATE.
            CheckOut.objects.filter(pk=co.pk).update(
                checked_out_at=now - timedelta(days=out_ago),
                returned_at=(now - timedelta(days=ret_ago)) if ret_ago is not None else None,
                condition_note="good" if ret_ago is not None else "",
            )
            if ret_ago is None:
                Asset.objects.filter(pk=assets[tag].pk).update(status=Asset.Status.CHECKED_OUT)

        User = get_user_model()
        user, created = User.objects.get_or_create(
            username="admin", defaults={"is_staff": True, "is_superuser": True}
        )
        if created:
            user.set_password("admin")
            user.save()
        token, _ = Token.objects.get_or_create(user=user)

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {len(ASSETS)} assets, {len(EMPLOYEES)} employees, "
            f"{len(CHECKOUTS)} check-outs."
        ))
        self.stdout.write(f"Admin login: admin / admin   API token: {token.key}")
