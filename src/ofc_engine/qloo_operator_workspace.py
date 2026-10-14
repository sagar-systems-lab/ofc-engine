"""Private, read-only proof workspace for a committed recorded Qloo repair."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from html import escape
import json
import os
from pathlib import Path
from stat import S_ISDIR
import tempfile
from typing import Any

from pydantic import ValidationError

from .durable import SQLiteRevisionStore, StorageIntegrityError
from .intake import AgentProposal
from .qloo import brand_affinity_probe
from .qloo_affinity import AffinityCaptureError, normalize_affinity_capture
from .qloo_agent_bridge import AgentEvidenceRejected, _read_private_json, compare_agent_proposal
from .qloo_recorded_recheck import inspect_recorded_recheck
from .revisions import digest_obj, plan_digest

_MAX_AGE = timedelta(hours=24)


def build_workspace(
    store: SQLiteRevisionStore,
    proposal: AgentProposal,
    original_capture: dict[str, Any],
    *,
    artist_id: str,
    brand_ids: tuple[str, ...],
    decision_id: str,
    now: datetime,
    historical: bool = False,
    recheck_capture: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a redacted view; never validate or release a real-world action."""
    if now.utcoffset() is None:
        raise AgentEvidenceRejected("INVALID_EVIDENCE_CLOCK")
    spec = brand_affinity_probe(artist_id, brand_ids)
    observed = normalize_affinity_capture(original_capture, spec)
    collected = datetime.fromisoformat(observed["collected_at"])
    if collected > now:
        raise AgentEvidenceRejected("CAPTURE_FROM_FUTURE")
    if not historical and now - collected >= _MAX_AGE:
        raise AgentEvidenceRejected("STALE_REQUIRES_HISTORICAL_MODE")
    original_as_of = collected + timedelta(minutes=1) if historical else now
    if original_as_of > now + timedelta(minutes=1):
        raise AgentEvidenceRejected("CAPTURE_FROM_FUTURE")

    comparison = compare_agent_proposal(
        proposal, original_capture,
        decision_id=decision_id, artist_id=artist_id, brand_ids=brand_ids,
        as_of=original_as_of, max_evidence_age=_MAX_AGE,
    )
    arms = comparison["comparison"]
    off, on = arms["evidence_off"], arms["evidence_on"]
    traces = on["traces"]
    if (
        not arms["same_baseline"]
        or off["status"] != "REVIEW_REQUIRED"
        or off["current_revision"] != 0
        or on["status"] != "RECORDED_RANK_REVIEW_ONLY"
        or on["current_revision"] != 1
        or on["repair_attempts"] != 1
        or len(traces) != 1
        or traces[0]["recheck_state"] != "STRUCTURAL_PASS"
        or len(traces[0]["affected_decision_ids"]) != 1
        or traces[0]["changed_decision_id"] != decision_id
    ):
        raise AgentEvidenceRejected("RECORDED_PROOF_NOT_QUALIFIED")

    latest = original_capture if recheck_capture is None else recheck_capture
    latest_observation = normalize_affinity_capture(latest, spec)
    latest_collected = datetime.fromisoformat(latest_observation["collected_at"])
    if latest_collected > now:
        raise AgentEvidenceRejected("RECHECK_CAPTURE_FROM_FUTURE")
    recheck_as_of = (
        max(collected, latest_collected) + timedelta(minutes=1)
        if historical else now
    )
    recheck = inspect_recorded_recheck(
        store, proposal, original_capture, latest,
        decision_id=decision_id, artist_id=artist_id, brand_ids=brand_ids,
        as_of=recheck_as_of, max_evidence_age=_MAX_AGE,
    )
    stored = store.get(comparison["plan_id"])
    if (
        stored.revision != 1
        or plan_digest(stored) != traces[0]["after_plan_digest"]
        or stored.decisions[0].candidate_id != on["selected_candidates"][decision_id]
        or recheck["stored_plan_digest"] != plan_digest(stored)
        or recheck["pending_recheck_ids"] != [decision_id]
    ):
        raise AgentEvidenceRejected("STORED_PROOF_MISMATCH")

    aliases = {candidate: f"Candidate {index:02}" for index, candidate in enumerate(brand_ids, 1)}
    initial_id = off["selected_candidates"][decision_id]
    final_id = on["selected_candidates"][decision_id]
    result = {
        "schema": "ofc-private-operator-workspace-v1",
        "title": "Recorded decision proof",
        "source_mode": "RECORDED_REDACTED",
        "historical_replay": historical,
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "captured_at": collected.astimezone(timezone.utc).isoformat(),
        "plan": {
            "baseline_digest": comparison["baseline_plan_digest"],
            "stored_digest": plan_digest(stored),
            "revision": stored.revision,
            "journal_events": len(store.audit_history(stored.plan_id)),
            "decision_count": len(stored.decisions),
        },
        "decision": {
            "initial": aliases[initial_id],
            "replacement": aliases[final_id],
            "changed": initial_id != final_id,
            "rank_order": [aliases[item["entity_id"]] for item in observed["ranking"]],
            "evidence_off": off["reviews"][0]["verdicts"][0]["verdict"],
            "evidence_on_initial": on["reviews"][0]["verdicts"][0]["verdict"],
            "evidence_on_after_repair": on["reviews"][-1]["verdicts"][0]["verdict"],
            "repair_attempts": on["repair_attempts"],
            "affected_decision_count": len(traces[0]["affected_decision_ids"]),
            "structural_recheck": traces[0]["recheck_state"],
        },
        "recheck": {
            "status": recheck["status"],
            "recorded_rank_verdict": recheck["recorded_rank_verdict"],
            "same_capture": recheck["same_capture"],
            "later_capture_observed": recheck["later_capture_observed"],
            "pending_count": len(recheck["pending_recheck_ids"]),
            "pending_recheck_cleared": False,
        },
        "qualifications": {
            "operator_supplied_capture_not_commit_attested": True,
            "provider_authenticated": False,
            "independent_recheck_completed": False,
            "overall_plan_verified": False,
            "release_authorized": False,
            "external_action_executed": False,
            "real_qloo_requests_this_export": 0,
        },
    }
    return result


