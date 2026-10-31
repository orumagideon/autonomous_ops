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

Run just after midnight, Monday through Friday. The target day is the current
Nairobi trading day (equivalent to advancing the previous calendar day to the
next weekday); weekends roll forward to Monday.

```cron
1 0 * * 1-5 /home/<user>/autonomous_ops/venv/bin/python /home/<user>/autonomous_ops/main.py >> /home/<user>/autonomous_ops/cron.log 2>&1
```

Replace `<user>` with the Linux account that owns the project. Set the cron
environment's timezone to `Africa/Nairobi` as well, or keep the application
timezone configured in `.env`.

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
