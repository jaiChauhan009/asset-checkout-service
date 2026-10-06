# ANSWERS

---

## Part B — Diagnosing the broken snippets

### Snippet 1 — overdue report view

**1. What is wrong**

1. **N+1 queries.** `c.asset` and `c.employee` each fire a lazy query per row, so each row costs 2 extra queries. With 5,000 open items that is about 10,000 queries.
2. **Filtering happens in Python.** It loads *every* open check-out, including ones that are not overdue, and then throws most of them away. The `due_at < now` check belongs in SQL.
3. **Sorting happens in Python on a rounded value.** Sorting by `days_overdue` (whole days) leaves items with the same day count in random order. Sorting by `due_at` in SQL is exact and can use an index.
4. **No pagination.** The response size and memory use are unbounded.
5. **`timezone.now()` is called several times per row.** Rows near the boundary can be judged against different "now" values, and `days_overdue` can disagree with the filter decision.
6. **No authentication.** It is a plain Django view, so DRF's `IsAuthenticated` never applies. Anyone can read employee names.
7. **Missing fields.** `employee_code` is not in the output, although the spec requires it.

**2. Why it looks correct locally.** The local database has about 10 rows, so 20 extra queries take a few milliseconds. Nobody counts queries unless they use the debug toolbar. Ties in day counts are rare with tiny data. The developer is logged in, or just never tests the endpoint while logged out.

**3. Fix**

```python
class OverdueReportView(generics.ListAPIView):           # DRF: auth + pagination
    serializer_class = OverdueRowSerializer               # adds employee_code, days_overdue

    def list(self, request, *a, **kw):
        self.now = timezone.now()                         # one "now" per request
        return super().list(request, *a, **kw)

    def get_queryset(self):
        return (CheckOut.objects
                .filter(returned_at__isnull=True, due_at__lt=self.now)   # filter in SQL
                .select_related("asset", "employee")                     # one JOIN, no N+1
                .order_by("due_at", "id"))                               # exact, stable order

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "now": self.now}
```

**4. What would have caught it.** A test that creates 20 rows and uses `assertNumQueries` / `CaptureQueriesContext` (as `test_overdue_report_boundaries_and_order` in this repo does), the `nplusone` package or django-debug-toolbar during development, and a test that calls the endpoint without credentials and expects 401.

### Snippet 2 — check-out endpoint

**1. What is wrong**

1. **Race condition (check-then-act).** Two requests can both read `status == AVAILABLE` and both create a CheckOut. Nothing locks the row.
2. **No transaction.** `CheckOut.objects.create` commits, and only then does `asset.save()` run. If the save fails (crash, timeout, deploy restart), a check-out row exists next to an AVAILABLE asset, which is exactly what rule 5 forbids.
3. **`asset.save()` writes every column.** It overwrites concurrent changes to the asset with a stale copy (a lost update). It should use `update_fields` or a conditional `UPDATE`.
4. **The 3-item limit is also racy.** Two parallel requests from the same employee both count 2 and both succeed, leaving the employee with 4 items.
5. **Unknown asset or employee gives 500.** `.get()` raises `DoesNotExist`, and a missing key in `request.data` raises `KeyError`.
6. **No inactive-employee check** (rule 2).
7. **No validation of `due_at`.** Past dates and dates more than 30 days ahead get through (rule 4). An unparseable string becomes a database or validation error and returns 500. A naive datetime gets stored with a warning.

**2. Why it looks correct locally.** Manual testing is sequential, so one request finishes before the next starts and the race window (a few milliseconds) is never hit. The database never fails between the two writes on a laptop. Testers use valid tags and active employees.

**3. Fix** (this is what `assets/services.py` does)

