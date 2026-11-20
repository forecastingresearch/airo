# Provision and recover forecasting hosts

The pyinfra roles target dedicated Ubuntu 24.04 hosts with systemd, SSH,
Python 3.12 and the distribution packages listed in the deployment.
`forecast-prod` owns forecasting state and job execution. `forecast-web`
serves only static publications and accepts a separate publishing key per site.
See [dashboard operations](../DASHBOARDS.md) for release and project commands.

## Prepare private inputs

Copy [the example inventory](inventory.example.py) outside the checkout and
fill in the installation's SSH destinations, projects, hostnames, paths and
policies. Use an admin account with sudo and verify the hosts' SSH fingerprints
in the controller's known-hosts file before connecting. On a fresh forecast
host, set the `node` pin in the inventory and place that platform's Node
`.tar.xz` archive at the private host path. Verify the checksum through your
trusted software source. The role verifies the archive SHA-256 and version,
installs it below `/opt/fri-ops/node` and prepends it to each project's
`path`. When adopting an existing installation, supply its checked Node
toolchain on that path. The builder needs Python with venv, Git and Node 22.21
or newer with npm. The role installs the distro Python, Git and `gh`.
The rollout account needs repository read access and a `gh` session
that can see the merge and checks, or the installation's equivalent GitHub
check authentication. Use the forecasting service account to test the
toolchain and repository access.

Prepare the runtime's `home/` privately, including the forecasting keys,
alerts and ForecastBench uploader configuration. Use the existing runtime
conventions for production proxies and environment. The role accepts an
optional private systemd `environment_file` on forecast-prod. Keep coding
provider logins out of this home. Start with read-only or synthetic inputs
for a staging exercise.

On forecast-web, supply certificate chains and keys before applying the role
and keep DNS and renewal in the private installation. The nginx worker must
be able to read Basic password files, normally `root:www-data` mode 0640.
Keep TLS keys root-readable only. For each protected Basic site, supply the
publisher's reader JSON, `{"username":"reader","password":"…"}`, owned by
`forecast-publish` with mode 0600 and kept in a root-controlled directory.
The verifier checks HTTPS, HTML, metadata and listed downloads, then tests
anonymous and invalid credentials on those paths. Renew TLS outside pyinfra,
then run `nginx -t` and reload nginx. Check the protected HTTPS paths after
renewal.

For an existing form gate, set the site's `access` to `custom`, provide the
private server-context nginx `policy_include` that applies the existing
gate and supply the private executable `verifier`. The executable takes a
candidate directory and must verify its served metadata, page and protected
downloads through the existing login flow, including rejection of anonymous
and invalid sessions. Reuse the current cookie name and protected paths.
Test this on staged ingress before forwarding the existing hostname.
Use `access: public` only for a site intentionally published without a gate.

Store the site-specific Ed25519 **private** keys and a pinned forecast-web
known-hosts file under the forecast service account's private directory.
Set each inventory `public_publisher` alias in its SSH config with
`HostName`, `User forecast-publish`, `IdentitiesOnly yes`, `IdentityFile`,
`UserKnownHostsFile` and `StrictHostKeyChecking yes`. The server grants that
key just the configured receiver for its own site. Use separate reader access
for the operator CLI's private `public_verifier` on protected sites. For
Basic access, put the following argv in that project's inventory.

```json
[
  "/usr/bin/python3", "/opt/fri-ops/bootstrap/packages/ops/eeai_operator.py", "public",
  "--headers-file", "/var/lib/forecast/.config/fri-ops/eeai-headers.json",
  "--expect-url", "https://eeai.example.org/"
]
```

