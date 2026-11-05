# Private GitHub Actions runner

Copy this directory into a separate **private** repository. It contains the workflow and an example configuration for running the public Visa Pickup Monitor with your private settings and state.

Keep this directory in the public repository as the maintained template, with synthetic example data. The active workflow lives in the private runner repository. When updating an existing runner, copy the changed template files into it and commit them there; preserve its private `config.env` and `data/state.json`. Editing this public template alone does not update an existing runner.

The workflow runs Monday through Thursday at 12:17, 13:17, 14:17, 15:17, and 17:17 in `Europe/Belgrade`, including daylight saving changes. It also supports manual runs. Daily reports and Healthchecks.io are not included in this iteration.

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
4. Check `MONITOR_REF` in `.github/workflows/monitor.yml`. It must be the full SHA of a tested commit available in the public `KirMakarov/visa-pickup-monitor` repository. Publish that commit before running the workflow. The supplied SHA identifies the completed local state-and-alerts implementation.
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

GitHub may delay or drop scheduled runs. Independent detection of missing runs will be added with Healthchecks.io in a later iteration. See [GitHub schedule limitations](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
