"""HID++ 2.0 request/response handling.

A HID++ 2.0 message is::

    report_id | device_index | feature_index | function<<4 | sw_id | params...

Short reports (0x10) carry 3 parameter bytes, long reports (0x11) carry 16.
Replies echo feature_index and function/sw_id; errors come back as feature
index 0xFF (HID++ 2.0) or 0x8F (HID++ 1.0). Messages with sw_id 0 are
unsolicited notifications from the device.
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from . import features as F
from .transport import REPORT_LONG, REPORT_SHORT, REPORT_SIZES

log = logging.getLogger(__name__)

# Software id used to tag our requests. Every program that has the hidraw node
# open sees every reply, so the GUI/CLI (0x0B) and the daemon (DAEMON_SW_ID)
# use different ids, and Solaar uses others again.
SW_ID = 0x0B
DAEMON_SW_ID = 0x0C

ERROR_NAMES = {
    0x00: "no error",
    0x01: "unknown",
    0x02: "invalid argument",
    0x03: "out of range",
    0x04: "hardware error",
    0x05: "not allowed",
    0x06: "invalid feature index",
    0x07: "invalid function",
    0x08: "busy",
    0x09: "unsupported",
}
ERR_BUSY = 0x08

HIDPP10_ERROR = 0x8F
HIDPP20_ERROR = 0xFF

# HID++ 1.0 errors come from the receiver rather than the device.
HIDPP10_ERRORS = {
    0x01: "invalid sub id",
    0x02: "invalid address",
    0x03: "invalid value",
    0x04: "connection failed",
    0x05: "too many devices",
    0x06: "already exists",
    0x07: "busy",
    0x08: "unknown device",
    0x09: "resource error",
    0x0A: "request unavailable",
    0x0B: "invalid parameter value",
    0x0C: "wrong PIN code",
}


class HIDPPError(Exception):
    def __init__(self, code: int, feature_index: int, function: int, feature_id: int | None = None):
        self.code = code
        self.feature_index = feature_index
        self.function = function
        self.feature_id = feature_id
        what = F.name(feature_id) if feature_id is not None else f"index {feature_index}"
        super().__init__(f"HID++ error {code:#04x} ({ERROR_NAMES.get(code, '?')}) from {what} fn {function}")


class FeatureNotSupported(Exception):
    pass


class DeviceOffline(Exception):
    """The receiver reports the device is not connected (asleep or switched off)."""


@dataclass(frozen=True)
class Message:
    report_id: int
    devnumber: int
    sub_id: int
    address: int
    data: bytes

    @property
    def function(self) -> int:
        return self.address >> 4

    @property
    def swid(self) -> int:
        return self.address & 0x0F

    @classmethod
    def parse(cls, raw: bytes) -> "Message | None":
        if len(raw) < 4 or raw[0] not in REPORT_SIZES:
            return None
        return cls(raw[0], raw[1], raw[2], raw[3], bytes(raw[4:]))

    def __str__(self) -> str:
        return f"[{self.report_id:02x} {self.devnumber:02x} {self.sub_id:02x} {self.address:02x}] {self.data.hex()}"


def pack_params(params: Iterable[int | bytes | bytearray]) -> bytes:
    out = bytearray()
    for p in params:
        if isinstance(p, (bytes, bytearray)):
            out += p
        else:
            out.append(p & 0xFF)
    return bytes(out)


class Device:
    """One HID++ 2.0 device reachable through a hidraw node.

    Requests are serialised by a lock. Without a listener thread, ``request``
    reads the node itself and hands any unrelated messages to the
    notification handlers. With ``start_listener`` a background thread owns
    reading and requests wait for the thread to deliver their reply.
    """

    def __init__(self, transport, *, devnumber: int = 0xFF, long_only: bool = False, short_ok: bool = True, swid: int = SW_ID):
        self.transport = transport
        self.swid = swid
        self.devnumber = devnumber
        self.long_only = long_only or not short_ok
        self._lock = threading.RLock()
        self._handlers: list[Callable[[Message], None]] = []
        self._listener: threading.Thread | None = None
        self._stop = threading.Event()
        self._waiting: tuple[int, int] | None = None
        self._reply: Message | None = None
        self._reply_ready = threading.Event()
        self.features: dict[int, int] = {}  # feature id -> index
        self.feature_meta: dict[int, tuple[int, int, int]] = {}  # index -> (id, type, version)
        self.protocol: tuple[int, int] | None = None

    # -- low level ---------------------------------------------------------
    def _build(self, sub_id: int, address: int, params: bytes) -> bytes:
        if len(params) <= 3 and not self.long_only:
            report = REPORT_SHORT
        else:
            report = REPORT_LONG
        size = REPORT_SIZES[report]
        if len(params) > size - 4:
            raise ValueError(f"too many parameters ({len(params)}) for HID++ report")
        return bytes([report, self.devnumber, sub_id, address]) + params.ljust(size - 4, b"\x00")

    @staticmethod
    def _matches(msg: Message, sub_id: int, address: int) -> bool:
        if msg.sub_id == sub_id and msg.address == address:
            return True
        if msg.sub_id in (HIDPP20_ERROR, HIDPP10_ERROR) and len(msg.data) >= 2:
            return msg.address == sub_id and msg.data[0] == address
        return False

    def _dispatch(self, msg: Message) -> None:
        for h in list(self._handlers):
            try:
                h(msg)
            except Exception:  # a broken handler must not kill the reader
                log.exception("notification handler failed")

    def _raise_if_error(self, msg: Message, sub_id: int, function: int) -> bytes:
        if msg.sub_id == HIDPP10_ERROR and msg.address == sub_id:
            code = msg.data[1]
            if code in (0x04, 0x08):
                raise DeviceOffline(f"device not connected ({HIDPP10_ERRORS[code]}); wake it by pressing a key or button")
            raise HIDPPError(code, sub_id, function, self.feature_meta.get(sub_id, (None,))[0])
        if msg.sub_id == HIDPP20_ERROR and msg.address == sub_id:
            code = msg.data[1]
            fid = self.feature_meta.get(sub_id, (None,))[0]
            raise HIDPPError(code, sub_id, function, fid)
        return msg.data

    def request(self, sub_id: int, function: int, params: bytes = b"", *, timeout: float = 2.0, retries: int = 3) -> bytes:
        address = (function << 4) | self.swid
        packet = self._build(sub_id, address, params)
        for attempt in range(retries + 1):
            with self._lock:
                msg = self._exchange(packet, sub_id, address, timeout)
            if msg is None:
                raise TimeoutError(f"no reply from device for feature index {sub_id} fn {function} (asleep or out of range?)")
            try:
                return self._raise_if_error(msg, sub_id, function)
            except HIDPPError as e:
                if e.code == ERR_BUSY and attempt < retries:
                    time.sleep(0.03 * (attempt + 1))
                    continue
                raise
        raise AssertionError("unreachable")

    def _exchange(self, packet: bytes, sub_id: int, address: int, timeout: float) -> Message | None:
        deadline = time.monotonic() + timeout
        if self._listener is not None:
            self._reply = None
            self._reply_ready.clear()
            self._waiting = (sub_id, address)
            try:
                self.transport.write(packet)
                if self._reply_ready.wait(timeout):
                    return self._reply
                return None
            finally:
                self._waiting = None
        self.transport.write(packet)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            raw = self.transport.read(remaining)
            if raw is None:
                return None
            msg = Message.parse(raw)
            if msg is None:
                continue
            if self._matches(msg, sub_id, address):
                return msg
            self._dispatch(msg)

    def send_no_reply(self, sub_id: int, function: int, params: bytes = b"") -> None:
        with self._lock:
            self.transport.write(self._build(sub_id, (function << 4) | self.swid, params))

    # -- notifications -----------------------------------------------------
    def add_handler(self, handler: Callable[[Message], None]) -> None:
        self._handlers.append(handler)

    def remove_handler(self, handler: Callable[[Message], None]) -> None:
        if handler in self._handlers:
            self._handlers.remove(handler)

    def start_listener(self) -> None:
        if self._listener is not None:
            return
        self._stop.clear()
        self._listener = threading.Thread(target=self._listen, name=f"hidpp-{self.transport.path}", daemon=True)
        self._listener.start()

    def stop_listener(self) -> None:
        if self._listener is None:
            return
        self._stop.set()
        self._listener.join(timeout=2)
        self._listener = None

    def _listen(self) -> None:
        while not self._stop.is_set():
            try:
                raw = self.transport.read(0.25)
            except OSError as e:
                log.warning("reading %s failed: %s", self.transport.path, e)
                self._stop.set()
                break
            if raw is None:
                continue
            msg = Message.parse(raw)
            if msg is None:
                continue
            waiting = self._waiting
            if waiting is not None and self._matches(msg, *waiting):
                self._reply = msg
                self._reply_ready.set()
                continue
            self._dispatch(msg)

    def poll(self, timeout: float) -> None:
        """Without a listener thread: read for ``timeout`` seconds and dispatch notifications."""
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            with self._lock:
                raw = self.transport.read(min(remaining, 0.25))
            if raw is None:
                continue
            msg = Message.parse(raw)
            if msg is not None:
                self._dispatch(msg)

    # -- HID++ 2.0 root and feature table -----------------------------------
    def ping(self, timeout: float = 2.0) -> tuple[int, int]:
        data = self.request(0x00, 0x1, bytes([0, 0, 0x5A]), timeout=timeout, retries=5)
        self.protocol = (data[0], data[1])
        return self.protocol

    def get_feature_index(self, feature_id: int) -> int:
        if feature_id in self.features:
            return self.features[feature_id]
        data = self.request(0x00, 0x0, struct.pack(">H", feature_id))
        index = data[0]
        if index == 0 and feature_id != F.ROOT:
            raise FeatureNotSupported(F.name(feature_id))
        self.features[feature_id] = index
        self.feature_meta[index] = (feature_id, data[1], data[2])
        return index

    def discover_features(self) -> dict[int, int]:
        """Read the full feature table through IFeatureSet (0x0001)."""
        fs = self.get_feature_index(F.FEATURE_SET)
        count = self.request(fs, 0x0)[0]
        self.features[F.ROOT] = 0
        self.feature_meta[0] = (F.ROOT, 0, 0)
        for index in range(1, count + 1):
            data = self.request(fs, 0x1, bytes([index]))
            fid = struct.unpack(">H", data[0:2])[0]
            self.features[fid] = index
            self.feature_meta[index] = (fid, data[2], data[3])
        return dict(self.features)

    def has(self, feature_id: int) -> bool:
        try:
            self.get_feature_index(feature_id)
            return True
        except FeatureNotSupported:
            return False

    def call(self, feature_id: int, function: int, *params: int | bytes, timeout: float = 2.0) -> bytes:
        """Call ``function`` of feature ``feature_id`` with the given parameter bytes."""
        index = self.get_feature_index(feature_id)
        return self.request(index, function, pack_params(params), timeout=timeout)

    def close(self) -> None:
        self.stop_listener()
        self.transport.close()
