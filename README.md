# Letterboxd Watchlist Dashboard

Cross-checks a Letterboxd watchlist against JustWatch streaming
availability and publishes it as a single-page dashboard — what's already
watchable, what's leaving soon, what's newly available, plus TMDB-powered
discovery and a shared "watch together" review queue.

**Live site**: https://joshmackwell19.github.io/letterboxd-watchlist/

Runs daily via GitHub Actions; no server to host, no API keys needed to
view it. See [`CLAUDE.md`](CLAUDE.md) for the architecture, database
schema, workflows, and CLI reference.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env   # fill in DATABASE_URL at minimum
python -m watchlist_justwatch.main --dashboard
```
