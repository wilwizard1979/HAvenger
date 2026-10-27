# Jarvis the Janitor – Backlog and Decisions

Master copy (private, with Home Assistant findings): Google Drive → Jarvis project → `jarvis-janitor-backlog.md`.
This public snapshot leaves out anything about the user's own Home Assistant. Refreshed at the end of each phase.
Plan: `jarvis-ha-agents-plan.md` (same Drive folder). Updated 2026-10-25.

## Naming
- The maintenance team and its Telegram bot are called **Jarvis the Janitor**.
- "Jarvis Home" stays as the Home Assistant approval bot. "Cassandra" is the Claude Code Telegram channel used for build steps.
- GitHub: fork `wilwizard1979/OpenJarvis` (public; upstream `open-jarvis/OpenJarvis`). Never push to upstream.
  Branches: `local/google-actions` (base), `feature/ha-agents` (this project).

## Decisions (2026-10-25)
| # | Decision |
|---|---|
| 1 | Current UI/perf work committed on `local/google-actions`, then branch `feature/ha-agents`. Pushed to the fork. |
| 2 | Cassandra is the dev channel for build steps. Until the phone is available, steps run in the terminal. |
| 3 | Proposal buttons: option A – a new, separate bot "Jarvis the Janitor", polled by Jarvis itself. Bot creation waits for the phone. |
| 4 | Keep both approvals at first (proposal approval + ha-mcp write gate). Option A (HA package change) is folded into Phase 5. |
| 5 | Privacy: scrub more than secrets before cloud calls (MAC, email, GPS, person names → placeholders). |
| 6 | API credits added. Use a dedicated key for the Janitor, stored in macOS Keychain (not in the repo, not in `cloud-keys.env`). |
| 7 | No full Drive replica on the SSD. See "Working files" below. |
| 8 | Report schedule is a setting: pilot = daily 07:30 (Europe/Berlin); target = weekly, Friday. |
| 9 | Scheduler = plain asyncio loop in the FastAPI lifespan (no new dependency). All Janitor settings must be easy to reach and edit in the UI (Settings → Maintenance). |

## Backlog (not scheduled)
- [ ] **Option B for approvals:** keep "Jarvis Home" for proposal buttons; HA forwards button callbacks to Jarvis. Needs Jarvis reachable from HA and an edit of the HA approval package.
- [ ] **Narrow the ha-mcp write gate** for the exact tools the executor uses, to remove the double approval (only after the pilot proves safe).
- [ ] **Read access to the HA approval package** (`/config/packages/ha_mcp_telegram_approvals.yaml`) – paste it or give read-only access.
- [ ] Always-on: LaunchAgent that starts Jarvis at login and restarts it if it dies (decide after Phase 2).
- [ ] Drive without a local replica: choose one – Drive for desktop in stream mode / small local folder uploaded per phase / GitHub docs branch.
- [ ] Qwen log pre-sort (feature flag, default off) – Phase 7 optional.
- [ ] Exposure of the UI on the home network / phone (Tailscale preferred) – not part of this project yet.

## Working files (§9 of the plan)
Reports, proposals, logs and phase summaries go to Drive → Jarvis project → Working files. Everything written there must pass the same redaction as cloud payloads.


## Status 2026-10-26
Phases 0-7 built (watchdog, clusters and matrix, plan and estimates, Haiku notes, Sonnet/Opus escalation, Telegram approval bot, executor in simulation, Settings tab, Qwen house status and read-aloud).

Next: "edit with diff" step for automation fixes (read config in investigations, small change applied by Python, before/after diff in the approval), then the first real approval test.

Open: automatic daily run and Telegram alerts (off), Option B approvals, LaunchAgent, Tailscale, weekly Friday report, merge to the main line, per-issue investigation, automation traces.

## Added 2026-10-27
- [ ] Export button (Python, on click) that writes redacted copies of tasks.md and resolved.md (full redaction incl. hidden words) to a Google Drive folder, so a Claude web project can use them. Wrap each record in a tag (`<task id="..." status="...">`) in the export copy only. Until then: copy and paste the local files.
- [ ] Optional Haiku "polish" button for the closing text (about half a cent per use).
- [ ] Read-back of edits made inside tasks.md / resolved.md (not planned; the files are one-way exports).
- [ ] Host-IP value filled in by Python for plan steps, and a check of what ha-mcp can do with config entries (Ollama case).
