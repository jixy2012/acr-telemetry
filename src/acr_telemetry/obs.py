"""Record a video of every run with OBS, synced to the run's own clock.

OBS Studio 28+ ships obs-websocket v5, a JSON-over-WebSocket remote control on
localhost:4455 (Tools -> WebSocket Server Settings). The logger already knows
exactly when a run starts, pauses and ends, so it drives the recording and
writes down where in the video each moment of the run landed.

Two design rules carry over from the recorder:

- **Video never costs a sample.** Every failure here -- OBS not running, a bad
  password, OBS closing mid-stage -- is reported once and switches video off
  until the next run. Nothing in this module may raise into the capture loop.
- **Measure the sync, never assume it.** OBS takes a variable moment to start
  encoding, and the run's clock stops while the game is paused while the video
  does not stop instantly. So the offset is not computed from timestamps; it is
  sampled: ask OBS how long the file is, note the run clock at that instant,
  and store the pair. A run gets a fresh pair after every start and resume, and
  one every `SYNC_EVERY_S` while driving, so a reader can see any drift rather
  than inherit it.

Stdlib only, like the rest of the capture path: a dependency that fails to
import must not be able to stop the logger.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable

# The first pair is taken shortly after start/resume rather than at once: until
# the encoder has produced a frame, OBS reports a duration of 0 and the pair
# would be meaningless.
FIRST_SYNC_AFTER_S = 1.0
SYNC_EVERY_S = 30.0
# Generous, because nothing waits on it but the worker thread: StopRecord does
# not answer until OBS has finalised the file, which can take seconds.
IO_TIMEOUT_S = 10.0


class ObsError(Exception):
    pass


class ObsClient:
    """The smallest obs-websocket v5 client that does the job.

    No event subscription, so the only messages that arrive are responses to
    our own requests (plus WebSocket pings). Each request blocks for one round
    trip on localhost, typically a millisecond or two.
    """

    def __init__(self, host: str, port: int, password: str | None,
                 timeout: float = IO_TIMEOUT_S):
        self.host, self.port, self.password = host, port, password
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""
        self._next_id = 0

    # ---- connection ---------------------------------------------------------
    def connect(self) -> None:
        try:
            self._sock = socket.create_connection(
                (self.host, self.port), timeout=self.timeout
            )
        except OSError as exc:
            raise ObsError(
                f"cannot reach OBS at {self.host}:{self.port} ({exc.strerror or exc}) "
                "-- is OBS running with its WebSocket server enabled?"
            ) from None
        self._sock.settimeout(self.timeout)
        self._handshake()

        hello = self._recv_json()
        if hello.get("op") != 0:
            raise ObsError(f"expected Hello from OBS, got op {hello.get('op')}")
        identify = {"rpcVersion": 1, "eventSubscriptions": 0}
        auth = hello["d"].get("authentication")
        if auth:
            if not self.password:
                raise ObsError(
                    "OBS asks for a password: pass --obs-password or set "
                    "ACR_OBS_PASSWORD (Tools -> WebSocket Server Settings -> "
                    "Show Connect Info)"
                )
            secret = base64.b64encode(
                hashlib.sha256((self.password + auth["salt"]).encode()).digest()
            )
            identify["authentication"] = base64.b64encode(
                hashlib.sha256(secret + auth["challenge"].encode()).digest()
            ).decode()
        self._send_json({"op": 1, "d": identify})
        try:
            reply = self._recv_json()
        except ObsError:
            # OBS answers a wrong password by closing the socket.
            raise ObsError("OBS rejected the connection -- wrong password?") from None
        if reply.get("op") != 2:
            raise ObsError(f"OBS did not identify us (op {reply.get('op')})")

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._send_frame(0x8, b"")
            except ObsError:
                pass
            self._sock.close()
            self._sock = None

    def request(self, kind: str, data: dict | None = None) -> tuple[dict, float]:
        """Send one request. Returns (responseData, perf_counter at midpoint).

        The midpoint of the round trip is the best estimate of when OBS read
        its own state, which is what a sync pair needs.
        """
        self._next_id += 1
        rid = str(self._next_id)
        msg = {"op": 6, "d": {"requestType": kind, "requestId": rid}}
        if data:
            msg["d"]["requestData"] = data
        sent = time.perf_counter()
        self._send_json(msg)
        while True:
            reply = self._recv_json()
            if reply.get("op") == 7 and reply["d"].get("requestId") == rid:
                break
        done = time.perf_counter()
        status = reply["d"]["requestStatus"]
        if not status.get("result"):
            raise ObsError(
                f"{kind} failed: {status.get('comment') or 'code ' + str(status.get('code'))}"
            )
        return reply["d"].get("responseData") or {}, (sent + done) / 2

    # ---- WebSocket framing (RFC 6455, client side) --------------------------
    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        self._sock.sendall(
            (
                f"GET / HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                "Sec-WebSocket-Protocol: obswebsocket.json\r\n\r\n"
            ).encode()
        )
        while b"\r\n\r\n" not in self._buf:
            self._fill()
        head, self._buf = self._buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status + b" ":
            raise ObsError(f"not an obs-websocket server ({status.decode(errors='replace')})")

    def _fill(self) -> None:
        try:
            chunk = self._sock.recv(65536)
        except OSError as exc:
            raise ObsError(f"lost connection to OBS ({exc})") from None
        if not chunk:
            raise ObsError("OBS closed the connection")
        self._buf += chunk

    def _take(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._fill()
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 1 << 16:
            head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        # Clients must mask every frame; servers drop unmasked ones.
        mask = os.urandom(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        try:
            self._sock.sendall(head + mask + body)
        except OSError as exc:
            raise ObsError(f"lost connection to OBS ({exc})") from None

    def _send_json(self, obj: dict) -> None:
        self._send_frame(0x1, json.dumps(obj).encode())

    def _recv_json(self) -> dict:
        message = b""
        while True:
            b0, b1 = self._take(2)
            opcode, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                (n,) = struct.unpack("!H", self._take(2))
            elif n == 127:
                (n,) = struct.unpack("!Q", self._take(8))
            mask = self._take(4) if b1 & 0x80 else None
            payload = self._take(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise ObsError("OBS closed the connection")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            message += payload
            if b0 & 0x80:
                return json.loads(message)


@dataclass
class ObsSettings:
    host: str = "localhost"
    port: int = 4455
    password: str | None = None


class RunVideo:
    """One run's recording, driven by the recorder's run lifecycle.

    The capture loop only ever drops a command on a queue or stores two
    floats; a worker thread does all the talking to OBS. A refused connection
    on Windows takes about a second to fail, and StopRecord waits for OBS to
    finalise the file, so doing either inline would stall the loop and lose
    samples to a shared-memory page that is overwritten 330 times a second.

    Every public method is safe to call in any state and never raises. On the
    first failure the reason is kept (it ends up in the run's metadata, so a
    run without video says why) and the rest of the run goes without.
    """

    def __init__(self, settings: ObsSettings, say: Callable[[str], None] = print,
                 on_saved: Callable[[dict], None] | None = None,
                 after: list[RunVideo] = ()):
        self.settings = settings
        self.say = say
        # Recordings still stopping. A restart stops one run and starts the
        # next in the same tick, and if this one asked OBS before the last
        # StopRecord landed it would find OBS busy and record nothing.
        self.after = list(after)
        # Called from the worker once the recording is stopped and OBS has
        # said where the file is, which is after the recorder has written the
        # run's metadata for the last time.
        self.on_saved = on_saved
        self._q: queue.Queue[str] = queue.Queue()
        self._thread = threading.Thread(target=self._work, name="obs", daemon=True)
        self._client: ObsClient | None = None
        self._file: str | None = None
        self._sync: list[list[float]] = []
        self._error: str | None = None
        self._due: float | None = None
        self._active = False
        # (t_s, perf_counter) from the capture loop's latest live tick, or
        # None while the run clock is stopped. Replaced whole, never mutated,
        # so the worker always reads a consistent pair.
        self._anchor: tuple[float, float] | None = None

    # ---- called from the capture loop: none of these block -----------------
    def start(self) -> None:
        self._thread.start()
        self._q.put("start")

    def pause(self) -> None:
        self._anchor = None
        self._q.put("pause")

    def resume(self) -> None:
        self._q.put("resume")

    def stop(self) -> None:
        self._anchor = None
        self._q.put("stop")

    def tick(self, t_s: float, at: float) -> None:
        """The run clock read `t_s` at perf_counter `at`. Call every live tick."""
        self._anchor = (t_s, at)

    def join(self, timeout: float) -> None:
        if self._thread.is_alive():
            self._thread.join(timeout)

    @property
    def busy(self) -> bool:
        return self._thread.is_alive()

    def meta(self) -> dict:
        return {
            # Absolute path as OBS reported it. None until the recording is
            # stopped, which is when OBS finalises the file.
            "file": self._file,
            # [run t_s, video seconds] pairs. Between two pairs, video time is
            # t_s - (pair t_s - pair video_s) of the latest pair at or before
            # t_s; a new pair is taken after every resume, which is the only
            # moment the two clocks step relative to each other.
            "sync": list(self._sync),
            "error": self._error,
        }

    # ---- worker -------------------------------------------------------------
    def _work(self) -> None:
        while True:
            wait = None if self._due is None else max(0.0, self._due - time.perf_counter())
            try:
                cmd = self._q.get(timeout=wait)
            except queue.Empty:
                self._take_sync()
                continue
            if cmd == "start":
                for previous in self.after:
                    previous.join(timeout=10.0)
                self.after = []
                self._start()
            elif cmd == "pause":
                if self._call("PauseRecord"):
                    self._due = None
            elif cmd == "resume":
                if self._call("ResumeRecord"):
                    self._due = time.perf_counter() + FIRST_SYNC_AFTER_S
            elif cmd == "stop":
                self._stop()
                if self.on_saved is not None:
                    try:
                        self.on_saved(self.meta())
                    except Exception as exc:  # never let a sidecar write kill the thread silently
                        self.say(f"WARNING — could not record the video in the run's .json: {exc}")
                return

    def _start(self) -> None:
        try:
            client = ObsClient(**vars(self.settings))
            client.connect()
            status, _ = client.request("GetRecordStatus")
            if status.get("outputActive"):
                # Someone else's recording. Stopping it would be rude, and
                # pairing this run against it would be wrong the moment they
                # stop it themselves.
                client.close()
                self._fail("OBS was already recording, so it was left alone")
                return
            client.request("StartRecord")
        except ObsError as exc:
            self._fail(str(exc))
            return
        self._client, self._active = client, True
        self._due = time.perf_counter() + FIRST_SYNC_AFTER_S
        self.say("● OBS recording")

    def _take_sync(self) -> None:
        if not self._active:
            self._due = None
            return
        try:
            status, at = self._client.request("GetRecordStatus")
        except ObsError as exc:
            self._fail(str(exc))
            return
        anchor = self._anchor
        duration_ms = status.get("outputDuration") or 0
        # The anchor must be from a tick close to the measurement; a stale one
        # means the run clock stopped (a pause) after it was taken.
        if (
            anchor is not None
            and abs(at - anchor[1]) < 0.25
            and status.get("outputActive")
            and not status.get("outputPaused")
            and duration_ms > 0
        ):
            run_t = anchor[0] + (at - anchor[1])
            self._sync.append([round(run_t, 3), round(duration_ms / 1000.0, 3)])
            self._due = time.perf_counter() + SYNC_EVERY_S
        else:
            self._due = time.perf_counter() + FIRST_SYNC_AFTER_S

    def _stop(self) -> None:
        if not self._active:
            return
        try:
            data, _ = self._client.request("StopRecord")
            self._file = data.get("outputPath")
        except ObsError as exc:
            self._fail(str(exc))
            return
        self._client.close()
        self._client, self._active, self._due = None, False, None
        self.say(f"■ OBS saved  {self._file}")

    def _call(self, kind: str) -> bool:
        if not self._active:
            return False
        try:
            self._client.request(kind)
            return True
        except ObsError as exc:
            self._fail(str(exc))
            return False

    def _fail(self, reason: str) -> None:
        if self._error is None:
            self._error = reason
            self.say(f"WARNING — no video for this run: {reason}")
        if self._client is not None:
            self._client.close()
        self._client, self._active, self._due = None, False, None
