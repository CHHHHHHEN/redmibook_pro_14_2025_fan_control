#!/usr/bin/env python3
"""Redmi Book Pro 14 2025 (TM2411) EC fan control.

Verified control path (live-tested on this machine):
  - EC command 0x93 writes one byte of EC RAM, 0x92 reads one byte.
  - Manual fan mode:  EC 0x0204 = 0xCC
  - Fan target/level: EC 0x0203 = 0x0217 = 0x1805 = level
  - Level 0x00/0x04 ~= stopped, 0x40 ~= 50%, 0x80 ~= 100%
    (level is 0..128, percent = level * 100 / 128)

The EC briefly kicks the fan to full speed when manual mode starts, then
settles to the requested level.  On exit the tool restores the EC's normal
auto mode and the fan ramps down.

Do not use vendor ACPI/WMI fan interfaces and do not write EC flash.

Usage:
  python fanctl.py status
  python fanctl.py hold 50 4          # 50% for 4 seconds, then restore
  python fanctl.py set 80             # hold 80% until Ctrl+C
  python fanctl.py auto               # stop manual mode, return to EC auto
  python fanctl.py read 0x1805
  python fanctl.py write 0x1805 0x40 --yes
  python fanctl.py dump 0x0200 0x20

Run as Administrator.  Writes are real hardware writes; start with short
holds and keep an eye on temperatures.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import struct
import subprocess
import sys
import time
from ctypes import wintypes

DRIVER_SERVICE = "WinRing0_1_2_0"
DRIVER_DEVICE = "\\??\\" + DRIVER_SERVICE
TS_DEVICE = "\\??\\ThrottleStop"

IOCTL_READ_IO_PORT_BYTE = 0x9C4060CC
IOCTL_WRITE_IO_PORT_BYTE = 0x9C40A0D8
IOCTL_TS_MAP_PHYS = 0x8000645C
IOCTL_TS_UNMAP = 0x80006460

EC_DATA = 0x62
EC_STATUS = 0x66
EC_CMD_READ_RAM = 0x92
EC_CMD_WRITE_RAM = 0x93
EC_IBF = 0x02
EC_OBF = 0x01

# ACPI shared RAM is at PCH MMIO 0xFE0B0300 and maps EC XRAM 0x0300.
MAP_PHYS = 0xFE0B0000
MAP_SIZE = 0x1000
ACPI_BASE = 0x0300

# Verified fan control registers (EC XRAM).
REG_FLAGS = 0x012C
REG_FAN_A_MODE = 0x0204
REG_FAN_A_TARGET = 0x0203
REG_FAN_A_TARGET2 = 0x0217
REG_FAN_A_DUTY = 0x1805
REG_FAN_B_MODE = 0x0272
REG_FAN_B_TARGET = 0x0271
REG_FAN_B_TARGET2 = 0x0285
REG_FAN_B_DUTY = 0x1804
REG_FAN_C_DUTY = 0x1806

REG_TEMPS = 0x0308
TEMP_COUNT = 12
REG_QFAN = 0x0368
REG_CHA1 = 0x03AD
REG_FAN_TARGET = 0x03B1

MANUAL_MODE = 0xCC
LEVEL_MAX = 0x80
TEMP_LIMIT_C = 90


class DriverError(RuntimeError):
    pass


class EcError(RuntimeError):
    pass


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("MaximumLength", ctypes.c_ushort),
        ("Buffer", ctypes.c_wchar_p),
    ]


class OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ulong),
        ("RootDirectory", ctypes.c_void_p),
        ("ObjectName", ctypes.POINTER(UNICODE_STRING)),
        ("Attributes", ctypes.c_ulong),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [
        ("Status", ctypes.c_void_p),
        ("Information", ctypes.c_void_p),
    ]


def _unicode_string(text: str) -> UNICODE_STRING:
    us = UNICODE_STRING()
    us.Buffer = text
    us.Length = len(text) * 2
    us.MaximumLength = (len(text) + 1) * 2
    return us


_NTDLL = ctypes.WinDLL("ntdll")
_K32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _nt_create_file(path: str):
    unicode_name = _unicode_string(path)
    oa = OBJECT_ATTRIBUTES()
    oa.Length = ctypes.sizeof(oa)
    oa.ObjectName = ctypes.pointer(unicode_name)
    oa.Attributes = 0x40  # OBJ_CASE_INSENSITIVE
    ios = IO_STATUS_BLOCK()
    handle = ctypes.c_void_p()
    nt_create_file = _NTDLL.NtCreateFile
    nt_create_file.restype = ctypes.c_long
    nt_create_file.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_ulong,
        ctypes.POINTER(OBJECT_ATTRIBUTES),
        ctypes.POINTER(IO_STATUS_BLOCK),
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
    ]
    status = nt_create_file(
        ctypes.byref(handle),
        0xC0000000,
        ctypes.byref(oa),
        ctypes.byref(ios),
        None,
        0,
        3,
        3,
        0,
        None,
        0,
    )
    if status != 0:
        raise DriverError("NtCreateFile(%s) failed: 0x%08X" % (path, status & 0xFFFFFFFF))
    return handle


class DeviceIo:
    def __init__(self) -> None:
        self._device_io_control = _K32.DeviceIoControl
        self._device_io_control.restype = wintypes.BOOL
        self._device_io_control.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]

    def call(self, handle, code, in_buf, in_size, out_size=0):
        in_buffer = ctypes.create_string_buffer(in_buf, max(1, len(in_buf)))
        out_buffer = ctypes.create_string_buffer(max(1, out_size))
        returned = wintypes.DWORD(0)
        ok = self._device_io_control(
            handle,
            code,
            in_buffer,
            in_size,
            out_buffer if out_size else None,
            out_size,
            ctypes.byref(returned),
            None,
        )
        if not ok:
            raise DriverError(
                "DeviceIoControl 0x%08X failed: %s"
                % (code, ctypes.get_last_error())
            )
        return out_buffer.raw[:out_size]


def _ensure_winring0() -> None:
    driver_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "WinRing0x64.sys"
    )
    subprocess.run(["sc", "start", DRIVER_SERVICE], capture_output=True, text=True)
    try:
        _nt_create_file(DRIVER_DEVICE)
        return
    except DriverError:
        pass
    if not os.path.exists(driver_path):
        raise DriverError(
            "WinRing0x64.sys not found next to fanctl.py; cannot start driver"
        )
    subprocess.run(
        [
            "sc",
            "create",
            DRIVER_SERVICE,
            "binPath=",
            driver_path,
            "type=",
            "kernel",
            "start=",
            "demand",
        ],
        capture_output=True,
        text=True,
    )
    subprocess.run(["sc", "start", DRIVER_SERVICE], capture_output=True, text=True)


class WinRing0Port:
    def __init__(self) -> None:
        self._io = DeviceIo()
        try:
            self._handle = _nt_create_file(DRIVER_DEVICE)
        except DriverError:
            _ensure_winring0()
            self._handle = _nt_create_file(DRIVER_DEVICE)
        self._k32 = _K32

    def close(self) -> None:
        if self._handle:
            self._k32.CloseHandle(self._handle)
            self._handle = None

    def read_byte(self, port: int) -> int:
        data = self._io.call(
            self._handle,
            IOCTL_READ_IO_PORT_BYTE,
            struct.pack("<H", port),
            2,
            2,
        )
        return data[0]

    def write_byte(self, port: int, value: int) -> None:
        self._io.call(
            self._handle,
            IOCTL_WRITE_IO_PORT_BYTE,
            struct.pack("<I", port) + bytes([value & 0xFF]),
            5,
            0,
        )


class ThrottleStopMap:
    """Optional fast view of the PCH EC-RAM window via ThrottleStop.sys."""

    def __init__(self) -> None:
        self._io = DeviceIo()
        self._handle = _nt_create_file(TS_DEVICE)
        data = self._io.call(
            self._handle,
            IOCTL_TS_MAP_PHYS,
            struct.pack("<QI", MAP_PHYS, MAP_SIZE),
            12,
            8,
        )
        self.address = struct.unpack("<Q", data)[0]
        self.buf = (ctypes.c_ubyte * MAP_SIZE).from_address(self.address)

    def close(self) -> None:
        if getattr(self, "address", 0):
            try:
                self._io.call(
                    self._handle,
                    IOCTL_TS_UNMAP,
                    struct.pack("<Q", self.address),
                    8,
                    0,
                )
            finally:
                self.address = 0
        if self._handle:
            _K32.CloseHandle(self._handle)
            self._handle = None

    def read(self, addr: int) -> int:
        return self.buf[addr]

    def write(self, addr: int, value: int) -> None:
        self.buf[addr] = value

    def read_block(self, addr: int, size: int) -> bytes:
        return bytes(self.buf[addr : addr + size])


class Ec:
    def __init__(self, port: WinRing0Port, timeout: float = 0.15) -> None:
        self.port = port
        self.timeout = timeout
        self.read_count = 0
        self.write_count = 0

    def _wait(self, predicate, what: str) -> bool:
        deadline = time.perf_counter() + self.timeout
        while time.perf_counter() < deadline:
            if predicate(self.port.read_byte(EC_STATUS)):
                return True
            time.sleep(0.0001)
        return False

    def _resync(self) -> None:
        try:
            self.port.read_byte(EC_STATUS)
            if self.port.read_byte(EC_STATUS) & EC_OBF:
                self.port.read_byte(EC_DATA)
        except Exception:
            pass
        time.sleep(0.02)

    def _ibf_clear(self) -> bool:
        return self._wait(lambda s: not (s & EC_IBF), "IBF clear")

    def _obf_set(self) -> bool:
        return self._wait(lambda s: bool(s & EC_OBF), "OBF set")

    def read(self, addr: int) -> int:
        last_error = None
        for _ in range(5):
            try:
                if not self._ibf_clear():
                    raise EcError("IBF clear timeout")
                if self.port.read_byte(EC_STATUS) & EC_OBF:
                    self.port.read_byte(EC_DATA)
                self.port.write_byte(EC_STATUS, EC_CMD_READ_RAM)
                if not self._ibf_clear():
                    raise EcError("IBF clear timeout")
                self.port.write_byte(EC_DATA, (addr >> 8) & 0xFF)
                if not self._ibf_clear():
                    raise EcError("IBF clear timeout")
                self.port.write_byte(EC_DATA, addr & 0xFF)
                if not self._ibf_clear():
                    raise EcError("IBF clear timeout")
                if not self._obf_set():
                    raise EcError("OBF set timeout")
                value = self.port.read_byte(EC_DATA)
                self.read_count += 1
                return value
            except Exception as exc:
                last_error = exc
                self._resync()
        raise EcError("EC read 0x%04X failed: %s" % (addr, last_error))

    def write(self, addr: int, value: int) -> None:
        last_error = None
        for _ in range(5):
            try:
                if not self._ibf_clear():
                    raise EcError("IBF clear timeout")
                if self.port.read_byte(EC_STATUS) & EC_OBF:
                    self.port.read_byte(EC_DATA)
                self.port.write_byte(EC_STATUS, EC_CMD_WRITE_RAM)
                if not self._ibf_clear():
                    raise EcError("IBF clear timeout")
                self.port.write_byte(EC_DATA, (addr >> 8) & 0xFF)
                if not self._ibf_clear():
                    raise EcError("IBF clear timeout")
                self.port.write_byte(EC_DATA, addr & 0xFF)
                if not self._ibf_clear():
                    raise EcError("IBF clear timeout")
                self.port.write_byte(EC_DATA, value & 0xFF)
                if not self._ibf_clear():
                    raise EcError("IBF clear timeout")
                self.write_count += 1
                return
            except Exception as exc:
                last_error = exc
                self._resync()
        raise EcError("EC write 0x%04X failed: %s" % (addr, last_error))

    def read_block(self, addr: int, size: int) -> bytes:
        return bytes(self.read(addr + i) for i in range(size))


def percent_to_level(percent: float) -> int:
    if percent <= 0:
        return 0
    if percent >= 100:
        return LEVEL_MAX
    return max(0, min(LEVEL_MAX, int(round(percent * LEVEL_MAX / 100.0))))


def level_to_percent(level: int) -> float:
    return level * 100.0 / LEVEL_MAX


def read_temps(
    ec: Ec,
    ts: ThrottleStopMap | None,
    start: int = 0,
    count: int = TEMP_COUNT,
) -> list[int]:
    if ts is not None:
        return list(ts.read_block(REG_TEMPS + start, count))
    try:
        return list(ec.read_block(REG_TEMPS + start, count))
    except EcError:
        return []


def current_level(ec: Ec) -> int:
    try:
        return ec.read(REG_FAN_A_DUTY)
    except EcError:
        return -1


def max_temp(temps: list[int]) -> int:
    valid = [t for t in temps if 0 < t < 128]
    return max(valid) if valid else 0


def apply_level(ec: Ec, level: int, manual: bool = True) -> None:
    if manual:
        ec.write(REG_FLAGS, 0x00)
        ec.write(REG_FAN_A_MODE, MANUAL_MODE)
        ec.write(REG_FAN_A_TARGET, level)
        ec.write(REG_FAN_A_TARGET2, level)
    ec.write(REG_FAN_A_DUTY, level)


def apply_level_b(ec: Ec, level: int, manual: bool = True) -> None:
    if manual:
        ec.write(REG_FLAGS, 0x00)
        ec.write(REG_FAN_B_MODE, MANUAL_MODE)
        ec.write(REG_FAN_B_TARGET, level)
        ec.write(REG_FAN_B_TARGET2, level)
    ec.write(REG_FAN_B_DUTY, level)


def apply_levels(ec: Ec, level_a: int, level_b: int, manual: bool = True) -> None:
    """Write both fan channels in one pass."""
    if manual:
        ec.write(REG_FLAGS, 0x00)
        ec.write(REG_FAN_A_MODE, MANUAL_MODE)
        ec.write(REG_FAN_A_TARGET, level_a)
        ec.write(REG_FAN_A_TARGET2, level_a)
        ec.write(REG_FAN_B_MODE, MANUAL_MODE)
        ec.write(REG_FAN_B_TARGET, level_b)
        ec.write(REG_FAN_B_TARGET2, level_b)
    ec.write(REG_FAN_A_DUTY, level_a)
    ec.write(REG_FAN_B_DUTY, level_b)


def restore_auto(ec: Ec, settle: float = 0.0) -> None:
    # Leaving manual mode makes the EC ramp back to its own curve.
    try:
        ec.write(REG_FAN_A_MODE, 0x00)
        ec.write(REG_FAN_A_TARGET, 0x00)
        ec.write(REG_FAN_A_TARGET2, 0x00)
        ec.write(REG_FAN_A_DUTY, 0x00)
        ec.write(REG_FAN_B_MODE, 0x00)
        ec.write(REG_FAN_B_TARGET, 0x00)
        ec.write(REG_FAN_B_TARGET2, 0x00)
        ec.write(REG_FAN_B_DUTY, 0x00)
        ec.write(REG_FLAGS, 0x00)
    except EcError as exc:
        print("restore warning: %s" % exc, file=sys.stderr)
    if settle:
        time.sleep(settle)


def run_control(ec: Ec, ts: ThrottleStopMap | None, percent: float, seconds: float | None) -> int:
    level = percent_to_level(percent)
    print(
        "requested %.1f%% -> EC level 0x%02X (readback will settle near this)"
        % (percent, level)
    )
    started = time.perf_counter()
    last_report = 0.0
    last_temps = -1.0
    temps: list[int] = []
    try:
        while True:
            now = time.perf_counter()
            if seconds is not None and now - started >= seconds:
                break

            if now - last_temps >= 1.0:
                temps = read_temps(ec, ts)
                last_temps = now
                if max_temp(temps) >= TEMP_LIMIT_C:
                    level = LEVEL_MAX
                    print(
                        "  temperature >= %dC, forcing 100%%" % TEMP_LIMIT_C,
                        file=sys.stderr,
                    )

            apply_level(ec, level, manual=True)

            if now - last_report >= 1.0:
                last_report = now
                duty = current_level(ec)
                print(
                    "  t=%5.1fs  level=0x%02X  duty=%s  max_temp=%dC  temps=%s"
                    % (
                        now - started,
                        level,
                        ("0x%02X" % duty) if duty >= 0 else "?",
                        max_temp(temps),
                        " ".join("%d" % t for t in temps if t > 0),
                    )
                )
    except KeyboardInterrupt:
        print("\ninterrupted; restoring EC auto mode")
    finally:
        restore_auto(ec, settle=0.3)
    print("restored EC auto mode; fan will ramp down")
    return 0


def cmd_status(ec: Ec, ts: ThrottleStopMap | None, _args) -> int:
    if ts is not None:
        version = ts.read_block(ACPI_BASE, 5)
        temps = list(ts.read_block(REG_TEMPS, TEMP_COUNT))
        qfan = ts.read(REG_QFAN)
        cha1 = ts.read(REG_CHA1)
        target = ts.read(REG_FAN_TARGET)
    else:
        version = ec.read_block(ACPI_BASE, 5)
        temps = list(ec.read_block(REG_TEMPS, TEMP_COUNT))
        qfan = ec.read(REG_QFAN)
        cha1 = ec.read(REG_CHA1)
        target = ec.read(REG_FAN_TARGET)
    print("EC version      : %s" % ".".join("%02X" % b for b in version))
    print("Temps (TSR0..B) : %s" % " ".join("%02X" % b for b in temps))
    print("Temps (dec)     : %s" % " ".join(str(b) for b in temps))
    print("QFAN  (0x0368)  : 0x%02X (%d)" % (qfan, qfan))
    print("CHA1  (0x03AD)  : 0x%02X" % cha1)
    print("Fan target 03B1 : 0x%02X" % target)
    print(
        "Fan A mode/target/target2/duty: 0x%02X / 0x%02X / 0x%02X / 0x%02X"
        % (
            ec.read(REG_FAN_A_MODE),
            ec.read(REG_FAN_A_TARGET),
            ec.read(REG_FAN_A_TARGET2),
            ec.read(REG_FAN_A_DUTY),
        )
    )
    print(
        "Fan B mode/target/target2/duty: 0x%02X / 0x%02X / 0x%02X / 0x%02X"
        % (
            ec.read(REG_FAN_B_MODE),
            ec.read(REG_FAN_B_TARGET),
            ec.read(REG_FAN_B_TARGET2),
            ec.read(REG_FAN_B_DUTY),
        )
    )
    print(
        "PWM test regs 1612/1613/1614/162F: 0x%02X / 0x%02X / 0x%02X / 0x%02X"
        % (
            ec.read(0x1612),
            ec.read(0x1613),
            ec.read(0x1614),
            ec.read(0x162F),
        )
    )
    return 0


def cmd_hold(ec: Ec, ts: ThrottleStopMap | None, args) -> int:
    return run_control(ec, ts, args.percent, args.seconds)


def cmd_set(ec: Ec, ts: ThrottleStopMap | None, args) -> int:
    return run_control(ec, ts, args.percent, None)


def cmd_auto(ec: Ec, _ts: ThrottleStopMap | None, _args) -> int:
    restore_auto(ec, settle=0.5)
    print("EC auto mode restored")
    return 0


def cmd_read(ec: Ec, _ts, args) -> int:
    value = ec.read(args.addr)
    print("0x%04X = 0x%02X (%d)" % (args.addr, value, value))
    return 0


def cmd_write(ec: Ec, _ts, args) -> int:
    if not args.yes:
        print("Refusing to write without --yes.", file=sys.stderr)
        return 2
    old = ec.read(args.addr)
    ec.write(args.addr, args.value)
    new = ec.read(args.addr)
    print(
        "0x%04X: 0x%02X -> 0x%02X (requested 0x%02X)"
        % (args.addr, old, new, args.value)
    )
    return 0


def cmd_dump(ec: Ec, _ts, args) -> int:
    data = ec.read_block(args.addr, args.size)
    for offset in range(0, len(data), 16):
        chunk = data[offset : offset + 16]
        hex_part = " ".join("%02X" % b for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print("%04X  %-47s  %s" % (args.addr + offset, hex_part, ascii_part))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="read decoded EC status")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("hold", help="hold a fan percentage for N seconds")
    p.add_argument("percent", type=float)
    p.add_argument("seconds", type=float)
    p.set_defaults(func=cmd_hold)

    p = sub.add_parser("set", help="hold a fan percentage until Ctrl+C")
    p.add_argument("percent", type=float)
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("auto", help="restore EC automatic fan control")
    p.set_defaults(func=cmd_auto)

    p = sub.add_parser("read", help="read one EC RAM byte")
    p.add_argument("addr", type=lambda s: int(s, 0))
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("write", help="write one EC RAM byte")
    p.add_argument("addr", type=lambda s: int(s, 0))
    p.add_argument("value", type=lambda s: int(s, 0))
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_write)

    p = sub.add_parser("dump", help="dump EC RAM")
    p.add_argument("addr", type=lambda s: int(s, 0))
    p.add_argument("size", type=lambda s: int(s, 0), nargs="?", default=0x20)
    p.set_defaults(func=cmd_dump)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    port = WinRing0Port()
    ts = None
    try:
        ec = Ec(port)
        try:
            ts = ThrottleStopMap()
        except Exception as exc:
            print(
                "note: ThrottleStop map unavailable (%s); using slow EC reads"
                % exc,
                file=sys.stderr,
            )
        return args.func(ec, ts, args)
    finally:
        if ts is not None:
            ts.close()
        port.close()


if __name__ == "__main__":
    raise SystemExit(main())