_STYLE = """
:root{color-scheme:light;font-family:Inter,Aptos,Segoe UI,system-ui,sans-serif;
--ink:#142747;--muted:#637695;--stroke:#dfe8f5;--blue:#2458e7;
--bg:#f6f8fd;--green:#168267;--amber:#bb671e}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink)}
a{color:inherit}.shell{max-width:1170px;margin:auto;padding:25px 26px 70px}
.top{display:flex;justify-content:space-between;align-items:center;gap:14px;
padding:10px 0 26px;border-bottom:1px solid var(--stroke)}
.logo{font-weight:900;letter-spacing:-.045em;font-size:21px}.logo b{color:var(--blue)}
.mark{display:inline-grid;place-items:center;background:var(--blue);color:white;
width:37px;height:37px;border-radius:12px;margin-right:9px}
.tag{border:1px solid #edcaab;background:#fff5eb;color:#90521d;
padding:8px 12px;border-radius:30px;font-size:11px;font-weight:800;letter-spacing:.05em}
.hero{padding:58px 0 36px;display:grid;grid-template-columns:1.6fr .7fr;gap:24px;align-items:center}
.eyebrow{font-size:11px;font-weight:850;letter-spacing:.16em;color:var(--blue)}
h1{font-size:clamp(35px,4.5vw,62px);line-height:1.07;letter-spacing:-.06em;margin:17px 0}
h1 em{color:var(--blue);font-family:Georgia,serif;font-weight:500}
p{line-height:1.65;color:var(--muted)}.hero p{max-width:540px;font-size:15px}
.lock{border:1px solid #d8e4fd;background:linear-gradient(150deg,#fff,#edf3ff);
border-radius:19px;padding:25px;box-shadow:0 14px 50px #234fa913}
.lock small,.metric small{display:block;color:var(--muted);font-size:12px}
.lock strong{font-size:27px;display:block;margin:13px 0;color:#bd6125}
.trust{border:1px solid #edc8a4;border-radius:13px;padding:13px 18px;
background:#fff7ee;color:#98551c;font-size:12px;line-height:1.6;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.metric{background:white;border:1px solid var(--stroke);border-radius:14px;
padding:21px;min-height:113px}.metric strong{font-size:25px;letter-spacing:-.035em}
.metric small{margin-top:9px}.section{margin-top:40px}
.head{display:flex;justify-content:space-between;align-items:baseline;gap:12px}
h2{letter-spacing:-.045em;font-size:26px;margin:0 0 16px}
.head small{color:var(--muted);font-size:12px}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.panel{background:white;border:1px solid var(--stroke);border-radius:16px;padding:23px}
.panel.off{border-top:4px solid #d6a36a}.panel.on{border-top:4px solid #25886a}
.flag{font-size:10px;font-weight:850;letter-spacing:.13em;color:#647895}
.verdict{font-size:24px;font-weight:900;letter-spacing:-.045em;margin:12px 0}
.off .verdict{color:#ad692a}.on .verdict{color:#168267}
.label{font-size:12px;color:var(--muted)}.selection{font-weight:850;font-size:18px}
.arrow{color:var(--blue);padding:0 10px}
.rank{display:grid;grid-template-columns:46px 1fr 110px;align-items:center;
padding:15px 18px;border-top:1px solid var(--stroke);font-size:13px}
.rank:first-child{border-top:0}.rank strong{font-size:17px;color:var(--blue)}
.rank .winner{font-weight:850;color:var(--green);text-align:right}
.rank .neutral{text-align:right;color:var(--muted)}
.flow{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:9px}
.stage{border:1px solid var(--stroke);background:white;border-radius:13px;
padding:16px;min-height:100px}.stage span{font-size:10px;color:var(--blue);font-weight:900}
.stage strong{display:block;margin:11px 0 5px;font-size:13px}
.stage small{font-size:11px;color:var(--muted)}
.stage.pending{background:#fff8ee;border-color:#efd6bb}
.kv{display:grid;grid-template-columns:200px 1fr;border-top:1px solid var(--stroke);
padding:12px 0;font-size:13px;gap:20px}.kv:first-child{border-top:0}
.kv span{color:var(--muted)}code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;
font-size:11px;overflow-wrap:anywhere}
footer{margin-top:35px;padding-top:20px;border-top:1px solid var(--stroke);
color:var(--muted);font-size:11px;line-height:1.6}
@media(max-width:760px){.hero{grid-template-columns:1fr;padding-top:32px}
.grid{grid-template-columns:1fr 1fr}.flow{grid-template-columns:1fr 1fr}
.pair{grid-template-columns:1fr}.kv{grid-template-columns:1fr;gap:5px}
.shell{padding:18px}h1{font-size:40px}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
"""


