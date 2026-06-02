"""Weekly Scheduler - Run this via cron or GitHub Actions."""

from datetime import datetime

from optimize_campaigns import run_optimizer


if __name__ == "__main__":
    print(f"📅 Scheduled run started: {datetime.now()}")
    result = run_optimizer(dry_run=True)
    print(f"✅ Scheduled optimization complete: {result.get('product_count', 0)} products processed")
