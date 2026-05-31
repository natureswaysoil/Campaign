"""Weekly Scheduler - Run this via cron or GitHub Actions"""

from optimize_campaigns import main as run_optimizer  # or just call the logic directly

if __name__ == "__main__":
    print(f"📅 Scheduled run started: {datetime.now()}")
    run_optimizer()   # or paste the full logic here
    print("✅ Scheduled optimization complete")
