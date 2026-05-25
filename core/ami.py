"""
Asterisk Manager Interface (AMI) client.

Minimal async AMI client that supports:
  - Login
  - Originate (outbound calls)
  - ActionID correlation so concurrent Originates don't race
  - Auto-reconnect on socket loss

AMI wire format (plain TCP on port 5038):
    Banner:   "Asterisk Call Manager/<version>\r\n"
    Messages: "Key: Value\r\n" repeated, terminated with a blank line.
              Each response/event carries an ActionID if the request did.

Usage:
    ami = await get_ami()  # singleton, connects + logs in on first call
    resp = await ami.originate(
        channel="SIP/trunk-out/9876543210",
        context="outbound-ai",
        extension="s",
        priority=1,
        variables={"CUSTOMER_UUID": "abc-123"},
        caller_id="Priya <91XXXXXXXXXX>",
        timeout_ms=30000,
    )
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from config.settings import Settings

logger = logging.getLogger(__name__)

_ENCODING = "utf-8"
_CRLF = "\r\n"
_MSG_TERM = "\r\n\r\n"


class AMIError(RuntimeError):
    pass


class AsteriskAMI:
    def __init__(self, host: str, port: int, username: str, secret: str):
        self.host = host
        self.port = port
        self.username = username
        self.secret = secret

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._connect_lock = asyncio.Lock()
        self._logged_in = False
        self._closed = False

    # ── Public API ──────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect + login. Idempotent and concurrency-safe."""
        async with self._connect_lock:
            if self._logged_in and self._writer is not None and not self._writer.is_closing():
                return
            await self._open_socket()
            await self._login()

    async def originate(
        self,
        channel: str,
        context: str,
        extension: str = "s",
        priority: int = 1,
        variables: dict[str, Any] | None = None,
        caller_id: str | None = None,
        timeout_ms: int = 30000,
        action_id: str | None = None,
    ) -> dict[str, str]:
        """
        Issue an AMI Originate. Resolves on the matching Response frame
        (Async=true, so you get Response: Success immediately — the actual
        call outcome arrives later as OriginateResponse events which this
        simple client does not currently consume).
        """
        from core.tracing import aspan, set_attr
        async with aspan("ami.originate", attrs={"ami.channel": channel, "ami.context": context}):
            if not self._logged_in:
                await self.connect()

            action_id = action_id or f"orig-{uuid.uuid4()}"
            set_attr("ami.action_id", action_id)
            msg: dict[str, str] = {
                "Action": "Originate",
                "Channel": channel,
                "Context": context,
                "Exten": extension,
                "Priority": str(priority),
                "Timeout": str(timeout_ms),
                "Async": "true",
                "ActionID": action_id,
            }
            if caller_id:
                msg["CallerID"] = caller_id
            if variables:
                msg["Variable"] = ",".join(f"{k}={v}" for k, v in variables.items())

            fut = self._register(action_id)
            try:
                self._send(msg)
                resp = await asyncio.wait_for(fut, timeout=(timeout_ms / 1000) + 10)
                set_attr("ami.response", resp.get("Response", ""))
                return resp
            finally:
                self._pending.pop(action_id, None)

    async def disconnect(self) -> None:
        self._closed = True
        self._logged_in = False
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = self._writer = None

    # ── Internals ───────────────────────────────────────────────────────────

    async def _open_socket(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        # Consume banner "Asterisk Call Manager/<version>\r\n"
        try:
            banner = await asyncio.wait_for(self._reader.readline(), timeout=5)
            logger.info(f"AMI banner: {banner.decode(_ENCODING, errors='replace').strip()}")
        except asyncio.TimeoutError:
            raise AMIError("AMI: no banner received within 5s")
        self._reader_task = asyncio.create_task(self._read_loop(), name="ami-reader")

    async def _login(self) -> None:
        action_id = f"login-{uuid.uuid4()}"
        fut = self._register(action_id)
        self._send({
            "Action": "Login",
            "Username": self.username,
            "Secret": self.secret,
            "Events": "off",  # we don't consume events yet; reduce noise
            "ActionID": action_id,
        })
        try:
            resp = await asyncio.wait_for(fut, timeout=10)
        finally:
            self._pending.pop(action_id, None)
        if resp.get("Response") != "Success":
            raise AMIError(f"AMI login failed: {resp}")
        self._logged_in = True
        logger.info(f"AMI: logged in as {self.username}")

    def _register(self, action_id: str) -> asyncio.Future:
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[action_id] = fut
        return fut

    def _send(self, msg: dict[str, str]) -> None:
        if not self._writer:
            raise AMIError("AMI: not connected")
        payload = "".join(f"{k}: {v}{_CRLF}" for k, v in msg.items()) + _CRLF
        self._writer.write(payload.encode(_ENCODING))

    async def _read_loop(self) -> None:
        assert self._reader is not None
        buf = ""
        try:
            while not self._closed:
                chunk = await self._reader.read(4096)
                if not chunk:
                    raise ConnectionError("AMI: connection closed by server")
                buf += chunk.decode(_ENCODING, errors="replace")
                while _MSG_TERM in buf:
                    raw, buf = buf.split(_MSG_TERM, 1)
                    if not raw.strip():
                        continue
                    self._dispatch(self._parse_message(raw))
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning(f"AMI reader exited: {e}")
            self._logged_in = False
            # Reject any outstanding futures so callers don't hang
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(AMIError(f"AMI disconnected: {e}"))
            self._pending.clear()
            if not self._closed:
                self._schedule_reconnect()

    @staticmethod
    def _parse_message(raw: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for line in raw.split(_CRLF):
            if not line or ":" not in line:
                continue
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
        return out

    def _dispatch(self, msg: dict[str, str]) -> None:
        action_id = msg.get("ActionID")
        if action_id and action_id in self._pending:
            fut = self._pending[action_id]
            if not fut.done():
                fut.set_result(msg)

    def _schedule_reconnect(self) -> None:
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(self._reconnect_loop(), name="ami-reconnect")

    async def _reconnect_loop(self) -> None:
        delay = 1.0
        while not self._closed and not self._logged_in:
            try:
                await asyncio.sleep(delay)
                logger.info(f"AMI: reconnecting to {self.host}:{self.port}...")
                await self._open_socket()
                await self._login()
                return
            except Exception as e:
                logger.warning(f"AMI reconnect failed: {e}")
                delay = min(delay * 2, 30.0)


# ── Connection pool ─────────────────────────────────────────────────────────


class AMIPool:
    """
    Round-robin pool of AsteriskAMI connections.

    Why: a single TCP socket + single reader loop serializes every Originate
    response demux. At burst scale (100+ originates in a few seconds) the
    reader becomes the bottleneck. With N parallel sockets, demux fan-out
    eliminates head-of-line blocking on the response stream.

    Compatible with the existing AsteriskAMI public API: `originate()` and
    `connect()` are the only externally used methods, and AMIPool exposes
    the same signatures.
    """

    def __init__(self, size: int, host: str, port: int, username: str, secret: str):
        self._size = max(1, size)
        self._members: list[AsteriskAMI] = [
            AsteriskAMI(host=host, port=port, username=username, secret=secret)
            for _ in range(self._size)
        ]
        self._rr_index = 0
        self._rr_lock = asyncio.Lock()

    async def connect(self) -> None:
        """Connect & login every member. Errors propagate so caller sees them."""
        await asyncio.gather(*(m.connect() for m in self._members))
        logger.info(f"[AMIPool] connected {self._size} sessions")

    async def _next(self) -> AsteriskAMI:
        async with self._rr_lock:
            m = self._members[self._rr_index % self._size]
            self._rr_index += 1
        # Heal a single dead member without taking the whole pool offline
        if not m._logged_in:
            try:
                await m.connect()
            except Exception as e:
                logger.warning(f"[AMIPool] member reconnect failed: {e}")
        return m

    async def originate(self, *args, **kwargs) -> dict[str, str]:
        member = await self._next()
        return await member.originate(*args, **kwargs)

    async def disconnect(self) -> None:
        await asyncio.gather(
            *(m.disconnect() for m in self._members),
            return_exceptions=True,
        )

    @property
    def healthy_count(self) -> int:
        return sum(1 for m in self._members if m._logged_in)

    @property
    def size(self) -> int:
        return self._size


# ── Module-level singleton ──────────────────────────────────────────────────

_ami: AsteriskAMI | AMIPool | None = None
_ami_lock = asyncio.Lock()


async def get_ami(settings: Settings | None = None) -> AsteriskAMI | AMIPool:
    """
    Return the connected AMI client. If AMI_POOL_SIZE > 1 in settings, a pool
    is created; otherwise a single AsteriskAMI singleton (legacy behaviour).
    Safe to call concurrently; subsequent callers see the same instance.
    """
    global _ami
    async with _ami_lock:
        if _ami is None:
            s = settings or Settings.from_env()
            pool_size = getattr(s, "ami_pool_size", 1)
            if pool_size > 1:
                _ami = AMIPool(
                    size=pool_size,
                    host=s.ami_host,
                    port=s.ami_port,
                    username=s.ami_user,
                    secret=s.ami_secret,
                )
                await _ami.connect()
            else:
                _ami = AsteriskAMI(
                    host=s.ami_host,
                    port=s.ami_port,
                    username=s.ami_user,
                    secret=s.ami_secret,
                )
                await _ami.connect()
        elif isinstance(_ami, AsteriskAMI) and not _ami._logged_in:
            await _ami.connect()
        return _ami


async def shutdown_ami() -> None:
    global _ami
    if _ami is not None:
        await _ami.disconnect()
        _ami = None
