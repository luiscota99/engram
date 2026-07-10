# ADR 0007: Inbox, monitors, and the asynchronous approval loop

Date: 2026-07-08
Status: accepted

## Context

Elicitation (ADR-adjacent, MCP 2025-06-18) lets a live session ask the user to
confirm destructive actions — but only while the user is present. Scheduled
monitors and long-running agents need a way to surface findings and *propose*
actions when nobody is watching, without ever gaining execution authority.

## Decision

- **Inbox**: one table for alerts (inform) and decision requests (propose +
  wait). `finding_key` dedups recurring findings so a daily self-check never
  re-files an open item. Existence and delivery are separate: everything is
  filed; only items ≥ `ENGRAM_NOTIFY_MIN_SEVERITY` invoke the user-approved
  `notify` reflex — the notification channel is itself under the trust model.
- **Monitors**: `reflexes.kind = monitor`. A monitor exiting non-zero is a
  finding on the watched system, not script brokenness — it files a deduped
  alert and never auto-demotes (demotion would disable the smoke detector for
  detecting smoke). Actions keep demotion semantics.
- **Scheduling**: `engram schedule` manages marker-tagged crontab entries.
  The OS owns the clock; Engram ships no daemon, queue, or workers.
- **Change journal**: scripts emit `ENGRAM_CHANGE target=… before=… after=…`;
  `run_reflex` journals them into `reflex_changes`. Mutations on remote
  systems cannot be snapshotted generically, but they can always be
  revertible-by-information.
- **Execution authority**: `engram decide <id> --approve --run` is the single
  path from proposal to execution — human-invoked by construction. Agents get
  exactly one new capability: proposing (`memory_propose_decision`).

## Consequences

- Same machinery serves both personas: the solo engineer consumes by pull
  (inbox at session start, self-check hygiene findings); the agent-framework
  operator consumes by push (monitors on real systems, notify to a device) —
  differing only in defaults, never in trust model.
- A truly irreversible remote action is protected only by human review at
  approval time; the journal records what happened but cannot always undo it.
  No honest framework promises otherwise.
