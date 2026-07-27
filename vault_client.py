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
- Vaults 1-12 each allow 20 probes and 20 attacks. Failed submissions
  (invalid QASM, over the size caps) are not charged against your budget;
  simulations that hit the 30-second time limit are.
- The combined circuit (vault + your program) may use at most 20 qubits and
  10,000 operations.
- Terminal measurements in your program are ignored (stripped before
  simulation); mid-circuit measurements are rejected as invalid.
- Requests are limited to 30 per minute across probes and attacks combined.
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

from typing import Any, Dict, List, Optional

from qbraid.exceptions import QbraidError
from qbraid.transpiler import transpile
from qbraid_core import QbraidSession


#: A submission: a Qiskit ``QuantumCircuit``, an OpenQASM 2/3 string, or any
#: other program type qBraid can transpile.
CircuitLike = Any


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
        raise TypeError(
            f"Unsupported program type {type(circuit).__name__!r}. Pass a Qiskit "
            "QuantumCircuit or an OpenQASM string."
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

    def __init__(self, qbraid_session: Optional[QbraidSession] = None):
        """Initialize the client with a QbraidSession object."""
        self._session = qbraid_session or QbraidSession()

    @property
    def session(self) -> QbraidSession:
        """Return the QbraidSession object."""
        return self._session

    @staticmethod
    def _verify_qasm_program(qasm: str) -> None:
        """Verify that the qasm program is valid."""
        try:
            _ = transpile(qasm, "cirq")
        except QbraidError as err:
            raise ValueError("Invalid OpenQASM program.") from err

    def _post_request(
        self, action: str, vault_index: int, circuit: CircuitLike
    ) -> Dict[str, Any]:
        """Send a probe or attack request."""
        qasm = to_qasm(circuit)
        # Catch a bad program locally rather than spending budget on it.
        self._verify_qasm_program(qasm)
        query = {"vaultIndex": vault_index, "openQasm": qasm}
        resp = self.session.post(
            f"/challenges/vault/{action}", json=query, timeout=self._HTTP_TIMEOUT
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
        return self.session.get(
            "/challenges/vault/state", timeout=self._HTTP_TIMEOUT
        ).json()["data"]

    def leaderboard(self, page: int = 1, limit: int = 50) -> List[Dict[str, Any]]:
        """Returns one page of the challenge standings, ranked by average
        score. The endpoint is paginated; pass ``page``/``limit`` to page
        through the full board (``limit`` is capped server-side)."""
        return self.session.get(
            "/challenges/vault/leaderboard",
            params={"page": page, "limit": limit},
            timeout=self._HTTP_TIMEOUT,
        ).json()["data"]
