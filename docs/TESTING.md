# How the Phone Link maps were tested

Everything in this repository was tested on **synthetic data only**. The maps were also checked against a real Windows 11 system's Phone Link databases, but no value, count or identifier from that system is published here. Only the method and the pass/fail outcome are.

## 1. Answer-key tests (`tests/run_tests.py`)

`tests/fixtures.py` builds Phone Link databases from invented values (`example.com`, `com.example.*`, `555-01xx` numbers). It also computes the expected output **in Python, independently of the maps' SQL**.

Each scenario can run through two engines:
- an emulator of SQLECmd's rules: string-equal IdentifyValue; blob names `{time}_{prefix}_{base}_{id}_{name ≤50}_{column}.{ext}`; case-insensitive overwrite; NULL-blob row shift. **CI runs this engine on every push.**
- the real SQLECmd.exe, only with `--sqlecmd <path to SQLECmd.exe>`. **CI does not run it** (SQLECmd.exe is not installed there); it is run by hand. Without `--sqlecmd` the run reports `SKIP: real SQLECmd engine`, not a pass.

| Scenario | What it proves |
|---|---|
| current | Each database matches exactly its own map. Every row, column, blob, file name, extension and sort order equals the answer key |
| edge-values | Zero, NULL, negative, text and out-of-range times, including text in size-based epoch columns. Unknown call types. CSV-hostile text: commas, quotes, newlines, emoji, RTL, `=HYPERLINK`. Path-traversal, reserved, over-long, cloned and case-only-different package names |
| wal-only | Rows that exist only in an un-checkpointed `-wal` are parsed |
| legacy-photos | The 2021 `media` + `photo` layout still matches PhotosDB v1.3 |
| negatives | Unrelated `media` / `wallpaper` tables, the older settings.db layout, empty, zero-byte, text, truncated databases, and **views planted under the expected table names**. None match, nothing crashes |
| cross-match | Only with `--upstream-maps <folder>`; CI passes SQLECmd's `SQLMap/Maps` at the pinned commit `c67634af503bd53777689998f1d9e3c0b4e2fdff` (93 maps). This repo's PhotosDB replaces upstream's map of the same file name, as it does when copied into SQLECmd's `Maps` folder. Each synthetic database must then match exactly one map. A missing folder, or one with fewer than 50 maps, fails instead of passing; without the option the check is reported as `SKIP` |

When the real SQLECmd is used (manual runs only), every CSV row is also checked:
- it has exactly the header's field count;
- its `SourceFile` cell holds the database path.

### The tests can fail

A test suite only counts if a broken map makes it fail. 11 deliberate breakages were run, and every one made the suite fail, on both engines:
- wrong blob extension (×2)
- changed ORDER BY
- an invented column
- a removed time guard
- non-unique icon names
- a duplicating lookup
- a removed blob COALESCE
- a view check loosened to accept views
- a removed text cast on a raw column
- a misordered epoch guard that let text become a date

## 2. Real-data validation (results only)

On a real Windows 11 system, the maps ran through SQLECmd on **copies** of the Phone Link databases. The originals were hashed before and after, and were unchanged.

- **Independent query run:** every query's output was compared cell by cell with an independent Python `sqlite3` run of the same SQL. 0 mismatched cells.
- **Independent tool:** call times and directions, and photo-taken times, match an independent Phone Link parser exactly. Exported icons and wallpaper are byte-identical to that parser's extraction.
- **Thumbnails:** every exported thumbnail is a valid JPEG, byte-identical to the stored blob.
- **Alignment:** every CSV row is aligned with its header.

## 3. Stress and performance (`tests/stress.py`, synthetic)

Run by hand (needs SQLECmd.exe; CI does not run it).

| Input | Result with the real SQLECmd |
|---|---|
| 1,000,000 calls, 500,000 photos, 20,000 apps with 16 KB icons, 20,000 PC-app links | All rows and all 20,000 icons out; about 85 s; about 760 MB peak memory |

"Peak memory" is the highest resident memory (working set) of the SQLECmd process seen while sampling it every 0.1 s with `psutil`, not Windows' recorded peak working set, so a short spike between samples can be missed. Without `psutil` installed, the script prints `n/a`.

The PC-app name lookup was quadratic in an earlier draft (29 s at 20,000 rows). It was rewritten as a pre-grouped join, which takes 0.1 s at the same size.

## 4. Robustness (synthetic fault injection)

Results with SQLECmd 2026.5.0:

**Handled:**
- WITHOUT ROWID tables
- UTF-16 databases
- 512 B and 64 KB page sizes
- a 100 MB blob
- NUL characters and 5 MB text

**Not handled, and not fixable in a map** (listed in README → *SQLECmd behaviours*):
- no query timeout
- a mismatched WAL is ignored
- locked databases are skipped with exit code 0
- `-shm` files are written beside the input
- stored types are converted to the declared column type

## 5. Review

Several review rounds (correctness, robustness, privacy) produced the fixes above, and the last round re-checked each fix. A privacy audit scanned every file and the full git history against all values extracted from the real test databases and the test machine, and found none.
