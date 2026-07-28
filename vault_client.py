# Copyright (c) 2026, qBraid Development Team
# All rights reserved.

"""
Client for the qBraid Vault Challenge.

Thirteen hidden "vault" circuits stand between you and a perfect score. Your
goal: for each vault, build a Qiskit circuit that, when appended after the
hidden vault circuit, returns the combined system to the all-zeros state.
Every participant gets their own vault set, generated server-side just for
them.

Pass ``probe``/``attack`` a Qiskit ``QuantumCircuit`` and the client exports it
to OpenQASM 3 for you (``qiskit.qasm3.dumps``). Raw OpenQASM 2/3 strings still
work if you prefer them, as do Cirq/Braket/pyQuil programs.

Actions
-------
- ``probe(vault_index, circuit)`` — run the combined circuit at low shots and
  get back a measurement histogram. Reconnaissance only; never scored.
- ``attack(vault_index, circuit)`` — run at high shots and receive a score:
  ``score = rawScore * costFactor``, where ``rawScore`` is the probability of
  measuring all zeros and ``costFactor`` scales with the total number of
  two-qubit gates in your submitted circuit — a circuit with no entangling
  gates has costFactor 1.

Rules
-----
- Vault 0 is a practice vault: 50 probes + 50 attacks, never scored. It is
  the same 3-qubit circuit for everyone — perfect for warming up.
- Vaults 1-12 each allow 20 probes and 20 attacks. Only a simulation that
  returns a result is charged: invalid QASM, over-the-size-cap circuits and
  30-second timeouts are all refunded.
- The combined circuit (vault + your program) may use at most 20 qubits and
  10,000 operations.
- Terminal measurements in your program are ignored (stripped before
  simulation); mid-circuit measurements are rejected as invalid.
- Requests are limited to 30 per minute across probes and attacks combined
  (reads are a separate, more generous bucket). The client paces itself to
  stay under this and waits out a rate limit rather than failing, so a tight
  loop of probes slows down instead of erroring. Rate-limited requests are
  refused before they reach the simulator and never cost budget.
- Your total score is the average of your best attack score on vaults 1-12.
  Ties on the leaderboard rank by fewest scored attacks used.

You are enrolled on your first probe or attack — no registration needed, and
checking your state or the leaderboard never enrolls you. Authentication
uses your qBraid API key via ``QbraidSession``.

Example
-------
>>> from qiskit import QuantumCircuit
>>> from vault_client import VaultClient
>>> client = VaultClient()
>>> client.state()                 # your scores and remaining budgets
>>> qc = QuantumCircuit(3)         # the practice vault is a 3-qubit GHZ
>>> qc.cx(0, 2)
>>> qc.cx(0, 1)
>>> qc.h(0)
>>> client.probe(0, qc)            # histogram of the practice vault
>>> client.attack(0, qc)           # attack the practice vault (unscored)
>>> client.leaderboard()           # standings
"""

import email.utils
import importlib.util
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

import pyqasm
from qbraid.exceptions import QbraidError
from qbraid.programs import get_program_type_alias
from qbraid.transpiler import transpile
from qbraid_core.sessions import QbraidSession


#: A submission: a Qiskit ``QuantumCircuit``, an OpenQASM 2/3 string, or any
#: other program type qBraid can transpile.
CircuitLike = Any


def _retry_after_seconds(err: BaseException) -> Optional[float]:
    """Seconds to wait per a 429's ``Retry-After``, or None if this isn't a 429.

    ``QbraidSession`` raises ``RequestsApiError(...) from err``, so the original
    ``requests`` exception -- and with it the response, its status and its
    headers -- survives on the ``__cause__`` chain. Walk that chain rather than
    matching on the message text, which is prose and may be reworded.
    """
    seen = set()
    while err is not None and id(err) not in seen:
        seen.add(id(err))
        response = getattr(err, "response", None)
        if response is not None and getattr(response, "status_code", None) == 429:
            header = response.headers.get("Retry-After", "")
            return _parse_retry_after(header)
        err = err.__cause__ or err.__context__
    return None


