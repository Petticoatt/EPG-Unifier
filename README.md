# EPG Unifier

A priority-aware XMLTV merger for TiviMate, UHF, and other XMLTV clients.

The repository contains the user-supplied IPTV-EPG.org, Open-EPG, EPGSHARE01, and i.mjh.nz source list. It creates one global XMLTV guide plus optional regional guides, in both plain XML and gzip-compressed form.

## Merge contract

1. Sources are processed by `priority`, then by `order` in `config/sources.json`.
2. Channel IDs are compared exactly and case-sensitively. `BBCOne.uk`, `bbc1.uk`, and `BBC.ONE.HD` remain three distinct channels.
3. The first source containing an exact channel ID owns that channel and its schedule.
4. A lower-priority source cannot replace or add programmes for an already-owned exact channel ID.
5. When the winning channel has no `<icon>`, the merger may copy an icon from a lower-priority source with the same exact ID. It then tries the iptv-org logo API with the same exact ID.
6. Programme entries are limited to a rolling window of six hours in the past and seven days in the future. This is a maximum window, not a promise that every upstream source provides seven days.
7. A failed build does not replace the previous output. Source-success and count-drop safeguards protect the last known good files.

## Files produced

The primary files are:

```text
data/output/epg.xml
data/output/epg.xml.gz
```

The configuration also produces:

```text
epg-us.xml(.gz)
epg-ca.xml(.gz)
epg-br.xml(.gz)
epg-europe.xml(.gz)
epg-asia-pacific.xml(.gz)
status.json
```

Regional classification is source-based. A globally scoped FAST-channel source is included in the global output but is not guessed into a country or region.

## Run locally with Docker

```bash
cp .env.example .env
docker compose build
./scripts/run-local.sh
```

The first build can download a substantial amount of data. Later builds use a persistent cache. Open-EPG sources are cached for 24 hours because that provider currently publishes daily, while the combined output can still be rebuilt every six hours.

## Run every six hours on Linux

Copy the repository to `/opt/epg-unifier`, then install the supplied systemd units:

```bash
sudo cp systemd/epg-unifier.service /etc/systemd/system/
sudo cp systemd/epg-unifier.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now epg-unifier.timer
systemctl list-timers epg-unifier.timer
```

The timer runs near 00:17, 06:17, 12:17, and 18:17 UTC, with a small randomized delay to reduce synchronized load on upstream providers.

## Serve stable HTTPS URLs from the same VPS

Point a DNS record such as `epg.example.com` to the VPS, add this to `.env`, then start Caddy:

```text
EPG_DOMAIN=epg.example.com
```

```bash
docker compose -f compose.serve.yaml up -d
```

The application URLs become:

```text
https://epg.example.com/epg.xml
https://epg.example.com/epg.xml.gz
```

Regional files use the same hostname and filenames listed above. Caddy obtains and renews TLS certificates automatically when DNS and ports 80/443 are correctly configured.

Object storage can be used instead. Upload the completed contents of `data/output` only after the merger exits successfully. Preserve the same object names so the application URLs never change.

## Create the repository under a separate GitHub account

Do not use a GitHub session authenticated as `alexbesp18`.

1. Open a separate browser profile or private window and sign in only to the intended GitHub account.
2. Install GitHub CLI, then authenticate and verify the active account:

```bash
gh auth login --hostname github.com --web
gh auth status --hostname github.com
gh api user --jq .login
```

3. If GitHub CLI already knows both accounts, switch explicitly:

```bash
gh auth switch --hostname github.com --user TARGET_USERNAME
gh api user --jq .login
```

4. Set commit identity only inside this repository:

```bash
git init -b main
git config user.name "TARGET DISPLAY NAME"
git config user.email "TARGET_ACCOUNT_NOREPLY_EMAIL"
git add .
git commit -m "Initial EPG unifier"
gh repo create TARGET_USERNAME/epg-unifier --public --source=. --remote=origin --push
```

Run `gh api user --jq .login` immediately before `gh repo create`. Stop if the printed login is not the intended account.

## Why production generation is not scheduled in GitHub Actions

The included GitHub workflow runs lightweight tests only. It deliberately does not download and republish the global EPG every six hours. A workload of this size can exceed repository or Pages limits, scheduled runs can be delayed, and GitHub prohibits using Actions as a CDN or serverless application. Keep GitHub as the source-code home and use a VPS, NAS, home server, or scheduled container for production generation.

## Configuration

Edit `config/sources.json` to disable a source, change its order, or change refresh cadence. Do not normalize channel IDs unless you intentionally want different IDs to collapse.

Key settings:

```json
{
  "future_days": 7,
  "past_hours": 6,
  "stale_if_error_hours": 72,
  "minimum_source_success_ratio": 0.65,
  "maximum_channel_drop_ratio": 0.30,
  "maximum_programme_drop_ratio": 0.45
}
```

The `status.json` report records source health, exact unique channel count, programmes retained, and output counts. Monitor it even in a mostly hands-off deployment.

## Testing

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt pytest
pytest -q
```

Tests cover exact-ID priority, preservation of channel-ID variants, icon fallback, seven-day filtering, gzip output, and timezone parsing.

## Important operational and legal notes

* Confirm that every upstream provider permits automated downloading and republication in a combined public feed. Public availability is not automatically a redistribution license.
* Upstream URLs, formats, channel IDs, logos, coverage, and availability can change without notice.
* Some providers may block particular networks, countries, VPNs, or excessive download rates.
* A seven-day window cannot create listings that an upstream source does not supply.
* Logo completeness is best effort. Broken remote image links and channels with no matching logo will remain possible.
* The code is licensed under MIT. That license does not grant rights to third-party guide data, logos, or trademarks.
