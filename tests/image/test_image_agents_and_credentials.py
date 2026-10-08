"""S2 P3: credential-free base, opt-in agents.

Absence checks carry a positive control (the image is an agent-tooling image
and the scan sees it), so a plain or broken image cannot pass them vacuously.
The credential detector is the pattern set below; a clean result is clearance
against that set, not proof that no secret exists.
"""
from __future__ import annotations

import re

import pytest

from image_harness import CLAUDE_VERSION, CODEX_VERSION, image_env, image_for, required, sh, unresolved

pytestmark = pytest.mark.image

AGENT_CLI_SCRIPT = r"""
command -v kp-agent-tooling >/dev/null 2>&1 && echo "CONTROL kp-agent-tooling"
for n in claude codex; do p=$(command -v "$n" 2>/dev/null) && echo "ONPATH $n $p"; done
find / -xdev \( -type f -o -type l \) \( -name claude -o -name codex \) -perm /111 2>/dev/null | sed 's/^/FOUND /'
echo SCAN-DONE
"""

# Checked paths from the order: /root, /home/*, /state, /etc/*token*, ~/.claude*, ~/.codex.
# Package trees are not credential stores and are left out of the name and content scans.
CREDENTIAL_FILE_SCRIPT = r"""
command -v kp-agent-tooling >/dev/null 2>&1 && echo "CONTROL kp-agent-tooling"
userhome=$1
for h in "$userhome" /root /home/* /state; do
  [ -d "$h" ] || continue
  for p in "$h"/.claude* "$h"/.codex; do
    if [ -e "$p" ] || [ -L "$p" ]; then echo "AGENTHOME $p"; fi
  done
done
for p in /etc/*token* /etc/*TOKEN* /etc/*Token*; do
  if [ -e "$p" ] || [ -L "$p" ]; then echo "ETCTOKEN $p"; fi
done
roots=""
for r in /root /home /state "$userhome"; do
  case "$r" in ""|/) continue ;; esac
  [ -d "$r" ] && roots="$roots $r"
done
if [ -n "$roots" ]; then
  find $roots -xdev \( -path '*/node_modules' -o -path '*/site-packages' -o -path '*/dist-packages' \) -prune -o -type f \( \
      -iname '*token*' -o -iname '*secret*' -o -iname '*credential*' -o -iname '.netrc' -o -iname '.npmrc' \
      -o -iname '.pypirc' -o -iname '.git-credentials' -o -iname 'auth.json' -o -iname 'id_rsa*' \
      -o -iname 'id_ed25519*' -o -iname 'id_ecdsa*' -o -iname '*.pem' -o -iname '*.key' -o -iname '.env' \
    \) -print 2>/dev/null | sed 's/^/CREDNAME /'
  find $roots -xdev \( -path '*/node_modules' -o -path '*/site-packages' -o -path '*/dist-packages' \) -prune -o \
    -type f -size -2048k -exec grep -l -I -E \
      'BEGIN [A-Z ]*PRIVATE KEY|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-ant-[A-Za-z0-9_-]{10,}|sk-[A-Za-z0-9]{32,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|"(access|refresh|oauth|id)_?[Tt]oken"[[:space:]]*:' \
      {} + 2>/dev/null | sed 's/^/CREDCONTENT /'
fi
echo SCAN-DONE
"""

CREDENTIAL_NAME = re.compile(r"TOKEN|KEY|SECRET", re.IGNORECASE)
# The python base image publishes the CPython release-signing key fingerprint;
# a public 40-hex OpenPGP fingerprint is not credential material.
PUBLIC_FINGERPRINT = re.compile(r"[0-9A-Fa-f]{40}")


def _default_home(image: str) -> str:
    """`~` of the image's own user (the scans below run as root, whose HOME differs)."""
    result = sh(image, 'printf "%s" "$HOME"', timeout=120)
    return result.stdout.strip()


def _scan(image: str, script: str, *args: str) -> list[str]:
    result = sh(image, script, *args, user="0:0", timeout=900)
    lines = result.stdout.splitlines()
    assert "SCAN-DONE" in lines, f"scan did not finish: exit {result.returncode}: {result.stderr[-500:]}"
    assert "CONTROL kp-agent-tooling" in lines, \
        f"positive control failed: {image} does not carry kp-agent-tooling, so an absence would prove nothing"
    return [line for line in lines if line not in ("SCAN-DONE", "CONTROL kp-agent-tooling")]


@pytest.mark.parametrize("target", ["product", "runtime"])
def test_target_contains_no_agent_cli(target):
    found = _scan(image_for(target), AGENT_CLI_SCRIPT)
    assert not found, f"{target} contains an agent CLI:\n" + "\n".join(found)


@pytest.mark.parametrize("cli, variable", [("claude", CLAUDE_VERSION), ("codex", CODEX_VERSION)],
                         ids=["claude", "codex"])
def test_agents_cli_answers_its_pinned_version(cli, variable):
    image = image_for("agents")
    pinned = required(variable)
    result = sh(image, f"{cli} --version", timeout=180)
    output = result.stdout + result.stderr
    assert result.returncode == 0, f"`{cli} --version` exited {result.returncode}: {output[-800:]}"
    assert re.search(rf"(?<![\w.]){re.escape(pinned)}(?![\w.])", output), \
        f"`{cli} --version` answered {output.strip()[:300]!r}; pinned version is {pinned!r}"


@pytest.mark.parametrize("target", ["product", "runtime", "agents"])
def test_target_contains_no_credential_files(target):
    image = image_for(target)
    found = _scan(image, CREDENTIAL_FILE_SCRIPT, _default_home(image))
    assert not found, f"{target} contains credential material:\n" + "\n".join(found[:60])


@pytest.mark.parametrize("target", ["product", "runtime", "agents"])
def test_target_environment_has_no_credential_variables(target):
    image = image_for(target)
    control = unresolved(image, ["kp-agent-tooling"])
    assert not control, f"positive control failed: {target} does not carry kp-agent-tooling: {control}"
    env = image_env(image)
    found = sorted(
        name for name, value in env.items()
        if CREDENTIAL_NAME.search(name) and not (name == "GPG_KEY" and PUBLIC_FINGERPRINT.fullmatch(value))
    )
    assert not found, f"{target} declares credential-named environment variables: {found}"
