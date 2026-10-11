"""Repository release preflight catches secrets and private files in deleted history."""
import json
from pathlib import Path
import subprocess
import sys

from scripts.audit_public_release import HistoryAuditError, audit

ROOT = Path(__file__).resolve().parents[1]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def init_repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Security Test")
    (tmp_path / "README.md").write_text("safe content\n", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "initial")
    return tmp_path


def commit(repo, message):
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)


def test_clean_history_passes_without_authorizing_publication(tmp_path):
    repo = init_repo(tmp_path)
    (repo / ".env.example").write_text("KEY=replace-me\n", encoding="utf-8")
    commit(repo, "safe example")
    result = audit(repo)
    assert result["result"] == "PASS"
    assert result["findings"]["sensitive_paths"] == 0
    assert result["findings"]["high_confidence_secret_markers"] == 0
    assert result["public_release_authorized"] is False
    assert result["manual_secret_review_required"] is True


def test_deleted_sensitive_path_still_blocks(tmp_path):
    repo = init_repo(tmp_path)
    sensitive = repo / "OFC_ENGINE_PRIVATE_PLAN.zip"
    sensitive.write_bytes(b"synthetic local placeholder")
    commit(repo, "temporary internal material")
    sensitive.unlink()
    commit(repo, "delete temporary file")
    assert not sensitive.exists()
    result = audit(repo)
    assert result["result"] == "BLOCKED"
    assert result["findings"]["sensitive_paths"] >= 1
    assert result["public_release_authorized"] is False


def test_deleted_secret_in_historical_patch_is_detected_without_disclosure(tmp_path):
    repo = init_repo(tmp_path)
    secret = "AKIA" + "A" * 16
    target = repo / "config.txt"
    target.write_text("token=" + secret + "\n", encoding="utf-8")
    commit(repo, "configuration")
    target.unlink()
    commit(repo, "remove config")
    report = json.dumps(audit(repo))
    assert '"result": "BLOCKED"' in report
    assert '"aws_access_key"' in report
    assert secret not in report
    assert "public_release_authorized" in report


def test_api_keys_and_private_capture_files_are_blocked(tmp_path):
    repo = init_repo(tmp_path)
    path = repo / "private-qloo-probes"
    path.mkdir()
    (path / "sample.json").write_text("{}", encoding="utf-8")
    commit(repo, "accidental capture")
    result = audit(repo)
    assert result["result"] == "BLOCKED"
    assert result["findings"]["sensitive_paths"] >= 1


def test_no_repository_is_error_not_false_pass(tmp_path):
    try:
        audit(tmp_path)
    except HistoryAuditError:
        pass
    else:
        raise AssertionError("non-Git folder accepted as qualified")


def test_cli_output_contains_only_categories_not_content(tmp_path):
    repo = init_repo(tmp_path)
    secret = "ghp_" + "z" * 36
    (repo / "settings.txt").write_text(secret, encoding="utf-8")
    commit(repo, "test marker")
    cmd = [sys.executable, str(ROOT / "scripts" / "audit_public_release.py"), "--repo", str(repo)]
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    assert completed.returncode == 1
    assert secret not in completed.stdout + completed.stderr
    assert json.loads(completed.stdout)["public_release_authorized"] is False
