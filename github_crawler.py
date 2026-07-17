#!/usr/bin/env python3
"""
Toplyne — Dev Contributor Crawler
=================================================
Uses Browserbase + Playwright to crawl GitHub for developer repos
and map who is contributing to what.

Input:  keyword (e.g. "vulnerability scanner", "SIEM", "pentest")
Output: top repos, top contributors, contributor profiles, activity scores

Usage:
  python3 github_crawler.py --keyword "vulnerability scanner" --repos 5
"""

import asyncio
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Optional

from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
import openai

try:
    import anthropic
except ImportError:
    anthropic = None

# ── Config ────────────────────────────────────────────────────────

GITHUB_TOKEN    = os.environ.get("GITHUB_TOKEN", "")
OPENAI_KEY      = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
USE_CLAUDE      = bool(ANTHROPIC_KEY) and anthropic is not None
SE_APP_KEY     = os.environ.get("SE_APP_KEY", "")   # optional — raises limit to 10k/day
PROSPEO_API_KEY = os.environ.get("PROSPEO_API_KEY", "")

# ── Data structures ───────────────────────────────────────────────

@dataclass
class Repo:
    name: str           # owner/repo
    url: str
    stars: int = 0
    description: str = ""
    language: str = ""
    contributors: list = field(default_factory=list)

@dataclass
class Contributor:
    username: str
    profile_url: str
    commits: int = 0
    avatar: str = ""
    name: str = ""
    company: str = ""
    location: str = ""
    bio: str = ""
    pinned_repos: list = field(default_factory=list)
    orgs: list = field(default_factory=list)
    email: str = ""
    email_source: str = ""   # github | stackoverflow | prospeo | search
    website: str = ""
    activity_score: int = 0
    repos_contributed: list = field(default_factory=list)


# ── Browser Scanner ───────────────────────────────────────────────

