"""
plconnect.py - phones (and other devices) connected to THIS PC by USB cable or Bluetooth.

Phone Link is one way a phone reaches a PC; a cable or a Bluetooth pairing leaves its own traces.
This module reads them from the LIVE Windows registry, logs and event logs of the computer it
runs on - read-only - so it describes THIS PC, never an evidence image. The viewer only shows it
when you ask to analyse this PC.

What is read (no admin needed for any of it) and what is deliberately NOT read:

  HKLM\\SYSTEM\\CurrentControlSet\\Enum\\USB                     USB device instances: VID/PID, serial (instance id),
                                                              description, manufacturer, driver service, ContainerID
  HKLM\\SYSTEM\\CurrentControlSet\\Enum\\SWD\\WPDBUSENUM          portable devices (MTP phones, cameras, volumes)
  HKLM\\SOFTWARE\\Microsoft\\Windows Portable Devices\\Devices   portable-device friendly names
  HKLM\\SYSTEM\\CurrentControlSet\\Services\\BTHPORT\\Parameters\\Devices
                                                              paired / seen Bluetooth devices: name, class of device,
                                                              manufacturer, LMP version, VID/PID, LastSeen, LastConnected
  HKLM\\SYSTEM\\CurrentControlSet\\Enum\\BTHENUM                  Bluetooth services per device (hands-free, phonebook, ...)
  HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\AutoplayHandlers\\KnownDevices
                                                              devices this user's AutoPlay has seen (label, ContainerID)
  C:\\Windows\\INF\\setupapi.dev.log                             first time each device's driver was installed
  Event logs: Microsoft-Windows-Kernel-PnP/Configuration (device configured / started),
              Microsoft-Windows-WPD-MTPClassDriver/Operational, Microsoft-Windows-Bluetooth-BthLEPrepairing/Operational

  NOT read: BTHPORT\\Parameters\\Keys (Bluetooth link keys - secrets) and the Enum\\...\\Properties
  subkeys (exact install / first / last arrival times - SYSTEM-only; need admin and a SYSTEM-level reader).

Registry key last-write times are shown as "KeyLastWriteUTC": Windows updates a device's key when
it changes, which often means when it was last connected - a lead, not a documented connection time.
Standard library only (winreg, ElementTree).
"""
import datetime as dt
import os
import re
import subprocess
import xml.etree.ElementTree as ET

HKLM, HKCU = "HKLM", "HKCU"
USB = r"SYSTEM\CurrentControlSet\Enum\USB"
WPD = r"SYSTEM\CurrentControlSet\Enum\SWD\WPDBUSENUM"
WPD_NAMES = r"SOFTWARE\Microsoft\Windows Portable Devices\Devices"
BTPORT = r"SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Devices"
BTENUM = r"SYSTEM\CurrentControlSet\Enum\BTHENUM"
BTLE = r"SYSTEM\CurrentControlSet\Enum\BTHLE"
BTLE_SVC = r"SYSTEM\CurrentControlSet\Enum\BTHLEDevice"
KNOWN = r"Software\Microsoft\Windows\CurrentVersion\Explorer\AutoplayHandlers\KnownDevices"
SETUPAPI = r"C:\Windows\INF\setupapi.dev.log"
CHANNELS = ["Microsoft-Windows-Kernel-PnP/Configuration", "Microsoft-Windows-WPD-MTPClassDriver/Operational",
            "Microsoft-Windows-Bluetooth-BthLEPrepairing/Operational"]
DEVICE_PREFIXES = ("USB\\", "SWD\\WPDBUSENUM", "BTHENUM\\", "BTHLE\\", "BTHLEDEVICE\\", "USBSTOR\\")
EVENT_CAP = 10000   # newest events read per channel; older ones are not shown (stated in the C_events view)
# Windows' ContainerID for "the PC itself" (not a device)
NULL_CONTAINER = "{00000000-0000-0000-ffff-ffffffffffff}"
# Kernel-PnP event fields that name ANOTHER device than the event's own (DeviceInstanceId)
PARENT_FIELD, MATCHING_FIELD = "ParentDeviceInstanceId", "MatchingDeviceId"

# [published] Bluetooth Core: class-of-device major classes
BT_MAJOR = {0: "Miscellaneous", 1: "Computer", 2: "Phone", 3: "Network access point", 4: "Audio/Video",
            5: "Peripheral", 6: "Imaging", 7: "Body-worn (class 7)", 8: "Toy", 9: "Health", 31: "Uncategorized"}
# [published] Bluetooth SIG assigned numbers: service class UUIDs a phone commonly exposes
BT_SERVICES = {"1101": "Serial Port", "1103": "Dial-up Networking", "1105": "OBEX Object Push",
               "1106": "OBEX File Transfer", "1108": "Headset", "110a": "Audio Source (A2DP)",
               "110b": "Audio Sink (A2DP)", "110c": "A/V Remote Control Target", "110e": "A/V Remote Control",
               "1112": "Headset Audio Gateway", "1115": "PAN User", "1116": "Network Access Point",
               "111e": "Hands-Free", "111f": "Hands-Free Audio Gateway", "112f": "Phonebook Access (server)",
               "1132": "Message Access (server)", "1133": "Message Notification (server)", "1200": "PnP Information",
               "1800": "Generic Access (Bluetooth LE)", "1801": "Generic Attribute (Bluetooth LE)"}
# [measured] Windows' own message text for each event ID, read from this PC's event logs
EVENT_MEANING = {
    ("Kernel-PnP", "400"): "device was configured", ("Kernel-PnP", "410"): "device was started",
    ("Kernel-PnP", "411"): "device had a problem starting", ("Kernel-PnP", "420"): "device was deleted",
    ("Kernel-PnP", "430"): "device requires further installation",
    ("Kernel-PnP", "442"): "device settings were not migrated from a previous Windows installation",
    ("WPD-MTPClassDriver", "1000"): "MTP driver started", ("WPD-MTPClassDriver", "1001"): "device will suspend when idle",
    ("WPD-MTPClassDriver", "1002"): "device entering idle state", ("WPD-MTPClassDriver", "1003"): "device resuming from idle",
    ("WPD-MTPClassDriver", "1004"): "driver cancelled an operation",
    ("WPD-MTPClassDriver", "1005"): "MTP driver customized for a connected device (make, model, OS version)",
    ("WPD-MTPClassDriver", "1006"): "MTP driver failed to start",
}

