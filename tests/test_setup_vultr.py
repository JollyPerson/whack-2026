"""scripts/setup-vultr.sh against fake gh/ssh/scp/ssh-keygen: what reaches the server and GitHub, and in what order.
Runs a copy in a temp tree (the wizard cds to its repo root and writes .env.vultr there); nothing real is contacted."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

STUBS = {
    # The value of `gh secret set NAME` comes on stdin.
    "gh": """case "$1 $2" in
  "auth status") exit 0 ;;
  "secret set") v=$(cat); echo "secret $3=$v" >> "$LOG"; [ -z "${FAIL_SECRET:-}" ] ;;
  "variable set") echo "var $3=$5" >> "$LOG" ;;
  "api "*) exit 1 ;;
  *) echo "gh $*" >> "$LOG" ;;
esac""",
    "ssh": """cmd=${*: -1}
case "$*" in
  *bootstrap.sh*) echo "bootstrap" >> "$LOG" ;;
  *set-env.sh*) set -- $(printf '%s' "$cmd" | tr -d "'"); echo "set-env ${2##*/} $3=$(cat)" >> "$LOG" ;;
  *htpasswd*) cat > /dev/null; echo 'user:$2y$12$fakehash' ;;
  *"grep -q"*) exit 1 ;;
esac""",
    "scp": 'echo "scp $*" >> "$LOG"',
    "ssh-keygen": """while [ $# -gt 0 ]; do
  case $1 in
    -lf) echo "256 SHA256:fake (ED25519)"; exit 0 ;;
    -f) echo "PRIVATE-KEY" > "$2"; echo "ssh-ed25519 AAAAci github-actions-deploy" > "$2.pub"; shift ;;
  esac
  shift
done""",
    "ssh-keyscan": 'echo "1.2.3.4 ssh-ed25519 AAAAhost"',
    "ssh-add": "exit 1",
    "openssl": 'echo "0123456789abcdef01234567"',
    "wslview": "exit 0",          # open_url's first choice: never open a real browser
}


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "setup-vultr.sh", tmp_path / "scripts")
    (tmp_path / "bin").mkdir()
    for name, body in STUBS.items():
        stub = tmp_path / "bin" / name
        stub.write_text("#!/usr/bin/env bash\n" + body + "\n")
        stub.chmod(0o755)
    for d in ("home", "tmp"):
        (tmp_path / d).mkdir()
    return tmp_path


def answers(**kw):
    """One line per prompt, in the order the wizard asks. A list value answers that prompt more than once."""
    a = dict(start="", host="1.2.3.4", confirm="y", domain="", user="", password="", saved="", profile="",
             anthropic="", openai="", exa="", evidence="", connect="", finish="")
    a.update(kw)
    lines = []
    for v in a.values():
        lines += v if isinstance(v, list) else [v]
    return "".join(f"{line}\n" for line in lines)


def wizard(repo, stdin, **env):
    r = subprocess.run(
        ["bash", str(repo / "scripts" / "setup-vultr.sh")], input=stdin, capture_output=True, text=True, timeout=30,
        env={**os.environ, "PATH": f"{repo / 'bin'}:{os.environ['PATH']}", "HOME": str(repo / "home"),
             "TMPDIR": str(repo / "tmp"), "LOG": str(repo / "calls.log"), **env},
    )
    log = (repo / "calls.log").read_text().splitlines() if (repo / "calls.log").exists() else []
    return r, log


def test_a_full_run_configures_the_server_and_github(repo):
    r, log = wizard(repo, answers())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "set-env .env DOMAIN=1-2-3-4.sslip.io" in log
    assert "set-env .env BASIC_AUTH_HASH=$2y$12$fakehash" in log
    assert "set-env .env APP_PROFILE=mock" in log
    assert "secret VULTR_SSH_KEY=PRIVATE-KEY" in log
    assert "secret VULTR_KNOWN_HOSTS=1.2.3.4 ssh-ed25519 AAAAhost" in log
    assert {"var VULTR_HOST=1.2.3.4", "var VULTR_DOMAIN=1-2-3-4.sslip.io", "var DEPLOY_ENABLED=true"} <= set(log)
    assert list((repo / "tmp").iterdir()) == []                # the CI key is gone once GitHub has it


def test_github_gets_the_new_ci_key_before_anything_else_can_stop_the_wizard(repo):
    # bootstrap.sh makes the server trust only the new key: from then on, losing it locks CI out.
    r, log = wizard(repo, answers())
    assert r.returncode == 0, r.stdout + r.stderr
    assert log.index("secret VULTR_SSH_KEY=PRIVATE-KEY") == log.index("bootstrap") + 1


LATER = ["domain", "user", "password", "saved", "profile", "anthropic", "openai", "exa", "evidence", "connect",
         "finish"]


@pytest.mark.parametrize("stop_at", ["domain", "user", "profile"])
def test_stopping_after_the_server_is_set_up_leaves_github_with_the_new_key(repo, stop_at):
    unanswered = LATER[LATER.index(stop_at):]                  # input ends at this prompt (Ctrl-D)
    r, log = wizard(repo, answers(**{k: [] for k in unanswered}))
    assert r.returncode != 0
    assert "bootstrap" in log and "secret VULTR_SSH_KEY=PRIVATE-KEY" in log


def test_a_ci_key_github_did_not_take_is_kept_and_the_wizard_stops(repo):
    r, log = wizard(repo, answers(), FAIL_SECRET="1")
    assert r.returncode != 0
    assert not any(line.startswith("set-env") for line in log)   # stopped right there
    [key_dir] = (repo / "tmp").iterdir()
    assert (key_dir / "ci_key").read_text() == "PRIVATE-KEY\n"
    assert str(key_dir / "ci_key") in r.stdout


def test_enter_at_the_username_prompt_means_debate(repo):
    r, log = wizard(repo, answers(user=""))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "set-env .env BASIC_AUTH_USER=debate" in log
    assert "BASIC_AUTH_USER='debate'" in (repo / ".env.vultr").read_text()


@pytest.mark.parametrize("bad", ["bad:user", "it's", "two words"])
def test_a_username_basic_auth_cannot_use_is_asked_again(repo, bad):
    r, log = wizard(repo, answers(user=[bad, "ref.team"]))
    assert r.returncode == 0, r.stdout + r.stderr
    assert [line for line in log if "BASIC_AUTH_USER" in line] == ["set-env .env BASIC_AUTH_USER=ref.team"]


def test_a_profile_the_app_does_not_know_is_asked_again(repo):
    r, log = wizard(repo, answers(profile=["Live", "drive"]))
    assert r.returncode == 0, r.stdout + r.stderr
    assert [line for line in log if "APP_PROFILE" in line] == ["set-env .env APP_PROFILE=drive"]


def test_the_profiles_the_wizard_offers_exist():
    from app.main import PROFILES
    assert {"mock", "drive", "live"} <= set(PROFILES)