```python
def check_out(*, asset_tag, employee_code, due_at):        # due_at parsed by a serializer
    asset = Asset.objects.filter(asset_tag=asset_tag).first() or raise_404()
    employee = Employee.objects.filter(employee_code=employee_code).first() or raise_404()
    if not employee.is_active: raise ValidationError(...)                 # 400
    validate_due_at(due_at)                                               # 400
    with transaction.atomic():
        Employee.objects.select_for_update().get(pk=employee.pk)          # serialise per employee
        if CheckOut.objects.filter(employee=employee, returned_at__isnull=True).count() >= 3:
            raise Conflict()                                              # 409
        claimed = Asset.objects.filter(pk=asset.pk, status="AVAILABLE") \
                               .update(status="CHECKED_OUT")              # atomic claim
        if not claimed:
            raise Conflict()                                              # 409
        return CheckOut.objects.create(asset=asset, employee=employee, due_at=due_at)
```

There is also a partial unique index: `UNIQUE (asset_id) WHERE returned_at IS NULL`.

**4. What would have caught it.** A `TransactionTestCase` that fires two requests through a `threading.Barrier` against Postgres and asserts `[201, 409]` (`test_concurrent_checkouts_exactly_one_wins`). Tests for unknown tags and inactive employees. A load test (k6 or locust) running parallel check-outs of one asset.

### Snippet 3 — nightly notice task

**1. What is wrong**

1. **Not idempotent.** If Celery retries after a partial failure, or Beat or a person triggers it twice, the task creates another notice for every check-out. With a unique constraint, the second run instead crashes on the first duplicate with `IntegrityError` and never reaches the remaining rows.
2. **Emails are re-sent on retry,** because `deliver_email.delay` sits in the same loop with no record of what was already sent. Emails are also queued before the notice commits. If the task is wrapped in `atomic` and later rolls back, the email still goes out. The fix is to use `transaction.on_commit` and only email for notices that were actually created.
3. **Model instances are passed to `.delay()`.** With the default JSON serializer this raises `TypeError: Object of type Employee is not JSON serializable`. With pickle, it ships stale snapshots. It should pass primary keys.
4. **It does not scale.** Iterating the queryset loads tens of thousands of model instances into memory. Each `create` is a separate INSERT and round trip. `c.employee` adds an N+1 query per row. With that many rows the run is long enough to exceed the Redis broker's `visibility_timeout`, so the message gets redelivered and a second copy runs at the same time, which multiplies the duplicates.
5. **`overdue.count()` at the end re-runs the query.** It is another full scan, and the number can differ from the rows actually processed, because time has passed and rows have changed.
6. **`timezone.now().date()` is the UTC date and is computed per row.** Near midnight, rows in one run can get different `notice_date` values. It should be computed once with `timezone.localdate()`.

**2. Why it looks correct locally.** There are a handful of rows, so the task finishes in milliseconds and never gets redelivered. Developers often run with `CELERY_TASK_ALWAYS_EAGER=True`, which skips serialization, so the model-instance bug is hidden. Nobody runs it twice in a day, and retries never happen when nothing fails.

**3. Fix**

```python
@shared_task(acks_late=True)
def send_overdue_notices():
    now, today = timezone.now(), timezone.localdate()
    ids = (CheckOut.objects.filter(returned_at__isnull=True, due_at__lt=now)
           .values_list("id", flat=True).iterator(chunk_size=1000))
    created_total = 0
    for chunk in batched(ids, 1000):
        with transaction.atomic():
            existing = set(OverdueNotice.objects.filter(checkout_id__in=chunk, notice_date=today)
                           .values_list("checkout_id", flat=True))
            new = [OverdueNotice(checkout_id=i, notice_date=today) for i in chunk if i not in existing]
            OverdueNotice.objects.bulk_create(new, ignore_conflicts=True)   # unique constraint backs it
            for n in new:
                transaction.on_commit(lambda cid=n.checkout_id: deliver_email.delay(cid, str(today)))
        created_total += len(new)
    return f"created {created_total} notices"
```

`deliver_email` takes ids, re-reads the data, and records `emailed_at` on the notice so it can skip notices that were already sent. That makes it idempotent too.

**4. What would have caught it.** A test that runs the task twice and asserts one notice (`test_flag_overdue_task_is_idempotent`). Running tests with `task_always_eager=False`, or a real worker in CI, so serialization is exercised. A query-count assertion. A test with 10k rows and a time budget.

