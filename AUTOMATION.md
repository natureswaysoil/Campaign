# Automatic Campaign Optimization

## 🎯 What It Does

Automatically optimizes Amazon PPC campaigns by:
- **Pausing** campaigns losing money (ACOS > 200%)
- **Scaling** top performers (ROAS > 3.0) by 3x budget
- **Reducing** marginal campaigns (ACOS 100-200%)
- **Testing** zero-impression campaigns at $5/day

## 🚀 Quick Start

### One-Time Setup (Already Done!)
```bash
# GCP credentials already configured
# Amazon Ads API credentials in GCP Secret Manager
```

### Weekly Workflow

**Step 1: Fetch Campaign IDs** (once per week)
```bash
python fetch_campaign_ids.py
# Takes ~10 minutes, creates campaign_ids.json
```

**Step 2: Download Campaign Report**
1. Go to Amazon Ads: https://advertising.amazon.com/cm/campaigns
2. Click "Download report" → Select last 7 days
3. Save as: `Campaign_YYYY-MM-DD.csv`

**Step 3: Run Optimizer**
```bash
# Dry run first (see what would change)
python optimize_campaigns.py Campaign_YYYY-MM-DD.csv

# Apply Priority 1 only (most urgent)
python apply_optimizations.py Campaign_YYYY-MM-DD.csv --apply --priority 1

# Apply all priorities
python apply_optimizations.py Campaign_YYYY-MM-DD.csv --apply
```

## 📊 Performance Tiers

| Tier | Criteria | Action | Budget Change |
|------|----------|--------|---------------|
| Top Performer | ROAS ≥ 3.0 | Scale Up | 3x |
| Profitable | ROAS 1.0-3.0 | Scale Up | 2x |
| Marginal | ROAS 0.5-1.0 | Scale Down | 0.7x |
| Unprofitable | ACOS > 200% | Pause | $0 |
| Zero Performance | No sales | Test | $5/day |

## 🎯 Priority Levels

1. **Priority 1**: Critical - Stop losses, scale obvious winners
2. **Priority 2**: Important - Scale profitable campaigns
3. **Priority 3**: Optimize - Reduce marginal spenders
4. **Priority 4**: Test - Fix zero-impression campaigns
5. **Priority 5**: Monitor - Low-impression campaigns

## 💰 Expected Impact

Based on March 23, 2026 analysis:
- **Current spend**: $341/day across 17 campaigns
- **Optimized spend**: $272/day
- **Savings**: $69/day = $2,070/month
- **Revenue gain**: $2,000-4,000/month (from scaled winners)

## 🔧 Files

- `campaign_optimizer.py` - Core optimization engine
- `optimize_campaigns.py` - Analysis tool (CSV → recommendations)
- `apply_optimizations.py` - Automatic application via API
- `fetch_campaign_ids.py` - Fetch campaign IDs from Amazon Ads
- `optimizer_config.json` - Configurable thresholds

## ⚙️ Configuration

Edit `optimizer_config.json` to adjust:
- Performance tier thresholds
- Budget scaling factors
- Test budget amount

## 📈 Monitoring

After applying changes:
1. Wait 3-7 days for data
2. Download fresh report
3. Run optimizer again to see new recommendations
4. Track ROAS improvements on scaled campaigns

## 🔒 Safety Features

- Dry run mode by default (requires `--apply` flag)
- Priority filtering (can apply only Priority 1)
- Confirmation prompt before applying changes
- All changes logged to console

## 🆘 Troubleshooting

**"Campaign ID not found"**
→ Run `python fetch_campaign_ids.py` to refresh IDs

**"Amazon Ads API error 401"**
→ Credentials expired, check GCP Secret Manager

**"No campaigns found in CSV"**
→ Check CSV format matches Amazon Ads report export
