# 90-second judge walkthrough

**Open:** https://ofc-fixture-preview.onrender.com/#plan

**0:00–0:15 · PROPOSE.** Click **Review the Qloo-backed tour**. An autonomous agent proposes a fictional $60,000 three-city emerging-artist tour, with a fixed shortlist and six decisions. Five choices are operator-constructed, unverified hypotheses.

**0:15–0:30 · QLOO CHECK.** Without evidence the brand is UNCERTAIN. A previously acquired **genuine Qloo brand-affinity ranking** changes the decision to REPAIR. The public visitor does not call Qloo again.

**0:30–0:45 · REPAIR.** Only the allowed brand candidate changes. Independent opener and venues stay as proposed. The brand and dependent afterparty require rechecking. The repair is committed to revision 1.

**0:45–1:05 · VERIFY.** Inspect the six verdicts, pending rechecks, observation time, original/current plan digests and hash-linked journal. The observed rank does not verify venues, contracts, attendance or budget.

**1:05–1:25 · HANDOFF.** OFC refuses execution: REVIEW_REQUIRED / WITHHELD. No booking, payment or transfer occurs. Safe decision infrastructure must reject unsupported authority.

**1:25–1:30 · Boundary.** The blank editor below is a different plan and cannot inherit Qloo evidence. Synthetic examples are separate.

## English project description for Devpost

**OFC Engine — Pre-execution decision control for AI agents**

AI agents can assemble plans rapidly, but a plausible recommendation is not automatically evidence-backed. OFC Engine freezes a proposed plan's choices and constraints, reviews narrowly scoped cultural-affinity evidence, repairs only the choice that evidence supports, and requires revision-bound verification before any high-impact action.

Our working demonstration is a fictional $60,000 emerging-artist tour across Chicago, Austin and Seattle. Six choices are fixed in advance. One previously acquired real Qloo brand-affinity response supports changing a single brand partner within its original candidate pool. OFC commits that bounded repair in PostgreSQL, preserves independent choices, invalidates dependent checks and maintains a hash-linked audit journal.

Judges can inspect the same frozen plan with Qloo evidence OFF and ON, follow the brand change, and review the post-repair blockers through a five-step hosted walkthrough. Public visitors require no credentials and never generate additional Qloo API requests.

OFC deliberately **withholds action** because five supporting choices and independent rechecks remain unverified. A brand-affinity ranking does not certify full-tour cultural fit, business results or provider authenticity. No real bookings, payments or other external actions are executed.

**Demo:** https://ofc-fixture-preview.onrender.com/#plan

**Open-source code (public release pending):** https://github.com/sagar-systems-lab/ofc-engine

**Stack:** Python, PostgreSQL, Qloo /v2/insights, static web Studio, deterministic tests and Chromium qualification.
