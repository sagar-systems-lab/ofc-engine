# Hosted Qloo Tour — Operation and Review

OFC Engine checks the choices in an agent's three-city tour before action. It may use a Qloo brand-affinity ranking to repair one frozen brand candidate and leaves unsupported or dependent decisions pending. The public site does not book venues, spend money, or authorize external actions.

## Visitor experience

Open `https://ofc-fixture-preview.onrender.com/#plan`.

- **Start six-decision tour review** stores a fresh six-decision proposal for 24 hours. This button does **not** call Qloo. The page reports `REVIEW_REQUIRED`.
- When the operator has separately approved publication of a real acquired Qloo rank observation, the same Plan Builder shows a **Qloo decision review**. It states when the response was collected, the brand decision that changed, unaffected choices, dependent checks, and the reasons plan handoff is withheld.
- The published example is read-only and uses an already acquired rank observation; visiting the site does **not** make a new Qloo request. Raw Qloo responses, credentials, operator tokens and anonymous browser tickets are not public.
- **Try the demo** and **Recorded proof** are separately labeled fixture/historical examples, not live provider results.

A Qloo affinity rank indicates relative association inside one fixed candidate pool, not attendance, ticket sales, contract suitability, or a provider-signed verification certificate.

## Private operator activation

The public Qloo showcase is disabled by default. Keep `OFC_DATABASE_URL` and `OFC_DURABLE_RUNS_MODE=ENABLED` private on the **OFC-only** Render service.

1. Start the six-decision tour in the live Plan Builder. Copy its `web-...` run ID; do **not** copy its private browser ticket to documentation. Verify the saved review and the exact configured artist/brand identity before authorizing any provider spend.
2. Only after confirming the event credential's available quota, cost and permission for the query, explicitly authorize **one** bounded Qloo request. Privately configure `QLOO_API_KEY`, a random high-entropy `OFC_HOSTED_OPERATOR_TOKEN`, `OFC_HOSTED_QLOO_MODE=ONE_REQUEST_EXPLICITLY_APPROVED`, and `OFC_HOSTED_BROWSER_RUN_ID=<saved run ID>` in Render. The server derives the artist, brand pool and selected brand from the saved private proposal; do not set conflicting explicit IDs. Do **not** paste the key or token into GitHub, the browser, logs, screenshots or ChatGPT.
3. After Render is live, send **one operator-authenticated** same-origin POST `/api/qloo/live-rank` with JSON `{"confirm":"SPEND_ONE_HOSTED_QLOO_REQUEST"}` and header `X-OFC-Operator-Token`. This is the **only** intended provider-spend action. It permanently reserves the run's attempt before network I/O. It does not auto-retry on timeout, 429 or failure. If rank evidence does not select a different brand, do not claim that a repair occurred.
4. Check the ticketed read-only `/api/runs/<run ID>/review` before publishing. Verify revision 1, exactly one `brand` change, original baseline digest, journal head, current plan digest, frozen request fingerprint, preserved independent decisions, invalidated dependents, `REVIEW_REQUIRED` and all release flags false. If any condition fails, stop. The provider's cryptographic attestation and independent six-decision recheck are **not** established.
5. Only after checking publication rights for the **derived redacted verdict**, explicitly approve this public example by setting `OFC_PUBLIC_QLOO_SHOWCASE=APPROVED` and `OFC_PUBLIC_QLOO_RUN_ID=<same run ID>` on the OFC Render service. Redeploy. The startup and every public request re-qualify its stored proof; if incomplete or mismatched, the service fails closed rather than showing fabricated Qloo data. This is not permission for any additional provider call.

The public showcase can remain readable after its original browser ticket expires, including after a service restart, because it is tied to an operator-approved durable provider result and verifies the stored journal on every read. Public visitors receive neither an operator credential nor a provider-spend endpoint.

## Qualifications before publishing code or submitting

- Run the repository CI and browser qualification at the exact deployed SHA; confirm the real website shows the observed Qloo repair and all blocked/uncertain decisions. Verify cold restart, 320px layout, no private values in public responses and no second Qloo call.
- Review the full Git history and all files for secrets, private raw provider captures, credential-bearing URLs and third-party data publication restrictions.
- Make the GitHub repository public only after the operator authorizes the reviewed source. Confirm the MIT LICENSE and all setup instructions are visible.
- Submit `https://ofc-fixture-preview.onrender.com` and the public repository to the official Qloo Agentic Hackathon at `https://qloo.devpost.com/`. The judging period is November 2–16, 2026; keep the website usable throughout.
- Include a truthful English project description and save the actual Devpost submission receipt. A working hosted preview or passing tests alone are **not** a submission.

Neither the one-query result nor a blocked handoff should be presented as independent verification of every tour choice.
