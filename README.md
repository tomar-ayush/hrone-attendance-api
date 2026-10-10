# Employee Attendance & Analytics API

FastAPI + PyMongo, all application code in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"   # or a .env file with MONGO_URI / MONGO_DB
export MONGO_DB="attendance_db"
python sample_seed.py                          # optional: loads the nine sample documents
uvicorn app.main:app --port 8000
```
Swagger UI at `http://localhost:8000/docs`. Indexes are created at startup, idempotently. Real environment
variables take priority over `.env`.

Tested with Python 3.14 against MongoDB 8.0 (Atlas); needs 6.0+ for `$densify` and `$setWindowFields`.

## What is here
* Part A: the starter's defects are fixed and listed in `REVIEW.md`.
* Part B: all endpoints in `openapi.yaml`. Analytics are aggregation pipelines; the explain endpoint runs the same
  pipeline builders with `executionStats`.
* Part C: `DECISIONS.md`.

## Things worth knowing
* Rounding is half-up everywhere (R4, R8). MongoDB's `$round` is half-to-even, so rounding is done with `Decimal`
  in Python, except the trend's `attendance_rate`, which is rounded inside the pipeline with
  `floor(x * 10000 + 0.5) / 10000` so the moving average is taken over the reported (rounded) rates.
* Working days (R7) are calendar arithmetic, computed in Python; everything that reads data is in the pipeline.
* The trend endpoint fetches the department's members (`emp_code`, `joined_on`) with one indexed query and passes
  them into the pipeline; the gap filling, headcount per day and moving average all run in MongoDB.
* `PATCH` uses an optimistic lock on the values it read (`status`, `punch_in`, `punch_out`): a concurrent edit
  that changed them first makes the loser 409 instead of writing a history entry with stale "from" values.
* Verified against a 100,000-record dataset: every list and analytics endpoint answers in well under a second and
  every explain plan is an index scan.
