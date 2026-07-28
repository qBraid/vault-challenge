# Copyright 2026 qBraid
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

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
- Requests are limited to 30 per minute across probes and attacks combined.
- Your total score is the average of your best attack score on vaults 1-12.
  Ties on the leaderboard rank by fewest scored attacks used.

You are enrolled on your first probe or attack — no registration needed, and
checking your state or the leaderboard never enrolls you. Authentication
uses your qBraid API key via ``QbraidSessionV1``.

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

import importlib.util
from typing import Any, Dict, List, Optional

import pyqasm
from qbraid import QbraidSessionV1
from qbraid.exceptions import QbraidError
from qbraid.programs import get_program_type_alias
from qbraid.transpiler import transpile


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
    #: this must exceed that; the session default of 30 s is too short.
    _HTTP_TIMEOUT = 40

    def __init__(self, qbraid_session: Optional[QbraidSessionV1] = None):
        """Initialize the client with a qBraid session object."""
        self._session = qbraid_session or QbraidSessionV1()

    @property
    def session(self) -> QbraidSessionV1:
        """Return the qBraid session object."""
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
