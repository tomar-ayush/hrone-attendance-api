# DECISIONS.md

1. **Indexes.** `employees.emp_code` unique: makes concurrent duplicate creates yield one 201. `employees.(department, emp_code)`: list by department, sorted. `employees.joined_on`: the summary's headcount match. `attendance_logs.(emp_code, date)` unique: the punch-in race, lookups by natural key, the monthly summary, the trend's `$in`. `attendance_logs.(date desc, emp_code)`: the attendance list's sort and range, the leaderboard's month. `attendance_logs.(status, date desc, emp_code)`: status-only filters. Rejected: an index on `punch_in` for punch-out; for one employee date order is punch-in order, so `(emp_code, date)` already serves it.

2. **Punch-in race.** Both requests load the employee, compute the same IST date, and call `insert_one`; there is no check first. The unique index accepts one; the other raises `DuplicateKeyError`, mapped to 409. No lock, no transaction.

3. **Ties.** `$rank` gives tied employees the same rank and skips the next. `rank <= limit` is applied after ranking, so with `limit=1` and two tied at the top you get two rows, both rank 1, ordered by `emp_code`.

4. **Headcount.** The pipeline starts from `employees` (`joined_on <= month end`), `$lookup`s each person's logs and unwinds with `preserveNullAndEmptyArrays`, so a person with no logs still adds 1 to headcount and 0 to every sum.

5. **100x.** Maintain a per-department daily rollup collection (updated by the write endpoints, or rebuilt nightly with `$merge`); summary, leaderboard and trend would read ~30 rows per department instead of a month of raw logs.
