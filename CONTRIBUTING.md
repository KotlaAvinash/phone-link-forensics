# Contributing

Thank you for helping. Bug reports, schema variants from new Phone Link builds, and fixes are all welcome.

## Synthetic data only

**Never put real evidence or personal data in an issue, a pull request, a commit, a test or a screenshot.** That includes databases, `-wal` files, `DeviceMetadataStorage.json`, registry exports, event logs, CSV output, screenshots of real cases, phone numbers, names, e-mail addresses, device names, serial numbers, Bluetooth addresses, GUIDs and hashes taken from a real device.

Use invented values instead, in the style the tests already use:

- phone numbers in the `555-01xx` range, for example `+1 555 0100`
- `example.com` addresses and `com.example.*` package names
- documentation-range MAC addresses such as `00:00:5E:00:53:01`
- device GUIDs and names from `viewer/pldemo.py` or `tests/fixtures.py`

If a bug only shows on a real database, describe the **schema** (`sqlite3 <copy>.db .schema`, run on a copy) and the shape of the values, then rebuild it with invented rows. A table or column name is fine to share; row contents are not.

## Tests are required

Every change to a map or to the viewer comes with a test that fails without the change, built from synthetic data. Run before opening a pull request:

```bash
pip install pyyaml yamllint
yamllint -c .yamllint maps
python tests/test_run_tests.py
python tests/run_tests.py
python viewer/test_plrelate.py
python viewer/test_plevidence.py
python viewer/test_pldevices.py
python viewer/test_plconnect.py
python viewer/test_plmapview.py
```

CI runs these on Windows with Python 3.12 (and the cross-match against SQLECmd's maps at a pinned commit). If you change a map and have `SQLECmd.exe`, also run `python tests/run_tests.py --sqlecmd <path to SQLECmd.exe>` and say so in the pull request; CI cannot run it. See [docs/TESTING.md](docs/TESTING.md).

## Windows first

Phone Link and the artifacts this tool reads exist only on Windows, and the viewer, its GUI tests and the cable / Bluetooth section are Windows-only. Changes must work on Windows; keep the viewer on the Python standard library.

## Maps

- Follow SQLECmd's map rules and keep `yamllint -c .yamllint maps` clean.
- Bump the map's `Version` and add a `# Version x.y:` comment saying what changed.
- The IdentifyQuery must check every column the queries read, and that the tables are real tables (not views).
- Never invent times: guard every conversion so 0, negative, text and out-of-range values give an empty time, and keep the raw value.

## Commits and pull requests

Keep each pull request to one logical change, describe what it fixes and how it was tested, and update the README and [CHANGELOG.md](CHANGELOG.md) when behaviour changes.
