# Deploy a dashboard on shared forecasting infrastructure

AIRO, EEAI and RSI use the same release builder, static server, scheduler, receipts
and rollback command. Each installation selects a dashboard by name. Its own
`current` pointer switches its code, site and schedule together. The serving
process pins every response to one publication. The existing AIRO configuration
and `airo` command remain valid.

## Server roles

Use **forecast-prod** for the forecasting server and **forecast-web** for the
web host in operator tooling and installation notes. These are role names.
Record the physical machines, SSH targets and public hostnames in the private
installation configuration.

- On forecast-prod, each project has its own release, data, receipts and
  publication directory. One timer calls `dashboardctl tick-all`. A private
  `forecast-prod NAME COMMAND` launcher can dispatch to this same CLI.
- On forecast-web, configure a virtual host for each public hostname, with its
  own document root and access policy. Set the root to `SITE_ROOT/current`.
  Keep raw forecasts and forecasting credentials on forecast-prod.
- A public hostname may reach its virtual host directly or through a legacy
  forwarding server. If forwarding, preserve the dashboard's identity, verify
  upstream TLS, and keep its access policy on the shared web host. Existing
  bookmarks can keep working throughout a migration.

### Put an existing site on the shared web host

1. Copy the active static snapshot into a separate staging directory. Compare a
   manifest of all file paths and SHA-256 hashes to the current origin. Import
   any existing password gate privately and keep independent cookie names.
2. Install `packages/ops/publish_ssh.py` on the web host. Give the SSH account
   write access to the site's publication root. Configure a verifier executable
   there that accepts one candidate directory, fetches the public hostname,
   compares the served release metadata and HTML to the candidate, and checks
   protected downloads reject anonymous access. Keep passwords outside git.
3. Test the new virtual host with its intended Host header. For a migrated
   hostname, test the forwarding rule or traffic switch and retain its existing
   configuration for rollback. Check public HTTPS with and without credentials
   as appropriate.
4. Configure `public_publisher` on forecast-prod using the argv below, with
   installation-specific absolute paths. The job appends the verified site
   directory as the final argument. Check **SSH from the forecasting service
   account** and run `dashboardctl NAME republish` to prove unattended delivery
   with existing data before enabling a paid schedule.

```json
{
  "public_publisher": [
    "/usr/bin/python3", "/path/to/checked/packages/ops/publish_ssh.py", "send",
    "--ssh-config", "/path/to/private/ssh-config", "--target", "forecast-web",
    "--receiver", "/path/to/web-host/publish_ssh.py",
    "--root", "/path/to/site-root", "--verify", "/path/to/site-verifier"
  ]
}
```

The receiver stages the transfer, checks its archive digest, switches the site
atomically and runs the verifier. A failed verification restores the preceding
publication. A publish-only retry never calls the forecasting models.

## Install another site

1. Merge the dashboard and runtime through the normal checks and deploy AIRO
   with `airo deploy`. Use the checked release as the bootstrap launcher for
   the new instance. Run the remaining setup on the production server with
   its forecasting service account and toolchain.
2. Install `packages/ops/dashboardctl` as `/usr/local/bin/dashboardctl`.
   Create a private `/etc/fri-dashboards/NAME.json` for each installation.
   An installation can point `DASHBOARD_CONFIG_DIR` at another private
   directory. To manage existing AIRO here, symlink `airo.json` to its existing
   runtime config. Keep its existing root, socket, service account and state.
3. For a survey, start from the example below. Replace the example paths with
   absolute production paths. Copy `repository`, `github_repository` and
   `path` from the working AIRO configuration. Give each instance a distinct
   root and socket. Keep the socket directory accessible to the forecast
   account and ingress service group. Keep raw data and private configs
   outside that directory.

```json
{
  "dashboard": "rsi",
  "root": "/srv/forecasts/rsi",
  "socket": "/run/fri-sites/rsi.sock",
  "repository": "<same clone URL as AIRO>",
  "github_repository": "<same owner/repository as AIRO>",
  "path": "<same production PATH as AIRO>",
  "panel_runtime_config": "/etc/fri-dashboards/airo.json",
  "bootstrap_code": "/srv/forecasts/airo/current/code"
}
```

