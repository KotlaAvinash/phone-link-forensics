# Changelog

All notable changes to this project are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions are git tags.

## [Unreleased]

### Fixed

- **Package-wide data is credited to the right device.** The FullTrust `phone_apps.db` is shared by every device folder. It is now copied, hashed and carved once per run, and a live or carved `hosted_app_ids` row is credited to a device only when its `device_id` equals that device's folder GUID. Rows no folder owns are reported package-wide instead of in every device's report. A FullTrust carve error is reported instead of swallowed.
- **Device metadata is credited by GUID.** Each device folder gets only the `DeviceMetadataStorage.json` entries whose `Metadata.Id` equals its folder GUID, or an explicit "no metadata entry matches this device folder". Orphan folders no longer receive another phone's metadata. Both package-wide inputs are hashed.
- **`secure_delete` is no longer reported as a property of the evidence.** `PRAGMA secure_delete` reports the examiner's SQLite connection, not the file. Recoverability is now judged from free pages that still hold bytes.
- **Open folder re-reads the case.** Opening a second folder now re-reads the linked phones and this-PC views for the new case; a failed load leaves the previous case untouched, and the status line says when the live this-PC views are dropped.
- **Pairing-folder times are stated as read.** A folder's creation time reflects the pairing only if timestamps were preserved; copy-time evidence is detected, and then no "linked" events, time matches or pairing order are claimed.
- **Windows entries are never called phones.** `DeviceMetadataStorage.json` entries get a Kind (phone, Windows PC, unknown device, empty placeholder), and only phones are matched to a folder by time. The view is "Linked devices".
- **Ambiguous time matches are not linked.** A match is made only when it is one-to-one; otherwise every candidate is listed as ambiguous.
- **A live file changing during the copy no longer discards the section.** It is marked `n/a - source changed during copy`.
- **The upstream cross-match test can no longer pass without running.** Without `--upstream-maps` it reports `SKIP`; a missing, empty or too small folder (under 50 maps) fails. A map missing a required key is a clean failure. New `tests/test_run_tests.py` tests the harness itself.
- **`Windows_YourPhone_PhotosDB.smap` v1.3:** the IdentifyQuery also checks the `thumbnail` and `media` columns its blob queries read (IdentifyValue 14).
- `run_tests.py` writes its 0x1F join separator as an escape instead of a raw byte.

### Changed

- CI runs on Windows, pins SQLECmd commit `c67634af503bd53777689998f1d9e3c0b4e2fdff` for the cross-match test, uses `actions/checkout@v7` and `actions/setup-python@v7`, and is read-only.
- Tests: 172 to 218.
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

[Unreleased]: https://github.com/KotlaAvinash/phone-link-forensics/compare/v2.0.1...HEAD
[2.0.1]: https://github.com/KotlaAvinash/phone-link-forensics/compare/v2.0...v2.0.1
[2.0]: https://github.com/KotlaAvinash/phone-link-forensics/compare/v1.0...v2.0
[1.0]: https://github.com/KotlaAvinash/phone-link-forensics/releases/tag/v1.0