def _parse_retry_after(header: str) -> float:
    """``Retry-After`` as seconds. Accepts the delay-seconds and HTTP-date forms.

    Falls back to the full window when the header is absent or unparseable: a
    429 is a real signal even when its hint is not, and waiting is always the
    safe direction. Capped so a bogus header cannot hang the caller.
    """
    header = (header or "").strip()
    if not header:
        return _WINDOW_SECONDS
    try:
        return max(0.0, min(float(header), _MAX_BACKOFF_SECONDS))
    except ValueError:
        pass
    try:  # HTTP-date form, e.g. "Wed, 21 Oct 2026 07:28:00 GMT"
        retry_at = email.utils.parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return _WINDOW_SECONDS
    if retry_at is None:
        return _WINDOW_SECONDS
    delta = retry_at.timestamp() - time.time()
    return max(0.0, min(delta, _MAX_BACKOFF_SECONDS))


#: The server's rate-limit window. Both buckets use the same one.
_WINDOW_SECONDS = 60.0

#: Never sleep longer than this on a single 429, however large the hint.
_MAX_BACKOFF_SECONDS = 120.0


class _RateLimiter:
    """Sliding-window pacer that keeps the client under a server bucket.

    The server rations probes and attacks at 30/minute and rejects the 31st
    with a 429. That is easy to trip by accident: a tomography loop firing 20
    probes back to back is normal play, and at full speed it arrives well
    inside one window. Pacing here turns that into a steady cadence instead of
    a hard error partway through a loop.

    Sliding rather than fixed-bucket, because the server's window slides: it
    keeps request timestamps and waits only until the oldest one ages out, so a
    burst after a quiet spell goes straight through at full speed and only a
    sustained burst is slowed.
    """

    def __init__(self, max_requests: int, window: float = _WINDOW_SECONDS):
        self.max_requests = max_requests
        self._window = window
        self._times: Deque[float] = deque()

    def acquire(self, notify: Optional[Callable[[float], None]] = None) -> None:
        """Block until another request would stay within the window."""
        now = time.monotonic()
        self._evict(now)
        if len(self._times) >= self.max_requests:
            wait = self._times[0] + self._window - now
            if wait > 0:
                if notify is not None:
                    notify(wait)
                time.sleep(wait)
            now = time.monotonic()
            self._evict(now)
        self._times.append(now)

    def _evict(self, now: float) -> None:
        """Drop timestamps that have aged out of the window."""
        cutoff = now - self._window
        while self._times and self._times[0] <= cutoff:
            self._times.popleft()


def to_qasm(circuit: CircuitLike) -> str:
    """Coerce a submission into OpenQASM for the wire.

    Accepts a Qiskit ``QuantumCircuit`` (the expected way to play), an OpenQASM
    2 or 3 string, or any other program type qBraid can transpile -- Cirq,
    Braket, pyQuil and friends all work, since qBraid's transpiler takes the
    native object directly.
    """
    if isinstance(circuit, str):
        return circuit
    try:
        return transpile(circuit, "qasm3")
    except QbraidError as err:
        # Two very different failures land here, and saying "unsupported type"
        # for both sends people hunting the wrong problem. qBraid's own
        # registry tells them apart: if it can name the program type, the type
        # was fine and some instruction inside it could not be expressed.
        try:
            get_program_type_alias(circuit)
        except QbraidError:
            raise TypeError(
                f"Unsupported program type {type(circuit).__name__!r}. Pass a "
                "Qiskit QuantumCircuit or an OpenQASM string."
            ) from err
        raise ValueError(
            "Could not convert this circuit to OpenQASM 3. The usual cause is "
            "an instruction with no OpenQASM equivalent -- state preparation "
            "such as `initialize`, or a custom/opaque gate. Try "
            "`circuit.decompose()` and submit that."
        ) from err


