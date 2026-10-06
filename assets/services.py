"""Check-out / return business rules. Views stay thin; all rules live here."""
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, NotFound, ValidationError

from .models import Asset, CheckOut, Employee

MAX_OPEN_CHECKOUTS = 3
MAX_LOAN_DAYS = 30


class Conflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "Conflict."
    default_code = "conflict"


def check_out(*, asset_tag, employee_code, due_at):
    # Rule 8: unknown references are 404, never 500.
    asset = Asset.objects.filter(asset_tag=asset_tag).first()
    if asset is None:
        raise NotFound(f"Asset '{asset_tag}' not found.")
    employee = Employee.objects.filter(employee_code=employee_code).first()
    if employee is None:
        raise NotFound(f"Employee '{employee_code}' not found.")

    # Rule 2
    if not employee.is_active:
        raise ValidationError({"employee_code": "Employee is inactive."})

    # Rule 4
    now = timezone.now()
    if due_at <= now:
        raise ValidationError({"due_at": "Must be in the future."})
    if due_at > now + timedelta(days=MAX_LOAN_DAYS):
        raise ValidationError({"due_at": f"Must be within {MAX_LOAN_DAYS} days."})

    # Rule 5: everything below commits or rolls back together.
    with transaction.atomic():
        # Rule 3: lock the employee row so two parallel requests by the same
        # employee cannot both see "2 open" and both succeed.
        Employee.objects.select_for_update().get(pk=employee.pk)
        open_count = CheckOut.objects.filter(
            employee=employee, returned_at__isnull=True
        ).count()
        if open_count >= MAX_OPEN_CHECKOUTS:
            raise Conflict(f"Employee already holds {MAX_OPEN_CHECKOUTS} items.")

        # Rules 1 and 7: a conditional UPDATE is atomic in the database. Two
        # concurrent requests both try it; the row lock makes the second one
        # wait, re-check status, match 0 rows and get a 409.
        claimed = Asset.objects.filter(
            pk=asset.pk, status=Asset.Status.AVAILABLE
        ).update(status=Asset.Status.CHECKED_OUT, updated_at=now)
        if not claimed:
            raise Conflict("Asset is not available.")

        try:
            # Savepoint so the IntegrityError doesn't poison the outer tx
            # before we raise; the outer atomic then rolls back the UPDATE.
            with transaction.atomic():
                return CheckOut.objects.create(
                    asset=asset, employee=employee, due_at=due_at
                )
        except IntegrityError:
            # Partial unique index (one open check-out per asset) backstop.
            raise Conflict("Asset is not available.")


def return_checkout(*, checkout_id, condition_note="", needs_maintenance=False):
    with transaction.atomic():
        checkout = (
            CheckOut.objects.select_for_update().filter(pk=checkout_id).first()
        )
        if checkout is None:
            raise NotFound("Check-out not found.")
        if checkout.returned_at is not None:
            raise Conflict("Check-out already returned.")

        checkout.returned_at = timezone.now()
        checkout.condition_note = condition_note
        checkout.save(update_fields=["returned_at", "condition_note"])

        new_status = (
            Asset.Status.MAINTENANCE if needs_maintenance else Asset.Status.AVAILABLE
        )
        Asset.objects.filter(pk=checkout.asset_id).update(
            status=new_status, updated_at=checkout.returned_at
        )
        return checkout