---

## Part C — Optimising the slow query

### 1. Rewritten query

```sql
SELECT c.id, c.asset_id, c.employee_id, c.checked_out_at, c.due_at
FROM checkouts c
JOIN employees e ON e.id = c.employee_id
WHERE c.checked_out_at >= TIMESTAMPTZ '2026-01-01 00:00:00+05:30'
  AND c.checked_out_at <  TIMESTAMPTZ '2026-07-01 00:00:00+05:30'
  AND c.returned_at IS NULL
  AND e.is_active
ORDER BY c.due_at, c.id
LIMIT 100;                      -- or keyset pagination: AND (c.due_at, c.id) > (:last_due, :last_id)
```

| Change | Gain | Cost |
|---|---|---|
| `DATE(checked_out_at) BETWEEN …` → a half-open range on the raw column | **Main fix.** Wrapping the column in a function makes the predicate non-sargable, so no plain index on `checked_out_at` can be used. `DATE()` on a timestamptz also depends on the session `TimeZone`, so results silently change between connections. The explicit offset fixes that. | The business time zone has to be stated explicitly (I assumed IST). |
| `SELECT *` → only the needed columns | Avoids reading `condition_note`, which can be large and TOASTed. Makes an index-only or covering scan possible. Sends fewer bytes. | The screen's column list has to be kept in sync. |
| `IN (subquery)` → `JOIN` | Mostly readability. Postgres already turns `IN` into a semi-join, so the speed gain here is small. (I'd say so honestly rather than claim it.) `employee.id` is unique, so the JOIN can't duplicate rows. | None. |
| `LIMIT` / keyset pagination | A screen doesn't need every open row of 6 months. With a matching index, Postgres can stop early instead of sorting everything. | The UI needs paging. |
| `, c.id` tiebreaker | Stable order across pages. | None. |

### 2. Indexes

```sql
-- A: the main one. Partial + composite + covering.
CREATE INDEX CONCURRENTLY checkouts_open_by_checked_out
    ON checkouts (checked_out_at)
    INCLUDE (employee_id, due_at, asset_id)
    WHERE returned_at IS NULL;
```

- **Partial (`WHERE returned_at IS NULL`).** Most of the 4.2M rows are returned items. Open check-outs are probably a few percent (to be measured), so the index is tiny, stays in memory, and matches the query's predicate exactly. A plain `(returned_at, checked_out_at)` composite would index millions of returned rows that this query never reads.
- **Key `checked_out_at`** serves the date range. **INCLUDE** lets it run as an index-only scan for the selected columns (subject to the visibility map, see below).
- **`CONCURRENTLY`** so building it on a live table doesn't block writes.

```sql
-- B: an alternative if the screen is paginated with LIMIT.
CREATE INDEX CONCURRENTLY checkouts_open_by_due
    ON checkouts (due_at, id) WHERE returned_at IS NULL;
```

Index B returns rows already in `ORDER BY` order, so `LIMIT 100` stops after about 100 matches, with no sort. I would only keep one of A and B. Which one depends on the date range's selectivity among open rows (see Q5): a narrow range favours A, an unbounded "scroll everything" view favours B.

**No index on `employees.is_active`.** It is a boolean on 12k rows and most employees are active, so the selectivity is poor. A hash join over 12k rows costs about a millisecond.

### 3. EXPLAIN (ANALYZE, BUFFERS)

**Before:**

```
Sort  (Sort Method: external merge  Disk: …kB)            <- maybe spilling
  -> Hash Join  (Hash Cond: c.employee_id = employees.id)
       -> Seq Scan on checkouts c
            Filter: (returned_at IS NULL AND date(checked_out_at) >= … AND …)
            Rows Removed by Filter: ~4,1xx,xxx
            Buffers: shared hit=… read=~100k+
       -> Hash -> Seq Scan on employees  Filter: is_active
Execution Time: ~8000 ms
```

**After:**

```
Sort (quicksort, Memory: …kB)     -- or no Sort node at all with index B + LIMIT
  -> Hash Join
       -> Index Only Scan using checkouts_open_by_checked_out on checkouts c
            Index Cond: (checked_out_at >= … AND checked_out_at < …)
            Heap Fetches: small
            Buffers: shared hit=a few hundred
Execution Time: tens of ms
```

**The line that proves it worked:** the checkouts scan node changes from `Seq Scan on checkouts … Rows Removed by Filter: 4.1M` to `Index (Only) Scan using checkouts_open_by_checked_out … Index Cond: (checked_out_at …)`, and shared buffers read drops by orders of magnitude. Execution Time confirms it. The row-count line explains *why*.

### 4. What breaks first as it grows (~8k rows a day, ~3M a year)

1. **The unbounded result set and the Sort.** The 6-month window keeps returning more rows, the Sort spills to disk (`work_mem`), and the response payload grows. Fix: pagination or `LIMIT` now (index B).
2. **Vacuum and visibility map lag.** Every return is an UPDATE (`returned_at`), which creates dead tuples. If autovacuum falls behind, the "index-only" scan does heap fetches again and the table bloats. Fix: per-table autovacuum settings (`autovacuum_vacuum_scale_factor = 0.01` on checkouts) and monitoring `n_dead_tup`.
3. **Longer term: whole-table operations.** Index builds, migrations (see D1) and seq scans by other queries get slower. Fix: **range-partition `checkouts` by `checked_out_at` (monthly)** before about 20–30M rows. Then date-range queries prune partitions and old partitions can be archived or detached. Heavy reporting moves to a read replica or a materialized summary.

### 5. What I would measure first

**The real selectivity: how many rows have `returned_at IS NULL`, and how many of those fall in the date range** (`SELECT count(*) … GROUP BY returned_at IS NULL`, plus `pg_stats` for the columns). The whole index decision depends on it. If 1% of rows are open, the partial index is tiny and decisive. If 40% are open (for example, returns not being recorded), the planner may correctly prefer a seq scan and the partial index buys little. I can't know this from the schema alone. I would also check the session `TimeZone` setting, because it changes what `DATE()` returns.

---

## Part D — Production reasoning

### D1. Zero-downtime migration: non-null `location_id` FK on 4.2M rows

Expand, backfill, contract. **Three deploys**, plus a backfill job.

**Deploy 1: expand, and code that writes the column.**
- The migration adds `location_id bigint NULL` with **no default**. That is a catalog-only change and runs in milliseconds.
- It adds the FK as `NOT VALID` (no table scan), plus `CREATE INDEX CONCURRENTLY` on `location_id` (Django `AddIndexConcurrently`, `atomic = False`).
- New code sets `location_id` on every insert and update.
- Each DDL runs with `SET lock_timeout = '3s'` and retries.
- **In-flight old code:** while instances roll over, old instances keep inserting without `location_id`. The column is nullable, so this works. Django selects explicit column lists, so old code never notices the extra column.

**Backfill (a management command, not a migration):** once all 4 instances run the new code, update in batches of about 5–10k rows by id range, one transaction per batch, with a short sleep. Watch replication lag and locks. Re-run until `count(*) WHERE location_id IS NULL = 0`. Then run `ALTER TABLE … VALIDATE CONSTRAINT` for the FK. That takes only `SHARE UPDATE EXCLUSIVE`, so reads and writes continue.

**Deploy 2: enforce NOT NULL safely.**
- `ADD CONSTRAINT location_nn CHECK (location_id IS NOT NULL) NOT VALID;` then `VALIDATE CONSTRAINT location_nn;` (a non-blocking scan).
- Then `ALTER COLUMN location_id SET NOT NULL`. On PG 12+ this uses the validated check and skips the scan, so the lock is brief.
- Then drop the check. The model becomes `null=False`.
- This is safe only because no running code writes NULL any more.

**Deploy 3 (optional):** clean up, for example remove the transitional code paths.

**What would lock the table if done wrong:** `ALTER TABLE … SET NOT NULL` (or Django's naive `AddField(null=False)` path) **without the validated CHECK constraint**. It takes an `ACCESS EXCLUSIVE` lock while scanning all 4.2M rows, which blocks every read and write. The same goes for adding the FK *without* `NOT VALID` and building the index without `CONCURRENTLY`. Even a fast `ALTER` can stall everything if it queues behind a long-running transaction, which is why `lock_timeout` matters.

### D2. Latency triage: `/reports/overdue/` at 25 s, no deploy in 9 days

In order:

1. **Is it just this endpoint?** Check APM or logs for p50/p95 across endpoints. If *everything* is slow, look at infrastructure (DB host CPU and IO, disk burst credits, a noisy neighbour, network) and not this query. If only this endpoint is slow, it is the query or its data.
2. **Where is the time spent: app or DB?** Use a trace span or `pg_stat_statements` (mean_time and calls for the report query, compared with yesterday). That rules app-side issues in or out: gunicorn worker saturation, connection pool exhaustion.
3. **Locks and long transactions:** `pg_stat_activity` filtered on `wait_event_type = 'Lock'`, plus the oldest `xact_start` and any `idle in transaction`. This rules blocking in or out.
4. **The plan:** `EXPLAIN (ANALYZE, BUFFERS)` of the exact query, compared with the known-good plan. Look for a seq scan or nested loop that was not there before.
5. **Statistics and bloat:** `pg_stat_user_tables` for `last_autovacuum`, `last_autoanalyze` and `n_dead_tup` on checkouts.
6. **Data volume:** how many open overdue rows there are now versus last week. A spike could come from an import, returns no longer being recorded, or a stuck client.

**Two most likely causes, given no code change:**

- **A plan flip from data growth or stale statistics.** The data crossed a threshold, or autoanalyze has not run, so the planner switched to a bad plan (seq scan, or a nested loop on a misestimate). *Confirm:* EXPLAIN shows estimated rows far from actual rows, and `last_autoanalyze` is old. Run `ANALYZE checkouts` and re-check the plan. A recovery confirms it.
- **A long-running or idle-in-transaction session.** A stuck ad-hoc query or a hung worker blocks vacuum, so dead tuples and bloat pile up (every return is an UPDATE), or it holds a lock the report waits on. *Confirm:* `pg_stat_activity` shows an `xact_start` hours or days old, `n_dead_tup` keeps climbing, and terminating that session plus a vacuum restores latency.

### D3. CI/CD on GitHub Actions

**On pull request (required checks):**
- `ruff` lint and format check.
- `python manage.py makemigrations --check --dry-run`, so no model change ships without a migration.
- A migration safety linter (`django-migration-linter` or `squawk` on `sqlmigrate` output) that flags locking operations: NOT NULL without a check, non-concurrent indexes, column drops.
- `pytest` against **Postgres and Redis service containers**, so the threaded concurrency test actually runs, plus coverage.
- `docker build`, plus `pip-audit` and an image scan.
- Branch protection: green checks plus 1 review.

**On merge to main:**
- Build the image tagged with the git SHA and push it to the registry (build once, promote the same artifact).
- Auto-deploy to **staging**: run `migrate` as a one-off job, roll out the app, run smoke tests (`/health/`, seed, the check-out → return → report flow).

**Production gate:**
- A GitHub Environment `production` with required reviewers. Staging smoke tests must be green and the migration linter clean.

**Migrations relative to code:**
- Migrations run as a separate job **before** the new app version rolls out.
- They must be backward compatible with the code currently running (expand/contract, as in D1).
- Destructive steps (dropping columns, NOT NULL) ship in a *later* release, after the code that needs them has been stable.

**Rollback:** because each migration is additive and the old code tolerates the new schema, rollback means **redeploying the previous image SHA**. The schema stays as it is. I would not run reverse migrations in production: they risk data loss and often take the same locks. If a migration itself is broken, fix forward with a new migration. Point-in-time recovery (PITR) backups are the last resort for data damage.