`panel_runtime_config` shares AIRO's private forecast home, operation lock,
Epoch snapshots and automatic model registrations. A survey forecast refreshes
the panel through the primary release before starting paid calls. The
date check in the runner also applies to manual calls. Each survey writes
its own results, attempts, sites and receipts under its own root.

4. Install the example systemd units in `/etc/systemd/system`. Set
   `User` and `Group` to the existing forecasting account and socket group.
   Create the socket directory on boot, for example with a tmpfiles entry
   `d /run/fri-sites 0750 forecast forecast -`. Grant the ingress worker
   traversal and access to the socket (0660). Start the site service before
   the first deploy so the activation probe can reach it.

```sh
systemctl daemon-reload
systemctl enable --now fri-dashboard@rsi.service
dashboardctl rsi deploy --publish-data
dashboardctl rsi status
dashboardctl rsi smoke --run-id install-smoke
```

RSI can start with the real empty survey. Its protected results directory
is created under `state/results`. For EEAI, use `"dashboard": "eeai"` and its
own paths. To move the existing EEAI site without changing any forecast,
copy its published folder to private storage, set `bootstrap_publication`
to that folder and do the first deploy with `--keep-publication`. Seed
`state/results/` with the complete real run JSONL files before the first
data rebuild. For licensed EEAI fonts, seed `state/fonts/` from the licensed
installation. Keep the existing hostname and password gate in place during
cutover. Compare the prior and staged download hashes before switching ingress.

On subsequent deploys, `dashboardctl NAME deploy` selects a checked commit,
builds the new frontend around the published data blocks, preserves the
download bytes and verifies the data snapshot hash. Use `deploy --publish-data`
to rebuild from accrued results. A failed build leaves the current site in
place. Use `dashboardctl NAME rollback` to select the previous release.
The primary release retains its AIRO-specific instrument validation and
state migration. Check `storage` before provisioning additional releases.

## Give a site a hostname and password

Point the site's DNS and TLS-enabled ingress at its configured Unix socket.
If ingress lives outside the production guest, forward that socket through
the installation's private relay and use the relay as the upstream. The
origin serves static files. Every public route to a password-protected
origin must go through the password gate.

