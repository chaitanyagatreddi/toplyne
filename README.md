---
title: GitHub Radar
emoji: 🛡️
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
---

# GitHub Radar — Cybersecurity Contributor Crawler

Scans GitHub for cybersecurity tool repos, maps top contributors, crawls publicly available emails, and scores them with gpt-4o-mini.

## Features

- **Repo Discovery** — Search by keyword (OWASP ZAP, nuclei, Burp Suite, etc.)
- **Contributor Mapping** — Profile scraping via Browserbase + Playwright
- **Email Crawling** — Public emails from GitHub Events API, commit patches, and profiles
- **AI Scoring** — gpt-4o-mini rates contributors as core/active/emerging with activity scores
- **Live SSE Streaming** — Real-time progress updates in the UI

## Stack

- Python + Flask
- Browserbase (headless browser)
- Playwright (DOM scraping)
- OpenAI gpt-4o-mini (contributor analysis)
- GitHub public APIs (events, commits, patches)

## Run locally

```bash
export BROWSERBASE_API_KEY=... BROWSERBASE_PROJECT_ID=... OPENAI_API_KEY=...
pip install -r requirements.txt
python app.py
```
