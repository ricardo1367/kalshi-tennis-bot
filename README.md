# Kalshi Tennis Bot

Automated tennis betting bot for [Kalshi](https://kalshi.com) that identifies edges by comparing Kalshi market prices against sharp Pinnacle odds, then sizes bets using the Half Kelly Criterion.

---

## How It Works

1. Every 30 minutes, GitHub Actions wakes the bot up
2. It fetches all open tennis markets on Kalshi
3. It fetches live Pinnacle odds via [The Odds API](https://the-odds-api.com)
4. For each Kalshi market it can match to a Pinnacle line, it calculates your edge
5. If edge > 5%, it sizes the bet using Half Kelly and places the order
6. Every action is logged to `bot_log.csv`

---

## Setup (One-Time)

### 1. Get Your API Keys

**Kalshi:**
- Go to [kalshi.com](https://kalshi.com) → Settings → API
- Create an API key — download the **Key ID** and **Private Key (.pem file)**
- Start with the **demo environment** at [demo.kalshi.co](https://demo.kalshi.co)

**The Odds API:**
- Sign up at [the-odds-api.com](https://the-odds-api.com) (free tier = 500 req/month)
- Copy your API key

---

### 2. Fork This Repo on GitHub

Click **Fork** in the top right on GitHub so you have your own copy.

---

### 3. Add GitHub Secrets

Go to your forked repo → **Settings → Secrets and variables → Actions → New repository secret**

Add these four secrets:

| Secret Name | Value |
|---|---|
| `KALSHI_API_KEY_ID` | Your Kalshi Key ID |
| `KALSHI_PRIVATE_KEY` | Full contents of your `.pem` private key file |
| `ODDS_API_KEY` | Your Odds API key |
| `BANKROLL` | Your starting bankroll in USD (e.g. `30`) |

---

### 4. Enable GitHub Actions

Go to the **Actions** tab in your repo and click **"I understand my workflows, go ahead and enable them"**.

---

### 5. Run It Manually First (Test)

Go to **Actions → Kalshi Tennis Bot → Run workflow**.
Leave `demo_mode` as `true`. Check the logs — you should see markets being scanned and any opportunities logged without real money moving.

---

### 6. Go Live (When Ready)

When you're satisfied with the dry-run results:
1. In the **Actions** tab, trigger a manual run and set `demo_mode` to `false`
2. Or edit `.github/workflows/bot.yml` and change the default `DEMO_MODE` to `false`

---

## Configuration

All settings live in `config.py`. The key ones:

| Setting | Default | Description |
|---|---|---|
| `DEMO_MODE` | `true` | Logs bets without placing them |
| `MIN_EDGE` | `0.05` | Only bet when edge > 5% |
| `KELLY_FRACTION` | `0.5` | Half Kelly (recommended) |
| `MAX_BET_PCT` | `0.10` | Max 10% of bankroll per bet |
| `MAX_OPEN_POSITIONS` | `3` | Never hold more than 3 bets at once |
| `MIN_BET_DOLLARS` | `$0.50` | Minimum bet size |

---

## Monitoring

- **Bet log:** Each GitHub Actions run uploads `bot_log.csv` as an artifact (Actions tab → click the run → Artifacts)
- **Live logs:** Click into any Actions run to see real-time output

---

## Important Notes

- Kalshi is a **CFTC-regulated** exchange — algorithmic trading is permitted
- Always run in **demo mode first** to validate the bot is working correctly
- Past performance of any betting strategy does not guarantee future results
- Never bet more than you can afford to lose