# driver services that indicate a phone on USB (the evidence for the flag is shown with it)
PHONE_SERVICES = {"WUDFWpdMtp": "MTP phone/camera driver", "dg_ssudbus": "vendor phone USB driver",
                  "Modem": "phone modem interface", "ssudmdm": "vendor phone modem",
                  "usbaapl64": "Apple mobile device USB driver"}


# ---------------------------------------------------------------- sources
class LiveSource:
    """Read-only access to this computer's registry, setupapi log and event logs."""

    def __init__(self):
        import winreg
        self.W = winreg
        self.roots = {HKLM: winreg.HKEY_LOCAL_MACHINE, HKCU: winreg.HKEY_CURRENT_USER}
        self.denied = []

    def _open(self, hive, path):
        try:
            return self.W.OpenKey(self.roots[hive], path, 0, self.W.KEY_READ)
        except PermissionError:
            self.denied.append(f"{hive}\\{path}")
            return None
        except OSError:
            return None

    def subkeys(self, hive, path):
        k = self._open(hive, path)
        out = []
        while k is not None:
            try:
                out.append(self.W.EnumKey(k, len(out)))
            except OSError:
                break
        return out

    def values(self, hive, path):
        k = self._open(hive, path)
        out, i = {}, 0
        while k is not None:
            try:
                n, v, _ = self.W.EnumValue(k, i)
                out[n] = v
            except OSError:
                break
            i += 1
        return out

    def last_write(self, hive, path):
        k = self._open(hive, path)
        return self.W.QueryInfoKey(k)[2] if k is not None else None

    def text(self, path):
        try:
            with open(path, "rb") as f:
                return decode_log(f.read())
        except OSError as e:
            self.denied.append(f"{path}: {e.strerror or type(e).__name__}")
            return ""

    def setupapi(self):
        """The current setupapi.dev.log and every archived copy Windows rotated next to it."""
        import glob
        files = sorted(glob.glob(os.path.join(os.path.dirname(SETUPAPI), "setupapi.dev*.log")))
        return [(os.path.basename(f), self.text(f)) for f in files]

    def events(self, channel, count=EVENT_CAP):
        """[(utc 'YYYY-MM-DD HH:MM:SS', event id, {data name: value})], newest first. Failures go to `denied`."""
        try:
            r = subprocess.run(["wevtutil", "qe", channel, f"/c:{count}", "/rd:true", "/f:xml"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            self.denied.append(f"event log {channel}: wevtutil failed ({type(e).__name__})")
            return []
        if r.returncode != 0:
            why = next((ln.strip() for ln in (r.stderr or "").splitlines() if ln.strip()), f"exit code {r.returncode}")
            self.denied.append(f"event log {channel}: {why[:150]}")
            return []
        problems = []
        out = parse_events_xml(r.stdout, problems)
        self.denied.extend(f"event log {channel}: {p}" for p in problems)
        return out


class FakeSource:
    """Synthetic source for tests and the demo: {(hive, path): {"values": {...}, "subkeys": [...], "lw": int}}."""

    logs_in_utc = True   # synthetic logs: no time zone of the running PC is applied

    def __init__(self, keys, text="", events=None, failures=None):
        """failures: {channel: message} - reading that event log fails, as a failed wevtutil call would."""
        self.keys, self._text, self._events, self.denied = keys, text, events or {}, []
        self._failures = failures or {}

    def subkeys(self, hive, path):
        return list(self.keys.get((hive, path), {}).get("subkeys", []))

    def values(self, hive, path):
        return dict(self.keys.get((hive, path), {}).get("values", {}))

    def last_write(self, hive, path):
        return self.keys.get((hive, path), {}).get("lw")

    def text(self, path):
        return self._text

    def setupapi(self):
        return [("setupapi.dev.log", self._text)]

    def events(self, channel, count=EVENT_CAP):
        if channel in self._failures:
            self.denied.append(f"event log {channel}: {self._failures[channel]}")
            return []
        return list(self._events.get(channel, []))


# ---------------------------------------------------------------- decoding helpers
def filetime(v):
    """FILETIME as int, or 8 little-endian bytes -> 'YYYY-MM-DD HH:MM:SS' UTC."""
    if isinstance(v, (bytes, bytearray)) and len(v) >= 8:
        v = int.from_bytes(bytes(v[:8]), "little")
    if not isinstance(v, int) or v <= 0:
        return ""
    try:
        return (dt.datetime(1601, 1, 1) + dt.timedelta(microseconds=v // 10)).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, ValueError):
        return ""


def bt_name(v):
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).split(b"\x00", 1)[0].decode("utf-8", "replace")
    return str(v or "")


def mac(key):
    k = re.sub(r"[^0-9a-fA-F]", "", key or "")
    return ":".join(k[i:i + 2] for i in range(0, 12, 2)).upper() if len(k) == 12 else (key or "")


def bt_class(cod):
    if not isinstance(cod, int):
        return "", False
    major = (cod >> 8) & 0x1F
    return f"{BT_MAJOR.get(major, f'major {major}')} (COD 0x{cod:06x})", major == 2


def multi(v):
    return "; ".join(v) if isinstance(v, (list, tuple)) else str(v or "")


def clean_desc(v):
    """'@oem12.inf,%device%;Example Phone' -> 'Example Phone' (the text after the last ';')."""
    s = multi(v)
    return s.rsplit(";", 1)[-1] if s.startswith("@") else s


def decode_log(raw):
    """Text of a log file: UTF-16 when it has a BOM, otherwise UTF-8, otherwise cp1252."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def container_of(v):
    """A ContainerID usable for joining devices: lower case, always in braces (so '{X}' and 'x' are the same
    container), and empty for the PC's own NULL container. The one normaliser for ContainerIDs in this module."""
    c = str(v or "").strip().strip("{}").strip().lower()
    c = "{%s}" % c if c else ""
    return "" if c == NULL_CONTAINER else c


def wpd_key_to_instance(key):
    """A WPD\\Devices key name -> the device instance id it names: 'USB#VID_1234&PID_0F10#SERIAL' ->
    'USB\\VID_1234&PID_0F10\\SERIAL'. Only the first two '#' separate enumerator, device and instance (an SWD
    instance itself contains '#'). A key with fewer parts is returned as it is."""
    parts = str(key or "").split("#", 2)
    return "\\".join(parts) if len(parts) == 3 and all(parts) else str(key or "")


def parse_events_xml(xml_text, problems=None):
    """Events from wevtutil's XML. A parse error is appended to the `problems` list (if given)."""
    out = []
    ns = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
    try:
        root = ET.fromstring("<Events>" + xml_text + "</Events>")
    except ET.ParseError as e:
        if problems is not None:
            problems.append(f"event XML could not be parsed ({e})")
        return out
    for ev in root.findall("e:Event", ns):
        sysn = ev.find("e:System", ns)
        if sysn is None:
            continue
        eid = sysn.findtext("e:EventID", default="", namespaces=ns)
        tc = sysn.find("e:TimeCreated", ns)
        t = (tc.get("SystemTime") or "")[:19].replace("T", " ") if tc is not None else ""
        data = {}
        for d in ev.iter("{http://schemas.microsoft.com/win/2004/08/events/event}Data"):
            data[d.get("Name") or f"Data{len(data)}"] = (d.text or "").strip()
        out.append((t, eid, data))
    return out


# Windows names this module matches: the USB root hub id prefix and the HID USB driver
ROOT_HUB_PREFIX, HID_USB_SERVICE = "USB\\ROOT_HUB", "HidUsb"
CONNECTED_ENUMERATORS = {"USB": "USB", "USBSTOR": "USB storage", "SWD\\WPDBUSENUM": "portable device (SWD-WPD)",
                         "BTHENUM": "Bluetooth", "BTHLE": "Bluetooth LE", "BTHLEDEVICE": "Bluetooth LE service"}


def install_category(devid, service=""):
    """'connected device: ...' for ids of devices plugged in or paired (USB, USB storage, portable devices,
    Bluetooth), 'internal/other: ...' for the PC's own hardware and software devices (PCI, HID, ROOT, ACPI, ...).
    A USB root hub is part of the PC. `service` is the USB driver, when its Enum\\USB key was read."""
    u = str(devid or "").upper()
    if u.startswith(ROOT_HUB_PREFIX):
        return "internal/other: USB root hub (part of the PC)"
    for prefix in sorted(CONNECTED_ENUMERATORS, key=len, reverse=True):
        if u.startswith(prefix + "\\"):
            what = CONNECTED_ENUMERATORS[prefix]
            if prefix == "USB" and str(service or "").lower() == HID_USB_SERVICE.lower():
                what += " - input device (HID), may be built in"
            return f"connected device: {what}"
    return f"internal/other: {u.split(chr(92), 1)[0] or '?'}"


def parse_setupapi(text):
    """[(device id, section start as logged (local time), section title)] for device installs."""
    out = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r">>>\s+\[(Device Install[^\]]*?) - (.+?)\]\s*$", line)
        if not m:
            continue
        start = ""
        for nxt in lines[i + 1:i + 4]:
            s = re.match(r">>>\s+Section start (\d{4}/\d\d/\d\d \d\d:\d\d:\d\d)", nxt)
            if s:
                start = s.group(1).replace("/", "-")
                break
        out.append((m.group(2).strip(), start, m.group(1).strip()))
    return out


def local_to_utc(s, logs_in_utc=False):
    """setupapi.dev.log logs local time; convert with this PC's time-zone rules, i.e. the UTC
    offset in force on that date (daylight saving included) as Windows knows it - not necessarily today's offset.

    Synthetic sources set logs_in_utc so the demo never reveals the running PC's time zone."""
    try:
        t = dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        if logs_in_utc:
            return t.strftime("%Y-%m-%d %H:%M:%S")
        t = t.astimezone()
        return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OverflowError, OSError):   # Windows cannot convert local times before 1970
        return ""


def _distinct(values):
    out = []
    for v in values:
        if v and v not in out:
            out.append(v)
    return out


def summarize_phones(seen, g):
    """One summary row per physical device from per-interface phone rows (`seen`). Rows that share a
    PhysicalDevice are one device; a row without one stays on its own and says so. Each grouped row gets an
    entity of its own, linked to every interface it was built from."""
    groups, order = {}, []
    for s in seen:
        key = s["PhysicalDevice"] or ("", id(s))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(s)
    out = []
    for key in order:
        members = groups[key]
        first = lambda col: min((m[col] for m in members if m[col]), default="")   # noqa: E731
        last = lambda col: max((m[col] for m in members if m[col]), default="")    # noqa: E731
        seen_as = "; ".join(f"{m['How']}: {m['Device']} ({m['Evidence']})" for m in members)
        if isinstance(key, tuple):
            e = members[0]["_e"]
            seen_as += " - no physical device id, not grouped with other rows"
        else:
            e = ("phonedevice", key)
            device = next((m["Device"] for m in members if m["_named"]), members[0]["Device"])
            g.label[e] = f"physical device {device} ({key})"
            for m in members:
                g.link("CR5", e, m["_e"], f"Same physical device ({key}): seen as {m['How']} - {m['Evidence']}")
        out.append({"_e": e, "How": "; ".join(_distinct(m["How"] for m in members)),
                    "Device": next((m["Device"] for m in members if m["_named"]), members[0]["Device"]),
                    "PhysicalDevice": "" if isinstance(key, tuple) else key,
                    "Serial": "; ".join(_distinct(m["Serial"] for m in members)), "Interfaces": len(members),
                    "SeenAs": seen_as, "Evidence": "; ".join(_distinct(m["Evidence"] for m in members)),
                    "FirstSeenUTC": first("FirstSeenUTC"), "LastConnectedUTC": last("LastConnectedUTC"),
                    "LastSeenUTC": last("LastSeenUTC"), "FirstInstallUTC": first("FirstInstallUTC"),
                    "KeyLastWriteUTC": last("KeyLastWriteUTC")})
    return out


# ---------------------------------------------------------------- build
class _Guard:
    """Wrap a source so one failing read becomes an empty result plus a line in the 'could not read' view."""

    EMPTY = {"subkeys": [], "values": {}, "last_write": None, "text": "", "setupapi": [], "events": []}

    def __init__(self, src):
        self.src, self.failed = src, []
        self.logs_in_utc = getattr(src, "logs_in_utc", False)

    def __getattr__(self, name):
        if name not in self.EMPTY:
            raise AttributeError(name)

        def call(*args):
            try:
                return getattr(self.src, name)(*args)
            except Exception as e:   # a source can fail in ways of its own; the other views must still appear
                self.failed.append(f"{name} {' '.join(str(a) for a in args)}: {type(e).__name__}: {e}")
                return type(self.EMPTY[name])() if self.EMPTY[name] is not None else None
        return call


def build(src, g, View):
    """Add the 'Phone connections (this PC)' views to graph g. Returns view ids."""
    vids, by_container, by_instance = [], {}, {}
    utc_logs = getattr(src, "logs_in_utc", False)
    guarded, src = src, _Guard(src)

    def to_utc(s):
        return local_to_utc(s, utc_logs)

    def add(vid, title, basis, cols, rows, entity=True):
        when = getattr(guarded, "read_utc", None)   # a CachedSource: the views are a snapshot taken once
        if when:
            basis = (f"{basis}. Snapshot: read from this PC at {when} UTC, once per viewer session - later changes "
                     f"are not shown until the viewer is reopened")
        v = View(vid, title, "conn", basis, cols, rows)
        g.add_view(v, "_e" if entity else None)
        vids.append(vid)

    def remember(e, container, instance):
        container = container_of(container)      # the PC's own NULL container joins nothing
        if container:
            by_container.setdefault(container, []).append(e)
        if instance:
            by_instance[instance.upper()] = e

    install = {}   # device id -> (earliest start as logged, section, log file)
    for fname, text in src.setupapi():
        for devid, start, title in parse_setupapi(text):
            k = devid.upper()
            if start and (k not in install or start < install[k][0]):
                install[k] = (start, title, fname)

    # USB
    rows = []
    for dev in src.subkeys(HKLM, USB):
        vm = re.search(r"VID_([0-9A-Fa-f]{4})", dev)
        pm = re.search(r"PID_([0-9A-Fa-f]{4})", dev)
        for inst in src.subkeys(HKLM, f"{USB}\\{dev}"):
            path = f"{USB}\\{dev}\\{inst}"
            v = src.values(HKLM, path)
            iid = f"USB\\{dev}\\{inst}"
            e = ("usb", iid)
            svc = str(v.get("Service") or "")
            first = install.get(iid.upper(), ("",))[0]
            name = clean_desc(v.get("FriendlyName")) or clean_desc(v.get("DeviceDesc"))
            g.label[e] = f"USB {name} ({vm.group(1) if vm else '?'}:{pm.group(1) if pm else '?'})"
            rows.append({"_e": e, "_dev": dev, "_svc": svc, "_prefix": str(v.get("ParentIdPrefix") or ""),
                         "Name": name, "Manufacturer": clean_desc(v.get("Mfg")),
                         "VID": vm.group(1).upper() if vm else "", "PID": pm.group(1).upper() if pm else "",
                         "SerialOrInstance": inst, "Driver": svc,
                         "PhoneEvidence": PHONE_SERVICES.get(svc, ""), "ContainerID": v.get("ContainerID", ""),
                         "Location": v.get("LocationInformation", ""), "FirstInstallLocal": first,
                         "FirstInstallUTC": to_utc(first), "KeyLastWriteUTC": filetime(src.last_write(HKLM, path)),
                         "HardwareID": multi(v.get("HardwareID")), "InstanceID": iid})
            remember(e, v.get("ContainerID"), iid)
    # a composite (usbccgp) parent of a phone interface (e.g. MTP) is the phone's USB identity and carries its serial
    usb_parent = {}
    for r in rows:
        if not r["PhoneEvidence"] or not re.search(r"&MI_[0-9A-Fa-f]{2}", r["_dev"], re.I):
            continue
        base = re.sub(r"&MI_[0-9A-Fa-f]{2}", "", r["_dev"], flags=re.I).upper()
        cid = container_of(r["ContainerID"])
        for par in rows:
            if par["_svc"].lower() != "usbccgp" or par["_dev"].upper() != base:
                continue
            by_prefix = par["_prefix"] and r["SerialOrInstance"].lower().startswith(par["_prefix"].lower() + "&")
            if by_prefix or (cid and container_of(par["ContainerID"]) == cid):
                usb_parent[r["_e"]] = par
                par["PhoneEvidence"] = par["PhoneEvidence"] or f"composite parent (usbccgp) of a phone interface ({r['PhoneEvidence']})"
                break
    add("C_usb", "USB devices (cable)", "HKLM\\" + USB + " - every USB device ever installed; PhoneEvidence shows "
        "why a row looks like a phone (driver service). FirstInstall from setupapi.dev.log (local time as logged)",
        ["Name", "Manufacturer", "VID", "PID", "SerialOrInstance", "Driver", "PhoneEvidence", "ContainerID",
         "Location", "FirstInstallLocal", "FirstInstallUTC", "KeyLastWriteUTC", "HardwareID", "InstanceID"], rows)

    # portable devices (MTP): the union of SWD\WPDBUSENUM and WPD\Devices. A WPD\Devices key is the device's
    # PnP id with '\' written as '#': 'SWD#WPDBUSENUM#<instance>' is an SWD instance (the same device), and
    # 'USB#VID_..#<serial>' an MTP phone or camera named by its USB instance.
    usb_rows = {r["InstanceID"].upper(): r for r in rows}
    phone_containers = {container_of(r["ContainerID"]) for r in rows if r["PhoneEvidence"]} - {""}
    wpd_keys = {}   # instance id (upper) -> (WPD\Devices key, its values)
    for k in src.subkeys(HKLM, WPD_NAMES):
        wpd_keys.setdefault(wpd_key_to_instance(k).upper(), (k, src.values(HKLM, f"{WPD_NAMES}\\{k}")))
    rows = []

    def wpd_row(e, iid, source, v, wv, path_hive_path):
        container = v.get("ContainerID", "")
        usb = usb_rows.get(iid.upper())
        if usb is not None and not container:
            container = usb["ContainerID"]
        evidence = ""
        if usb is not None and usb["PhoneEvidence"]:
            evidence = f"same device as a USB phone ({usb['PhoneEvidence']})"
        elif container_of(container) in phone_containers:
            evidence = "same ContainerID as a USB phone"
        elif iid.upper().startswith("USB\\"):
            evidence = ("portable device named by a USB instance (an MTP/PTP device: a phone or camera), "
                        "no USB driver key read for it")
        name = clean_desc(v.get("FriendlyName")) or clean_desc(wv.get("FriendlyName"))
        g.label[e] = f"portable device {name}"
        first = install.get(iid.upper(), ("",))[0]
        hive, path = path_hive_path
        rows.append({"_e": e, "Name": name, "Source": source, "Manufacturer": clean_desc(v.get("Mfg")),
                     "Description": clean_desc(v.get("DeviceDesc")), "Driver": v.get("Service", ""),
                     "PhoneEvidence": evidence, "ContainerID": container, "FirstInstallLocal": first,
                     "FirstInstallUTC": to_utc(first), "KeyLastWriteUTC": filetime(src.last_write(hive, path)),
                     "InstanceID": iid})
        if iid.upper() in by_instance:      # the USB device already owns this instance id: link, do not replace
            g.link("CR1", by_instance[iid.upper()], e, "Stored link: WPD\\Devices names this USB instance id")
            remember(e, container, None)
        else:
            remember(e, container, iid)

    for inst in src.subkeys(HKLM, WPD):
        path = f"{WPD}\\{inst}"
        iid = f"SWD\\WPDBUSENUM\\{inst}"
        k, wv = wpd_keys.pop(iid.upper(), None) or wpd_keys.pop(inst.upper(), None) or ("", {})
        wpd_row(("wpd", iid), iid, "both" if k else "SWD", src.values(HKLM, path), wv, (HKLM, path))
    for iid_u, (k, wv) in wpd_keys.items():
        iid = wpd_key_to_instance(k)
        wpd_row(("wpd", iid), iid, "WPD-Devices", {}, wv, (HKLM, f"{WPD_NAMES}\\{k}"))
    add("C_wpd", "Portable devices (MTP phones, cameras, volumes)",
        "HKLM\\" + WPD + " and HKLM\\" + WPD_NAMES + " - what Windows showed in This PC when the device was plugged "
        "in. One row per device in either key (Source: SWD, WPD-Devices or both; a WPD\\Devices key is the device id "
        "with '#' for '\\'). KeyLastWriteUTC is the SWD key's, or the WPD\\Devices key's for WPD-Devices rows. "
        "PhoneEvidence: the USB phone driver of the same device or ContainerID, or a portable device named by a USB "
        "instance (MTP/PTP)",
        ["Name", "Source", "Manufacturer", "Description", "Driver", "PhoneEvidence", "ContainerID",
         "FirstInstallLocal", "FirstInstallUTC", "KeyLastWriteUTC", "InstanceID"], rows)

    # Bluetooth paired / seen
    rows, bt = [], {}
    for key in src.subkeys(HKLM, BTPORT):
        path = f"{BTPORT}\\{key}"
        v = src.values(HKLM, path)
        cls, is_phone = bt_class(v.get("COD"))
        e = ("bt", key.lower())
        name = bt_name(v.get("Name")) or bt_name(v.get("FriendlyName")) or bt_name(v.get("LEName"))
        g.label[e] = f"Bluetooth {name or mac(key)}"
        bt[key.lower()] = e
        rows.append({"_e": e, "Name": name, "FriendlyName": bt_name(v.get("FriendlyName")),
                     "LEName": bt_name(v.get("LEName")), "Address": mac(key), "Class": cls,
                     "IsPhone": "yes" if is_phone else "", "LastConnectedUTC": filetime(v.get("LastConnected")),
                     "LastSeenUTC": filetime(v.get("LastSeen")), "ManufacturerId": v.get("ManufacturerId", ""),
                     "LMPVersion": v.get("LmpVersion", ""), "VID": v.get("VID", ""), "PID": v.get("PID", ""),
                     "KeyLastWriteUTC": filetime(src.last_write(HKLM, path))})
        remember(e, v.get("LeContainerId"), None)
    add("C_bt", "Bluetooth devices (paired or seen)",
        "HKLM\\" + BTPORT + " - name, class of device (Phone = major class 2) and LastSeen / LastConnected as Windows "
        "stored them. Link keys are never read", ["Name", "FriendlyName", "LEName", "Address", "Class", "IsPhone",
                                                 "LastConnectedUTC", "LastSeenUTC", "ManufacturerId", "LMPVersion",
                                                 "VID", "PID", "KeyLastWriteUTC"], rows)

    # Bluetooth services
    rows = []
    for svc_key in src.subkeys(HKLM, BTENUM):
        um = re.search(r"\{0000([0-9a-fA-F]{4})-0000-1000-8000-00805f9b34fb\}", svc_key)
        uuid = um.group(1).lower() if um else ""
        for inst in src.subkeys(HKLM, f"{BTENUM}\\{svc_key}"):
            path = f"{BTENUM}\\{svc_key}\\{inst}"
            v = src.values(HKLM, path)
            am = re.search(r"[&_]([0-9A-Fa-f]{12})(?:_|$)", inst)   # ...&0&<MAC>_C00000000
            addr = am.group(1).lower() if am else ""
            iid = f"BTHENUM\\{svc_key}\\{inst}"
            e = ("btsvc", iid)
            u128 = re.match(r"\{([0-9a-fA-F-]{36})\}", svc_key)
            service = BT_SERVICES.get(uuid, f"UUID {uuid}" if uuid else
                                      "device node" if svc_key.upper().startswith("DEV_") else
                                      f"vendor-specific service {u128.group(1)}" if u128 else svc_key)
            g.label[e] = f"Bluetooth service {service} on {mac(addr) if addr else '?'}"
            rows.append({"_e": e, "Service": service, "Address": mac(addr) if addr else "",
                         "Name": clean_desc(v.get("FriendlyName")) or clean_desc(v.get("DeviceDesc")),
                         "Driver": v.get("Service", ""), "ContainerID": v.get("ContainerID", ""),
                         "KeyLastWriteUTC": filetime(src.last_write(HKLM, path)), "InstanceID": iid})
            remember(e, v.get("ContainerID"), iid)
            if addr in bt:
                g.link("CR2", bt[addr], e, f"Stored link: the service instance carries this device's address ({service})")
    add("C_btsvc", "Bluetooth services per device",
        "HKLM\\" + BTENUM + " - one row per profile a device offered (Hands-Free, Phonebook, Message Access, ...); names "
        "from the Bluetooth SIG assigned numbers", ["Service", "Address", "Name", "Driver", "ContainerID",
                                                   "KeyLastWriteUTC", "InstanceID"], rows)

    # Bluetooth LE devices and their GATT services
    rows = []
    for base, kind in ((BTLE, "LE device"), (BTLE_SVC, "LE service")):
        for sub in src.subkeys(HKLM, base):
            for inst in src.subkeys(HKLM, f"{base}\\{sub}"):
                path = f"{base}\\{sub}\\{inst}"
                v = src.values(HKLM, path)
                tail = sub.rsplit("_", 1)[-1] if "_" in sub else ""   # ...REV&0006_c0d3fbe8f5a1
                am = re.findall(r"([0-9A-Fa-f]{12})", inst) or re.findall(r"Dev_([0-9A-Fa-f]{12})", sub)
                addr = tail.lower() if re.fullmatch(r"[0-9A-Fa-f]{12}", tail) else am[-1].lower() if am else ""
                um = re.match(r"\{0000([0-9a-fA-F]{4})-0000-1000-8000-00805f9b34fb\}", sub)
                u128 = re.match(r"\{([0-9a-fA-F-]{36})\}", sub)
                service = (BT_SERVICES.get(um.group(1).lower(), f"UUID {um.group(1).lower()}") if um else
                           f"vendor-specific service {u128.group(1)}" if u128 else "")
                iid = f"{base.rsplit(chr(92), 1)[-1]}\\{sub}\\{inst}"
                e = ("btle", iid)
                name = clean_desc(v.get("FriendlyName")) or clean_desc(v.get("DeviceDesc"))
                g.label[e] = f"Bluetooth {kind} {name or service} on {mac(addr) if addr else '?'}"
                rows.append({"_e": e, "Kind": kind, "Name": name, "Service": service,
                             "Address": mac(addr) if addr else "", "ContainerID": v.get("ContainerID", ""),
                             "KeyLastWriteUTC": filetime(src.last_write(HKLM, path)), "InstanceID": iid})
                remember(e, v.get("ContainerID"), iid)
                if addr in bt:
                    g.link("CR2", bt[addr], e, f"Stored link: the LE instance carries this device's address ({kind})")
    add("C_btle", "Bluetooth LE devices and services",
        "HKLM\\" + BTLE + " and HKLM\\" + BTLE_SVC + " - Bluetooth Low Energy devices and the GATT services they "
        "offered", ["Kind", "Name", "Service", "Address", "ContainerID", "KeyLastWriteUTC", "InstanceID"], rows)

    # AutoPlay known devices
    rows = []
    for key in src.subkeys(HKCU, KNOWN):
        path = f"{KNOWN}\\{key}"
        v = src.values(HKCU, path)
        e = ("autoplay", key)
        g.label[e] = f"AutoPlay {v.get('Label', key)}"
        rows.append({"_e": e, "Label": v.get("Label", ""), "ContainerID": v.get("ContainerID", ""),
                     "KeyLastWriteUTC": filetime(src.last_write(HKCU, path)), "Key": key})
        remember(e, v.get("ContainerID"), None)
    add("C_autoplay", "AutoPlay known devices (this user)",
        "HKCU\\" + KNOWN + " - devices this Windows user plugged in, with the label AutoPlay showed",
        ["Label", "ContainerID", "KeyLastWriteUTC", "Key"], rows)

    # first installs
    rows = []
    for devid, (start, title, fname) in install.items():
        if not devid.startswith(DEVICE_PREFIXES):
            continue
        dev = by_instance.get(devid)
        ie = ("install", devid)
        g.label[ie] = f"first install {start} - {devid[:60]}"
        if dev:
            g.link("CR3", dev, ie, "Stored link: setupapi.dev.log names this device id - its first driver install")
        svc = usb_rows[devid]["_svc"] if devid in usb_rows else ""
        rows.append({"_e": ie, "DeviceID": devid, "Category": install_category(devid, svc),
                     "FirstInstallLocal": start, "FirstInstallUTC": to_utc(start), "Section": title, "Log": fname, "MatchedDevice": g.label.get(dev, "") if dev else ""})
    rows.sort(key=lambda r: r["FirstInstallLocal"])
    add("C_install", "First driver install (setupapi.dev.log)",
        SETUPAPI + " and the archived setupapi.dev.<date>.log copies beside it - the first time Windows installed a "
        "driver for each USB, USB storage, portable (SWD-WPD) or Bluetooth device id (earliest across all logs); "
        "local time as logged; UTC converted with this PC's time-zone rules (the offset in force on that date). "
        "Category tells devices that were connected from the PC's own hardware (USB root hubs, and any PCI / HID / ROOT / ACPI id): every row is kept",
        ["FirstInstallLocal", "FirstInstallUTC", "Category", "DeviceID", "Section", "Log", "MatchedDevice"], rows)

    # events
    rows, mtp_phones, capped = [], {}, []
    for ch in CHANNELS:
        got = src.events(ch)
        if len(got) >= EVENT_CAP:
            capped.append(ch.split("/")[0].replace("Microsoft-Windows-", ""))
        for t, eid, data in got:
            hits = [(k, x) for k, x in data.items() if x.upper().startswith(DEVICE_PREFIXES)]
            field, devid = next(((k, x) for k, x in hits if k == "DeviceInstanceId"), hits[0] if hits else ("", ""))
            if ch.endswith("Kernel-PnP/Configuration") and not devid:
                continue
            dev = by_instance.get(devid.upper()) if devid else None
            ee = ("event", f"{ch}|{len(rows)}")
            log = ch.split("/")[0].replace("Microsoft-Windows-", "")
            g.label[ee] = f"event {eid} {log} {t}"
            reported = ""
            if log == "WPD-MTPClassDriver" and eid == "1005":
                reported = ", ".join(x for x in (data.get("Manufacturer"), data.get("Model"), data.get("Version")) if x)
                if reported:
                    p = mtp_phones.setdefault(reported, {"first": t, "last": t, "n": 0, "events": []})
                    p["first"], p["last"] = min(p["first"], t), max(p["last"], t)
                    p["n"] += 1
                    p["events"].append(ee)
            meaning, whose = EVENT_MEANING.get((log, eid), ""), ""
            own = data.get("DeviceInstanceId") or "not named"
            if field == PARENT_FIELD:
                whose = f"event for a child device of {devid}"
            elif field and field != "DeviceInstanceId" and log == "Kernel-PnP":
                whose = f"event for device {own}, matched only through {field} {devid}"
            if whose:
                meaning = f"{meaning} ({whose})" if meaning else f"({whose})"
            if dev:
                g.link("CR4", dev, ee, f"Stored link: event {eid} in {log} names this device id"
                       + (f" as its {field} ({whose})" if whose else ""))
            rows.append({"_e": ee, "TimeUTC": t, "Log": log, "EventID": eid, "Meaning": meaning,
                         "ReportedDevice": reported, "DeviceID": devid, "MatchedField": field,
                         "MatchedDevice": g.label.get(dev, "") if dev else "",
                         "Data": "; ".join(f"{k}={v}" for k, v in data.items() if v and v != devid)[:300]})
    rows.sort(key=lambda r: r["TimeUTC"], reverse=True)
    add("C_events", "Connection events (event logs)",
        "Kernel-PnP/Configuration (device configured / started), WPD-MTP and Bluetooth LE pairing logs. Logs roll over, "
        f"so only recent connections remain; at most the newest {EVENT_CAP:,} events of each log are read"
        + (f" (reached for {', '.join(capped)} - older events are not shown)" if capped else "")
        + ". A Kernel-PnP event is kept when any of its fields is a USB / portable / Bluetooth device id; "
        f"MatchedField says which (DeviceInstanceId = the event's own device; {PARENT_FIELD} = the event is "
        f"for a child device of it; {MATCHING_FIELD} = the driver id matched, for another device) and Meaning says so. "
        "Meaning = Windows' own message text for that event ID; event 1005 names the "
        "connected phone's make, model and OS version", ["TimeUTC", "Log", "EventID", "Meaning", "ReportedDevice",
                                                        "DeviceID", "MatchedField", "MatchedDevice", "Data"], rows)

    # same physical device across sources (ContainerID)
    rel = []
    for cid, ents in by_container.items():
        if len(ents) < 2:
            continue
        for i, a in enumerate(ents):
            for b in ents[i + 1:]:
                g.link("CR1", a, b, "Stored link: same ContainerID - the same physical device")
        for a in ents:
            rel.append({"_e": a, "ContainerID": cid, "Source": a[0], "Device": g.label.get(a, str(a))})
    add("CR1", "Same physical device across USB, MTP, Bluetooth and AutoPlay (ContainerID)",
        "rows that share a ContainerID - Windows' own id for one physical device", ["ContainerID", "Source", "Device"],
        rel, entity=False)

    seen = []   # one entry per interface / source row that looks like a phone
    parent_of_phone = {}
    for child_e, par in usb_parent.items():
        parent_of_phone.setdefault(par["_e"], next(r for r in g.views["C_usb"].rows if r["_e"] == child_e))
    for vid in ("C_usb", "C_wpd", "C_bt"):
        for r in g.views[vid].rows:
            why = r.get("PhoneEvidence") or ("Bluetooth class of device = Phone" if r.get("IsPhone") == "yes" else "") or \
                ("same ContainerID as a USB phone" if vid == "C_wpd" and container_of(r.get("ContainerID"))
                 in phone_containers else "")
            if why:
                # the grouping key for one physical device: ContainerID, else Bluetooth address, else the device id
                phys = container_of(r.get("ContainerID")) or r.get("Address") or r.get("InstanceID", "")
                if r["_e"] in parent_of_phone:      # a composite parent is the same device as its phone interface
                    child = parent_of_phone[r["_e"]]
                    phys = container_of(child["ContainerID"]) or child["InstanceID"]
                serial = ""
                if vid == "C_usb":
                    owner = usb_parent.get(r["_e"], r)
                    serial = owner["SerialOrInstance"] if "&" not in owner["SerialOrInstance"] else ""
                seen.append({"_e": r["_e"], "_named": bool(r.get("Name")),
                             "How": {"C_usb": "USB cable", "C_wpd": "USB cable (MTP)", "C_bt": "Bluetooth"}[vid],
                             "Device": g.label[r["_e"]], "PhysicalDevice": phys, "Serial": serial, "Evidence": why,
                             "LastConnectedUTC": r.get("LastConnectedUTC", ""), "LastSeenUTC": r.get("LastSeenUTC", ""),
                             "KeyLastWriteUTC": r.get("KeyLastWriteUTC", ""),
                             "FirstInstallUTC": r.get("FirstInstallUTC", ""), "FirstSeenUTC": ""})
    for reported, p in sorted(mtp_phones.items(), key=lambda kv: kv[1]["first"]):
        pe = ("mtpphone", reported)
        g.label[pe] = f"phone over USB (MTP): {reported}"
        for ee in p["events"]:
            g.link("CR4", pe, ee, "Stored link: this event 1005 names the phone")
        seen.append({"_e": pe, "_named": True, "How": "USB cable (MTP event 1005)", "Device": reported,
                     "PhysicalDevice": "", "Serial": "",   # make/model/OS only: not keyed as one physical device
                     "Evidence": f"MTP driver customized for this phone ({p['n']} events, "
                                 f"{p['first'][:10]} to {p['last'][:10]}); same make/model/OS \u2013 may be more "
                                 f"than one phone",
                     "LastConnectedUTC": p["last"], "LastSeenUTC": "", "FirstInstallUTC": "",
                     "KeyLastWriteUTC": "", "FirstSeenUTC": p["first"]})
    phones = summarize_phones(seen, g)
    add("C_phones", "Phones connected to this PC (cable or Bluetooth) - summary, one row per physical device",
        "one row per physical device: every row above that looks like a phone, grouped by PhysicalDevice (the "
        "ContainerID, else the Bluetooth address, else the device id), so a phone seen as a USB composite device, its "
        "MTP and modem interfaces and its portable device is one row. SeenAs lists each interface with its evidence; "
        "times are the earliest (First...) and latest (Last..., KeyLastWrite) across them; the per-interface rows stay "
        "in the views above. Rows with no physical device id (MTP event 1005) are not grouped and say so. Evidence: a "
        "phone driver on USB, the MTP portable-device driver, Bluetooth class of device = Phone, or an MTP event 1005 "
        "naming the phone's make, model and OS version (this catches iPhones and other phones without a phone-specific "
        "driver). An empty LastConnected with a LastSeen means Windows saw the phone but stored no connection time",
        ["How", "Device", "PhysicalDevice", "Serial", "Interfaces", "SeenAs", "Evidence", "FirstSeenUTC",
         "LastConnectedUTC", "LastSeenUTC", "FirstInstallUTC", "KeyLastWriteUTC"], phones)
    unread = list(getattr(guarded, "denied", None) or []) + src.failed
    if unread:
        add("C_denied", "Locations this account could not read",
            "access denied (run as administrator to include them) or a read that failed - the views above may be "
            "incomplete", ["Location"], [{"Location": d} for d in sorted(set(unread))], entity=False)
    return vids


def _utc_now():
    """Now in UTC as 'YYYY-MM-DD HH:MM:SS' (a function so tests can fix the clock)."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class CachedSource:
    """Wrap a source so the registry, logs and event logs are read once per viewer session. read_utc is when the
    first read was made - the time of the snapshot every view built from it shows."""

    def __init__(self, src):
        self.src, self.memo = src, {}
        self.logs_in_utc = getattr(src, "logs_in_utc", False)
        self.read_utc = None

    @property
    def denied(self):
        return getattr(self.src, "denied", [])

    def _get(self, name, *args):
        k = (name,) + args
        if k not in self.memo:
            if self.read_utc is None:
                self.read_utc = _utc_now()
            self.memo[k] = getattr(self.src, name)(*args)
        return self.memo[k]

    def subkeys(self, hive, path):
        return self._get("subkeys", hive, path)

    def values(self, hive, path):
        return self._get("values", hive, path)

    def last_write(self, hive, path):
        return self._get("last_write", hive, path)

    def text(self, path):
        return self._get("text", path)

    def setupapi(self):
        return self._get("setupapi")

    def events(self, channel, count=EVENT_CAP):
        return self._get("events", channel, count)


def demo_source():
    """Synthetic registry / logs: one phone seen over USB (MTP) and Bluetooth, one headset, one USB stick.

    Values are invented: VID 1234, documentation MAC range 00:00:5E:00:53:xx (RFC 7042), example names.
    """
    ft = 133500000000000000                     # a FILETIME in 2024
    phone_c, stick_c = "{e5e5e5e5-0000-4000-a000-000000000001}", "{e5e5e5e5-0000-4000-a000-000000000002}"
    k = {}

    def key(hive, path, values=None, subkeys=(), lw=ft):
        k[(hive, path)] = {"values": values or {}, "subkeys": list(subkeys), "lw": lw}

    key(HKLM, USB, subkeys=["VID_1234&PID_0F01", "VID_1234&PID_0F02"])
    key(HKLM, USB + r"\VID_1234&PID_0F01", subkeys=["EXAMPLEPHONE01"])
    key(HKLM, USB + r"\VID_1234&PID_0F01\EXAMPLEPHONE01",
        {"DeviceDesc": "@oem1.inf,%phone%;Example Phone", "Mfg": "ExampleCorp", "Service": "WUDFWpdMtp",
         "ContainerID": phone_c, "HardwareID": ["USB\\VID_1234&PID_0F01"]}, lw=ft + 10 ** 9)
    key(HKLM, USB + r"\VID_1234&PID_0F02", subkeys=["EXAMPLESTICK01"])
    key(HKLM, USB + r"\VID_1234&PID_0F02\EXAMPLESTICK01",
        {"DeviceDesc": "USB Mass Storage Device", "Mfg": "ExampleCorp", "Service": "USBSTOR", "ContainerID": stick_c})
    key(HKLM, WPD, subkeys=["EXAMPLE_MTP_PHONE"])
    key(HKLM, WPD + r"\EXAMPLE_MTP_PHONE", {"FriendlyName": "Example Phone", "Mfg": "ExampleCorp",
                                             "Service": "WUDFWpdFs", "ContainerID": phone_c})
    key(HKLM, WPD_NAMES, subkeys=["EXAMPLE_MTP_PHONE"])
    key(HKLM, WPD_NAMES + r"\EXAMPLE_MTP_PHONE", {"FriendlyName": "Example Phone"})
    key(HKLM, BTPORT, subkeys=["00005e005301", "00005e005302", "00005e005303"])
    key(HKLM, BTPORT + r"\00005e005303", {"LEName": b"Example Watch\x00", "LastSeen": ft})
    key(HKLM, BTPORT + r"\00005e005301", {"Name": b"Example Phone\x00", "COD": 0x000204, "LastSeen": ft + 2 * 10 ** 9,
                                          "LastConnected": ft + 2 * 10 ** 9, "ManufacturerId": 117, "LmpVersion": 10})
    key(HKLM, BTPORT + r"\00005e005302", {"Name": b"Example Headset\x00", "COD": 0x000418, "LastSeen": ft,
                                          "LastConnected": 0})
    key(HKLM, BTENUM, subkeys=["{0000111f-0000-1000-8000-00805f9b34fb}_EXAMPLEMFG&0000",
                               "{0000112f-0000-1000-8000-00805f9b34fb}_EXAMPLEMFG&0000"])
    for svc in ("111f", "112f"):
        sk = "{0000%s-0000-1000-8000-00805f9b34fb}_EXAMPLEMFG&0000" % svc
        key(HKLM, BTENUM + "\\" + sk, subkeys=["7&1a2b3c4d&0&00005E005301_C00000000"])
        key(HKLM, BTENUM + "\\" + sk + "\\7&1a2b3c4d&0&00005E005301_C00000000",
            {"DeviceDesc": "Example Phone", "ContainerID": "{e5e5e5e5-0000-4000-a000-000000000003}"})
    key(HKLM, BTLE, subkeys=["Dev_00005e005303"])
    key(HKLM, BTLE + r"\Dev_00005e005303", subkeys=["7&2b3c4d5e&0&00005e005303"])
    key(HKLM, BTLE + r"\Dev_00005e005303\7&2b3c4d5e&0&00005e005303",
        {"FriendlyName": "Example Watch", "ContainerID": "{e5e5e5e5-0000-4000-a000-000000000004}"})
    key(HKCU, KNOWN, subkeys=["EXAMPLE_KNOWN_1"])
    key(HKCU, KNOWN + r"\EXAMPLE_KNOWN_1", {"Label": "Example Phone", "ContainerID": phone_c})
    key(HKLM, r"SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Keys",
        {"secret": b"MUST-NOT-BE-READ"}, ["00005e005300"])
    setup = ("\n>>>  [Device Install (Hardware initiated) - USB\\VID_1234&PID_0F01\\EXAMPLEPHONE01]\n"
             ">>>  Section start 2030/01/01 12:00:00.000\n<<<  Section end 2030/01/01 12:00:05.000\n")
    events = {"Microsoft-Windows-Kernel-PnP/Configuration": [
        ("2030-01-02 09:00:00", "410", {"DeviceInstanceId": "USB\\VID_1234&PID_0F01\\EXAMPLEPHONE01"}),
        ("2030-01-02 09:00:01", "410", {"DeviceInstanceId": "PCI\\EXAMPLE_NOT_A_PHONE"})],
        "Microsoft-Windows-WPD-MTPClassDriver/Operational": [
        ("2030-01-03 10:00:00", "1005", {"Manufacturer": "ExampleCorp", "Model": "Example Phone 2", "Version": "9.9",
                                         "HackModel": "1"}),
        ("2030-01-04 10:00:00", "1005", {"Manufacturer": "ExampleCorp", "Model": "Example Phone 2", "Version": "9.9",
                                         "HackModel": "1"}),
        ("2030-01-04 10:05:00", "1001", {})]}
    return FakeSource(k, setup, events)
