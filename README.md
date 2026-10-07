# Field Asset Check-Out Service

Django + DRF service for checking equipment out to employees and back in. It runs on PostgreSQL, uses Celery with a Redis broker, and ships with Docker.

Answers to Parts B, C and D are in [ANSWERS.md](ANSWERS.md).

**Screen recording:** [Artikate-Backend-Assessment-Asset-Checkout-Demo.mp4 (Google Drive, 6:20)](https://drive.google.com/file/d/1WO2O8_SLdtV4c61Uoc9b6hRFp5M0gnrC/view?usp=sharing). It shows the stack starting on a clean database, migrate and seed, the full check-out flow with every business rule, the employee summary, the overdue report, the test suite passing on Postgres, and the Celery task running twice through the worker.

## Run with Docker (from clone to working API)

```bash
git clone <this repo> && cd asset-checkout
docker compose up -d --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py seed_demo_data   # prints the API token
docker compose exec web pytest -q                        # full suite incl. concurrency test on Postgres
```

- API: http://localhost:8000/api/v1/
- Django admin: http://localhost:8000/admin/ (login `admin` / `admin`, created by the seed command)
- Celery worker + Beat run in the `worker` service. To see the hourly task: `docker compose logs -f worker`

## Run locally without Docker (SQLite)

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows; use .venv/bin/activate on Linux/macOS
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo_data
python manage.py runserver
pytest -q
```

If `DATABASE_URL` is not set, the app falls back to SQLite. On SQLite, the threaded concurrency test is skipped because SQLite has no row locks.

## Authentication

The API uses DRF **token auth**. The seed command prints a token. You can also get one with:

```bash
curl -X POST localhost:8000/api/v1/auth/token/ -d "username=admin&password=admin"
```

Then send the header `Authorization: Token <key>`. Session auth also works, which is useful for the browsable API after logging in at `/admin/`.

## Endpoints (`/api/v1/`)

| Method | Path | Notes |
|---|---|---|
| POST/GET | `assets/` | Filters: `?status=`, `?category=`. Search: `?search=` (matches name or asset_tag). 20 per page |
| GET | `assets/{id}/` | Includes `current_holder` |
| POST | `checkouts/` | `{asset_tag, employee_code, due_at}` |
| POST | `checkouts/{id}/return/` | `{condition_note, needs_maintenance}` |
| GET | `employees/{code}/summary/` | Four numbers from one aggregate query |
| GET | `reports/overdue/` | Most overdue first; one joined query |
| GET | `health/` | Public; checks the database |

Example:

```bash
T="Authorization: Token <key>"
curl -H "$T" -H "Content-Type: application/json" -X POST localhost:8000/api/v1/checkouts/ \
  -d '{"asset_tag":"CAM-002","employee_code":"E004","due_at":"2026-10-10T12:00:00Z"}'
curl -H "$T" localhost:8000/api/v1/employees/E001/summary/
curl -H "$T" localhost:8000/api/v1/reports/overdue/
```

## Key design decisions

- **Concurrency (rule 7).** A check-out runs inside `transaction.atomic()` and claims the asset with a conditional update: `UPDATE asset SET status='CHECKED_OUT' WHERE id=? AND status='AVAILABLE'`. In Postgres, a second concurrent request blocks on the row lock. After the first request commits, the second re-checks the condition, matches 0 rows and returns 409. As a backstop, a partial unique index (`one_open_checkout_per_asset` on `asset` where `returned_at IS NULL`) means the database itself cannot hold two open check-outs for one asset.
- **3-item limit.** The employee row is locked with `select_for_update()` before counting open check-outs. This stops two parallel requests from the same employee from both getting through.
- **Atomicity (rule 5).** The asset status update and the CheckOut insert happen in the same transaction. Any failure rolls back both.
- **Idempotent task.** A unique constraint on `(checkout, notice_date)` plus `bulk_create(ignore_conflicts=True)` in batches of 1000 means re-runs and retries never create duplicates.
- **Business logic** lives in `assets/services.py`, so the views stay thin.

## Assumptions

- "Overdue" means `due_at < now` (strictly earlier). An item due exactly now is **not** overdue. `days_overdue` is the number of whole days, so an item 1 second late shows 0.
- `due_at` exactly 30 days from now is allowed. Anything later is rejected.
- Rule order: unknown asset/employee gives 404 first, then inactive employee or bad `due_at` gives 400, then the limit or an unavailable asset gives 409.
- "Mean hold duration" uses only returned items, is shown in days rounded to 2 decimals, and is `null` if nothing has been returned.
- An asset in MAINTENANCE has no `current_holder`. There is no endpoint to move an asset out of MAINTENANCE; use the Django admin for that.
- The OverdueNotice date is the current date in UTC (`TIME_ZONE = "UTC"`).
- The seed command manages its own demo rows. On a re-run it deletes and recreates the check-outs for its 8 demo assets, which also removes their notices. It also creates the user `admin` / `admin`, which is for the demo only.
- Celery Beat runs embedded in the worker (`-B`) to keep it to the four required services. With more than one worker, it should become its own service.
- No extra model fields were added beyond the spec. The only additions are two constraints and default orderings.

## Known gaps

- Email delivery for overdue notices is not implemented. The spec only asks for the notice rows.
- Verified under `docker compose`: 23/23 tests pass on Postgres 16, including the threaded concurrency test. The Celery worker and Beat start, and `flag_overdue_checkouts` run twice through the worker created 2 notices and then 0. Without Docker, SQLite skips the concurrency test.
- No Celery result backend is configured, so `task.delay().get()` is not supported. The task's results are visible in the worker logs.
- `web` uses `runserver` in compose for live reload during review. The Dockerfile's default command is gunicorn for production use.
