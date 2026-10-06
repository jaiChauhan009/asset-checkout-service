import threading
from datetime import timedelta

import pytest
from django.db import IntegrityError, connection, connections
from django.utils import timezone
from rest_framework.test import APIClient

from assets.models import Asset, CheckOut

from .conftest import due_in

URL = "/api/v1/checkouts/"


def body(asset, employee, days=2):
    return {"asset_tag": asset.asset_tag, "employee_code": employee.employee_code,
            "due_at": due_in(days)}


def test_requires_auth(db):
    assert APIClient().post(URL, {}, format="json").status_code == 401


def test_checkout_success_sets_asset_checked_out(api, make_asset, make_employee):
    asset, emp = make_asset(), make_employee()
    r = api.post(URL, body(asset, emp), format="json")
    assert r.status_code == 201
    asset.refresh_from_db()
    assert asset.status == Asset.Status.CHECKED_OUT
    assert CheckOut.objects.filter(asset=asset, returned_at__isnull=True).count() == 1


def test_unavailable_asset_409(api, make_asset, make_employee):
    asset = make_asset(status=Asset.Status.MAINTENANCE)
    assert api.post(URL, body(asset, make_employee()), format="json").status_code == 409


def test_inactive_employee_400(api, make_asset, make_employee):
    r = api.post(URL, body(make_asset(), make_employee(is_active=False)), format="json")
    assert r.status_code == 400


@pytest.mark.parametrize("days", [-1, 0, 31])
def test_due_at_bounds_400(api, make_asset, make_employee, days):
    r = api.post(URL, body(make_asset(), make_employee(), days=days), format="json")
    assert r.status_code == 400


def test_unknown_asset_or_employee_404(api, make_asset, make_employee):
    asset, emp = make_asset(), make_employee()
    assert api.post(URL, {**body(asset, emp), "asset_tag": "NOPE"}, format="json").status_code == 404
    assert api.post(URL, {**body(asset, emp), "employee_code": "NOPE"}, format="json").status_code == 404


def test_three_open_checkouts_limit(api, make_asset, make_employee):
    emp = make_employee()
    for _ in range(3):
        assert api.post(URL, body(make_asset(), emp), format="json").status_code == 201
    fourth = make_asset()
    assert api.post(URL, body(fourth, emp), format="json").status_code == 409
    fourth.refresh_from_db()
    assert fourth.status == Asset.Status.AVAILABLE  # rolled back, nothing leaked


def test_returned_items_do_not_count_towards_limit(api, make_asset, make_employee, make_checkout):
    emp = make_employee()
    for _ in range(3):
        make_checkout(make_asset(), emp, due_at=timezone.now() + timedelta(days=1),
                      returned_at=timezone.now())
    assert api.post(URL, body(make_asset(), emp), format="json").status_code == 201


def test_second_checkout_of_same_asset_409(api, make_asset, make_employee):
    asset = make_asset()
    assert api.post(URL, body(asset, make_employee()), format="json").status_code == 201
    assert api.post(URL, body(asset, make_employee()), format="json").status_code == 409


def test_db_rejects_two_open_checkouts_for_one_asset(make_asset, make_employee):
    """The partial unique index is the last line of defence for rule 7."""
    asset = make_asset()
    due = timezone.now() + timedelta(days=1)
    CheckOut.objects.create(asset=asset, employee=make_employee(), due_at=due)
    with pytest.raises(IntegrityError):
        CheckOut.objects.create(asset=asset, employee=make_employee(), due_at=due)


@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="needs real row locking; run against Postgres (docker compose)",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_checkouts_exactly_one_wins(user, make_asset, make_employee):
    asset = make_asset()
    employees = [make_employee(), make_employee()]
    barrier = threading.Barrier(2)
    results = []

    def attempt(emp):
        client = APIClient()
        client.force_authenticate(user)
        barrier.wait()
        try:
            results.append(client.post(URL, body(asset, emp), format="json").status_code)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=attempt, args=(e,)) for e in employees]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [201, 409]
    assert CheckOut.objects.filter(asset=asset).count() == 1


class TestReturn:
    def test_return_sets_available(self, api, make_asset, make_employee, make_checkout):
        co = make_checkout(make_asset(), make_employee(), due_at=timezone.now() + timedelta(days=1))
        r = api.post(f"{URL}{co.pk}/return/", {"condition_note": "ok"}, format="json")
        assert r.status_code == 200
        co.refresh_from_db()
        assert co.returned_at is not None and co.condition_note == "ok"
        assert co.asset.status == Asset.Status.AVAILABLE

    def test_return_needs_maintenance(self, api, make_asset, make_employee, make_checkout):
        co = make_checkout(make_asset(), make_employee(), due_at=timezone.now() + timedelta(days=1))
        api.post(f"{URL}{co.pk}/return/", {"needs_maintenance": True}, format="json")
        co.asset.refresh_from_db()
        assert co.asset.status == Asset.Status.MAINTENANCE

    def test_double_return_409(self, api, make_asset, make_employee, make_checkout):
        co = make_checkout(make_asset(), make_employee(), due_at=timezone.now() + timedelta(days=1))
        assert api.post(f"{URL}{co.pk}/return/", {}, format="json").status_code == 200
        assert api.post(f"{URL}{co.pk}/return/", {}, format="json").status_code == 409

    def test_unknown_checkout_404(self, api, db):
        assert api.post(f"{URL}999/return/", {}, format="json").status_code == 404
