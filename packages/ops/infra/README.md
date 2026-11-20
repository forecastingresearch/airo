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

## Run the two-host staging acceptance

A second operator can build a private staging pair from a checkout of this PR. Use an x86-64 Ubuntu controller with Python 3.12, Git, curl, OpenSSL, OpenSSH, tar, `debootstrap`, `e2fsprogs`, `python3-venv` `qemu-system-x86` and `libnss3`. The controller needs sudo to build two bootable disks, access to Ubuntu, Node, PyPI and npm, and space for two 10 GiB sparse disks, the build tree and the package caches. The guests use about 3 GiB RAM in total. KVM speeds up the run where available. Keep the destination outside the checkout, on a private volume with a short path.

From the repository root, pass a new directory under an existing parent. The command builds the images, generates the guest and publisher identities, verifies the pinned Node archive, builds the publications and runs the acceptance checks. The guest network connects the two roles to one another and forwards administrative SSH to controller loopback. The recipe generates credentials for this pair.

```sh
packages/ops/infra/staging_pair.sh rehearse /var/tmp/my-forecast-stage
```

A successful run leaves both guests online. To verify the three origins, the reader-side HTTPS responses and protected EEAI content later, or to power-cycle the pair, use the same directory.

```sh
packages/ops/infra/staging_pair.sh verify /var/tmp/my-forecast-stage
packages/ops/infra/staging_pair.sh stop /var/tmp/my-forecast-stage
packages/ops/infra/staging_pair.sh start /var/tmp/my-forecast-stage
```

For an interactive session use `ssh -F /var/tmp/my-forecast-stage/ssh-config stage-prod` or `stage-web`. The installation record is private. It includes the disk images, SSH key and generated connections for this pair. Run `sudo -u forecast forecast-prod status --verify-content --json` on the forecast guest. The controller's last verified status is written to `status.json` in the private directory.

After the power cycle, a Chromium reader opens the AIRO methodology dialog, authenticates to EEAI and changes a forecast control, and opens an RSI survey question. The browser loads the versions of the frontend libraries referenced by the dashboard from local npm packages. It uses fallback fonts and suppresses analytics during the test.

The input publications exercise the actual dashboard renderers and download formats. AIRO includes a fictional forecast export, EEAI uses the reviewed survey/history preview, and RSI renders its instrument with an empty results tree. The drill uses a local Git repository and fixture GitHub check responses. The two-host checks cover restricted publisher SSH, real origin services, ingress TLS, EEAI Basic authentication, deliberately failed updates, restore and restart. Restic repositories and the test TLS trust anchor belong to the disposable pair. For comparison with live production state, hosted delivery or external integrations, use the [replica evaluation](REPLICA-EVALUATION.md).

## Run the single-machine smoke test

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

## Build and deploy the real dashboards in staging

Use Python with venv, Git, and the release builder's Node/npm toolchain.
Choose a disk-backed temporary directory with room for the release checkouts
and dependencies. The output paths below must be new directories.

```sh
python3 packages/ops/infra/build_staging_publications.py /private/staging-initial
python3 packages/ops/infra/staging_release_drill.py /private/staging-initial \
  --candidate-output /private/staging-updated
```

The first command builds browsable output for each dashboard from the selected
Git source. It preserves AIRO's checked assembled page and generates its
download files through the real exporter with a fictional run. EEAI builds from
the checked instrument, historical series and public cohort aggregates.
RSI displays its checked instrument with an empty forecast history.

The second command creates a disposable Git repository from that checked tree
and uses a local fixture for GitHub's required-check responses. The real
`dashboardctl` launcher and release builder install dependencies into fresh
releases and serve them over temporary Unix sockets. The drill verifies the
cold import, rejects a candidate with an omitted required check, deploys its
frontend around the saved data and downloads, and rolls back. It compares
served file bytes and leaves scheduling off. The optional candidate output can
be used in a separate transfer and HTTPS rehearsal.

After restoring the publications on a disposable provisioned pair, run the
same checked-release path on the forecast guest using its existing SSH
publisher and HTTPS verifier.

```sh
python3 packages/ops/infra/staging_guest_release.py \
  --ssh-config /private/staging-ssh-config \
  --prepare-cache /private/staging-release-dependencies
```

The controller needs package-registry access and the same Python version and
architecture as the guest for wheel selection. The first invocation builds a
private cache of public dependencies. The same preparation command resumes a
partially downloaded cache after a registry interruption. Reuse it with `--python-wheelhouse` and
`--npm-cache` pointed at its `python-wheelhouse/` and `npm-cache/`
subdirectories. The guest requires the staging marker, disabled scheduling
and restored baseline publications. The drill runs `forecast-prod` as the
forecast service account, verifies the public endpoint before and after the
checked offline deploy, rolls back, and verifies delivery again.

Pass `--keep-verified-release` to reselect the tested candidate after the rollback
checks. The guest then retains real installed release dependencies and a working
previous publication for operator practice. Repeat the checked-deployment exercise
through this controller entry point, which recreates its temporary source/check
fixtures and restores the guest's original repository configuration afterward.

## Use an existing pair of empty guests

Use two empty, isolated Ubuntu 24.04 guests with real systemd, an SSH route
from the controller, and an SSH and HTTPS route from the forecast guest
to the serving guest. Prepare the Node archive with its trusted SHA-256 on
the forecast guest. Install the base apt dependencies beforehand if the
network is offline. The controller needs pinned pyinfra, Python, OpenSSL,
ssh-keygen and a private SSH config with root identities and pinned host
keys for `stage-prod` and `stage-web`.

Generate a random hexadecimal nonce (32 to 128 characters) on the
controller. Provision a root-owned mode 0600 `/etc/fri-acceptance-host`
regular file on each guest with that nonce on its first line and the guest's
role on the second (`forecast-prod` or `forecast-web`). Pass the nonce file
and the web guest's private IPv4 address to the rehearsal. Use two
credential-free publication fixture directories for all three sites, from
different frontend revisions with matching data snapshots.

```sh
TMPDIR=/var/tmp python3 packages/ops/infra/accept_staging.py \
  --ssh-config /private/staging/ssh-config --nonce-file /private/staging/nonce \
  --web-address 192.0.2.2 --pyinfra /private/venv/bin/pyinfra \
  --node-archive /var/tmp/node-v22.21.1-linux-x64.tar.xz --node-sha256 "$NODE_SHA256" \
  /private/staging-initial /private/staging-updated
```

The harness checks the marks, distinct guests, web address and empty paths
before provisioning. It generates a private 90-day TLS certificate and fixture
credentials. It compares every served fixture file to its original
hash, tests the EEAI gate and site-restricted SSH keys, injects a publication
failure, and confirms rollback on all three dashboards. It backs up both
roles to separate encrypted fixture repositories, restores their trees
with paid scheduling and credentials excluded, reapplies the roles, and
restarts the actual services. Archive any output privately. The guests
remain staged for inspection. Reset them to empty images to repeat the
full rehearsal.