def render_html(report: dict[str, Any]) -> str:
    plan, decision, recheck = report["plan"], report["decision"], report["recheck"]
    safe = lambda value: escape(str(value), quote=True)
    ranks = "".join(
        '<div class="rank"><strong>#' + str(i) + '</strong><span>' + safe(name) +
        '</span><span class="' + ("winner" if i == 1 else "neutral") + '">' +
        ("LEADING RANK" if i == 1 else "OBSERVED") + "</span></div>"
        for i, name in enumerate(decision["rank_order"], 1)
    )
    rows = [
        ("Evidence source", "Recorded Qloo affinity / operator-supplied"),
        ("Snapshot", "Revision " + str(plan["revision"]) + " · " + str(plan["journal_events"]) + " journal events"),
        ("Initial baseline", plan["baseline_digest"]),
        ("Persisted plan", plan["stored_digest"]),
        ("Recorded post-commit check", recheck["status"]),
        ("Recheck state", "Pending, not discharged"),
        ("Execution permission", "Not authorized"),
    ]
    provenance = "".join(
        '<div class="kv"><span>' + safe(key) + '</span><code>' + safe(value) + "</code></div>"
        for key, value in rows
    )
    status = "HISTORICAL REPLAY" if report["historical_replay"] else "RECORDED EVIDENCE"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><meta http-equiv="Content-Security-Policy"
