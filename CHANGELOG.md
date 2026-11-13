# Changelog

All notable changes to this project are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions are git tags.

## [Unreleased]

## [2.1.2] - 2026-11-13

Tests only; the tool is unchanged from 2.1.1.

### Fixed

- The toolbar test required the device box to shrink at 1000 px, which is true only at this machine's display scaling; at 100 % scaling (GitHub's Windows runner) everything fits at full width. A new test checks the rule itself on any screen: full width while the toolbar has room, narrower only when it has not, and full width again when room returns.

## [2.1.1] - 2026-11-13

### Fixed

- The linked-rows message is shorter ("No linked rows in its database"), so a narrow pane shows all of it.
- Two layout tests asked for windows wider than the CI runner's screen, which Windows clamps; a size the screen cannot hold is now reported as skipped, with the screen size, instead of failing.

## [2.1.0] - 2026-11-13

### Added

- **Excel-style column filters on every table.** Each heading opens a panel with type-aware sorting (A → Z, Smallest → Largest, Oldest → Newest; blanks last), conditions (contains / begins with / greater than / between / before / after …), a value search, a tick list with counts, **(Select All)** and **(Blanks)**, and OK / Cancel. Filters on several columns combine; rows with no value or date are kept unless (Blanks) is unticked.
- **Dates in every view:** a case-wide From / To range with quick ranges counted back from the newest date in the data, applied to every view.
- **Honest counts:** filtered views show "112 of 686", checks show their problems ("2 issues / 28") and relations their matched rows ("69 of 91 linked").
- **Filtered export:** only the rows shown, in the order shown, plus a `.filter.txt` sidecar recording view, sort, filters and counts.
- **Recovered notifications whose record header was overwritten,** from their surviving JSON (`json-carved (record header lost)`), with a TimeBasis column (JSON `postTime` is Unix ms, the table's `post_time` FILETIME). Rows of WAL page versions replaced by a later frame are decoded (`wal-superseded`).
- **Checks:** messages whose SIM is missing from `subscription` are reported with the other orphans.
- **Dark mode:** Light / Dark / Follow Windows (toolbar box, Ctrl+Shift+L, `--theme`), every text colour at WCAG AA contrast or better in both themes (the previous light colours failed 11 text/background pairs), a title bar that follows the theme, named fonts and row heights that scale on high-DPI displays. The choice is saved in `%APPDATA%\phone-link-forensics\viewer.json` (not in demo or screenshot runs).
- Long Contents names are shortened with an ellipsis only when space is short, with the full name in a tooltip.
- Invisible characters are shown as `⟨U+200E⟩` etc.; keyboard: Ctrl+F, Ctrl+E, Ctrl+Shift+F, Alt+Down, F6 / Shift+F6, Ctrl+Shift+L.

### Fixed

- **Package-wide data is credited to the right device.** The FullTrust `phone_apps.db` is shared by every device folder. It is now copied, hashed and carved once per run, and a live or carved `hosted_app_ids` row is credited to a device only when its `device_id` equals that device's folder GUID. Rows no folder owns are reported package-wide instead of in every device's report. A FullTrust carve error is reported instead of swallowed.
- **Device metadata is credited by GUID.** Each device folder gets only the `DeviceMetadataStorage.json` entries whose `Metadata.Id` equals its folder GUID, or an explicit "no metadata entry matches this device folder". Orphan folders no longer receive another phone's metadata. Both package-wide inputs are hashed.
- **`secure_delete` is no longer reported as a property of the evidence.** `PRAGMA secure_delete` reports the examiner's SQLite connection, not the file. Recoverability is now judged from free pages that still hold bytes.
- **Open folder re-reads the case.** Opening a second folder now re-reads the linked phones and this-PC views for the new case; a failed load leaves the previous case untouched, and the status line says when the live this-PC views are dropped.
- **Pairing-folder times are stated as read.** A folder's creation time reflects the pairing only if timestamps were preserved; copy-time evidence is detected, and then no "linked" events, time matches or pairing order are claimed.
- **Windows entries are never called phones.** `DeviceMetadataStorage.json` entries get a Kind (phone, Windows PC, unknown device, empty placeholder), and only phones are matched to a folder by time. The view is "Linked devices (phones and PCs)".
- **Ambiguous time matches are not linked.** A match is made only when it is one-to-one; otherwise every candidate is listed as ambiguous.
- **A live file changing during the copy no longer discards the section.** It is marked `n/a - source changed during copy`.
- **The upstream cross-match test can no longer pass without running.** Without `--upstream-maps` it reports `SKIP`; a missing, empty or too small folder (under 50 maps) fails. A map missing a required key is a clean failure. New `tests/test_run_tests.py` tests the harness itself.
- **`Windows_YourPhone_PhotosDB.smap` v1.3:** the IdentifyQuery also checks the `thumbnail` and `media` columns its blob queries read (IdentifyValue 14).
- `run_tests.py` writes its 0x1F join separator as an escape instead of a raw byte.
- **Sync counters are not row counts.** The `content_sequence` check compares the sync counter with the expected counter (*same / differs*) instead of calling a mismatch "INCOMPLETE".
- **A live `-shm` no longer fails the load** with a false hash mismatch; it is hashed as copied and not verified against its constantly changing source (the `.db` and `-wal` still are).
- **A truncated database no longer aborts the run**; the file is named in a warning. A column or table missing in some Phone Link version is stated, not fatal. Threads are not called "aged out" when the message read failed.
- **Portable devices (MTP: phones, cameras, drives)** reads both registry sources; phones and drives listed only under `Windows Portable Devices\Devices` were dropped.
- **Phones connected (summary)** has one row per physical device instead of one per interface.
- Pairing status wording agrees with its row count; connection events say which field matched (a parent-device match is labelled as a child device's event); first driver installs are categorised as connected device or internal hardware; times without a zone stay in the pairing history; streamed apps with no pairing folder are flagged; live views say when they were read.
- The toolbar labels sit beside their own controls ("Device: 6" looked like a device number), and changing Nearby no longer re-copies the whole package.
- **WAL recovery keeps what an examiner needs:** an earlier version of a row that still exists (for example an edited message body) is kept when its values differ (`earlier version of live row` / `wal-superseded (earlier version of live row)`); the main file's copy of a page the WAL replaced is decoded; WAL frames are validated by salts, checksums and commit frames, and rows of uncommitted, old-generation or corrupt frames are reported as `wal-uncommitted`, `wal-stale` or `wal-invalid (checksum mismatch)`.
- The notification JSON pass reports only objects in dead space; an intact record is decoded as a record, not labelled "record header lost".
- **A phone is linked to its pairing folder by its stored `Metadata.Id`,** which on real data is the folder GUID; time matching is only a labelled fallback for entries whose Id is not a GUID, and never for a GUID that names no folder. The demo data now follows the real layout.
- Every staged copy is re-hashed and compared with its source; a database and its `-wal`/`-shm` are copied as one set; `DeviceMetadataStorage.json` is only used from a Phone Link layout; device ids are compared with one normaliser (case and braces); SIM ids of -1 / blank mean "no SIM recorded", not an orphan; a copy folder that cannot be removed is retried and reported.
- `--input` pointing at a device folder (`Indexed\{GUID}`) now finds its databases.
- **The `.smap` reader follows YAML indentation:** a `Query: |` block's indent is taken from its first line (SQLECmd's own maps use 12 to 16 spaces; 31 upstream queries were read as empty), trailing `# comments` are not part of a value, and compact `- Name:` items are read. It matches PyYAML on all 97 maps, and CI now checks that against every upstream map.
- **Viewer:** the export always matches its `.filter.txt` (a text search still waiting for its timer is applied first); reopening a filter and pressing OK keeps it; "(no zone)" times count as undated; the status bar and the filter panel's OK / Cancel stay visible on small screens; the linked-rows pane shows its message; the date range prefers a UTC column; Ctrl+Left / Ctrl+Right choose the keyboard's column; whole numbers keep full precision; quick ranges ignore implausible future dates; control borders and scrollbars meet 3:1 contrast; the title and toolbars fit at laptop widths.

### Changed

- CI runs on Windows, pins SQLECmd commit `c67634af503bd53777689998f1d9e3c0b4e2fdff` for the cross-match test, uses `actions/checkout@v7` and `actions/setup-python@v7`, and is read-only.
- Tests: 172 to 403 unit tests on Python 3.12 and 3.14, plus the answer-key and cross-match checks in `tests/run_tests.py`.
- Documentation: the README says where working copies go and that running on a suspect PC is live response, separates what CI runs from manual tests, recommends copying the maps into SQLECmd's `Maps` folder, and lists tested versions and what the tool does not do. `ContinuityDB` states the date range of each size-based epoch reading. New `viewer/maps-upstream/README.md`, `SECURITY.md`, `CONTRIBUTING.md` and an issue template.
- The GitHub owner name in links is `KotlaAvinash`.

## [2.0.1] - 2026-10-22

Production code identical to 2.0; test fixtures made platform-neutral.

### Fixed

- Carving fixtures keep their deleted bytes on SQLite builds with `secure_delete` on.
- Platform-neutral checks for pre-1970 dates and path case; the upper-cased package-path check runs on Windows only.

## [2.0] - 2026-10-22

Review release: evidence integrity, a schema-tolerant engine, a safer window and exports, and USB/Bluetooth and linked-phone fixes (172 tests).

### Fixed

- **Evidence integrity.** Each database set is hashed before and after copying and copied again (up to three tries) if it changed; a set that never settles is kept but flagged, and claims no source hash. Carving never reads past a cell's local payload and marks values cut at an overflow boundary. Read-only opens escape `#` and `%` in paths. A table whose live rows cannot be read labels its carved rows `unknown - live rows unreadable`.
- **Relation engine.** A missing column blanks one field instead of stopping the build; shared numbers are ambiguous by distinct contact; the nearby-events link is bounded; RCS chats and file transfers join the timeline; odd stored values no longer raise.
- **Window.** The toolbar is always usable; CSV export is atomic and Excel-safe; switching devices is safe; demo paths are redacted; the this-PC check is exact; callback errors are shown.
- **USB / Bluetooth and linked phones.** .NET and zone-less timestamps, honest pairing status, one bad source never sinks a section, junctions are not followed, PhysicalDevice grouping, NULL ContainerID ignored, USB composite parent serial, BTHLE address, reported read failures, a 10,000-event cap, log decoding, and the last-seen time kept in the pairing history.

## [1.0] - 2026-10-20

First release.

### Added

- Five SQLECmd maps for Phone Link: a PhotosDB v1.2 fix, and new maps for `calling.db`, `continuity.db`, `phoneapps.db` and `deviceData.db`, with synthetic answer-key tests and CI.
- A desktop viewer that works on hash-verified read-only copies, shows every table, carves deleted rows and links rows inside a table and between the tables of one database, starting with `notifications.db`.
- **Linked phones (this PC):** every pairing folder, phone details, trust relationships and apps streamed per device from the Phone Link package.
- **Phone connections (this PC):** USB, MTP, Bluetooth and Bluetooth LE devices, AutoPlay, first driver install and connection events from the live registry and logs, read-only.
- README with synthetic screenshots and a table of where Windows stores phone connection traces; a plain-English Contents list.

[Unreleased]: https://github.com/KotlaAvinash/phone-link-forensics/compare/v2.1.2...HEAD
[2.1.2]: https://github.com/KotlaAvinash/phone-link-forensics/compare/v2.1.1...v2.1.2
[2.1.1]: https://github.com/KotlaAvinash/phone-link-forensics/compare/v2.1.0...v2.1.1
[2.1.0]: https://github.com/KotlaAvinash/phone-link-forensics/compare/v2.0.1...v2.1.0
[2.0.1]: https://github.com/KotlaAvinash/phone-link-forensics/compare/v2.0...v2.0.1
[2.0]: https://github.com/KotlaAvinash/phone-link-forensics/compare/v1.0...v2.0
[1.0]: https://github.com/KotlaAvinash/phone-link-forensics/releases/tag/v1.0
