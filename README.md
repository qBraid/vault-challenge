# The qBraid Vault Challenge

**July 28 – August 11, 2026 · opens 10:00 CDT · [account.qbraid.com](https://account.qbraid.com/explore?tab=challenges)**

<img width="1200" height="627" alt="vault_challenge" src="https://github.com/user-attachments/assets/2b14f21d-37d3-4cbf-8ff7-7baf5070c229" />


## The story

Deep in qBraid's archives sits a wall of thirteen quantum vaults. Each one was
sealed years ago by an anonymous engineer known only as *the Locksmith*, who
scrambled the contents with a secret quantum circuit and walked away. The
design documents are gone. The Locksmith is not answering email.

Here is what we know. Every vault holds a register of qubits that starts in
the pristine state $|0\ldots0\rangle$. When the vault seals itself, the
Locksmith's hidden circuit $U_k$ runs on that register and scrambles it into
some secret state $|\psi_k\rangle = U_k|0\ldots0\rangle$. The locking
mechanism is simple and unforgiving: the door opens only when every tumbler —
every qubit — reads **0** again.

That is where you come in. You are a quantum safecracker. You cannot see the
Locksmith's circuit, but you *can* run your own circuit on the register after
it — put your stethoscope to the door, listen to how the tumblers fall, and
craft the counter-circuit $U_A$ that unwinds the scramble:

$$U_A \, U_k \, |0\ldots0\rangle \;\approx\; |0\ldots0\rangle$$

Do it perfectly and the door swings open. Do it *efficiently* and you top the
leaderboard. One more thing: the Locksmith sealed a **different set of vaults
for every participant**. Your twelve scored vaults are generated just for
you — every safecracker gets their own set of locks, and nobody can crack
yours for you.

## Your goal

For each vault, build a **Qiskit circuit** that inverts the hidden vault
circuit. Your circuit is **appended after** the hidden circuit, on the same
qubit register — the server runs $U_A U_k |0\ldots0\rangle$ and measures.
The closer the combined system lands to all zeros, and the fewer entangling
gates you spend getting there, the higher your score.

- **Vault 0** is a shared practice vault — the same fixed 3-qubit GHZ circuit
  for everyone, never scored. Use it to learn the mechanics risk-free.
- **Vaults 1–12** are yours alone and scored. They come from three families of
  lock, and the vaults get harder as you move through each family — but the
  families are different *kinds* of puzzle rather than successive difficulty
  tiers, so a higher vault number does not mean a tougher lock.

## The tools of the trade

You have two moves, both taking a vault index and a circuit. Hand them a
Qiskit `QuantumCircuit` and the client exports it for you; raw OpenQASM 2 or 3
strings are still accepted if you prefer writing them by hand:

### Probe — reconnaissance

A **probe** runs your circuit appended after the hidden circuit for
**200 shots** and returns the measurement histogram. Probes are never scored
— they are your stethoscope. Histogram keys are the **big-endian decimal**
value of the measured bitstring: on 3 qubits, key `"0"` means `000` and key
`"7"` means `111`.

### Attack — the real thing

An **attack** runs the combined circuit and returns a score. Only attacks
count toward the leaderboard, and only your **best** attack per vault is kept —
a bad attack can never lower a good one.

## The vault lineup

The Locksmith used three families of circuits. Within a family the locks get
harder as the vault number climbs; across families they are simply different
beasts, so do not assume vault 12 is tougher than vault 4:

| Vaults | Family | Size |
|---|---|---|
| 0 | Practice: [GHZ state](https://www.quantiki.org/wiki/ghz) | 3 qubits (same for everyone) |
| 1–4 | [Matrix-product-state](https://tensornetwork.org/mps/) circuits | 6–12 qubits, 1–3 entangling layers |
| 5–8 | Perturbed [graph states](https://en.wikipedia.org/wiki/Graph_state) | 3–14 qubits, plus small single-qubit rotations |
| 9–12 | [Hardware-efficient ansätze](https://arxiv.org/abs/1812.08862) | 3–5 qubits, alternating single-qubit and entangling layers |

Each family rewards a different attack strategy — see the hints at the end.

## Rules and budgets

| | Practice vault (0) | Scored vaults (1–12) |
|---|---|---|
| Probes | 50 | 20 each |
| Attacks | 50 | 20 each |
| Counts toward score | No | Yes |

- **Rate limit:** 30 requests per minute, shared across probes and attacks.
- **Size limits:** the combined circuit (vault + your program) may span at
  most **20 qubits** and **10,000 operations**, and submissions are capped at
  **500,000 characters**; over-limit submissions are rejected without
  spending budget.
- **Measurements:** terminal measurements in your program are ignored
  (stripped before simulation); **mid-circuit measurements are rejected** as
  invalid.
- **Refunds:** a submission that fails never costs you budget. That covers
  invalid programs (malformed QASM, or circuits over the qubit/operation
  caps) and simulations that hit the **30-second time limit** — the simulator
  is shared, so a timeout may say more about how busy it is than about your
  circuit, and you shouldn't pay for that. Only a simulation that returns a
  result is charged.
- **Enrollment:** automatic on your first probe or attack. No registration,
  no sign-up form — just start playing. (Your personal vault set is generated
  at that moment.) Checking your state or the leaderboard never enrolls you.
- **Event window:** two weeks exactly — opens July 28, 2026 at 10:00 CDT
  (15:00 UTC) and closes August 11 at the same hour. The live window is
  configured server-side — `client.state()` reports the authoritative dates.
  Probes and attacks are only accepted while the window is open.

## Scoring

Every attack is scored as

$$\text{score} = \text{rawScore} \times \text{costFactor}$$

**rawScore** is the probability of measuring all zeros — computed exactly from
the final state, not estimated from samples, so an identical solution always
earns an identical score. A perfect inversion gives $\text{rawScore} = 1$.

(Probes still sample: their 200-shot histogram is what you read the vault
from, and its noise is part of the reconnaissance problem.)

**costFactor** is the safecracker's tax on brute force. Two-qubit gates are
the expensive, noisy part of any circuit — the drill, not the stethoscope.
Let $c$ be the number of two-qubit gates in the *hidden* vault circuit (its
cost base), and $\Delta$ the **total** number of two-qubit gates in *your*
attack circuit. Then

$$\text{costFactor} = \frac{4c}{4c + \Delta}$$

Read it intuitively:

- Use **no entangling gates at all**, and $\text{costFactor} = 1$ — no
  penalty.
- Every two-qubit gate you spend chips away at your score, gently at first,
  then steeply — and the toll bites harder on vaults with a small cost base.
- Worked example: the practice vault has cost base $c = 2$, and the exact
  GHZ inverse uses 2 CNOTs, so a perfect inversion scores
  $1.0 \times \tfrac{8}{8+2} = 0.8$.
- This means a **partial inversion with a cheap circuit can beat a perfect
  inversion with an expensive one**. If a lock has one stubborn tumbler,
  sometimes the winning move is to leave it and pocket the other
  ninety percent — the challenge is as much circuit *compression* as circuit
  inversion.

**Your total score** is the average of your best attack scores across vaults
1–12. Leaderboard ties are broken by **fewest scored attacks used** — the
safecracker who needed fewer tries ranks higher, so make every attack count.

## Prizes

The top three safecrackers on the final leaderboard win:

| Place | Amazon gift card | qBraid credits |
|---|---|---|
| 1st | $100 | 10,000 |
| 2nd | $50 | 5,000 |
| 3rd | $25 | 2,500 |

qBraid credits are worth $0.01 each and pay for real QPU time, GPU compute, and
AI usage with leading models.

Standings are the average of your best attack across vaults 1–12, with ties
broken by fewest scored attacks used. Both are reported live on the
leaderboard, so you always know where you stand.

## Getting started

The fastest route is the qBraid Lab walkthrough notebook
(`vault_challenge.ipynb`), which ships alongside the client
(`vault_client.py`). Authentication uses your qBraid API key — already
configured inside qBraid Lab; elsewhere, run [`qbraid configure`](https://docs.qbraid.com/v2/cli/api-reference/qbraid_configure) first.

```bash
pip install qbraid qbraid-core qiskit
```

```python
from qiskit import QuantumCircuit
from vault_client import VaultClient

client = VaultClient()

client.state()        # your scores, remaining budgets, and the event window
client.probe(0, qc)   # 200-shot histogram — reconnaissance
client.attack(0, qc)  # 2000-shot attack (vault 0 is practice — unscored)
client.leaderboard()  # current standings
```

A minimal first probe — an *empty* circuit on the practice vault, which shows
you exactly what state the hidden circuit leaves behind:

```python
empty_probe = QuantumCircuit(3)   # a register, no gates

client.probe(0, empty_probe)
```

And the first crack, straight out of Qiskit — no OpenQASM anywhere:

```python
ghz_inverse = QuantumCircuit(3)
ghz_inverse.cx(0, 2)
ghz_inverse.cx(0, 1)
ghz_inverse.h(0)

client.attack(0, ghz_inverse)   # {'rawScore': 1.0, 'costFactor': 0.8, 'score': 0.8}
```

The client converts your circuit to OpenQASM 3 before it goes over the wire, so
anything Qiskit can express is fair game — and the same call accepts Cirq,
Braket and pyQuil circuits if you would rather work in one of those.

Roughly half the shots on key `"0"` (`000`) and half on `"7"` (`111`), with
nothing in between? That signature is a GHZ state,
$\tfrac{1}{\sqrt{2}}(|000\rangle + |111\rangle)$ — and its inverse is yours
to write.

## Field notes for safecrackers

A few tricks of the trade, free of charge:

- **Probe with an empty circuit first.** Submitting a gateless program on the
  right-sized register shows you the vault's raw output distribution — the
  single most informative measurement you can make, for one probe.
- **Learn the classic signatures.** Certain states have unmistakable
  histograms: a GHZ state puts all its weight on the two extreme bitstrings;
  a uniform distribution over half the bitstrings smells like stabilizer
  structure; a single dominant peak means you are already close.
- **Near-stabilizer states are almost classically simulable.** Vaults 5–8 are
  graph states followed by small single-qubit rotations. The graph structure is
  still there and still worth recovering — a few well-chosen probes (try basis
  changes, not just the empty circuit) will expose it — but the rotations mean
  the tell-tale parities are only *strongly biased*, never perfectly
  deterministic. Textbook stabilizer identification, which assumes exact
  determinism, will find nothing. Estimate the bias instead of demanding
  certainty, and remember that undoing the leftover rotations is free: they are
  single-qubit, so they cost you no entangling gates at all.
- **MPS circuits have low entanglement.** Vaults 1–4 come from
  matrix-product states of small bond dimension. Their entanglement is
  local and shallow — a short-depth ansatz optimized classically against
  your probe data can get remarkably far with very few entangling gates.
- **Small ansätze invite brute force.** Vaults 9–12 have only 3–5 qubits.
  A parametrized trial circuit tuned with a classical optimizer (against a
  local simulator, then confirmed with probes) is a strong play — variational
  loops need far fewer server calls than you might think.
- **Mind the exchange rate.** Before spending an extra CNOT to fix a small
  amplitude, check the math: with $\text{costFactor} = 4c/(4c+\Delta)$, an
  extra gate on a low-c vault costs a lot more than on a high-c one.
- **Budget like a professional.** Twenty probes is plenty if each one tests a
  hypothesis. Twenty attacks is plenty if you only attack when you expect a
  new personal best — remember, ties rank by fewest attacks used.

The vaults are waiting, and the Locksmith is betting you can't open them.

Good luck. 🔓

---

*© 2026 qBraid. Questions? Reach out on the qBraid community [Discord](https://discord.com/invite/9jpmpeEV65).*