content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>OFC Engine · Private recorded proof</title><style>{_STYLE}</style></head>
<body><main class="shell">
<header class="top"><div class="logo"><span class="mark">◇</span>OFC <b>ENGINE</b></div>
<span class="tag">PRIVATE / {status}</span></header>
<section class="hero"><div><div class="eyebrow">AGENT DECISION CONTROL / OPERATOR EVIDENCE</div>
<h1>One decision changed.<br><em>The boundary didn't.</em></h1>
<p>A real stored agent proposal reviewed against a private recorded Qloo affinity response.
Exact plan lineage is checked; no external action is executed.</p></div>
<div class="lock"><small>EXECUTION BOUNDARY</small><strong>LOCKED ↗</strong>
<small>Recorded rank is not cultural approval or live provider authentication.</small></div></section>
<div class="trust"><b>PROVENANCE LIMITATION.</b> This is an offline operator report, not a provider certificate.
The capture was supplied by the operator and is not cryptographically tied to the original commit.
No API request occurs while generating this page. Do not publish without reviewing provider data-use terms.</div>
<section class="grid" aria-label="Run metrics">
<div class="metric"><strong>r{plan["revision"]}</strong><small>PERSISTED REVISION</small></div>
<div class="metric"><strong>{decision["repair_attempts"]}</strong><small>SELECTIVE REPAIR</small></div>
<div class="metric"><strong>{recheck["pending_count"]}</strong><small>RECHECK PENDING</small></div>
<div class="metric"><strong>0</strong><small>API REQUESTS TO EXPORT</small></div>
</section>
<section class="section"><div class="head"><h2>Evidence changes the decision.</h2>
<small>One sealed baseline · two recorded arms</small></div>
<div class="pair">
<div class="panel off"><span class="flag">EVIDENCE OFF</span>
<div class="verdict">{safe(decision["evidence_off"])}</div>
<p>Without supporting affinity evidence, the original proposal remains uncertain.</p>
<div class="label">ORIGINAL CANDIDATE</div><div class="selection">{safe(decision["initial"])}</div></div>
<div class="panel on"><span class="flag">EVIDENCE ON</span>
<div class="verdict">{safe(decision["evidence_on_initial"])} → {safe(decision["evidence_on_after_repair"])}</div>
<p>Recorded ranks trigger one bounded candidate-only repair. This is not a real-world approval.</p>
<div class="label">RECORDED REPLACEMENT</div>
<div class="selection">{safe(decision["initial"])} <span class="arrow">→</span> {safe(decision["replacement"])}</div></div>
</div></section>
<section class="section"><div class="head"><h2>Observed affinity order.</h2>
<small>Ordinal rank only · raw affinity values excluded</small></div>
<div class="panel">{ranks}</div></section>
<section class="section"><h2>Decision lifecycle.</h2><div class="flow">
<div class="stage"><span>01 / PROPOSE</span><strong>Frozen</strong><small>Baseline and candidate pool bound</small></div>
<div class="stage"><span>02 / CHECK</span><strong>Recorded</strong><small>Decision scope and query fingerprint checked</small></div>
<div class="stage"><span>03 / REPAIR</span><strong>Committed</strong><small>One persisted candidate-only patch</small></div>
<div class="stage pending"><span>04 / VERIFY</span><strong>Still pending</strong><small>{safe(recheck["status"])}</small></div>
<div class="stage pending"><span>05 / ACT</span><strong>Blocked</strong><small>No permission to execute</small></div>
</div></section>
<section class="section"><h2>Inspect the lineage.</h2><div class="panel">{provenance}</div></section>
<footer>OFC Engine · Private recorded proof · Generated {safe(report["generated_at"])}.
Provider authenticated: no · Independent recheck completed: no · Release authorized: no.
This artifact contains aliased candidate labels and cannot independently authenticate a Qloo response.</footer>
</main></body></html>"""


def _save(report: dict[str, Any]) -> tuple[Path, Path]:
    parent = Path.home() / ".local/share/ofc-engine/private-qloo-proof-workspaces"
    if parent.is_symlink():
        raise AgentEvidenceRejected("UNSAFE_OUTPUT_DIRECTORY")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    status = parent.lstat()
    if (
        not S_ISDIR(status.st_mode) or parent.is_symlink()
        or status.st_uid != os.getuid() or status.st_mode & 0o077
    ):
        raise AgentEvidenceRejected("UNSAFE_OUTPUT_DIRECTORY")
    directory = Path(tempfile.mkdtemp(prefix="workspace-", dir=parent))
    directory.chmod(0o700)
    manifest = directory / "proof.json"
    html_path = directory / "index.html"

    def write(path: Path, text: str) -> None:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)

    proof_document = {"payload": report, "sha256": digest_obj(report)}
    write(manifest, json.dumps(proof_document, sort_keys=True, indent=2, allow_nan=False) + "\n")
    write(html_path, render_html(report))
    return manifest, html_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a private, recorded-only operator proof workspace")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--recheck-capture", type=Path)
    parser.add_argument("--artist-id", required=True)
    parser.add_argument("--brands", required=True)
    parser.add_argument("--decision-id", required=True)
    parser.add_argument("--historical", action="store_true")
    args = parser.parse_args(argv)
    try:
        info = args.database.lstat()
        from stat import S_ISREG
        if not S_ISREG(info.st_mode) or info.st_size <= 0:
            raise AgentEvidenceRejected("UNSAFE_DATABASE")
        proposal = AgentProposal.model_validate_json(json.dumps(_read_private_json(args.proposal, max_bytes=96 * 1024)))
        capture = _read_private_json(args.capture, max_bytes=1024 * 1024)
        latest = _read_private_json(args.recheck_capture, max_bytes=1024 * 1024) if args.recheck_capture else None
        report = build_workspace(
            SQLiteRevisionStore(args.database), proposal, capture,
            artist_id=args.artist_id, brand_ids=tuple(args.brands.split(",")),
            decision_id=args.decision_id, now=datetime.now(timezone.utc),
            historical=args.historical, recheck_capture=latest,
        )
        manifest, page = _save(report)
        result = {
            "status": "PRIVATE_RECORDED_PROOF_CREATED",
            "html_path": str(page),
            "manifest_path": str(manifest),
            "proof_sha256": digest_obj(report),
            "source_mode": "RECORDED_REDACTED",
            "pending_recheck_cleared": False,
            "provider_authenticated": False,
            "release_authorized": False,
            "real_qloo_requests": 0,
        }
    except (
        OSError, ValueError, TypeError, UnicodeError, ValidationError,
        StorageIntegrityError, AffinityCaptureError, json.JSONDecodeError,
    ):
        print(json.dumps({"status": "BLOCKED", "release_authorized": False, "real_qloo_requests": 0}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
