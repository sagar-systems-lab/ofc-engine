# OFC Engine — self-service evaluation

**Live application:** https://ofc-fixture-preview.onrender.com/

OFC Engine is a pre-execution **cultural decision reviewer**, not a replacement for a general-purpose conversational model. It grounds one bounded proposed action in Qloo observations, selectively revises the supported mutable choice and records what remained unverified.

## What the product actually demonstrates

A fluent assistant can argue for either brand. OFC instead keeps the proposed choice and candidate pool fixed, checks Qloo's relative cultural ranking inside that pool, independently repeats that scoped ranking, and shows the **same-evidence** comparison before and after OFC's policy decision. A preserved choice is as meaningful as a supported revision. The saved proof also explains why a culturally preferred candidate is **not** automatically approved for a commercial partnership.

The observable result is a reviewable *decision boundary*, not a claim that a brand partnership is available, affordable or likely to succeed.

## First look (no provider spend)

1. Open the [hosted application](https://ofc-fixture-preview.onrender.com/) and read its supported-scope notice. The core live demonstration is **artist → two to five named brands → original brand choice**.
2. Open the flask icon's explicitly **synthetic** scenarios to see how OFC handles a repair, preservation or withheld decision. These scenarios are fixtures, not live Qloo responses.
3. For a real historical Qloo observation, visit the [recorded Studio tour](https://ofc-fixture-preview.onrender.com/studio.html#plan). Its one saved ranking is distinct from the live Copilot's two-observation check.

To evaluate a **new live request**, supply a named artist, two to five named brand candidates and your original choice. For example, an ordinary question is: *"We're weighing Brand A against Brand B for Artist X; our team currently prefers A. Which choice is supported by the available cultural evidence, and what should we still verify before signing anything?"* Replace these placeholders with real names when running a live query. Confirm the provider usage before Send; ambiguous entity names may require selection. The Copilot's live result includes a decision brief, a graph of the **saved review stages** (not synthetic plan dependencies), OFF/ON comparison and downloadable proof. Neither OFF/ON switching nor viewing saved history calls Qloo again.

The application runs on a free Render instance. Hosting and external model/Qloo calls can take 2–3 minutes or longer; that is **not a completion-time guarantee**. Keep the request tab open while exploring Plan Review or Lab in a separate tab. Global provider quotas are shared across visitors. If capacity is exhausted, use the synthetic exploration path rather than resubmitting the same request.

## Test with your own request

After completing the introductory scope notice, write your own prompt. The presently qualified real-data decision is **a named artist selecting between two to five named brand candidates**. Supply the original candidate selection so the system has a fair baseline. Names and wording are up to the evaluator; results are not limited to a particular artist or pair of brands.

The app extracts a structured proposal and may ask for clarification or explicit Qloo entity selection. Every live request requires approval and uses shared model/Qloo quota. For free, repeatable error demonstrations, open the clearly labeled synthetic-scenario menu instead.

Once a live review is saved, verify these invariants:

- **Shared evidence:** OFF and ON refer to the same Qloo observations and model identity. OFF retains the original proposal; ON may revise only a qualified mutable decision.
- **Recheck:** A brand revision requires two ordered, matching and recent rank observations bound to the same candidate scope.
- **Proof:** Comparison, decision map and saved JSON/CSV export show the actual selected choices, revision, source/provenance and remaining uncertainties. Reloading does not rerun Qloo.
- **Fail-closed boundaries:** Unresolvable names, unavailable Qloo, disagreements, expired session, exhausted budget and tampered evidence cannot become a fabricated PASS.
- **No external actions:** Cultural affinity alone never approves contracts, bookings, budgets, endorsements or payments.

## Multi-city and unsupported decisions

Structured proposals can include cities, dependent decisions and unverified place choices. **Venue/locality policy evaluation is not live-qualified**; it can be observed without implying business suitability, or explicitly marked NOT_EVALUATED to preserve bounded provider quota. The system must not present these decisions as fully approved. A demonstrated brand repair is not evidence that an entire tour is ready to execute.

OFC is not intended to answer general knowledge, coding, open-ended web research or document-understanding questions. The app's scope guide describes those exclusions.

## Separate historical walkthrough

The recorded tour at https://ofc-fixture-preview.onrender.com/studio.html#plan retains a fixed $60,000 three-city plan with historical Qloo evidence. It is a **read-only** historical example with one observed rank, not a fresh call and not a claim of independently repeated live evidence. The live Copilot above is the interactive experience.

## Deployment and reproducibility

The root app is hosted externally. The source ships with an MIT license, setup instructions, isolated PostgreSQL integration tests, browser qualification and synthetic replay/negative fixtures. A fresh local install runs without private credentials in synthetic-only mode; the live hosted backend requires owner-managed keys and private Postgres configuration.

**Access:** The production website currently limits per-session submissions and provider calls. If capacity is unavailable during a test, the user-facing status must identify quota/availability rather than fabricate evidence; increase capacity only after provider/billing review.

Source and architecture: [README](../README.md), [Try it](TRY_IT.md), [Product contract](PROJECT.md).
