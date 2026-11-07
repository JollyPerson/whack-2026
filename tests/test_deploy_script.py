"""deploy/deploy.sh and set-env.sh against a fake `docker` that records its calls."""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
OLD, NEW = "ghcr.io/o/r:old", "ghcr.io/o/r:new"

FAKE_DOCKER = """#!/usr/bin/env bash
echo "$* | stdin=$([ "$1" = login ] && cat) | image=$(cat image.env 2>/dev/null)" >> calls.log
case " $* " in
  *" pull "*) [ -z "${FAIL_PULL:-}" ] ;;
  *" up "*) ! grep -qxF "IMAGE=${UNHEALTHY:-none}" image.env ;;
esac
"""


@pytest.fixture
def server(tmp_path):
    shutil.copy(DEPLOY / "deploy.sh", tmp_path)
    (tmp_path / "bin").mkdir()
    docker = tmp_path / "bin" / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    (tmp_path / ".env").touch()
    return tmp_path


def deploy(server, image=NEW, token="", **env):
    return subprocess.run(
        [str(server / "deploy.sh"), image], input=token, capture_output=True, text=True,
        env={**os.environ, "PATH": f"{server / 'bin'}:{os.environ['PATH']}", **env},
    )


def calls(server):
    return (server / "calls.log").read_text().splitlines()


def current(server):
    return (server / "image.env").read_text().strip()


def test_healthy_deploy_switches_image_and_records_it(server):
    (server / "image.env").write_text(f"IMAGE={OLD}\n")
    r = deploy(server)
    assert r.returncode == 0, r.stderr
    assert current(server) == f"IMAGE={NEW}"
    assert NEW in (server / "deploys.log").read_text()


def test_unhealthy_deploy_rolls_back_and_fails(server):
    (server / "image.env").write_text(f"IMAGE={OLD}\n")
    r = deploy(server, UNHEALTHY=NEW)
    assert r.returncode == 1
    assert current(server) == f"IMAGE={OLD}"
    assert "Rolling back" in r.stderr
    assert not (server / "deploys.log").exists()


def test_rollback_that_also_fails_says_the_site_is_down(server):
    # The fake matches one image, so make the "previous" image the same broken one.
    (server / "image.env").write_text(f"IMAGE={NEW}\n")
    r = deploy(server, UNHEALTHY=NEW)
    assert r.returncode == 1
    assert "the site is down" in r.stderr


def test_first_deploy_failure_has_nothing_to_roll_back_to(server):
    r = deploy(server, UNHEALTHY=NEW)
    assert r.returncode == 1
    assert "No previous image" in r.stderr


def test_failed_pull_leaves_the_running_stack_alone(server):
    (server / "image.env").write_text(f"IMAGE={OLD}\n")
    r = deploy(server, FAIL_PULL="1")
    assert r.returncode != 0
    assert current(server) == f"IMAGE={OLD}"
    assert not any(" up " in c for c in calls(server))


def test_token_comes_from_stdin_and_is_logged_out_after(server):
    r = deploy(server, token="s3cret")
    assert r.returncode == 0, r.stderr
    log = calls(server)
    assert any(c.startswith("login ghcr.io") and "stdin=s3cret" in c for c in log)
    assert not any("s3cret" in c.split(" | ")[0] for c in log)   # never on a command line
    assert log[-1].startswith("logout ghcr.io")


def set_env(file, key, value):
    return subprocess.run([str(DEPLOY / "set-env.sh"), str(file), key], input=value, capture_output=True, text=True)


def test_set_env_replaces_only_its_key_and_keeps_dollars_literal(tmp_path):
    f = tmp_path / ".env"
    f.write_text("OTHER='x'\nHASH='old'\n")
    assert set_env(f, "HASH", "$2y$12$abc").returncode == 0
    assert f.read_text() == "OTHER='x'\nHASH='$2y$12$abc'\n"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


@pytest.mark.parametrize("bad", ["it's", "two\nlines"])
def test_set_env_refuses_values_it_cannot_quote(tmp_path, bad):
    f = tmp_path / ".env"
    f.write_text("KEEP='x'\n")
    assert set_env(f, "KEY", bad).returncode == 1
    assert f.read_text() == "KEEP='x'\n"
