# Private GitHub Actions runner

Copy this directory into a separate **private** repository. It contains the workflow and an example configuration for running the public Visa Pickup Monitor with your private settings and state.

Keep this directory in the public repository as the maintained template, with synthetic example data. The active workflow lives in the private runner repository. When updating an existing runner, copy the changed template files into it and commit them there; preserve its private `config.env` and `data/state.json`. Editing this public template alone does not update an existing runner.

The workflow runs Monday through Thursday at 12:17, 13:17, 14:17, 15:17, and 17:17 in `Europe/Belgrade`, including daylight saving changes. The final scheduled occurrence requests a daily report, even if GitHub starts it late. Earlier scheduled and manual runs contribute to daily totals without requesting a report.

## Set up the private repository

1. Create an empty private repository named `KirMakarov/visa-pickup-monitor-runner` in your GitHub account, without adding a README, license, or Git ignore template.
2. From your local public monitor repository, clone the empty runner repository and copy the files:

   ```sh
   git clone git@github.com:KirMakarov/visa-pickup-monitor-runner.git ../visa-pickup-monitor-runner
   cp -R examples/github-actions/. ../visa-pickup-monitor-runner/
   cd ../visa-pickup-monitor-runner
   cp config.env.example config.env
   ```

3. Edit `config.env` with your own `NUMBERS` and `PAGE_URL`. In the private repository's **Settings > Secrets and variables > Actions**, create both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as **repository secrets** to enable Telegram. Omit both secrets for console-only operation. In hosted mode, both Telegram settings come from Secrets; environment variables supplied by the workflow override `.env` values.
4. Set `MONITOR_REF` in `.github/workflows/monitor.yml` to the full SHA of the tested reporting implementation after publishing it to the public `KirMakarov/visa-pickup-monitor` repository. The supplied SHA predates daily reporting and must be updated before enabling the report schedule.
5. Commit and push the runner files yourself:

   ```sh
   git add .github/workflows/monitor.yml .gitignore README.md config.env.example config.env
   git commit -m "Set up private visa pickup monitor"
   git branch -M main
   git push -u origin main
   ```

6. Open **Actions > Visa Pickup Monitor > Run workflow** and run it from the default branch. Verify the run succeeds and, if state changed, that the bot commits `data/state.json`. Run it again to verify the state is reused. If you want to exercise delivery of a Telegram match notification, use a current target of your own that appears in the PDF.

The workflow must be present on the default branch for scheduled runs and the manual-run button. It refuses to run in public repositories. See GitHub's [schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule).

If Telegram configuration fails, check both repository secret names and confirm the private workflow references both under the check step's `env`. The check logs show only whether each secret is configured, without printing values. After pushing a workflow fix, start a new **Run workflow**. This template does not select a GitHub environment, so environment-scoped secrets require additional job configuration; use repository secrets for this setup. See GitHub's [secrets documentation](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets).

## State and updates

All monitor execution happens from the private repository's root directory. The public source is checked out into ignored `monitor-code/`; `config.env` is copied to ignored `.env`, and the monitor reads and updates private `data/state.json`. The supplied `.gitignore` deliberately allows `data/state.json` to be committed.

Both scheduled and manual runs share one concurrency group. Each run checks out the current default branch after it starts, so it reads the latest committed state. State is committed after either a successful or failed monitor check, only when it changed. A failed check still fails the workflow. A failure to push state also fails the workflow. The bot stages only `data/state.json`; it does not commit configuration changes or public source code.

The built-in `GITHUB_TOKEN` receives `contents: write` permission for this repository, allowing state commits without a personal access token. Repository rules must allow the bot to write directly to the default branch. If another writer changes the branch during a run, a rejected push fails the run; automatic conflict resolution is not attempted. Avoid editing runner configuration while a workflow run is active.

By default, local and hosted monitoring start with separate history. To continue your existing local history, copy your local `data/state.json` into this private repository and commit it **before the first hosted run**. This prevents notifications already sent locally from being sent again. Keep local and hosted monitoring from running concurrently for the same targets if you want one notification history.

To update the monitor, test and publish the public code first. Then change `MONITOR_REF` to the selected full commit SHA in the private workflow, commit it yourself, and perform a manual run. Updating the public default branch alone does not update the running monitor.

### Copy template updates with the helper

From the public monitor checkout, run the helper with the path to your existing
private runner and the full SHA of the tested public version you have published:

```sh
python3 scripts/update_runner.py ../vpm-runner --monitor-ref YOUR_PUBLISHED_COMMIT_SHA --dry-run
python3 scripts/update_runner.py ../vpm-runner --monitor-ref YOUR_PUBLISHED_COMMIT_SHA
git -C ../vpm-runner diff
```

Replace `YOUR_PUBLISHED_COMMIT_SHA` with the actual 40-character SHA. The helper
sets `MONITOR_REF` to that value but does not verify that GitHub has the commit.
It also works from another directory when invoked by its full script path.
On Windows, use `python` instead of `python3`.

Only the workflow, `.gitignore`, `README.md`, and `config.env.example` are updated.
Private `config.env`, `.env`, state, and other files are preserved. Existing custom
changes in the four template files must be committed or moved before updating;
review the resulting diff and reapply any customizations you still need. The
helper requires the root of an existing checkout and refuses symlinked update
paths. It does not commit, push, or start a workflow.

Run the helper while the private workflow is idle. Review and commit the updates
yourself, push them, then perform a manual smoke run before scheduled acceptance.

GitHub may delay or drop scheduled runs. See [GitHub schedule limitations](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

## Optional independent Healthchecks monitoring

Create a production check at [Healthchecks.io](https://healthchecks.io/) using cron
`17 12-15,17 * * 1-4`, time zone `Europe/Belgrade`, and a 30-minute grace period.
Connect standard failure and recovery notifications to Telegram and a backup email
address; leave extra periodic reminders disabled. Add the check's base ping URL as
the private repository secret `HEALTHCHECKS_PING_URL`. Leave it unset to disable
the integration. Keep the URL private: it is a credential.

Only scheduled executions send a final signal, after monitoring, report delivery,
and state persistence have been attempted. Any failed required step signals failure,
including setup or state push. Completed monitoring still signals success. A failed
or rejected signal fails the workflow; an interrupted run may instead be detected as
missing after the grace period. Manual and local runs never signal Healthchecks.
The script's detailed error alerts and independent Healthchecks alerts can both
arrive for one failure. No personal monitoring data is sent with the signal.
The workflow checks for HTTP 200 and the exact `OK` response; the
[ping API](https://healthchecks.io/docs/http_api/) can otherwise return HTTP 200
for an unknown check or a rate-limited request.

Before rollout, publish the tested public revision and explicitly update the private
workflow and its `MONITOR_REF`, preserving the private configuration and state. For
acceptance testing, use a separate control check for failure and missed-run alerts;
do not interrupt the production check. When retiring monitoring, disable the
workflow first, then pause its independent Healthchecks check.
