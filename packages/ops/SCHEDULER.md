# Queued scheduling

The checked release supports legacy scheduling until each installation is
explicitly switched with `dashboardctl NAME scheduler-cutover`. The queued
installation uses one minute timer and two worker services. The timer runs
`dashboardctl tick-all --queued`, which records the owning release and UTC
slot for each due occurrence and exits. It also records recent missing slots
for workers to reconcile. An enabled legacy installation causes the queued
tick to report an error while still recording work for queued installations.

The paid lane handles dashboard forecasts and ForecastBench. It keeps paid
work and the shared AIRO panel serialized for installations using the same
`panel_runtime_config`. The data lane handles EEAI economic refresh and the
AIRO news poll. A project keeps its own operation lock across job execution,
deployment, rollback, publication and pruning. An EEAI data refresh can
therefore finish while a paid AIRO or ForecastBench job is still running.
AIRO news and AIRO forecasts share the AIRO project lock. Each lane polls for
receipt changes every two seconds and can service other projects while one
project in that lane is busy.

## Switch the host

1. Deploy a checked release with queued scheduling to each enabled dashboard.
   Install the queued timer and the paid and data worker service definitions
   without starting them. If replacing the existing timer under the same name,
   stage the new definitions until the legacy timer has drained.
2. Stop the old scheduling timer and disable any legacy cron dispatch.
   Let the running tick service and authorized manual operations finish.
   Inspect the systemd service and run receipts before switching. Stopping
   a service while a job runs can interrupt the job. Leave serving services up.
3. From the forecasting service account, run
   `dashboardctl NAME scheduler-cutover` for every enabled dashboard. The
   command checks that it can acquire the old and new operation locks, closes
   interrupted legacy receipts and reconciles earlier slots. It writes the
   private `scheduler-v2.json` marker and admits new starts from the following
   UTC minute. The existing `jobs-enabled` marker keeps its meaning.
4. Reload the unit definitions and enable the worker instances and the one
   queued timer.

```sh
systemctl daemon-reload
systemctl enable --now fri-dashboard-worker@paid.service fri-dashboard-worker@data.service
systemctl enable --now fri-dashboard-tick.timer
systemctl status fri-dashboard-tick.timer fri-dashboard-worker@paid.service fri-dashboard-worker@data.service
```

Check the loaded minute service uses `dashboardctl tick-all --queued` and
both workers use the same private config directory and service account.
Use the existing smoke command to test the runner without provider calls.
Observe receipts at an already scheduled minute when validating the live
dispatch. A staging restore can exercise the same sequence with isolated
example configurations and dummy jobs.

## Pause or return to legacy

Remove `jobs-enabled` from a project's private runtime root to stop new
scheduled starts. A worker checks the marker before claiming an occurrence
and again before its work begins. Survey forecasts check again after panel
preparation, before starting the paid runner. Already launched work can
finish. Explicit manual runs continue to use their own run IDs and operation
locks. To resume, create a fresh marker. Its identity and modification time
give the new scheduling interval a separate boundary, so a queued occurrence
from before the pause cannot spend after resuming, including within the same
UTC minute. Missed queued jobs retain their receipts.

A normal rollback can select another release that supports queued scheduling.
To select an older release, first stop the queued timer, pause new starts
and let running jobs finish. Once the services and receipts show the work has
drained, stop the queued workers and run
`dashboardctl NAME scheduler-cutover --legacy` for each project. The command
closes any queued occurrences and retains all completed receipts before
removing the marker. Restore the legacy unit configuration and, if paused,
create fresh `jobs-enabled` markers for the sites that should run. Start only
the legacy timer. Use the normal rollback command if selecting an older
release. Queue-mode deploy and rollback reject releases that lack queued
scheduling support.

## Receipts and recovery

The occurrence key and its `runs/KEY.json` receipt are shared with the legacy
scheduler. The short dispatch lock and atomic, fsynced JSON writes make each
key the durable scheduling record. The worker records `running` before
launching work. Using that same receipt for the queue, claim and final result
avoids replay between two stores. Existing receipts, including manual runs,
continue to prevent reuse of their run IDs.

The project operation lock is inherited by active job subprocesses. A worker
can close an abandoned `running` receipt as `interrupted` only after it can
acquire the project lock. Queued releases are retained until reconciliation.
An occurrence queued for a superseded release is closed without starting its
job or publishing its data. An unclaimed slot closes as `missed` after its UTC
minute. AIRO's cadence check can record an off-day as `skipped`. Failed and
interrupted jobs keep their logs and receipts for the existing operator
recovery commands. Paid work is never caught up automatically, and repeating
a failed or interrupted ID cannot spend again.
