# TradeOps Automated Status Scheduler

This service signs in to TradeOps, visits the configured Trader Portal, and
updates the active hedge-result day in each client's Evaluations table. It
defaults to `Africa/Nairobi`; credentials and the timezone are read from `.env`.

## Setup

```bash
cp .env.example .env
# Edit .env and set TRADEOPS_EMAIL and TRADEOPS_PASSWORD.
chmod +x setup.sh
./setup.sh
```

The script uses Playwright's Chromium browser in headless mode by default.
Set `HEADLESS=false` in `.env` to run it visibly while troubleshooting. The
portal's layout and selectors may change; verify the first run against your
TradeOps account before scheduling it.

## Schedule

Run at midnight, Monday through Saturday, in East Africa Time. On Saturday,
the previous trading day is Friday and the target day is Monday. Sunday and
Monday also resolve to Friday and Monday respectively.

```cron
0 0 * * 1-6 /root/autonomous_ops/run.sh
```

Install this entry in the VPS's root crontab, and ensure the existing
`/root/autonomous_ops/run.sh` wrapper runs the updater. The wrapper is managed
on the server and is not included in this repository. Cron must schedule in
`Africa/Nairobi` (EAT); setting `TIMEZONE` in `.env` controls the application's
day calculation but does not change the cron daemon's scheduling timezone.

## Behavior and logs

- The updater only changes the latest numbered `HEDGE RESULT` column for rows
  with an active status (in progress, pending, Hit TP, or Hit SL).
- It skips rows already containing the target day and leaves other values
  untouched.
- Changes are submitted with Enter/blur and an available Save button.
- Execution details are logged to `execution.log`; failures capture a
  full-page screenshot under `errors/`. Both locations are excluded from Git.
- Keep `.env` private. Never commit credentials or screenshots containing
  account information.
