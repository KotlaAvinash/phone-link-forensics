# Upstream SQLECmd maps (third-party, MIT)

The four `.smap` files in this folder are **not part of this project's own work**. They are SQLECmd's stock Phone Link maps, copied unchanged from Eric Zimmerman's [SQLECmd](https://github.com/EricZimmerman/SQLECmd) repository (`SQLMap/Maps`):

| File | Author | Version |
|---|---|---|
| `Windows_YourPhone_ContactsDB.smap` | Andrew Rathbun | 1.0 |
| `Windows_YourPhone_NotificationsDB.smap` | Andrew Rathbun | 1.1 |
| `Windows_YourPhone_PhoneDB-SMSMessages.smap` | Andrew Rathbun | 1.1 |
| `Windows_YourPhone_SettingsDB.smap` | Andrew Rathbun | 1.1 |

Their content is identical to the files at SQLECmd commit [`c67634af503bd53777689998f1d9e3c0b4e2fdff`](https://github.com/EricZimmerman/SQLECmd/tree/c67634af503bd53777689998f1d9e3c0b4e2fdff/SQLMap/Maps), the commit CI pins for the cross-match test (line endings may differ).

## Why they are here

The viewer's **SQLECmd map output** view runs all nine Phone Link maps on the same copies: the five in [`../../maps`](../../maps) and these four, which still match current Phone Link builds and are therefore not duplicated in `maps/`. Keeping them here lets the viewer show that output without a SQLECmd install.

To use them with SQLECmd itself, take them from SQLECmd's own `Maps` folder, not from here.

`maps/Windows_YourPhone_PhotosDB.smap` in this repository is a modified version of SQLECmd's `Windows_YourPhone_PhotosDB.smap` (Andrew Rathbun, v1.1); see its `# Version` comments for what changed.

## Licence

SQLECmd and its maps are released under the MIT License. Its notice, as published in the SQLECmd repository at the commit above:

```
MIT License

Copyright (c) Eric R. Zimmerman 2027

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

The rest of this repository is under its own [LICENSE](../../LICENSE).