class GitHubBrowserScanner:
    """Crawl4AI scanner targeting GitHub pages."""

    def __init__(self):
        self.crawler = None
        self._browser_cfg = BrowserConfig(headless=True, verbose=False)
        self._run_cfg = CrawlerRunConfig(page_timeout=20000)

    async def start(self):
        print("  🌐 Starting Crawl4AI session...")
        self.crawler = AsyncWebCrawler(config=self._browser_cfg)
        await self.crawler.__aenter__()
        print("  ✅ Browser connected")
        return self

    # Blacklisted prefixes — not real repos
    SKIP_OWNERS = {"sponsors", "search", "trending", "explore", "topics",
                   "marketplace", "features", "enterprise", "collections",
                   "contact", "about", "login", "signup", "settings", "orgs"}

    async def search_repos(self, keyword: str, max_repos: int = 5) -> list[Repo]:
        """Search GitHub for repos matching the keyword, sorted by stars."""
        url = (
            f"https://github.com/search?q={keyword.replace(' ', '+')}"
            f"&type=repositories&s=stars&o=desc"
        )
        print(f"  🔍 Searching GitHub: {keyword}")
        repos = []
        seen = set()

        try:
            result = await self.crawler.arun(url=url, config=self._run_cfg)
            raw = re.findall(
                r'href="(/([a-zA-Z0-9][a-zA-Z0-9\-]{0,38})/([a-zA-Z0-9][a-zA-Z0-9\-\.\_]{0,99}))"',
                result.html or ""
            )
            for href, owner, repo_name in raw:
                if owner in self.SKIP_OWNERS:
                    continue
                if "." in owner:
                    continue
                full = f"https://github.com/{owner}/{repo_name}"
                if full not in seen:
                    seen.add(full)
                    repos.append(Repo(name=f"{owner}/{repo_name}", url=full))
                if len(repos) >= max_repos:
                    break
        except Exception as e:
            print(f"  ⚠️  Search error: {e}")

        print(f"  ✅ Found {len(repos)} repos")
        return repos[:max_repos]

    async def get_repo_details(self, repo: Repo) -> Repo:
        """Get repo details via GitHub API."""
        try:
            data = self._fetch_json(f"https://api.github.com/repos/{repo.name}")
            if isinstance(data, dict):
                repo.stars = data.get("stargazers_count", 0)
                repo.description = (data.get("description") or "")[:200]
                repo.language = data.get("language") or ""
        except Exception as e:
            print(f"  ⚠️  Repo detail error for {repo.name}: {e}")
        return repo

    async def get_contributors(self, repo: Repo, max_contributors: int = 8) -> list[Contributor]:
        """Get contributors via GitHub REST API (primary) with browser scrape fallback."""
        contributors = []
        seen_users: set = set()

        # Strategy 1: GitHub REST API — reliable, no auth needed for public repos
        api_data = self._fetch_json(
            f"https://api.github.com/repos/{repo.name}/contributors?per_page={max_contributors}&anon=false"
        )
        if isinstance(api_data, list):
            for item in api_data[:max_contributors]:
                username = item.get("login", "")
                if username and username not in seen_users:
                    seen_users.add(username)
                    contributors.append(Contributor(
                        username=username,
                        profile_url=f"https://github.com/{username}",
                        commits=item.get("contributions", 0),
                        repos_contributed=[repo.name],
                    ))

        # Strategy 2: Crawl4AI fallback if API returned nothing
        if not contributors:
            try:
                result = await self.crawler.arun(url=f"{repo.url}/contributors", config=self._run_cfg)
                hrefs = re.findall(r'href="/([a-zA-Z0-9][a-zA-Z0-9\-]{0,37})"', result.html or "")
                for href in hrefs:
                    if href not in seen_users and href not in self.SKIP_OWNERS:
                        seen_users.add(href)
                        contributors.append(Contributor(
                            username=href,
                            profile_url=f"https://github.com/{href}",
                            repos_contributed=[repo.name],
                        ))
                    if len(contributors) >= max_contributors:
                        break
            except Exception as e:
                print(f"  ⚠️  Contributors crawl error for {repo.name}: {e}")

        return contributors[:max_contributors]

    async def get_profile(self, contributor: Contributor) -> Contributor:
        """Visit a contributor's GitHub profile and extract details."""
        try:
            result = await self.crawler.arun(url=contributor.profile_url, config=self._run_cfg)
            h = result.html or ""

            # Name
            m = re.search(r'itemprop="name"[^>]*>\s*([^<]{1,80})', h) or re.search(r'class="[^"]*p-name[^"]*"[^>]*>\s*([^<]{1,80})', h)
            if m: contributor.name = m.group(1).strip()

            # Company
            m = re.search(r'itemprop="worksFor"[^>]*>\s*([^<]{1,80})', h) or re.search(r'p-org[^>]*>\s*([^<]{1,80})', h)
            if m: contributor.company = m.group(1).strip()

            # Location
            m = re.search(r'itemprop="homeLocation"[^>]*>\s*([^<]{1,80})', h) or re.search(r'p-label[^>]*>\s*([^<]{1,80})', h)
            if m: contributor.location = m.group(1).strip()

            # Bio
            m = re.search(r'data-bio-text="([^"]{3,200})"', h) or re.search(r'p-note[^>]*>\s*([^<]{3,200})', h)
            if m: contributor.bio = html.unescape(m.group(1).strip())[:200]

            # Pinned repos
            contributor.pinned_repos = re.findall(r'class="[^"]*repo[^"]*"[^>]*>\s*([a-zA-Z0-9\-\.\_]{1,50})\s*<', h)[:6]

            # Orgs
            contributor.orgs = list(dict.fromkeys(re.findall(r'data-hovercard-type="organization"[^>]*href="/([^"]+)"', h)))[:5]

            # Email visible on profile
            m = re.search(r'href="mailto:([^"]+)"', h)
            if m: contributor.email = m.group(1).strip()
            if not contributor.email:
                m = re.search(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', result.markdown or "")
                if m and "github.com" not in m.group(0): contributor.email = m.group(0)

            # Website
            m = re.search(r'itemprop="url"[^>]*href="([^"]+)"', h) or re.search(r'rel="nofollow me"[^>]*href="([^"]+)"', h)
            if m: contributor.website = m.group(1).strip()

        except Exception as e:
            print(f"  ⚠️  Profile error for {contributor.username}: {e}")

        return contributor

    # ── Fix A: personal website email mining via Firecrawl ───────

    @staticmethod
    def crawl_website_email(website_url: str) -> str:
        """Scrape a contributor's personal website with Firecrawl and extract email.

        Firecrawl handles JS-rendered sites and returns clean markdown.
        Tries root URL first, then /contact and /about paths.
        Returns the first clean email found, or empty string.
        """
        if not website_url or not website_url.startswith("http"):
            return ""

        fc_key = os.environ.get("FIRECRAWL_API_KEY", "")
        if not fc_key:
            return ""

        try:
            from firecrawl import FirecrawlApp
        except ImportError:
            return ""

        NOREPLY = {"noreply@github.com", "users.noreply.github.com"}
        EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

        app = FirecrawlApp(api_key=fc_key)
        base = website_url.rstrip("/")
        paths_to_try = ["", "/contact", "/about", "/about-me"]

        for path in paths_to_try:
            url = base + path
            try:
                result = app.scrape_url(url, formats=["markdown"])
                text = html.unescape(result.markdown or "")
                for match in EMAIL_RE.finditer(text):
                    email = match.group(0).lower()
                    if not any(nr in email for nr in NOREPLY):
                        return email
            except Exception:
                continue

        return ""

    # ── Fix C: Stack Overflow profile ────────────────────────────

    @staticmethod
    def enrich_via_stackoverflow(username: str, name: str) -> str:
        """Look up contributor on Stack Overflow and extract email or website.

        Uses the SE API v2.3 exclusively — no Firecrawl.
        Step 1: search users by inname, pick best match.
        Step 2: fetch that user's full profile (about_me + website_url).
        Returns email found in about_me, or falls back to crawling website_url.
        """
        EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
        NOREPLY  = {"noreply@github.com", "users.noreply.github.com", "stackoverflow.com"}

        def _se_url(path: str, **params) -> str:
            base_params = {"site": "stackoverflow"}
            if SE_APP_KEY:
                base_params["key"] = SE_APP_KEY
            base_params.update(params)
            qs = urllib.parse.urlencode(base_params)
            return f"https://api.stackexchange.com/2.3{path}?{qs}"

        # Step 1: find SO user_id
        search_term = urllib.parse.quote(username)
        search_url = _se_url(
            "/users",
            inname=search_term,
            pagesize=3,
            order="desc",
            sort="reputation",
        )
        data = GitHubBrowserScanner._fetch_json(search_url)
        user_id = None

        if isinstance(data, dict):
            items = data.get("items", [])
            for item in items:
                display = (item.get("display_name") or "").lower()
                if username.lower() in display or (name and name.lower().split()[0] in display):
                    user_id = item.get("user_id")
                    break
            if not user_id and items:
                user_id = items[0].get("user_id")

        if not user_id:
            return ""

        # Step 2: fetch full profile — about_me and website_url
        profile_url = _se_url(
            f"/users/{user_id}",
            filter="!SyjNqbwGU2NWZ1y5pj",  # custom filter: default + user.about_me
        )
        profile_data = GitHubBrowserScanner._fetch_json(profile_url)
        if not isinstance(profile_data, dict):
            return ""

        profile_items = profile_data.get("items", [])
        if not profile_items:
            return ""

        profile = profile_items[0]

        # Check about_me for email
        about_me = html.unescape(profile.get("about_me") or "")
        for match in EMAIL_RE.finditer(about_me):
            email = match.group(0).lower()
            if not any(nr in email for nr in NOREPLY):
                return email

        # Fall back to crawling website_url
        website_url = (profile.get("website_url") or "").strip()
        if website_url and website_url.startswith("http"):
            return GitHubBrowserScanner.crawl_website_email(website_url)

        return ""

    # ── Fix C: Prospeo enrichment by name + company ───────────────

    @staticmethod
    def enrich_via_prospeo(name: str, company: str, username: str) -> str:
        """Lookup contributor email via Prospeo enrich-person API.

        Uses name + company (from GitHub profile) to find verified work email.
        Returns email or empty string. Costs 1 credit per successful match.
        """
        if not PROSPEO_API_KEY:
            print(f"[Prospeo] SKIP @{username}: no API key")
            return ""
        if not name or not company:
            print(f"[Prospeo] SKIP @{username}: missing name='{name}' or company='{company}'")
            return ""
        try:
            parts = name.strip().split(maxsplit=1)
            first = parts[0]
            last = parts[1] if len(parts) > 1 else ""
            payload = {
                "only_verified_email": True,
                "data": {
                    "first_name": first,
                    "last_name": last,
                    "full_name": name,
                    "company_name": company,
                },
            }
            req = urllib.request.Request(
                "https://api.prospeo.io/enrich-person",
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-KEY": PROSPEO_API_KEY,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())

            if data.get("error"):
                print(f"[Prospeo] ERROR @{username}: {data.get('error_code')}")
                return ""

            person = data.get("person") or {}
            email_obj = person.get("email") or {}
            email = email_obj.get("email", "").lower()

            if email and email_obj.get("status") == "VERIFIED":
                NOREPLY = {"noreply@github.com", "users.noreply.github.com"}
                if not any(nr in email for nr in NOREPLY):
                    print(f"[Prospeo] FOUND @{username}: {email}")
                    return email
            print(f"[Prospeo] NO EMAIL @{username}")
        except urllib.error.HTTPError as e:
            print(f"[Prospeo] HTTP {e.code} @{username}: {e.read().decode()[:200]}")
        except Exception as e:
            print(f"[Prospeo] EXC @{username}: {e}")
        return ""

    # ── Fix B: DuckDuckGo search via Firecrawl ───────────────────

    @staticmethod
    def enrich_via_search(name: str, company: str, username: str) -> str:
        """Search DuckDuckGo for the contributor's email using Firecrawl.

        Query: '"name" "company" email contact'
        Scrapes the search results page and extracts any email found.
        Returns the first clean email or empty string.
        """
        fc_key = os.environ.get("FIRECRAWL_API_KEY", "")
        if not fc_key or not name:
            return ""

        try:
            from firecrawl import FirecrawlApp
        except ImportError:
            return ""

        NOREPLY = {"noreply@github.com", "users.noreply.github.com"}
        EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

        app = FirecrawlApp(api_key=fc_key)

        # Build a tight query — name + company or username
        query_parts = [f'"{name}"']
        if company:
            query_parts.append(f'"{company}"')
        else:
            query_parts.append(username)
        query_parts.append("email contact")
        query = " ".join(query_parts)

        search_url = "https://duckduckgo.com/html/?q=" + urllib.parse.quote(query)

        try:
            result = app.scrape_url(search_url, formats=["markdown"])
            text = html.unescape(result.markdown or "")
            for match in EMAIL_RE.finditer(text):
                email = match.group(0).lower()
                if not any(nr in email for nr in NOREPLY) and "duckduckgo" not in email:
                    return email
        except Exception:
            pass

        return ""

    # ── Public email crawling ────────────────────────────────────

    @staticmethod
    def _fetch_json(url: str) -> list | dict | None:
        """Fetch JSON from GitHub API with optional auth token."""
        headers = {"User-Agent": "GitHubRadar/1.0"}
        if GITHUB_TOKEN:
            headers["Authorization"] = f"token {GITHUB_TOKEN}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, json.JSONDecodeError, Exception):
            return None

    @staticmethod
    def crawl_public_emails(username: str) -> list[str]:
        """Crawl publicly available emails for a GitHub user.

        Sources (all public, no auth needed):
        1. GitHub Events API — PushEvent payloads contain commit author emails
        2. GitHub .patch endpoint — commit patches on personal repos expose Author email
        3. Personal repo commit history — scan own repos (not org repos) more aggressively

        Returns deduplicated list of emails, filtering out noreply addresses.
        """
        emails: set[str] = set()
        NOREPLY = {"noreply@github.com", "users.noreply.github.com"}

        def is_clean(email: str) -> bool:
            return bool(email) and not any(nr in email for nr in NOREPLY)

        # Source 1: Events API — /users/{user}/events/public
        events = GitHubBrowserScanner._fetch_json(
            f"https://api.github.com/users/{username}/events/public"
        )
        if isinstance(events, list):
            for event in events:
                if event.get("type") == "PushEvent":
                    for commit in event.get("payload", {}).get("commits", []):
                        email = commit.get("author", {}).get("email", "")
                        if is_clean(email):
                            emails.add(email.lower())

        # Source 2 & 3: Personal repos (owned by user, not forks) — mine commit patches
        # Personal repos have lower privacy paranoia than security org repos.
        # We fetch more repos and more commits per repo to maximize hit rate.
        repos_data = GitHubBrowserScanner._fetch_json(
            f"https://api.github.com/users/{username}/repos?sort=pushed&per_page=10&type=owner"
        )
        if isinstance(repos_data, list):
            # Prioritise non-forked repos first (original work = more likely to have real email)
            owned = [r for r in repos_data if not r.get("fork", False)]
            forked = [r for r in repos_data if r.get("fork", False)]
            ordered = owned[:6] + forked[:2]  # up to 8 repos total

            for repo in ordered:
                if emails:
                    # Stop once we have at least one clean email — avoid rate limits
                    break
                repo_full = repo.get("full_name", "")
                if not repo_full:
                    continue

                # Fetch up to 5 recent commits by this author in their own repo
                commits = GitHubBrowserScanner._fetch_json(
                    f"https://api.github.com/repos/{repo_full}/commits?author={username}&per_page=5"
                )
                if not isinstance(commits, list):
                    continue

                for commit_item in commits[:5]:
                    sha = commit_item.get("sha", "")
                    if not sha:
                        continue

                    # Try commit detail API first (faster, no extra request for patch)
                    commit_detail = GitHubBrowserScanner._fetch_json(
                        f"https://api.github.com/repos/{repo_full}/git/commits/{sha}"
                    )
                    if isinstance(commit_detail, dict):
                        email = (commit_detail.get("author") or {}).get("email", "")
                        if is_clean(email):
                            emails.add(email.lower())
                            continue  # Got it, no need for patch

                    # Fallback: .patch file (richer but slower)
                    patch_url = f"https://github.com/{repo_full}/commit/{sha}.patch"
                    try:
                        req = urllib.request.Request(patch_url, headers={"User-Agent": "GitHubRadar/1.0"})
                        with urllib.request.urlopen(req, timeout=10) as resp:
                            patch_text = resp.read().decode("utf-8", errors="ignore")[:2000]
                        for match in re.finditer(r'(?:From|Author):\s*[^<]*<([^>]+)>', patch_text):
                            email = match.group(1).lower()
                            if is_clean(email):
                                emails.add(email)
                    except Exception:
                        pass

        return sorted(emails)

    async def stop(self):
        try:
            if self.crawler:
                await self.crawler.__aexit__(None, None, None)
                print("  🛑 Crawl4AI session closed")
        except Exception as e:
            print(f"  ⚠️  Cleanup error: {e}")


# ── Analyzer ──────────────────────────────────────────────────────

class GitHubAnalyzer:
    """Uses gpt-4o-mini to score and summarize contributors."""

    def __init__(self):
        self.client = openai.OpenAI(api_key=OPENAI_KEY)

    def score_contributor(self, contributor: Contributor, keyword: str) -> dict:
        """Score and summarize a contributor based on their profile."""
        profile_text = f"""
Username: {contributor.username}
Name: {contributor.name}
Company: {contributor.company}
Location: {contributor.location}
Bio: {contributor.bio}
Pinned repos: {', '.join(contributor.pinned_repos)}
Orgs: {', '.join(contributor.orgs)}
Commits in scanned repos: {contributor.commits}
Repos contributed to: {', '.join(contributor.repos_contributed)}
"""
        prompt = f"""You are analyzing a GitHub contributor in the developer / {keyword} space.

{profile_text}

Return JSON only. No markdown:
{{
  "activity_score": <0-100, based on commits, repos, engagement>,
  "tier": "core" | "active" | "emerging",
  "summary": "<1 sentence: who they are and what they build>",
  "focus_areas": ["area1", "area2"],
  "interesting": true/false
}}"""
        try:
            resp = self.client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            txt = resp.choices[0].message.content.strip()
            start = txt.find("{")
            end = txt.rfind("}") + 1
            if start >= 0:
                return json.loads(txt[start:end])
        except Exception as e:
            print(f"  ⚠️  Score error for {contributor.username}: {e}")

        return {"activity_score": contributor.commits, "tier": "active", "summary": "", "interesting": True}

    def score_contributors_bulk(self, contributors: list, keyword: str) -> list[dict]:
        """Score ALL contributors in one Claude call. Returns list aligned with input order.

        Falls back to per-contributor OpenAI scoring if Claude isn't available.
        """
        if not USE_CLAUDE or not contributors:
            return [self.score_contributor(c, keyword) for c in contributors]

        # Build compact JSON array of contributor profiles
        profiles = [
            {
                "username": c.username,
                "name": c.name,
                "company": c.company,
                "location": c.location,
                "bio": (c.bio or "")[:200],
                "pinned_repos": c.pinned_repos[:5],
                "orgs": c.orgs[:5],
                "commits": c.commits,
                "repos_contributed": c.repos_contributed[:5],
            }
            for c in contributors
        ]
        prompt = f"""You are scoring {len(profiles)} GitHub contributors in the developer / {keyword} space.

For EACH contributor below, return a score object. Output a single JSON array, same length and order as input.

Input contributors:
{json.dumps(profiles, indent=2)}

Output schema for each contributor:
{{
  "username": "<copy from input>",
  "activity_score": <0-100, based on commits + repos + engagement>,
  "tier": "core" | "active" | "emerging",
  "summary": "<1 sentence: who they are and what they build>",
  "focus_areas": ["area1", "area2"],
  "interesting": true | false
}}

Return ONLY the JSON array. No markdown, no preamble."""
        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
            msg = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=8000,
                messages=[{"role": "user", "content": prompt}],
            )
            txt = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
            start = txt.find("[")
            end = txt.rfind("]") + 1
            if start < 0 or end <= start:
                raise ValueError("no JSON array in response")
            scored = json.loads(txt[start:end])
            # Re-align to input order by username
            score_map = {s.get("username", ""): s for s in scored if isinstance(s, dict)}
            results = []
            for c in contributors:
                s = score_map.get(c.username)
                if s:
                    results.append(s)
                else:
                    # Missing entry → safe default
                    results.append({
                        "username": c.username,
                        "activity_score": c.commits,
                        "tier": "active",
                        "summary": "",
                        "focus_areas": [],
                        "interesting": True,
                    })
            return results
        except Exception as e:
            print(f"  ⚠️  Claude bulk score failed ({e}); falling back to per-contributor OpenAI")
            return [self.score_contributor(c, keyword) for c in contributors]

    def build_report(self, keyword: str, repos: list[Repo], contributors: list[Contributor], analyses: list[dict]) -> dict:
        """Build the final Toplyne report."""
        # Deduplicate contributors by username, merge repos_contributed
        contrib_map: dict[str, Contributor] = {}
        for c in contributors:
            if c.username not in contrib_map:
                contrib_map[c.username] = c
            else:
                contrib_map[c.username].repos_contributed = list(set(
                    contrib_map[c.username].repos_contributed + c.repos_contributed
                ))
                contrib_map[c.username].commits += c.commits

        # Merge analysis scores
        analysis_map = {a.get("username", ""): a for a in analyses}
        for username, c in contrib_map.items():
            a = analysis_map.get(username, {})
            c.activity_score = a.get("activity_score", c.commits)

        # Sort by activity score
        sorted_contributors = sorted(contrib_map.values(), key=lambda c: c.activity_score, reverse=True)

        return {
            "keyword": keyword,
            "repos_scanned": len(repos),
            "contributors_found": len(sorted_contributors),
            "repos": [
                {
                    "name": r.name,
                    "url": r.url,
                    "stars": r.stars,
                    "description": r.description,
                    "language": r.language,
                    "contributor_count": len(r.contributors),
                }
                for r in repos
            ],
            "top_contributors": [
                {
                    "username": c.username,
                    "name": c.name,
                    "company": c.company,
                    "location": c.location,
                    "bio": c.bio,
                    "email": c.email,
                    "email_source": c.email_source,
                    "activity_score": c.activity_score,
                    "commits": c.commits,
                    "repos_contributed": c.repos_contributed,
                    "pinned_repos": c.pinned_repos,
                    "orgs": c.orgs,
                    "profile_url": c.profile_url,
                    **analysis_map.get(c.username, {}),
                }
                for c in sorted_contributors[:20]
            ],
        }


# ── Main Agent ────────────────────────────────────────────────────

def parse_github_url(url: str) -> dict:
    """Parse a GitHub URL and return type + owner/repo info.

    Returns:
        {"type": "repo",  "owner": "...", "repo": "..."}
        {"type": "org",   "owner": "..."}
        {"type": "user",  "owner": "..."}
        {"type": "unknown"}
    """
    url = url.strip().rstrip("/")
    # Normalise — strip protocol, www, and bare github.com host
    url = re.sub(r'^https?://(www\.)?github\.com/?', '', url)
    url = re.sub(r'^(www\.)?github\.com/?', '', url)
    parts = [p for p in url.split("/") if p]

    if len(parts) == 0:
        return {"type": "unknown"}
    if len(parts) == 1:
        # Could be org or user — check via API
        owner = parts[0]
        data = GitHubBrowserScanner._fetch_json(f"https://api.github.com/orgs/{owner}")
        if isinstance(data, dict) and data.get("type") == "Organization":
            return {"type": "org", "owner": owner}
        return {"type": "user", "owner": owner}
    if len(parts) >= 2:
        return {"type": "repo", "owner": parts[0], "repo": parts[1]}

    return {"type": "unknown"}


class GitHubRadarAgent:
    """Orchestrates the full GitHub crawl pipeline with SSE streaming."""

    DEFAULT_SOURCES = {"github", "website", "stackoverflow", "websearch"}

    def __init__(self, keyword: str = "", max_repos: int = 5, max_contributors: int = 8,
                 enabled_sources: set = None, github_url: str = ""):
        self.keyword = keyword
        self.github_url = github_url
        self.max_repos = max_repos
        self.max_contributors = max_contributors
        self.sources = enabled_sources if enabled_sources is not None else self.DEFAULT_SOURCES
        self.scanner = GitHubBrowserScanner()
        self.analyzer = GitHubAnalyzer()

    async def run_stackoverflow_pipeline(self, yield_event=None):
        """SO-only pipeline — no GitHub, no browser. Searches SO top answerers by tag."""

        def emit(type_, msg, data=None):
            if yield_event:
                yield_event(type_, msg, data)
            else:
                print(f"  [{type_}] {msg}")

        emit("agent", f"🔶 Stack Overflow Radar starting for: {self.keyword}")

        def se_url(path, **params):
            base = {"site": "stackoverflow"}
            if SE_APP_KEY:
                base["key"] = SE_APP_KEY
            base.update(params)
            qs = urllib.parse.urlencode(base)
            return f"https://api.stackexchange.com/2.3{path}?{qs}"

        EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
        NOREPLY  = {"noreply@github.com", "users.noreply.github.com", "stackoverflow.com"}

        # Step 1: get top answerers for the tag
        tag = urllib.parse.quote(self.keyword.lower().replace(" ", "-"))
        emit("agent", f"🔍 Fetching top Stack Overflow answerers for tag: {self.keyword}")
        data = GitHubBrowserScanner._fetch_json(
            se_url(f"/tags/{tag}/top-answerers/all_time", pagesize=self.max_contributors)
        )

        items = (data or {}).get("items", [])
        if not items:
            emit("error", f"No Stack Overflow users found for tag: {self.keyword}")
            return {"error": "no_results", "keyword": self.keyword}

        emit("contributors_found", f"Found {len(items)} top answerers", {
            "contributors": [i.get("user", {}).get("display_name") for i in items]
        })

        # Step 2: fetch full profile for each user
        contributors = []
        user_ids = [str(i["user"]["user_id"]) for i in items if i.get("user", {}).get("user_id")]
        ids_joined = ";".join(user_ids)
        profiles_data = GitHubBrowserScanner._fetch_json(
            se_url(f"/users/{ids_joined}", filter="!SyjNqbwGU2NWZ1y5pj", pagesize=self.max_contributors)
        )
        profiles = {str(p["user_id"]): p for p in (profiles_data or {}).get("items", [])}

        for item in items:
            user = item.get("user", {})
            uid = str(user.get("user_id", ""))
            profile = profiles.get(uid, user)

            display_name = profile.get("display_name", "")
            so_link = profile.get("link") or user.get("link", "")
            about_me = html.unescape(profile.get("about_me") or "")
            website_url = (profile.get("website_url") or "").strip()
            location = profile.get("location", "")

            emit("profiling", f"👤 Processing {display_name}...")

            # Find email
            email = ""
            for m in EMAIL_RE.finditer(about_me):
                e = m.group(0).lower()
                if not any(nr in e for nr in NOREPLY):
                    email = e
                    break

            if not email and website_url and website_url.startswith("http"):
                emit("crawling_email", f"📧 [SO] Crawling website for {display_name}...")
                email = GitHubBrowserScanner.crawl_website_email(website_url)

            if email:
                emit("email_found", f"📧 [SO] {display_name} → {email}")
            else:
                emit("email_none", f"📧 {display_name} — Email not found")

            c = Contributor(
                username=display_name,
                profile_url=so_link,
                name=display_name,
                location=location,
                website=website_url,
                email=email,
            )
            contributors.append(c)

        # Step 3: analyze with Claude (bulk, 1 call) — fallback to OpenAI per-contributor
        model_label = "claude-haiku-4-5 (bulk)" if USE_CLAUDE else "gpt-4o-mini"
        emit("agent", f"🧠 Analysing {len(contributors)} contributors with {model_label}...")
        emit("analyzing", f"🤖 Scoring {len(contributors)} contributors in a single call…")
        analyses = self.analyzer.score_contributors_bulk(contributors, self.keyword)
        for c, analysis in zip(contributors, analyses):
            analysis["username"] = c.username
            emit("scored", f"{c.name} → score {analysis.get('activity_score', 0)}", analysis)

        # Step 4: build report
        emit("agent", "📊 Building report...")
        analysis_map = {a["username"]: a for a in analyses}
        report = {
            "keyword": self.keyword,
            "source": "stackoverflow",
            "repos_scanned": 0,
            "contributors_found": len(contributors),
            "repos": [],
            "top_contributors": [
                {
                    "username": c.username,
                    "name": c.name,
                    "location": c.location,
                    "email": c.email,
                    "email_source": c.email_source,
                    "profile_url": c.profile_url,
                    "website": c.website,
                    "activity_score": analysis_map.get(c.username, {}).get("activity_score", 0),
                    "tier": analysis_map.get(c.username, {}).get("tier", "active"),
                    "summary": analysis_map.get(c.username, {}).get("summary", ""),
                    "repos_contributed": [],
                }
                for c in contributors
            ],
        }
        emit("complete", "✅ Stack Overflow scan complete!", report)
        return report

    async def run_from_url(self, yield_event=None):
        """Crawl contributors from a direct GitHub repo/org/user URL."""

        def emit(type_, msg, data=None):
            if yield_event:
                yield_event(type_, msg, data)
            else:
                print(f"  [{type_}] {msg}")

        parsed = parse_github_url(self.github_url)
        emit("agent", f"🔗 Scanning GitHub URL: {self.github_url}")
        emit("agent", f"Detected type: {parsed.get('type')} — {parsed.get('owner','')}{('/' + parsed.get('repo','')) if parsed.get('repo') else ''}")

        repos: list[Repo] = []

        if parsed["type"] == "repo":
            repo_name = f"{parsed['owner']}/{parsed['repo']}"
            repo = Repo(name=repo_name, url=f"https://github.com/{repo_name}")
            repo = await self.scanner.get_repo_details(repo)
            emit("repo_detail", f"⭐ {repo.stars:,} stars — {repo.description[:80]}", {
                "repo": repo.name, "stars": repo.stars, "description": repo.description
            })
            repos = [repo]

        elif parsed["type"] in ("org", "user"):
            owner = parsed["owner"]
            endpoint = "orgs" if parsed["type"] == "org" else "users"
            emit("agent", f"📦 Fetching repos for {parsed['type']}: {owner}")
            repos_data = GitHubBrowserScanner._fetch_json(
                f"https://api.github.com/{endpoint}/{owner}/repos?sort=stars&per_page={self.max_repos}&type=public"
            )
            if isinstance(repos_data, list):
                for r in repos_data[:self.max_repos]:
                    repo = Repo(
                        name=r.get("full_name", ""),
                        url=r.get("html_url", ""),
                        stars=r.get("stargazers_count", 0),
                        description=(r.get("description") or "")[:200],
                        language=r.get("language") or "",
                    )
                    repos.append(repo)
                emit("repos_found", f"Found {len(repos)} repos for {owner}", {
                    "repos": [r.name for r in repos]
                })
        else:
            emit("error", f"Could not parse GitHub URL: {self.github_url}")
            return {"error": "invalid_url", "url": self.github_url}

        # From here — same pipeline as keyword scan
        self.keyword = self.keyword or parsed.get("owner", "github")
        return await self._run_pipeline(repos, yield_event=yield_event)

    async def _run_pipeline(self, repos: list, yield_event=None):
        """Shared pipeline: repos → contributors → emails → analysis → report."""

        def emit(type_, msg, data=None):
            if yield_event:
                yield_event(type_, msg, data)
            else:
                print(f"  [{type_}] {msg}")

        all_contributors: list[Contributor] = []
        all_analyses: list[dict] = []

        try:
            emit("agent", "🌐 Starting Crawl4AI session...")
            await self.scanner.start()
            emit("agent", "✅ Browser connected")

            for i, repo in enumerate(repos):
                emit("scanning_repo", f"📦 Scanning {repo.name} ({i+1}/{len(repos)})")
                emit("agent", f"👥 Fetching contributors for {repo.name}...")
                contributors = await self.scanner.get_contributors(repo, self.max_contributors)
                repo.contributors = [c.username for c in contributors]
                emit("contributors_found", f"Found {len(contributors)} contributors in {repo.name}", {
                    "repo": repo.name,
                    "contributors": [c.username for c in contributors]
                })
                all_contributors.extend(contributors)

            seen = set()
            unique_contributors = []
            for c in all_contributors:
                if c.username not in seen:
                    seen.add(c.username)
                    unique_contributors.append(c)

            top_to_profile = sorted(unique_contributors, key=lambda c: c.commits, reverse=True)[:10]

            for i, contributor in enumerate(top_to_profile):
                emit("profiling", f"👤 Profiling @{contributor.username} ({i+1}/{len(top_to_profile)})")
                contributor = await self.scanner.get_profile(contributor)

                if "github" in self.sources:
                    emit("crawling_email", f"📧 [GitHub] Crawling commits for @{contributor.username}...")
                    crawled_emails = self.scanner.crawl_public_emails(contributor.username)
                    if crawled_emails:
                        contributor.email = ", ".join(sorted(set(crawled_emails)))
                        contributor.email_source = "github"
                        emit("email_found", f"📧 [GitHub] @{contributor.username} → {contributor.email}")

                if not contributor.email and "stackoverflow" in self.sources:
                    emit("crawling_email", f"📧 [SO] Trying Stack Overflow for @{contributor.username}...")
                    so_email = GitHubBrowserScanner.enrich_via_stackoverflow(
                        contributor.username, contributor.name
                    )
                    if so_email:
                        contributor.email = so_email
                        contributor.email_source = "stackoverflow"
                        emit("email_found", f"📧 [SO] @{contributor.username} → {contributor.email}")

                if not contributor.email:
                    if not PROSPEO_API_KEY:
                        emit("crawling_email", f"📧 [Prospeo] SKIP @{contributor.username}: no API key")
                    elif not contributor.name:
                        emit("crawling_email", f"📧 [Prospeo] SKIP @{contributor.username}: no name on GitHub")
                    elif not contributor.company:
                        emit("crawling_email", f"📧 [Prospeo] SKIP @{contributor.username}: no company on GitHub")
                    else:
                        emit("crawling_email", f"📧 [Prospeo] TRYING @{contributor.username} ({contributor.name} @ {contributor.company})...")
                        prospeo_email = GitHubBrowserScanner.enrich_via_prospeo(
                            contributor.name, contributor.company, contributor.username
                        )
                        if prospeo_email:
                            contributor.email = prospeo_email
                            contributor.email_source = "prospeo"
                            emit("email_found", f"📧 [Prospeo] FOUND @{contributor.username} → {contributor.email}")
                        else:
                            emit("crawling_email", f"📧 [Prospeo] NO MATCH @{contributor.username}")

                if not contributor.email:
                    emit("email_none", f"📧 @{contributor.username} — Email not found")

                emit("profile_done", f"@{contributor.username} — {contributor.company or contributor.bio[:50] or 'no bio'}")

            await self.scanner.stop()
            emit("agent", "🛑 Browser session closed")

            model_label = "claude-haiku-4-5 (bulk)" if USE_CLAUDE else "gpt-4o-mini"
            emit("agent", f"🧠 Analysing {len(top_to_profile)} contributors with {model_label}...")
            emit("analyzing", f"🤖 Scoring {len(top_to_profile)} contributors in a single call…")
            bulk_results = self.analyzer.score_contributors_bulk(top_to_profile, self.keyword)
            for contributor, analysis in zip(top_to_profile, bulk_results):
                analysis["username"] = contributor.username
                all_analyses.append(analysis)
                emit("scored", f"@{contributor.username} → score {analysis.get('activity_score', 0)} ({analysis.get('tier', '?')})", analysis)

            # Step 3b: Mamba GTM Suite — company-level enrichment (hiring + tech stack)
            company_signals: dict[str, dict] = {}
            if os.environ.get("APIFY_TOKEN", "").strip():
                try:
                    import mamba
                    # Collect unique company domains from contributor profiles
                    domains = []
                    for c in top_to_profile:
                        # Pull domain from contributor.website or .company if it looks like a URL
                        candidate = (c.website or "").strip() if hasattr(c, "website") else ""
                        if not candidate and getattr(c, "company", "").startswith("http"):
                            candidate = c.company.strip()
                        if candidate:
                            # Strip protocol, www., trailing path
                            d = candidate.replace("https://", "").replace("http://", "").split("/")[0]
                            d = d.removeprefix("www.")
                            if d and d not in domains:
                                domains.append(d)
                    domains = domains[:5]  # cap at 5 to control Apify spend per scan

                    if domains:
                        emit("agent", f"🏢 Enriching {len(domains)} companies via Mamba GTM Suite (Apify)...")
                        for d in domains:
                            emit("enriching_company", f"🏢 {d} — hiring + tech-stack signals")
                            try:
                                items = mamba.aggregate_signals(d)
                                if items and isinstance(items, list):
                                    company_signals[d] = items[0]
                                    sig = items[0]
                                    emit("company_signal", f"  → {d}: {sig.get('composite_signal', '?')} ({sig.get('gtm_role_count', 0)} GTM roles)", sig)
                            except Exception as ce:
                                emit("company_signal_skip", f"  ⚠️  {d}: {ce}")
                except Exception as e:
                    emit("agent", f"⚠️  Mamba enrichment skipped: {e}")

            emit("agent", "📊 Building report...")
            report = self.analyzer.build_report(self.keyword, repos, all_contributors, all_analyses)
            report["company_signals"] = company_signals
            emit("complete", "✅ Toplyne scan complete!", report)
            return report

        except Exception as e:
            import traceback
            emit("error", f"❌ {str(e)}")
            traceback.print_exc()
            try:
                await self.scanner.stop()
            except:
                pass
            return {"error": str(e)}

    async def run(self, yield_event=None):
        """
        Run the full crawl.
        yield_event(type, message, data=None) — called for SSE streaming.
        """

        if self.github_url:
            return await self.run_from_url(yield_event=yield_event)

        if self.sources == {"stackoverflow"}:
            return await self.run_stackoverflow_pipeline(yield_event=yield_event)

        def emit(type_, msg, data=None):
            if yield_event:
                yield_event(type_, msg, data)
            else:
                print(f"  [{type_}] {msg}")

        emit("agent", "🚀 Toplyne starting...", {"keyword": self.keyword})

        try:
            # Step 1: Search repos by keyword
            emit("agent", "🌐 Starting Crawl4AI session...")
            await self.scanner.start()
            emit("agent", "✅ Browser connected")
            emit("agent", f"🔍 Searching GitHub for: {self.keyword}")
            repos = await self.scanner.search_repos(self.keyword, self.max_repos)
            emit("repos_found", f"Found {len(repos)} repos", {"repos": [r.name for r in repos]})

            for i, repo in enumerate(repos):
                emit("scanning_repo", f"📦 Scanning {repo.name} ({i+1}/{len(repos)})")
                repo = await self.scanner.get_repo_details(repo)
                emit("repo_detail", f"⭐ {repo.stars:,} stars — {repo.description[:80]}", {
                    "repo": repo.name, "stars": repo.stars, "description": repo.description
                })

            await self.scanner.stop()

            # Step 2: Run shared pipeline
            return await self._run_pipeline(repos, yield_event=yield_event)

        except Exception as e:
            import traceback
            emit("error", f"❌ {str(e)}")
            traceback.print_exc()
            try:
                await self.scanner.stop()
            except:
                pass
            return {"error": str(e), "keyword": self.keyword}


# ── CLI ───────────────────────────────────────────────────────────

async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Toplyne — Dev Contributor Crawler")
    parser.add_argument("--keyword", default="vulnerability scanner", help="Search keyword")
    parser.add_argument("--repos", type=int, default=3, help="Max repos to scan")
    parser.add_argument("--contributors", type=int, default=6, help="Max contributors per repo")
    args = parser.parse_args()

    if not BB_API_KEY or not BB_PROJECT_ID or not OPENAI_KEY:
        print("❌ Set BROWSERBASE_API_KEY, BROWSERBASE_PROJECT_ID, OPENAI_API_KEY")
        sys.exit(1)

    agent = GitHubRadarAgent(
        keyword=args.keyword,
        max_repos=args.repos,
        max_contributors=args.contributors,
    )
    report = await agent.run()

    print("\n" + "="*60)
    print(f"  🛡️  TOPLYNE — {report.get('keyword', '').upper()}")
    print("="*60)
    print(f"  Repos scanned: {report.get('repos_scanned', 0)}")
    print(f"  Contributors: {report.get('contributors_found', 0)}")
    print("\n📦 TOP REPOS")
    for r in report.get("repos", []):
        print(f"  ⭐ {r['stars']:>6,}  {r['name']:<40}  {r['description'][:60]}")

    print("\n👥 TOP CONTRIBUTORS")
    for c in report.get("top_contributors", [])[:10]:
        tier = c.get("tier", "?")
        score = c.get("activity_score", 0)
        email = c.get("email", "") or "Email not found"
        summary = c.get("summary", c.get("bio", ""))[:70]
        print(f"  [{tier:8}] @{c['username']:<20} score={score:>3}  📧 {email}")
        if summary:
            print(f"             {summary}")

    # Save
    out = f"github_radar_{args.keyword.replace(' ', '_')}.json"
    with open(out, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n📄 Saved: {out}")


if __name__ == "__main__":
    asyncio.run(main())
