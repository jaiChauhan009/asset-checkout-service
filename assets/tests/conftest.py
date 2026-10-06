from datetime import date, timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APIClient

from assets.models import Asset, CheckOut, Employee


@pytest.fixture
def user(db):
    return User.objects.create_user("tester", password="pw")


@pytest.fixture
def api(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def make_asset(db):
    counter = iter(range(1, 10_000))

    def _make(**kw):
        n = next(counter)
        defaults = dict(
            asset_tag=f"T-{n:03}", name=f"Asset {n}",
            category=Asset.Category.LAPTOP, purchase_date=date(2024, 1, 1),
        )
        return Asset.objects.create(**{**defaults, **kw})
    return _make


@pytest.fixture
def make_employee(db):
    counter = iter(range(1, 10_000))

    def _make(**kw):
        n = next(counter)
        defaults = dict(
            employee_code=f"E{n:03}", full_name=f"Emp {n}", email=f"e{n}@x.com",
        )
        return Employee.objects.create(**{**defaults, **kw})
    return _make


@pytest.fixture
def make_checkout(db):
    """Create a CheckOut with explicit times (bypasses auto_now_add)."""
    def _make(asset, employee, *, checked_out_at=None, due_at, returned_at=None):
        co = CheckOut.objects.create(asset=asset, employee=employee, due_at=due_at)
        CheckOut.objects.filter(pk=co.pk).update(
            checked_out_at=checked_out_at or timezone.now(), returned_at=returned_at
        )
        if returned_at is None:
            Asset.objects.filter(pk=asset.pk).update(status=Asset.Status.CHECKED_OUT)
        co.refresh_from_db()
        return co
    return _make


def due_in(days=2):
    return (timezone.now() + timedelta(days=days)).isoformat()
