"""MiOT authentication and encrypted GATT transport for CUKTECH chargers."""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import struct
from collections.abc import Callable
from typing import Any

from bleak import BleakClient
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.hmac import HMAC
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .ble_protocol import (
    CHAR_AUTH_CTRL,
    CHAR_AUTH_DATA,
    CHAR_CMD_RECV,
    CHAR_CMD_SEND,
    CHAR_DEVICE_INFO,
    CHAR_FW_VERSION,
    parse_property_value,
)

_LOGGER = logging.getLogger(__name__)


class AuthenticationError(ConnectionError):
    """Raised when the charger rejects or does not complete MiOT login."""


class CuktechBLEController:
    """Protocol controller operating on an externally established Bleak client."""

    def __init__(
        self,
        client: BleakClient,
        token: bytes,
        ble_key: bytes | None = None,
        *,
        push_callback: Callable[[bytes], None] | None = None,
    ) -> None:
        if len(token) != 12:
            raise ValueError("token must be 12 bytes")
        if ble_key is not None and len(ble_key) != 16:
            raise ValueError("ble_key must be 16 bytes")
        self.client = client
        self.token = token
        self.ble_key = ble_key
        self.authenticated = False
        self.device_model = ""
        self.firmware_version = ""
        self._queues: dict[str, asyncio.Queue[bytes]] = {}
        self._notifying: dict[str, str] = {}
        self._session_keys: dict[str, bytes] | None = None
        self._send_iteration = 0
        self._miot_sequence = 1
        self._device_counter = 0
        self._decrypt_failures = 0
        self._queue_overflow = False
        self._push_callback = push_callback

    def _notification_handler(self, name: str):
        queue = self._queues.setdefault(name, asyncio.Queue(maxsize=256))

        def handler(_sender: Any, data: bytearray) -> None:
            try:
                queue.put_nowait(bytes(data))
            except asyncio.QueueFull:
                # Losing a transport fragment invalidates this session.
                self._queue_overflow = True

        return handler

    async def async_setup(self) -> None:
        """Subscribe all transport channels on the already connected client."""
        if not self.client.is_connected:
            raise ConnectionError("Bleak client is not connected")
        self.authenticated = False
        self._session_keys = None
        for queue in self._queues.values():
            self._drain_queue(queue)
        for characteristic, name in (
            (CHAR_AUTH_CTRL, "auth_ctrl"),
            (CHAR_AUTH_DATA, "auth_data"),
            (CHAR_CMD_SEND, "cmd_send"),
            (CHAR_CMD_RECV, "cmd_recv"),
        ):
            await self.client.start_notify(characteristic, self._notification_handler(name))
            self._notifying[name] = characteristic

    async def async_close(self) -> None:
        """Stop notifications without taking ownership of client disconnection."""
        self.authenticated = False
        for name, characteristic in tuple(self._notifying.items()):
            if self.client.is_connected:
                try:
                    await asyncio.wait_for(self.client.stop_notify(characteristic), timeout=3)
                except Exception:  # Device may already be gone.
                    pass
            self._notifying.pop(name, None)
        for queue in self._queues.values():
            self._drain_queue(queue)

    @staticmethod
    def _drain_queue(queue: asyncio.Queue[bytes]) -> None:
        while not queue.empty():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def wait_notify(self, name: str, timeout: float = 5.0) -> bytes | None:
        if self._queue_overflow:
            raise ConnectionError("BLE notification queue overflow")
        queue = self._queues.get(name)
        if queue is None:
            return None
        try:
            return await asyncio.wait_for(queue.get(), timeout)
        except TimeoutError:
            return None

    async def read_device_info(self) -> tuple[str, str]:
        """Read model and firmware before authentication."""
        await self.client.start_notify(CHAR_DEVICE_INFO, self._notification_handler("device_info"))
        self._notifying["device_info"] = CHAR_DEVICE_INFO
        try:
            await self.client.write_gatt_char(CHAR_DEVICE_INFO, b"\x00", response=False)
            await self.wait_notify("device_info")
            await self.client.write_gatt_char(CHAR_DEVICE_INFO, b"\x03", response=False)
            data = await self.wait_notify("device_info")
            if data and len(data) > 2:
                chip = data[2 : 2 + data[1]].decode("ascii", errors="replace")
                self.device_model = f"njcuk.fitting.ad1204_{chip}"
            firmware = await self.client.read_gatt_char(CHAR_FW_VERSION)
            self.firmware_version = (
                bytes(firmware).rstrip(b"\x00").decode("ascii", errors="replace")
            )
        finally:
            if self.client.is_connected:
                await self.client.stop_notify(CHAR_DEVICE_INFO)
            self._notifying.pop("device_info", None)
        return self.device_model, self.firmware_version

    async def authenticate(self) -> None:
        """Perform MiOT token login and initialize encrypted command transport."""
        auth_data = self._queues["auth_data"]
        self._drain_queue(auth_data)
        await self.client.write_gatt_char(CHAR_AUTH_CTRL, b"\xa4", response=False)
        init_response = await self.wait_notify("auth_data", 3.0)
        if not init_response:
            raise AuthenticationError("MiOT initialization timed out")
        ack = bytearray(init_response)
        if len(ack) >= 3:
            ack[2] += 1
        await self.client.write_gatt_char(CHAR_AUTH_DATA, ack, response=False)

        key_data = await self.wait_notify("auth_data", 5.0)
        if key_data and len(key_data) < 20 and len(key_data) >= 3 and key_data[2] == 0x04:
            for _ in range(3):
                candidate = await self.wait_notify("auth_data", 3.0)
                if candidate and len(candidate) >= 20 and candidate[2] == 0x04:
                    key_data = candidate
                    break
        if not key_data or len(key_data) < 20:
            raise AuthenticationError("MiOT key exchange timed out")
        placeholder = b"\x00\x00\x05\x01" + b"\xf2" * max(0, len(key_data) - 4)
        await self.client.write_gatt_char(CHAR_AUTH_DATA, placeholder, response=False)
        await asyncio.sleep(0.6)
        self._drain_queue(auth_data)

        await self.client.write_gatt_char(CHAR_AUTH_CTRL, b"\x24\x00\x00\x00", response=False)
        random_key = secrets.token_bytes(16)
        await self.client.write_gatt_char(
            CHAR_AUTH_DATA, b"\x00\x00\x00\x0b\x01\x00", response=False
        )
        for _ in range(5):
            if await self.wait_notify("auth_data", 3.0) == b"\x00\x00\x01\x01":
                break
        else:
            raise AuthenticationError("Device was not ready for login key")
        await self.client.write_gatt_char(CHAR_AUTH_DATA, b"\x01\x00" + random_key, response=False)
        if await self.wait_notify("auth_data", 3.0) != b"\x00\x00\x01\x00":
            raise AuthenticationError("Device did not acknowledge login key")

        device_random = await self._receive_auth_value()
        device_hmac = await self._receive_auth_value()
        if not device_random or len(device_random) < 16 or not device_hmac or len(device_hmac) < 32:
            raise AuthenticationError("Invalid MiOT authentication response")
        device_random = device_random[:16]
        salt = random_key + device_random
        derived = HKDF(
            algorithm=hashes.SHA256(),
            length=64,
            salt=salt,
            info=b"mible-login-info",
            backend=default_backend(),
        ).derive(self.token)
        self._session_keys = {
            "dev_key": derived[0:16],
            "app_key": derived[16:32],
            "dev_iv": derived[32:36],
            "app_iv": derived[36:40],
        }
        verifier = HMAC(self._session_keys["dev_key"], hashes.SHA256())
        verifier.update(device_random + random_key)
        if not hmac.compare_digest(verifier.finalize(), device_hmac[:32]):
            raise AuthenticationError("Device HMAC validation failed")
        signer = HMAC(self._session_keys["app_key"], hashes.SHA256())
        signer.update(salt)
        our_hmac = signer.finalize()
        await self.client.write_gatt_char(
            CHAR_AUTH_DATA, b"\x00\x00\x00\x0a\x01\x00", response=False
        )
        if await self.wait_notify("auth_data", 3.0) != b"\x00\x00\x01\x01":
            raise AuthenticationError("Device was not ready for login proof")
        await self.client.write_gatt_char(CHAR_AUTH_DATA, b"\x01\x00" + our_hmac, response=False)
        await self.wait_notify("auth_data", 3.0)
        result = await self.wait_notify("auth_ctrl", 5.0)
        if not result or result[0] not in (0x11, 0x21):
            raise AuthenticationError("MiOT login rejected")
        self.authenticated = True
        self._send_iteration = 0
        self._miot_sequence = 1
        self._device_counter = 0
        self._decrypt_failures = 0
        await asyncio.sleep(0.5)
        # Authentication triggers a burst of device pushes. Wait for a quiet
        # period so their ACK traffic cannot collide with the first command.
        await self.drain_command_notifications(idle_timeout=0.5, max_duration=6.0)

    async def _receive_auth_value(self) -> bytes | None:
        try:
            async with asyncio.timeout(18):
                return await self._receive_auth_value_frames()
        except TimeoutError as err:
            raise AuthenticationError("Authentication fragments timed out") from err

    async def _receive_auth_value_frames(self) -> bytes | None:
        data = await self.wait_notify("auth_data", 3.0)
        if not data or len(data) < 4:
            return None
        if data[2] == 0x02:
            await self.client.write_gatt_char(CHAR_AUTH_DATA, b"\x00\x00\x03\x00", response=False)
            return data[4:]
        if data[2] != 0x00 or len(data) < 6:
            return None
        count = min(int.from_bytes(data[4:6], "little"), 100)
        await self.client.write_gatt_char(CHAR_AUTH_DATA, b"\x00\x00\x01\x01", response=False)
        payload = bytearray()
        for _ in range(count):
            frame = await self.wait_notify("auth_data", 3.0)
            if not frame or len(frame) < 2:
                return None
            payload.extend(frame[2:])
        await self.client.write_gatt_char(CHAR_AUTH_DATA, b"\x00\x00\x01\x00", response=False)
        return bytes(payload)

    def encrypt(self, plaintext: bytes) -> bytes:
        if self._session_keys is None:
            raise AuthenticationError("No active session")
        iteration = struct.pack("<I", self._send_iteration)
        nonce = self._session_keys["app_iv"] + b"\x00" * 4 + iteration
        encrypted = AESCCM(self._session_keys["app_key"], tag_length=4).encrypt(
            nonce, plaintext, None
        )
        self._send_iteration += 1
        return iteration[:2] + encrypted

    def decrypt(self, data: bytes) -> bytes | None:
        if self._session_keys is None or len(data) < 6:
            return None
        low = int.from_bytes(data[:2], "little")
        high = self._device_counter >> 16
        # Try the current epoch and adjacent epochs, including delayed pre-wrap
        # frames. Only an authenticated frame may advance the counter.
        for epoch in (high, high + 1, high - 1):
            if not 0 <= epoch <= 0xFFFF:
                continue
            counter = (epoch << 16) | low
            nonce = self._session_keys["dev_iv"] + b"\x00" * 4 + struct.pack("<I", counter)
            try:
                plaintext = AESCCM(self._session_keys["dev_key"], tag_length=4).decrypt(
                    nonce, data[2:], None
                )
            except InvalidTag:
                continue
            self._device_counter = max(self._device_counter, counter)
            self._decrypt_failures = 0
            return plaintext
        self._decrypt_failures += 1
        if self._decrypt_failures >= 3:
            raise ConnectionError("BLE session failed to decrypt three consecutive frames")
        return None

    async def _send_encrypted(self, plaintext: bytes) -> None:
        queue = getattr(self, "_queues", {}).get("cmd_send")
        if queue is not None:
            self._drain_queue(queue)
        iteration = self._send_iteration
        encrypted = self.encrypt(plaintext)
        try:
            await self.client.write_gatt_char(
                CHAR_CMD_SEND, b"\x00\x00\x00\x00\x01\x00", response=False
            )
            if not await self._wait_cmd_send_ack(b"\x00\x00\x01\x01", 3.0):
                raise ConnectionError("Command channel did not become ready")
        except BaseException:
            # The encrypted frame was never sent, so this nonce remains reusable.
            if self._send_iteration == iteration + 1:
                self._send_iteration = iteration
            raise
        await self.client.write_gatt_char(CHAR_CMD_SEND, b"\x01\x00" + encrypted, response=False)
        if not await self._wait_cmd_send_ack(b"\x00\x00\x01\x00", 3.0):
            raise ConnectionError("Command frame was not acknowledged")

    async def _wait_cmd_send_ack(self, expected: bytes, timeout: float) -> bool:
        """Wait for a transport ACK while ignoring interleaved Spec notifications."""
        deadline = asyncio.get_running_loop().time() + timeout
        while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
            data = await self.wait_notify("cmd_send", remaining)
            if data is None:
                return False
            if data == expected:
                return True
            if data in (b"\x00\x00\x01\x01", b"\x00\x00\x01\x00"):
                _LOGGER.debug("Ignoring stale CMD_SEND ACK while waiting for %s", expected.hex())
            else:
                _LOGGER.debug("Ignoring interleaved BLE Spec notification (%d bytes)", len(data))
        return False

    def drain_spec_notifications(self) -> int:
        """Discard queued Spec telemetry that is not used by the MiOT state parser."""
        queue = self._queues.get("cmd_send")
        if queue is None:
            return 0
        count = 0
        while not queue.empty():
            try:
                data = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if data not in (b"\x00\x00\x01\x01", b"\x00\x00\x01\x00"):
                count += 1
        return count

    @staticmethod
    def _build_miot_tlv(sequence: int, siid: int, piid: int, value: int | None) -> bytes:
        if value is None:
            opcode, type_id, encoded = 0x02, 1, b"\x00"
        elif value <= 0xFF:
            opcode, type_id, encoded = 0x00, 1, bytes((value,))
        else:
            opcode, type_id, encoded = 0x00, 5, value.to_bytes(4, "little")
        typed_length = (type_id << 12) | len(encoded)
        return (
            bytes(
                (
                    11 + len(encoded),
                    0x20,
                    sequence,
                    0,
                    opcode,
                    1,
                    siid,
                    piid & 0xFF,
                    piid >> 8,
                    typed_length & 0xFF,
                    typed_length >> 8,
                )
            )
            + encoded
        )

    async def send_miot_command(
        self, siid: int, piid: int, value: int | None = None, timeout: float = 8.0
    ) -> dict[str, Any] | None:
        """Send one MiOT command."""
        if not self.authenticated:
            raise AuthenticationError("Not authenticated")
        if value is not None:
            await self.drain_command_notifications()
            sequence = self._miot_sequence
            self._miot_sequence = (sequence + 1) & 0xFF
            await self._send_encrypted(self._build_miot_tlv(sequence, siid, piid, value))
            return {"piid": piid, "value": value, "raw": None}
        return await self._send_miot_command_once(siid, piid, value, timeout)

    async def _send_miot_command_once(
        self, siid: int, piid: int, value: int | None, timeout: float
    ) -> dict[str, Any] | None:
        """Perform one MiOT command attempt."""
        await self.drain_command_notifications()
        sequence = self._miot_sequence
        self._miot_sequence = (sequence + 1) & 0xFF
        await self._send_encrypted(self._build_miot_tlv(sequence, siid, piid, value))
        deadline = asyncio.get_running_loop().time() + timeout
        got_ack = False
        while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
            plaintext = await self.receive_command_payload(min(remaining, 3.0))
            if plaintext is None or len(plaintext) < 9:
                continue
            opcode = plaintext[4]
            if opcode == 0x04 and self._push_callback is not None:
                self._push_callback(plaintext)
            response_siid = plaintext[6]
            response_piid = int.from_bytes(plaintext[7:9], "little")
            if response_siid != siid or response_piid != piid:
                continue
            if value is not None and opcode == 0x01:
                got_ack = True
                deadline = min(deadline, asyncio.get_running_loop().time() + 1.0)
                continue
            if opcode in (0x03, 0x04):
                if opcode == 0x03 and len(plaintext) >= 11 and (plaintext[9] or plaintext[10]):
                    return None
                result_value = self._parse_result_value(plaintext)
                return {"piid": piid, "value": result_value, "raw": plaintext}
        return {"piid": piid, "value": None, "raw": None} if got_ack else None

    @staticmethod
    def _parse_result_value(payload: bytes) -> int | None:
        return parse_property_value(payload)

    async def receive_command_payload(self, timeout: float = 3.0) -> bytes | None:
        """Consume one inline or multi-frame cmd_recv message and decrypt it."""
        try:
            async with asyncio.timeout(timeout + 15):
                return await self._receive_command_frames(timeout)
        except TimeoutError as err:
            raise ConnectionError("Command fragments timed out") from err

    async def _receive_command_frames(self, timeout: float) -> bytes | None:
        data = await self.wait_notify("cmd_recv", timeout)
        if not data or len(data) < 4:
            return None
        if data[2] == 0x02:
            await self.client.write_gatt_char(CHAR_CMD_RECV, b"\x00\x00\x03\x00", response=False)
            return self.decrypt(data[4:])
        if data[2] != 0x00 or len(data) < 6:
            return None
        count = int.from_bytes(data[4:6], "little")
        if not 1 <= count <= 100:
            raise ConnectionError("Invalid command fragment count")
        await self.client.write_gatt_char(CHAR_CMD_RECV, b"\x00\x00\x01\x01", response=False)
        encrypted = bytearray()
        for _ in range(count):
            frame = await self.wait_notify("cmd_recv", 3.0)
            if not frame or len(frame) < 2:
                raise ConnectionError("Incomplete multi-frame command notification")
            encrypted.extend(frame[2:])
        await self.client.write_gatt_char(CHAR_CMD_RECV, b"\x00\x00\x01\x00", response=False)
        return self.decrypt(bytes(encrypted))

    async def drain_command_notifications(
        self, *, idle_timeout: float = 0.0, max_duration: float = 1.0
    ) -> None:
        """Acknowledge pushes until the queue is empty or remains quiet."""
        queue = self._queues.get("cmd_recv")
        if queue is None:
            return
        deadline = asyncio.get_running_loop().time() + max_duration
        while True:
            if queue.empty():
                if idle_timeout <= 0:
                    return
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return
                timeout = min(idle_timeout, remaining)
            else:
                timeout = 0.1
            payload = await self.receive_command_payload(timeout)
            if payload is not None and self._push_callback is not None:
                self._push_callback(payload)
            if payload is None and queue.empty():
                return
            if max_duration > 0 and asyncio.get_running_loop().time() >= deadline:
                return
