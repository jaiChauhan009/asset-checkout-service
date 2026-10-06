from celery import shared_task
from django.utils import timezone

from .models import CheckOut, OverdueNotice

BATCH_SIZE = 1000


@shared_task
def flag_overdue_checkouts():
    """Create today's OverdueNotice for every open, overdue check-out.

    Idempotent: the unique (checkout, notice_date) constraint plus
    ignore_conflicts means re-runs (or Celery retries) insert nothing new.
    """
    now = timezone.now()
    today = timezone.localdate(now)
    before = OverdueNotice.objects.filter(notice_date=today).count()

    overdue_ids = (
        CheckOut.objects.filter(returned_at__isnull=True, due_at__lt=now)
        .values_list("id", flat=True)
        .iterator(chunk_size=BATCH_SIZE)
    )
    batch = []
    for checkout_id in overdue_ids:
        batch.append(OverdueNotice(checkout_id=checkout_id, notice_date=today))
        if len(batch) >= BATCH_SIZE:
            OverdueNotice.objects.bulk_create(batch, ignore_conflicts=True)
            batch = []
    if batch:
        OverdueNotice.objects.bulk_create(batch, ignore_conflicts=True)

    created = OverdueNotice.objects.filter(notice_date=today).count() - before
    return f"created {created} notices for {today}"