class VaultClient:
    """Client for the qBraid Vault Challenge: probe and attack hidden
    vault circuits, and check your standing.

    The combined circuit (vault + your program) may use at most 20 qubits
    and 10,000 operations. The challenge runs for two weeks: July 28 to
    August 11, 2026, opening at 10:00 CDT. The window is configured
    server-side, and ``state()`` reports the authoritative live dates.
    """

    #: HTTP timeout (seconds) for challenge requests. The server may take up
    #: to ~35 s in the worst case (30 s simulation cap plus overhead), so
    #: this must exceed that; QbraidSession's default of 30 s is too short.
    _HTTP_TIMEOUT = 40

    #: Server bucket for probe + attack combined (shared across both).
    _ACTION_RATE = 30

    #: Server bucket for the read endpoints (state, leaderboard).
    _READ_RATE = 60

    #: Extra attempts after a 429. The wait comes from the server's
    #: ``Retry-After``, so one retry almost always suffices; the second covers
    #: a window that had already partly elapsed when we were told about it.
    _MAX_RETRIES = 2

    def __init__(
        self,
        qbraid_session: Optional[QbraidSession] = None,
        pace_requests: bool = True,
    ):
        """Initialize the client with a QbraidSession object.

        Args:
            qbraid_session: Session to use. Defaults to a fresh ``QbraidSession``.
            pace_requests: Keep requests under the server's rate limit by
                waiting when a call would exceed it. On by default. Turning it
                off does not gain you throughput -- the server still enforces
                the limit -- it only converts the wait into a 429, which is
                occasionally what you want when testing error handling.
        """
        self._session = qbraid_session or QbraidSession()
        self._pace = pace_requests
        # Separate limiters because the server keys separate buckets: reading
        # the leaderboard in a loop must not eat into your attack allowance.
        self._action_limiter = _RateLimiter(self._ACTION_RATE)
        self._read_limiter = _RateLimiter(self._READ_RATE)

    @property
    def session(self) -> QbraidSession:
        """Return the QbraidSession object."""
        return self._session

    @staticmethod
    def _verify_qasm_program(qasm: str) -> None:
        """Pre-flight check: reject a bad program before it costs budget.

        A convenience, not a gate — the server validates every submission and
        refunds invalid ones. Two layers, because they catch different things
        and are not equally available:

        * ``pyqasm`` parses the program. It is a hard dependency of qbraid, so
          this always runs, and it catches malformed QASM.
        * A Cirq round-trip additionally rejects programs that parse but the
          simulator cannot run (timing instructions, mid-circuit measurement).
          qbraid installs Cirq only as the ``[cirq]`` extra, so this layer is
          skipped when it is missing.
        """
        # pyqasm is a hard dependency of qbraid, so this layer always runs and
        # catches the most common mistake by far: QASM that does not parse.
        try:
            pyqasm.loads(qasm).validate()
        except Exception as err:
            raise ValueError(
                "This does not parse as OpenQASM. If you built it in Qiskit, "
                "pass the circuit itself rather than a hand-written string."
            ) from err

        # Round-tripping through Cirq additionally catches programs that parse
        # but the simulator will not run. qbraid ships Cirq only as the
        # optional [cirq] extra, so this layer is skipped when it is absent
        # rather than failing a good circuit; the server still validates.
        try:
            if importlib.util.find_spec("cirq") is None:
                return
        except (ImportError, ValueError):  # absent, or a broken partial install
            return
        try:
            _ = transpile(qasm, "cirq")
        except QbraidError as err:
            raise ValueError(
                "This program is not something the simulator can run. Timing "
                "instructions (`delay`), mid-circuit measurement and other "
                "non-unitary operations are the usual cause -- the vault "
                "simulator only accepts unitary circuits."
            ) from err

    @staticmethod
    def _notify(message: str) -> None:
        """Tell the user we are waiting, so a pause never looks like a hang."""
        print(message, flush=True)

    def _send(self, limiter: "_RateLimiter", send: Callable[[], Any]) -> Any:
        """Perform one request, pacing ahead of it and backing off on a 429.

        Retrying is safe here in a way it is not for qBraid requests generally,
        and that is why this lives in the vault client rather than in the
        session: the challenge API mounts its rate limiter *before* the route
        handler, so a 429 is refused before it reaches the simulator and never
        touches your probe/attack budget. A retried request costs nothing but
        time.

        ``QbraidSession`` will not do this for us. Its retry policy forces
        retries only on the 5xx codes in its ``STATUS_FORCELIST``, and 429 is
        not among them -- so a POST is never retried on a rate limit whatever
        the ``Retry-After`` says. Its 5-second ceiling on ``Retry-After`` then
        discards the server's 60-second hint on the GET endpoints, where the
        retry would otherwise be allowed.
        """
        if self._pace:
            limiter.acquire(
                lambda wait: self._notify(
                    f"Rate limit: pausing {wait:.0f}s to stay under "
                    f"{limiter.max_requests}/min."
                )
            )
        for attempt in range(self._MAX_RETRIES + 1):
            try:
                return send()
            except Exception as err:  # noqa: BLE001 - re-raised unless it's a 429
                wait = _retry_after_seconds(err)
                if wait is None or attempt == self._MAX_RETRIES:
                    raise
                self._notify(
                    f"Rate limited by the server; waiting {wait:.0f}s and "
                    "retrying (this does not use up your budget)."
                )
                time.sleep(wait)
        raise AssertionError("unreachable")  # pragma: no cover

    def _post_request(
        self, action: str, vault_index: int, circuit: CircuitLike
    ) -> Dict[str, Any]:
        """Send a probe or attack request."""
        qasm = to_qasm(circuit)
        # Catch a bad program locally rather than spending budget on it.
        self._verify_qasm_program(qasm)
        query = {"vaultIndex": vault_index, "openQasm": qasm}
        resp = self._send(
            self._action_limiter,
            lambda: self.session.post(
                f"/challenges/vault/{action}",
                json=query,
                timeout=self._HTTP_TIMEOUT,
            ),
        )
        return resp.json()["data"]

    def attack(self, vault_index: int, circuit: CircuitLike) -> Dict[str, float]:
        """Attack a vault with a Qiskit circuit (or OpenQASM).

        Returns rawScore, costFactor, and score.
        """
        resp_data = self._post_request("attack", vault_index, circuit)
        score_keys = {"rawScore", "costFactor", "score"}
        return {k: resp_data[k] for k in score_keys if k in resp_data}

    def probe(self, vault_index: int, circuit: CircuitLike) -> Dict[str, float]:
        """Probe a vault with a Qiskit circuit (or OpenQASM).

        Returns histogram data with keys in the big-endian decimal
        representation of measurement bit strings.
        """
        resp_data = self._post_request("probe", vault_index, circuit)
        return resp_data["histogram"]

    def state(self) -> Dict[str, Any]:
        """Returns your challenge state: scores, remaining budgets, and the
        event window. Never enrolls you — enrollment happens on your first
        probe or attack."""
        resp = self._send(
            self._read_limiter,
            lambda: self.session.get(
                "/challenges/vault/state", timeout=self._HTTP_TIMEOUT
            ),
        )
        return resp.json()["data"]

    def leaderboard(self, page: int = 1, limit: int = 50) -> List[Dict[str, Any]]:
        """Returns one page of the challenge standings, ranked by average
        score. The endpoint is paginated; pass ``page``/``limit`` to page
        through the full board (``limit`` is capped server-side)."""
        resp = self._send(
            self._read_limiter,
            lambda: self.session.get(
                "/challenges/vault/leaderboard",
                params={"page": page, "limit": limit},
                timeout=self._HTTP_TIMEOUT,
            ),
        )
        return resp.json()["data"]