[Example nginx virtual host](examples/dashboard.nginx.conf) protects every
URL, including downloads, assets and release metadata. Copy it into the
ingress installation's private configuration, set the hostname, upstream and
certificate paths, and create the password file interactively. Reuse an
existing compatible password file when migrating. Create a new file with
`htpasswd -cB /etc/nginx/dashboard-passwords/rsi reader`; omit `-c` when
updating it. Restrict it to the ingress account. Run `nginx -t` before
reloading. For an intentionally public dashboard, omit the two `auth_basic`
directives and the private cache policy. [nginx HTTP Basic authentication](https://nginx.org/en/docs/http/ngx_http_auth_basic_module.html)

Verify the **public hostname** after cutover. On a protected site, requests
without credentials and with an incorrect password must not receive the protected
content. Basic authentication returns 401; a form gate can redirect readers to
a sign-in page and reject invalid sign-ins there. An
authenticated request should return the new release and the intended page.
For EEAI, repeat the access test for `/eeai-data.zip`. The authenticated
command below prompts for the password.

```sh
curl -sS -o /dev/null -w '%{http_code}\n' https://rsi.example.org/
curl --fail --user reader https://rsi.example.org/release.json
```

## Schedule and recover runs

Run `dashboardctl status` on forecast-prod to see AIRO, EEAI, RSI and the
ForecastBench rounds registered under AIRO. Add `--json` for structured
results. The command reports selected releases, scheduled job receipts and
data freshness separately from public delivery. Its default probes compare
dashboard identity, frontend commit and data snapshot hash in served release
metadata. The local socket and public HTTPS route have separate results.

Use `dashboardctl status --verify-content` when you need to compare the
public homepage and configured downloads with the selected publication.
Content checks read and hash those files. For protected HTTPS routes,
configure a private read-only `public_verifier` as described below. A missing
configuration, failed authentication or unreachable route produces an
unknown public result.

Use the **one host timer**, `fri-dashboard-tick.timer`. It invokes
`dashboardctl tick-all` every UTC minute. Each installed dashboard reads
its own entries from the same repository file, `packages/ops/jobs.json`,
at the commit it has deployed. Existing entries with no `dashboard` field
belong to the primary AIRO installation, including ForecastBench rounds.
The runtime refreshes panel state, runs the selected instrument on the server,
validates the publication and switches that site's pointer.

RSI runs both Baseline research-access conditions every Friday at 02:00 UTC, ahead of AIRO’s 12:00 UTC decision. Its reviewed schedule is:

```json
{"id":"rsi-weekly","dashboard":"rsi","kind":"forecast","weekly_utc":"Fri 02:00"}
```

Use `daily_utc` for `HH:MM`, `weekly_utc` for `Mon` through `Sun` and
`HH:MM`, or `at` for an exact UTC timestamp. Changes take effect when the
owning dashboard deploys. Stagger jobs that share the forecast lock.
To enable scheduling for a site, create its private `jobs-enabled` file in
the runtime root. For migration, verify the new timer includes AIRO and all
previously scheduled jobs, then replace the old tick timer. Keep the serving
services running. Install commands after staging the configs and units:

```sh
systemctl enable --now fri-dashboard-tick.timer
dashboardctl rsi status
```

For an explicitly authorized paid run, use
`dashboardctl rsi run-dashboard --run-id rsi-YYYYMMDD-first`. RSI's production job runs both research-access conditions (eight calls for a four-model panel). Manual and scheduled jobs use the same execution path. A receipt is written before
forecasting. Reusing its ID never spends again. Missed or interrupted jobs
are recorded and alerted without catch-up. For surveys, partial attempts
remain in `state/attempts/<receipt-id>/`; a successful run moves into
`state/results/` before the site is built. If publication alone failed, fix
the cause and deploy with `--publish-data` to publish the saved results.

### Remote publication and alerts

For a separate web host, set `public_publisher` in the private runtime JSON to an
argument list such as `["/path/to/publish-site"]`. The runtime appends the
absolute directory of the verified site. The publisher must return success only
after the web host has installed that snapshot. Use atomic activation on the
web host and keep its existing password gate. A socket-backed site omits this
setting. Manual deployments, rollback, and scheduled publications use the same
publisher. After a transfer failure, `dashboardctl NAME republish` verifies and
resends the selected site without any forecast calls.

AIRO, RSI and EEAI use `fri_forecaster.alert` and the production forecasting
home’s private alert channels. A failed forecast job records the stage and
pages the shared channel. A failed build or public transfer after forecasting
records `publish FAILED` and pages that same channel. Completed survey results
remain available for recovery. Keep channel routing configured to accept all
three deployment names.

### EEAI operator pilot

Run these on forecast-prod. The installed `forecast-prod eeai` launcher can
stand in for `dashboardctl eeai`.

```sh
dashboardctl eeai doctor
dashboardctl eeai status
dashboardctl eeai refresh --verify --run-id manual-20261119
```

`doctor` returns JSON and exits nonzero when a check fails or is unknown.
It checks the loaded timer, its effective `dashboardctl tick-all` service,
the schedule marker, and whether the scheduler reads the same runtime config.
It compares the effective HTTPS proxy and bypass settings without printing
their values. Its keyless HTTPS request to the official FRED observations
endpoint uses the scheduler's route. An identifiable FRED missing-key response
establishes connectivity. The check makes no request with FRED credentials.

The route comes from a readable service process or observable systemd
manager and unit properties, including simple literal `EnvironmentFile`
assignments. An unreadable file, unsupported expansion or syntax, unavailable
inherited or PAM state, or isolated network namespace can make the route
unknown. Run the probe from the service account when certificate or file
permissions differ. The run-file check detects an unseeded installation.
The refresh builder checks those files for completeness.

The doctor compares public `release.json` with the selected frontend commit
and snapshot. It reports sign-in redirects, access denials, stale metadata,
and unverifiable delivery separately. Selected fields from the offline
CONNECT-denial fixture follow.

```json
{
  "project": "eeai",
  "https_proxy_settings": "matched",
  "fred": {"destination": "api.stlouisfed.org", "route": "proxy", "result": "proxy_connect_denied"},
  "public_delivery": {
    "url": "https://dashboard.invalid/eeai/",
    "source": "public_verifier",
    "result": "verified",
    "checks": {"release.json": "verified"}
  }
}
```

`status` reads `published_baseline` from the selected site. Its date can be
null for the survey seed or a legacy page. `latest_verified_refresh` records
the time and snapshot of a public verification. Older success receipts
without verification do not qualify. `latest_refresh_failure` includes failed,
missed and interrupted refreshes. `economic_schedule` reports the next UTC slot
when the marker enables scheduling and the active minute timer dispatches the
expected service. Otherwise it reports disabled, unscheduled or unknown.

Manual and scheduled refreshes use their invocation environments and the same
job path. The runtime refreshes FRED observations and resolutions, builds
from completed forecast runs, validates the baseline and ZIP, and uses the
external `public_publisher` to transfer the candidate. It verifies served HTML
and ZIP bytes plus the release commit and snapshot before selecting the
candidate locally. The pilot requires the external publisher and uses no
forecasting-model calls.

Same-ID invocations return the existing receipt. After an upstream or build
failure, fix the cause and run with a new ID. Failed transfer or verification
leaves the prior local selection in place and attempts to restore it publicly.
The receipt records whether restoration was verified. If restoration fails or
the process stops during remote transfer, use `dashboardctl eeai republish`
to send the selected site and `dashboardctl eeai doctor` to check its public
metadata. Retry a failed transfer or verification without fetching FRED
again while the same release and prior publication are selected and the saved
candidate remains available.

```sh
dashboardctl eeai refresh --verify --retry manual-20261119 --run-id retry-20261119
```

**Private configuration.** Set the installation's HTTPS `public_url` and use
its existing `public_publisher`. Set `scheduler_service` and `scheduler_timer`
if the unit names differ from `fri-dashboard-tick.service` and
`fri-dashboard-tick.timer`. The default verifier uses anonymous HTTPS.
For a protected site, set `public_verifier` to a private argv list. This
read-only command receives a JSON manifest on stdin with `url`,
`frontend_commit`, `data_snapshot_sha256`, and `files`. For a refresh, `files`
maps `index.html` and `eeai-data.zip` to `sha256` and byte `size`. The doctor
sends an empty `files` map.

The verifier fetches `release.json`, compares its frontend commit and snapshot,
and checks exact bytes for each requested file. Use the base URL for
`index.html`. It exits zero and returns per-file results as a JSON object
with values drawn from `verified`, `stale`, `login_redirect`, `access_denied`,
`unverifiable`, `timeout`, `dns`, `tls`, and `proxy_connect_denied`.
Missing results, command errors or timeouts fail verification.

The existing authenticated route on the web host can supply those reads. For
example, use read-only SSH to run `eeai_operator.py public` there. The helper's
`--headers-file` accepts a private JSON file with `Authorization` or `Cookie`
headers, and `--expect-url` binds them to the configured HTTPS URL. The helper
leaves redirect handling, credential renewal, and access-policy checks to the
installation. The existing publisher hook can still verify atomic remote
activation and check anonymous access policy independently.

## Add a fourth dashboard

Package its runner and static builder as project entry points. The runner
accepts an output directory and produces one completed run file or folder.
The builder accepts the completed results directory and an empty site folder,
validates the data and writes `index.html` plus its downloads. Use the dashboard
kit's named data blocks to separate forecast data from the frontend.

Add one entry in `packages/ops/dashboard_projects.py` specifying the package,
web manifest, build and run arguments, public data block, downloads and
asset directories. Declare external downloads in `downloads` for hashing and
in `required` when every valid publication must contain them. Keep private
inputs, raw attempts, passwords and receipts outside the site. Follow the
same config, hosting and schedule steps above.

### Scheduled resolution

To opt in, set the dashboard's `refresh` command in `dashboard_projects.py`
and add a `refresh` job for it in `jobs.json`. The default cadence for that
job is daily at 15:15 UTC. Set `daily_utc`, `weekly_utc` or `at` to override
it. The command reads source evidence and saves outcomes. The scheduler then
rebuilds and verifies the page using the dashboard's existing forecast runs.
EEAI's command also refreshes its historical baseline.

For EEAI, run `dashboardctl eeai deploy --commit SHA --wait` from the production
runtime. It pins the merged commit and waits up to 30 minutes for its required
checks before acquiring the deployment lock. The normal deployment uses the
existing data snapshot. It compares the public release metadata, homepage and
download with the candidate and prints the verified URL, frontend commit and
snapshot SHA-256. A failed public check restores the previous publication.
