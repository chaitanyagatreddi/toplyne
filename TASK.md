# Task: Build an Animated React UI for GitHub Radar

**Role:** Full-stack Developer (Frontend-focused)
**Experience level:** Fresh graduate / 0 years — this task is written to be beginner-friendly. Take your time, ask questions, and read the linked docs.
**Estimated time:** 5–8 days

---

## Context

GitHub Radar scans GitHub for cybersecurity tool repos, maps top contributors, and scores them with AI. Today the UI is server-rendered from a Python + Flask backend (`app.py`) that streams live progress over Server-Sent Events (SSE).

Your job is to build a **new React frontend** that talks to the existing backend, with smooth, tasteful animations powered by **[motion.dev](https://motion.dev)** (the Motion library, formerly Framer Motion).

You are **not** changing any Python/backend logic. You consume the existing endpoints.

---

## What to build

A single-page React app that lets a user:

1. **Enter a search keyword** (e.g. `nuclei`, `OWASP ZAP`, `Burp Suite`) and start a scan.
2. **Watch live progress** stream in as the backend works (SSE).
3. **See contributor result cards** appear with animation — each card shows the contributor's handle, score, and their tier (core / active / emerging).
4. **Handle empty + error states** gracefully.

### Animation requirements (use motion.dev for all of these)

- Result cards **animate in** as they arrive (fade + slide up, staggered).
- The **"Scan" button** has a subtle press/hover animation and a loading state.
- A **progress indicator** animates smoothly as SSE events come in.
- Page/section transitions use motion — no hard cuts.
- Keep it tasteful: motion should guide the eye, not distract. Prefer short durations (150–300ms) and spring easing.

---

## Tech stack (required)

- **React 18** (with [Vite](https://vitejs.dev) — fast, beginner-friendly setup)
- **[motion.dev](https://motion.dev)** — `npm i motion`, import from `motion/react`
- Plain CSS or CSS Modules is fine. No heavy UI framework required.
- TypeScript is a **nice-to-have**, not required. Start with JS if TS is new to you.

---

## Getting started

```bash
# 1. Scaffold the React app inside a new /frontend folder
npm create vite@latest frontend -- --template react
cd frontend
npm install
npm install motion

# 2. Run the backend (separate terminal, from repo root)
#    See README.md for the required env vars
python app.py            # serves on http://localhost:7860

# 3. Run the frontend
npm run dev              # Vite dev server, usually http://localhost:5173
```

Point your fetch/SSE calls at the backend's URL. Look at `app.py` to find the
route names and the shape of the SSE events it sends — read the backend, don't guess.

---

## Acceptance criteria

- [ ] React app lives in a new `/frontend` folder; backend untouched.
- [ ] User can start a scan and see live progress from the SSE stream.
- [ ] Contributor cards render from real backend data (not mock data).
- [ ] All four animation requirements above are implemented with motion.dev.
- [ ] Loading, empty, and error states are handled and don't crash the UI.
- [ ] Works on desktop and is at least usable on mobile widths.
- [ ] `README` in `/frontend` explains how to run it.

---

## Definition of done

- Code is pushed to a branch named `feature/react-ui`.
- A short screen recording (or GIF) of the animated flow is attached to the PR.
- No console errors in the browser during a normal scan.

---

## Learning resources (read these first if new)

- Motion (motion.dev) React quick start: https://motion.dev/docs/react-quick-start
- Motion `animate` + gestures: https://motion.dev/docs/react-animation
- Staggered lists with motion: https://motion.dev/docs/react-animation#stagger
- Vite + React guide: https://vitejs.dev/guide/
- What is SSE (Server-Sent Events): https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events

---

## Tips (for a first real task)

- Build it in small steps: static UI first, then wire up the backend, then add animations last.
- Get **one** card animating before you animate the whole list.
- Commit often with clear messages. Small PRs are easier to review.
- Stuck for more than ~30 min? Write down what you tried and ask — that's expected, not a failure.
