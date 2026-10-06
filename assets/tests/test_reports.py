from datetime import timedelta
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from assets.models import Asset, OverdueNotice
from assets.tasks import flag_overdue_checkouts

NOW = timezone.now().replace(microsecond=0)


def test_health_is_public(db):
    r = APIClient().get("/api/v1/health/")
    assert r.status_code == 200 and r.json()["database"] == "ok"


def test_asset_filters_search_and_holder(api, make_asset, make_employee, make_checkout):
    cam = make_asset(category=Asset.Category.CAMERA, name="Canon R6")
    make_asset(category=Asset.Category.LAPTOP)
    emp = make_employee()
    make_checkout(cam, emp, due_at=NOW + timedelta(days=1))

    assert api.get("/api/v1/assets/?category=CAMERA").json()["count"] == 1
    assert api.get("/api/v1/assets/?status=CHECKED_OUT").json()["count"] == 1
    assert api.get("/api/v1/assets/?search=canon").json()["count"] == 1
    holder = api.get(f"/api/v1/assets/{cam.pk}/").json()["current_holder"]
    assert holder == {"employee_code": emp.employee_code, "full_name": emp.full_name}


def test_overdue_report_boundaries_and_order(api, make_asset, make_employee, make_checkout):
    emp = make_employee()
    make_checkout(make_asset(name="due-now"), emp, due_at=NOW)                       # not overdue
    make_checkout(make_asset(name="1s-late"), emp, due_at=NOW - timedelta(seconds=1))  # 0 days
    make_checkout(make_asset(name="3d-late"), make_employee(), due_at=NOW - timedelta(days=3))
    make_checkout(make_asset(name="returned"), make_employee(), due_at=NOW - timedelta(days=5),
                  returned_at=NOW - timedelta(days=1))

    with mock.patch("django.utils.timezone.now", return_value=NOW):
        with CaptureQueriesContext(connection) as ctx:
            rows = api.get("/api/v1/reports/overdue/").json()["results"]

    assert [r["asset_name"] for r in rows] == ["3d-late", "1s-late"]
    assert [r["days_overdue"] for r in rows] == [3, 0]
    assert len(ctx.captured_queries) <= 2  # COUNT + one JOINed SELECT, no N+1


def test_employee_summary(api, make_asset, make_employee, make_checkout):
    emp = make_employee()
    # returned after 2 days and after 4 days -> mean 3.0
    make_checkout(make_asset(), emp, checked_out_at=NOW - timedelta(days=10),
                  due_at=NOW - timedelta(days=5), returned_at=NOW - timedelta(days=8))
    make_checkout(make_asset(), emp, checked_out_at=NOW - timedelta(days=6),
                  due_at=NOW - timedelta(days=1), returned_at=NOW - timedelta(days=2))
    make_checkout(make_asset(), emp, due_at=NOW - timedelta(days=1))  # held, overdue
    make_checkout(make_asset(), emp, due_at=NOW + timedelta(days=1))  # held, not overdue
    make_checkout(make_asset(), make_employee(), due_at=NOW - timedelta(days=1))  # someone else

    with CaptureQueriesContext(connection) as ctx:
        data = api.get(f"/api/v1/employees/{emp.employee_code}/summary/").json()

    assert data["lifetime_checkouts"] == 4
    assert data["currently_held"] == 2
    assert data["currently_overdue"] == 1
    assert data["mean_hold_days"] == 3.0
    assert len(ctx.captured_queries) == 1


def test_employee_summary_404(api, db):
    assert api.get("/api/v1/employees/NOPE/summary/").status_code == 404


def test_flag_overdue_task_is_idempotent(make_asset, make_employee, make_checkout):
    overdue = make_checkout(make_asset(), make_employee(), due_at=NOW - timedelta(days=2))
    make_checkout(make_asset(), make_employee(), due_at=NOW + timedelta(days=2))

    flag_overdue_checkouts()
    flag_overdue_checkouts()

    assert OverdueNotice.objects.count() == 1
    assert OverdueNotice.objects.get().checkout == overdue
