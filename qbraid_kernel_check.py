# Copyright 2025 qBraid
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
Vault Challenge kernel / environment guard.

Ensures a notebook is running on the Vault Challenge kernel. If not, it works out
*why* and gives the matching fix:

  1. Not in qBraid                      -> skip the check.
  2. Correct kernel already active      -> pass.
  3. Env not installed                  -> install it (CLI or sidebar).
  4. Env installed, kernel not added    -> add the kernel (CLI or panel).
  5. Kernel available but not selected  -> switch kernel (upper-right picker).

Usage from a notebook cell in the same directory:

    from qbraid_kernel_check import check_vault_kernel
    check_vault_kernel()
"""

import os
import sys
import glob
import json
import shutil
import subprocess

_VAULT_PREFIX  = "/home/jovyan/.qbraid/environments/vault"
_ENV_NAME     = "Vault Challenge"
_KERNEL_NAME  = "Python 3 [Vault Challenge]"
_INSTALL_SLUG = "vault_8t6kfi"
_DOCS_URL     = "https://docs.qbraid.com/v2/lab/user-guide/notebooks#switch-notebook-kernel"


_LAB_HOME = "/home/jovyan"


def _running_in_qbraid() -> bool:
    """True if we appear to be inside qBraid Lab.

    Deliberately NOT `shutil.which("qbraid")`. The qBraid CLI is exactly what
    participants running outside Lab are told to install (`pip install qbraid`,
    then `qbraid configure`), so its presence is evidence of nothing — and
    treating it as evidence made this guard raise on the first cell of the
    notebook for every local user, with instructions about the qBraid sidebar
    that do not apply to them.

    The Lab image is Linux with ``/home/jovyan`` as HOME, which is the same
    assumption the rest of this module already makes: the authoritative pass
    check is ``sys.executable.startswith(_VAULT_PREFIX)``, and that prefix is
    rooted there. The environments directory is accepted as an alternative
    signal so a Lab session still matches if HOME is ever relocated.
    """
    return (
        os.path.realpath(os.path.expanduser("~")) == _LAB_HOME
        or os.path.isdir(_LAB_HOME + "/.qbraid/environments")
    )


def _vault_env_id():
    """Look up the installed Vault environment and return its env id.

    Returns the env id (str) if installed, False if it is confirmed not
    installed, or None if installation could not be determined.

    The env folder name and id are generated dynamically (e.g. vault_2_scc0),
    and change if the environment is reinstalled. We check the filesystem
    first because it is robust even while `qbraid envs list` is mid-operation
    or renders the path with a status suffix; the CLI is a fallback.
    """
    # 1) Filesystem (authoritative): a fully-installed env has pyenv/bin/python.
    #    The env id is the trailing token of the folder name, e.g.
    #    vault_2_scc0 -> 'scc0', matching the Env ID column of `qbraid envs list`.
    for path in sorted(glob.glob(_VAULT_PREFIX + "*")):
        if os.path.isdir(path) and os.path.exists(
            os.path.join(path, "pyenv", "bin", "python")
        ):
            return os.path.basename(path).split("_")[-1]

    # 2) Fallback: parse `qbraid envs list`.
    qbraid = shutil.which("qbraid")
    if qbraid is None:
        return None
    try:
        result = subprocess.run(
            [qbraid, "envs", "list"],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    # Rows look like:  Vault Challenge        scc0      /home/jovyan/.qbraid/environments/vault_2_scc0
    # Locate the path token wherever it sits on the line (robust to trailing
    # status text) and read the env id from the token just before it.
    for line in result.stdout.splitlines():
        parts = line.split()
        for i, tok in enumerate(parts):
            if tok.startswith(_VAULT_PREFIX):
                return parts[i - 1] if i >= 1 else os.path.basename(tok).split("_")[-1]
    return False


def _vault_python_kernel_registered():
    """True if a *Python* Jupyter kernel pointing at the Vault env is registered."""
    jupyter = shutil.which("jupyter")
    if jupyter is None:
        return None
    try:
        result = subprocess.run(
            [jupyter, "kernelspec", "list", "--json"],
            capture_output=True, text=True, timeout=60,
        )
        specs = json.loads(result.stdout)["kernelspecs"]
    except Exception:
        return None
    for info in specs.values():
        spec = info.get("spec", {})
        argv = spec.get("argv", [])
        language = (spec.get("language") or "").lower()
        if language.startswith("python") and argv and argv[0].startswith(_VAULT_PREFIX):
            return True
    return False


def check_vault_kernel(raise_on_error: bool = True) -> bool:
    """Verify the active kernel is the Vault Challenge environment.

    Returns True if OK (or skipped because we're not in qBraid).
    Raises RuntimeError otherwise (unless raise_on_error=False).
    """
    if not _running_in_qbraid():
        # Playing locally is supported — the README says so — so this is a
        # note, never an error. Point at what a local run actually needs,
        # since the install cell below is commented out by default.
        print("ℹ️  Not running in qBraid Lab — skipping the kernel check.")
        print("   Running locally? Make sure the dependencies are installed")
        print("   (see the next cell) and that you have run 'qbraid configure'.")
        return True

    if sys.executable.startswith(_VAULT_PREFIX):
        print(f"✅ {_ENV_NAME} kernel is active.")
        print(f"   sys.executable = {sys.executable}")
        return True

    # Wrong kernel selected — diagnose why.
    env_id = _vault_env_id()             # str id | False (not installed) | None (unknown)
    installed = env_id not in (False, None)

    lines = [
        "",
        f"❌ This notebook is NOT running on the {_ENV_NAME} kernel.",
        f"   Current kernel:  {sys.executable}",
        f"   Expected path to start with:  {_VAULT_PREFIX}...",
        "",
    ]

    if env_id is False:
        # State 3: environment not installed.
        lines += [
            f"The {_ENV_NAME} environment is not installed.",
            "1) Install it, either:",
            f"     • from a terminal:   qbraid envs install {_INSTALL_SLUG}",
            f"     • or via the Environment Manager in the qBraid sidebar",
            f"       (find '{_ENV_NAME}' in the 'Hackathons & Competitions' environment group and click Install).",
            f"2) Then switch this notebook's kernel to '{_KERNEL_NAME}'",
            "   using the kernel picker in the upper-right of the notebook.",
        ]
    elif installed and _vault_python_kernel_registered() is False:
        # State 4: env installed, but its Python kernel has not been added.
        add_target = env_id if isinstance(env_id, str) else "<env id>"
        lines += [
            f"The {_ENV_NAME} environment is installed, but its Python kernel",
            "has not been added to Jupyter yet.",
            "1) Add the kernel, either:",
            f"     • click 'Add kernel' in the {_ENV_NAME} panel of the",
            "       Environment Manager (qBraid sidebar), or",
            f"     • from a terminal:   qbraid kernels add {add_target}",
            f"2) Then switch this notebook's kernel to '{_KERNEL_NAME}'",
            "   using the kernel picker in the upper-right of the notebook.",
        ]
    else:
        # State 5: kernel is available (or status unknown) — just not selected.
        lines += [
            f"Switch this notebook's kernel to '{_KERNEL_NAME}':",
            "  • Click the kernel name in the upper-right of the notebook,",
            "    then choose it from the list.",
        ]

    lines += ["", f"Docs: {_DOCS_URL}", ""]
    message = "\n".join(lines)

    if raise_on_error:
        raise RuntimeError(message)
    print(message)
    return False