Provision a service-owned mode 0600 JSON `Authorization` header file. For a
form gate, use a private manifest verifier that authenticates through the
existing form. See the [operator pilot](../DASHBOARDS.md#eeai-operator-pilot)
for the read-only verifier protocol.

## Apply the roles

From the checked release source, install the pinned pyinfra into an isolated
controller environment. Apply the inventory to both hosts or use
`--limit forecast_prod` or `--limit forecast_web` for a single role.

```sh
python3 -m venv "$HOME/.venvs/fri-infra"
"$HOME/.venvs/fri-infra/bin/pip" install -r packages/ops/infra/requirements.txt
"$HOME/.venvs/fri-infra/bin/pyinfra" /private/inventory.py packages/ops/infra/deploy.py --sudo --yes
```

The role installs `/usr/local/bin/forecast-prod` and `dashboardctl`, a
system-Python bootstrap of checked code, per-project private runtime files
and origin sockets. Run the launcher as the configured `forecast` account.
Before deployment or scheduling, run the local toolchain preflight as that
account. It checks the configured Node, npm, Git, GitHub CLI and Python venv.

```sh
sudo -u forecast /opt/fri-ops/toolchain.py check /etc/fri-dashboards/eeai.json
```

Set `start_origins: True` to enable the local origin services, or start them
explicitly with `systemctl enable --now fri-dashboard@eeai.service` and the
other installed projects. Confirm the socket and `forecast-prod eeai status`
before deployment. For a first deploy with existing data, seed a verified
`bootstrap_publication` and follow the normal checked-release process.
After the deploy, use `forecast-prod eeai republish` and the public verifier
to prove unattended transfer without forecast calls.

The example provisions staging by default. The staging flag blocks both
worker services and the tick service and forbids the forecasting environment
file. The role leaves paid scheduling inactive. For a [scheduler migration](../SCHEDULER.md),
coordinate the application cutover and then explicitly enable the one
`fri-dashboard-tick.timer` and the `fri-dashboard-worker@paid.service` and
`fri-dashboard-worker@data.service` units. Stop and drain existing schedulers
before switching. Provisioning refuses to replace an enabled legacy timer
or an active legacy tick. Leave an existing AIRO ingress and schedule in place
until its cutover is authorized.

## Back up and restore

Assign each role a restic repository on durable storage outside the
forecasting and serving hosts. Put its URL in the private `repository_file`
and its encryption password in the private `password_file`. Supply any
backend credentials privately via the backup role's `environment_file`.
Initialize the repositories once with `restic --repository-file … --password-file
… init`. Keep independent access to the encryption and backend credentials
for recovery. Use separately managed retention or immutable storage as
appropriate. Once repositories exist and a manual backup works, set
`backup.enabled: True` on production. The generated restic timers run each
day. Monitor the last successful snapshot and failed backup unit.

```sh
sudo systemctl start fri-backup.service
sudo /opt/fri-ops/backup.py /etc/fri-ops/backup.json snapshots
sudo /opt/fri-ops/backup.py /etc/fri-ops/backup.json check
```

Include the configured backend environment for manual backup operations if
required. Production snapshots cover accrued results, attempts, the
forecasting homes including private operational state, logs, receipts,
releases, and the selected and preceding sites. Web snapshots cover each
site's publication directory and selected pointer. The backup holds runtime
operation or publication locks during capture. The scheduler can still
atomically register new receipts while the forecast files are stable. If an
exact ledger cutoff matters, stop the tick before capture. Role inventories,
web TLS/access files and backup access must also be recoverable from the
installation's private configuration and secret store.

1. Provision clean isolated hosts in staging mode. Allow access to the backup
   backend and locally routed staging HTTPS only. Use private staging
   hostnames and keys. Keep scheduler units disabled and provider credentials
   absent.
2. Select an explicit snapshot ID for each role. Restore into a new quarantine
   under a protected parent. Staging restores omit runtime configuration,
   homes and scheduling flags. They retain the receipts and accrued results.

   ```sh
   sudo /opt/fri-ops/backup.py /etc/fri-ops/backup.json restore SNAPSHOT_ID /var/lib/fri-restore-1 --staging
   ```

3. Inspect the restored paths. Copy the runtime and publication trees to their
   original **absolute paths on the isolated hosts** so release symlinks and
   virtual environments resolve locally. For example, copy the contents of
   `/var/lib/fri-restore-1/snapshot/srv/forecasts/` into `/srv/forecasts/` on
   forecast-prod and use the corresponding `srv/forecast-sites/` tree on
   forecast-web. Preserve symlinks and reassign ownership to the provisioned
   service accounts when their numeric IDs differ. Confirm the restored
   runtime roots still have no `jobs-enabled` or `scheduler-v2.json`.
4. Start only the origins and nginx. Verify selected release metadata and
   download hashes over HTTPS, exercise the access gate, then check
   `forecast-prod NAME status` and a credential-free `republish` using the
   staging SSH keys. Check that private receipts and results are readable
   locally and cannot be fetched over HTTPS.
5. For an authorized production recovery, use `--recovery` to restore full
   private runtime state into quarantine. Reapply the checked role with
   recovered private inputs. Review schedule markers, claimed jobs and
   upstream upload receipts before copying operational state or enabling
   scheduling. Compare forecast-prod and forecast-web publication metadata
   before reopening ingress. If the copied runtime needs its dependencies
   rebuilt, deploy a checked release with `--keep-publication` from the
   validated restored site. Reenable scheduling only after the recovery
   checkpoint and receipt state have been reviewed.

For the grants, production checkpoint and acceptance tests for a complete replacement, see [Evaluate a cold rebuild](REPLICA-EVALUATION.md).

## Rehearse locally

Run the drill with nginx, restic, OpenSSL, ssh-keygen and the pinned pyinfra available.

```sh
python3 packages/ops/infra/staging_drill.py
```

The drill generates temporary TLS and password fixtures, runs real nginx
and restic, and exercises the repository sender and forced receiver through
a local pipe. It uses fresh path trees for restoration and verifies that
the same protected publication can be served again. Validate actual systemd
activation, SSH policy, and off-host backup credentials on disposable hosts
before a production rollout.

## Exercise the installed scheduler

On a disposable, booted forecast guest provisioned in staging mode, run
the scheduler acceptance drill as root. Allow about three minutes and
at least 2 GiB of free space on the fixture filesystem.

```sh
sudo python3 packages/ops/infra/scheduler_drill.py --disposable-staging
```

The drill clones the installed timer and service units into temporary systemd
units, uses the installed launcher and release runtime, and verifies the
staging condition blocks the copied services before activating their isolated
fixtures. The real staging marker stays in place. Test processes use a private
network namespace with IP sockets disabled and use only synthetic UTC schedules.

The minute timer dispatches a long synthetic AIRO forecast and an EEAI refresh.
The refresh must verify its locally copied publication while the forecast is
still running. A repeated tick and repeated run ID must preserve the receipts.
Stopping and restarting the paid worker must close the interrupted attempt and
an expired queued slot without a second paid-runner invocation. The drill uses
synthetic replacements at the provider, upstream data, alerts, build and
publication transport boundaries. Host reboot, live providers, public HTTPS
and the remote publication host are outside this drill.
