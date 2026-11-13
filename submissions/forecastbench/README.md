# ForecastBench submission setup

The round scripts generate submission files. Upload is currently manual; creating
an uploader identity does not enable automatic uploads or change the schedule.
See the [round workflow](ROUND.md) and
[ForecastBench's submission instructions](https://github.com/forecastingresearch/forecastbench/wiki/How-to-submit-to-ForecastBench).

## Google Cloud project and uploader identity

Use a dedicated service account for unattended uploads. A personal `gcloud`
login can require interactive reauthentication, even when it has a refresh token.
The examples below use placeholders: keep actual installation details, team
identifiers, credentials and upload receipts in private configuration outside git.

Authenticate as a project administrator for setup, then select a globally unique
project ID. An existing project is also suitable; skip its creation in that case.
Creating service accounts requires `iam.serviceAccounts.create` on the project.

```sh
gcloud auth login
FB_PROJECT_ID='your-globally-unique-project-id'
gcloud projects create "$FB_PROJECT_ID" --name='Automated forecasting'
gcloud services enable iam.googleapis.com --project="$FB_PROJECT_ID"
gcloud iam service-accounts create forecastbench-uploader \
  --project="$FB_PROJECT_ID" \
  --display-name='ForecastBench uploader'
gcloud iam service-accounts describe \
  "forecastbench-uploader@${FB_PROJECT_ID}.iam.gserviceaccount.com" \
  --project="$FB_PROJECT_ID" --format='value(email)'
```

Send that service-account email to the ForecastBench organizers and ask them to
grant access to your team's submission prefix, including future round dates.
Request object creation and, if supported, object metadata reads so uploads can
be verified. Overwrite and delete access are unnecessary for creating new files.
The account needs no project-wide role to write to a separately authorized
ForecastBench bucket.

## Production authentication

Configure authentication on the actual submission runner, using its runtime home
and environment. A successful login in a development shell does not configure
the production runner. Prefer an attached service account or Workload Identity
Federation where the host supports it.

If the host requires a service-account JSON key, the setup administrator needs
`iam.serviceAccountKeys.create` and the project's organization policies must
permit key creation. Create the key directly into a private directory, then
securely provision it to the runner; never commit or paste its contents into logs.

```sh
FB_KEY_DIR="$HOME/.config/forecastbench-uploader"
mkdir -p "$FB_KEY_DIR"
chmod 700 "$FB_KEY_DIR"
(umask 077
 gcloud iam service-accounts keys create "$FB_KEY_DIR/key.json" \
   --iam-account="forecastbench-uploader@${FB_PROJECT_ID}.iam.gserviceaccount.com" \
   --project="$FB_PROJECT_ID")
```

In the runner's private environment, use a separate Cloud SDK configuration and
activate the service account with its provisioned key:

```sh
export CLOUDSDK_CONFIG="$HOME/.config/forecastbench-uploader/gcloud"
export CLOUDSDK_CORE_DISABLE_PROMPTS=1
gcloud auth activate-service-account \
  --key-file="$HOME/.config/forecastbench-uploader/key.json"
```

Keep this configuration separate from the forecasting models' agent directory.
Replace keys when needed and revoke retired keys after verifying their replacement.
Network-restricted setup environments need access to
`cloudresourcemanager.googleapis.com`, `serviceusage.googleapis.com`,
`iam.googleapis.com` and the authentication endpoints; uploads also need the
Cloud Storage API endpoints.

## Verify and upload

After the organizers grant access, verify an actual upload from the runner using
a test filename and location agreed with them. `gcloud auth list` alone does not
prove that credentials can refresh or that the account can write to the bucket.
Do not place a test forecast into a live round without their agreement.

For a real submission, configure the destination from the round invitation and
upload each validated forecast set explicitly:

```sh
FB_ROUND_DESTINATION='gs://your-submission-bucket/your-team-prefix/binary/YYYY-MM-DD'
FB_SUBMISSION_FILE='/private/round/YYYY-MM-DD.team.1.json'
gcloud storage cp --if-generation-match=0 \
  "$FB_SUBMISSION_FILE" "$FB_ROUND_DESTINATION/"
```

The generation precondition refuses to overwrite an existing object. Upload only
the validated submission JSON files, excluding question sets, provenance files
and logs. Respect the current submission limits and the round's UTC deadline.
Confirm the remote object's metadata/checksum when read permission is available.
Keep receipts private. If upload fails, retain the built forecasts and retry the
upload without rerunning paid forecasting work.

An automatic uploader should report failure through the runner's existing alert
channel and mark the submission successful only after upload confirmation.

References: [service-account creation](https://docs.cloud.google.com/iam/docs/service-accounts-create),
[service-account keys](https://docs.cloud.google.com/iam/docs/keys-create-delete),
[CLI authentication](https://docs.cloud.google.com/sdk/docs/authenticate),
[Cloud Storage uploads](https://docs.cloud.google.com/sdk/gcloud/reference/storage/cp).
