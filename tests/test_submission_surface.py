"""Submission boundary checks: frozen product assets and public-source discipline."""
from pathlib import Path
import shutil

from scripts.verify_submission_surface import ASSETS, ROOT, _git_blob_sha, inspect


def copy_preparation_tree(tmp_path: Path) -> Path:
    for name in ASSETS:
        target = tmp_path / "src/ofc_engine/web" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "src/ofc_engine/web" / name, target)
    for name in (
        "LICENSE", "Dockerfile", "render.yaml",
        "src/ofc_engine/qloo.py", "src/ofc_engine/qloo_live_recheck.py",
        "src/ofc_engine/qloo_decision_gate.py", "src/ofc_engine/agent_workspace.py",
        "docs/PROJECT.md", "docs/TRY_IT.md",
    ):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    return tmp_path


def test_preparation_gate_tracks_current_frozen_product_ui():
    output = inspect(ROOT)
    assert output["result"] == "PREPARATION_PASS"
    assert output["checks"]["frozen_ui_unchanged"] is True
    assert output["changed_locked_assets"] == []
    assert output["real_qloo_requests"] == 0
    assert output["submission_ready"] is False
    assert output["repo_public_verified"] is False
    assert output["external_hosted_demo_verified"] is False
    assert output["hosted_qloo_decision_experience_verified"] is False
    for name, expected in ASSETS.items():
        data = (ROOT / "src/ofc_engine/web" / name).read_bytes()
        assert _git_blob_sha(data) == expected


def test_one_pixel_asset_edit_is_detected(tmp_path):
    root = copy_preparation_tree(tmp_path)
    ui = root / "src/ofc_engine/web/studio.css"
    ui.write_bytes(ui.read_bytes() + b"\n/* unreviewed */\n")
    output = inspect(root)
    assert output["result"] == "BLOCKED"
    assert output["changed_locked_assets"] == ["studio.css"]
    assert output["checks"]["frozen_ui_unchanged"] is False


def test_public_release_cannot_silently_replace_synthetic_host(tmp_path):
    root = copy_preparation_tree(tmp_path)
    dockerfile = root / "Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text().replace(
            "ofc_engine.hosted_preview", "ofc_engine.agent_workspace"
        )
    )
    output = inspect(root)
    assert output["result"] == "BLOCKED"
    assert output["checks"]["deploy_stays_synthetic_and_keyless"] is False


def test_license_and_release_docs_are_required(tmp_path):
    root = copy_preparation_tree(tmp_path)
    (root / "LICENSE").unlink()
    (root / "docs/TRY_IT.md").unlink()
    output = inspect(root)
    assert output["result"] == "BLOCKED"
    assert output["checks"]["license_present"] is False
    assert output["checks"]["submission_docs_present"] is False
