# Deploying to Vultr

Every push to `master` is tested, built and deployed to one Vultr server. Pull requests get the tests and the
image build, and never deploy. The pipeline is [`.github/workflows/ci-cd.yml`](../.github/workflows/ci-cd.yml).

```
push to master -> test -> build + smoke test -> push to ghcr.io -> deploy over SSH -> check the public URL
```

The server runs two containers from [`deploy/compose.yml`](../deploy/compose.yml): the app, and Caddy in front of
it for HTTPS and the site password. Only Caddy is public. Browsers block the microphone without HTTPS, so `live`
needs it. The app has no auth of its own and `/publish` and `/control` can steer a debate, so everything except
`/health` needs the password. `/health` stays open for the pipeline, and shows module choices and the log filename.

## First-time setup

```bash
./scripts/setup-vultr.sh
```

It walks you through creating the server, sets it up, and fills in the GitHub settings below. Re-run it to change
any answer. Re-running also replaces the deploy key. Your non-secret answers are kept in `.env.vultr` (gitignored).

| GitHub setting | Kind | What it is |
|---|---|---|
| `VULTR_SSH_KEY` | secret | Private key for the server's `deploy` user |
| `VULTR_KNOWN_HOSTS` | secret | The server's SSH identity, so the pipeline can't be pointed at another machine |
| `VULTR_HOST` | variable | Server IP |
| `VULTR_DOMAIN` | variable | Public hostname |
| `DEPLOY_ENABLED` | variable | Must be `true` for anything to deploy |

Until `DEPLOY_ENABLED` is `true`, the deploy job is skipped and the rest of the pipeline still runs.

The `deploy` user can run Docker, which is equivalent to root on this server, so keep the server dedicated to this app.

## Day to day

**Freeze before a demo.** A deploy restarts the app, and a restart drops the running debate. Merges stop deploying
until you set this back to `true`:

```bash
gh variable set DEPLOY_ENABLED --body false
```

**Roll back** to any of the last ten images. The tag is the commit SHA. A deploy that fails its health check goes
back to the previous image by itself, so this is for a release that is healthy but wrong.

```bash
gh workflow run ci-cd.yml --ref master -f image_tag=<commit-sha>
```

**Redeploy the current commit**, for example after changing settings on the server:

```bash
gh workflow run ci-cd.yml --ref master
```

**Change API keys or role overrides.** On the server they live in `/opt/debate-ref/app.env` (`EARS=`, `EXA_API_KEY=`, ...),
and the profile is `APP_PROFILE` in `/opt/debate-ref/.env`. Re-running the setup wizard asks for them again, or set one
from your machine. The value goes on stdin, so it stays out of shell history, and `sudo -u deploy` keeps the file
readable by the pipeline. Then redeploy. Keys never go through GitHub.

```bash
read -rs KEY && printf '%s' "$KEY" | ssh root@<ip> "sudo -u deploy /opt/debate-ref/set-env.sh /opt/debate-ref/app.env EXA_API_KEY"
printf drive | ssh root@<ip> "sudo -u deploy /opt/debate-ref/set-env.sh /opt/debate-ref/.env APP_PROFILE"
```

**Evidence indexes.** After each successful deploy, `deploy.sh` starts
`python -m app.evidence.preresearch --all --missing` in the background inside the app container. It builds the
demo Motions' indexes into the `cache` volume (`/srv/cache/evidence`), which survives deploys, so once they're built
it does nothing. It never holds up or fails a deploy, and it's skipped while `EXA_API_KEY` isn't set in `app.env`.
A deploy restarts the app, which stops a build part way through; the next deploy finishes it. Its output is in the
`logs` volume:

```bash
docker compose --env-file .env --env-file image.env exec app tail -f /srv/logs/preresearch.log
```

The app reads `/srv/cache/evidence-v2` (set in `compose.yml`): big indexes with vectors, built on a laptop because
embedding them takes hours on this server's single vCPU, and copied in. The deploy's pre-research runs with
`EVIDENCE_EMBEDDER=none`, so a Motion missing from the folder gets a small BM25 index rather than tying up the CPU.
`/srv/cache/evidence` keeps the older BM25 indexes for a rollback to an image from before the switch.
`/srv/cache/fastembed` holds the embedding model, so the app doesn't download it on start.

To add or rebuild one with vectors, build it on a laptop with a Python 3.12 venv (see `app/evidence/README.md`),
then copy it in:

```bash
scp cache/evidence/<name>.* root@<ip>:/root/
ssh root@<ip> 'cd /opt/debate-ref && for f in /root/<name>.*; do docker compose --env-file .env --env-file image.env cp "$f" app:/srv/cache/evidence-v2/; done && docker compose --env-file .env --env-file image.env exec -T -u root app chown -R app:app /srv/cache/evidence-v2'
```

To rebuild one on the server instead, delete its `.json` and `.npy` (`exec app sh -c 'rm /srv/cache/evidence-v2/<name>.*'`) and redeploy.

**Look at the server.** Run these from `/opt/debate-ref` as `deploy`. Event logs are in the `logs` volume, and
`deploys.log` lists past deploys.

```bash
docker compose --env-file .env --env-file image.env logs -f app
```

## Good to know

- The default profile is `mock`. `drive` plays the recorded debate through the real Brain and Evidence, and needs
  `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY`) and `EXA_API_KEY`. `live` shows a red dot for Ears until `app/ears` has a
  `run.py`.
- The default address `<ip>.sslip.io` is a free shared DNS service. Let's Encrypt limits certificates per domain, so it
  can occasionally refuse one. Use your own domain for anything that matters.
- External role processes (`EVIDENCE=external`) must send the site password when they connect to `/ws` or `/publish`.
  So must any browser page: Safari may not send the saved password on a WebSocket, so test the demo browser first.
- The pipeline tests on Python 3.12, and the image uses the same version. Requirements aren't pinned, so a rebuild can pick
  up newer packages.
