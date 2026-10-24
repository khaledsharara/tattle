# Contributing to Tattle

Thanks for helping. The most useful contributions right now:

1. **Testing a beta provider** on your own account: WE, Vodafone Egypt, Aussie Broadband, Telenet/BASE, Ogero, Xfinity or Start.ca. Sign in from Settings and [send a test report](https://github.com/khaledsharara/tattle/issues/new?template=test-report.yml), whether it worked or not.
2. **Testing a beta router**: OpenWrt with nlbwmon, MikroTik RouterOS 7, or UniFi. Same report.
3. **Support for your provider**: [docs/adding-a-provider.md](docs/adding-a-provider.md). If you can't write it, [ask for it](https://github.com/khaledsharara/tattle/issues/new?template=provider-request.yml) with what you know about its app.
4. **Support for your router**: [docs/adding-a-router.md](docs/adding-a-router.md).
5. Bug reports and fixes.

## Personal data

Tattle handles phone numbers, account numbers, bills and the devices in people's homes, so this rule comes first: **no real numbers, account or contract IDs, tokens, passwords, bills, names, MAC addresses or public IP addresses** in code, tests, issues, logs, screenshots or pull requests.

- Replace them with made-up values and keep the structure: a provider's field names and nesting are what matter.
- MAC addresses in tests come from the documentation range `00:00:5e:00:53:00`–`ff` (or locally administered ones like `02:…` for "private Wi-Fi addresses"); CI rejects others.
- Screenshots come from the demo (`TATTLE_DEMO=1`), never from a real home.

## Set up

```bash
git clone https://github.com/khaledsharara/tattle.git && cd tattle

# backend (Python 3.12+)
python -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements-dev.txt
cd backend && pytest

# dashboard (Node 22+)
cd frontend && npm ci && npm run build

# run the demo on http://localhost:8090 with the dashboard you just built
cd backend && TATTLE_DEMO=1 TATTLE_DB=/tmp/tattle-demo.db TATTLE_STATIC=../frontend/dist python -m tattle

# or work on the dashboard with hot reload (it talks to the demo above)
cd frontend && npm run dev
```

`docker build -t tattle .` builds the image the way releases do.

## Before a pull request

```bash
cd backend && pytest && python -m pyflakes tattle
cd frontend && npm run build          # typecheck + build
python scripts/check-privacy.py       # what CI runs on every file
```

Changes to the dashboard: attach a screenshot from the demo. Changes to gateway mode: `tests/gateway-e2e.sh` runs in CI on a throwaway virtual network; read it before changing the gateway.

## Conventions

- Backend: Python standard library first. A provider or router plugin adds **no new dependency**.
- Dashboard: React + TypeScript + Tailwind CSS 3. Code lives in `frontend/src/modules/<feature>/` (`pages`, `components`, `types`, `utils`); anything used by more than one module goes in `modules/shared`.
- Messages people see are plain, specific sentences that say what to do next. Buttons say what they do.
- Comments say *why*, not what.
- Keep commits focused, with messages that say what changes and why.

## Releases

Maintainers tag `vX.Y.Z`; [release.yml](.github/workflows/release.yml) builds the image for amd64 and arm64, pushes it to `ghcr.io/khaledsharara/tattle`, and publishes the release.
