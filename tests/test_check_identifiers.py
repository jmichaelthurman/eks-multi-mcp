import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_identifiers", ROOT / "scripts" / "check_identifiers.py")
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


# Assembled at run time so this file never contains a literal non-placeholder account ID.
FAKE_ID = "98765" + "4321098"


def test_private_terms_and_account_ids_are_caught():
    pat = ci.compile_terms({"acme-prod-cluster", "AcmeBreakGlass"})
    hits = ci.scan_text("x.yaml", f"cluster: acme-prod-cluster\nrole: acmebreakglass\nid: {FAKE_ID}\n", pat)
    assert len(hits) == 3
    assert all("acme-prod-cluster" not in h for h in hits)  # output is masked


def test_placeholders_and_partial_words_pass():
    pat = ci.compile_terms({"acme-prod"})
    text = f"id: 111111111111\nid: 123456789012\ncluster: acme-prod-two\nsha: a{FAKE_ID}b\n"
    assert ci.scan_text("x.yaml", text, pat) == []


def test_terms_come_from_local_config(files, monkeypatch):
    monkeypatch.setenv("AWS_CONFIG_FILE", files["aws_config"])
    monkeypatch.setenv("KUBECONFIG", files["kubeconfig"])
    terms = ci.private_terms()
    assert {"prod-admin", "AdministratorAccess", "web-prod-blue"} <= terms
    assert "dev" not in terms and "111111111111" not in terms  # common words / placeholders skipped


def test_repository_has_no_identifiers(monkeypatch):
    """Generic rules (CI has no local AWS config): no non-placeholder account IDs anywhere."""
    monkeypatch.chdir(ROOT)
    assert ci.main(["--all"]) == 0


def _repo(tmp_path, monkeypatch, email):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    def git(*a):
        subprocess.run(["git", *a], check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", email)
    return git


def _commit(git, path, text, msg):
    Path(path).write_text(text)
    git("add", "-A")
    git("commit", "-q", "-m", msg)


def test_range_catches_content_added_then_deleted(tmp_path, monkeypatch):
    deny = tmp_path / "deny.txt"
    deny.write_text("acme-prod-cluster\n")
    monkeypatch.setenv("EKS_MULTI_MCP_DENYLIST", str(deny))
    git = _repo(tmp_path, monkeypatch, "1+dev@users.noreply.github.com")
    _commit(git, "a.txt", "ok\n", "base")
    _commit(git, "a.txt", "cluster: acme-prod-cluster\n", "add")
    _commit(git, "a.txt", "ok\n", "remove it again")
    assert ci.main(["--all"]) == 0  # the working tree alone looks clean...
    assert ci.main(["--range", "HEAD~2..HEAD"]) == 1  # ...but the pushed history is not


def test_range_checks_commit_message_and_identity(tmp_path, monkeypatch):
    git = _repo(tmp_path, monkeypatch, "someone@acme-corp.example")
    _commit(git, "a.txt", "ok\n", "base")
    assert ci.main(["--range", "HEAD --max-count=1"]) == 1  # email not allowed
    git("config", "identifiers.allowedEmail", r"@acme-corp\.example$")
    assert ci.main(["--range", "HEAD --max-count=1"]) == 0
    _commit(git, "b.txt", "ok\n", f"note account {FAKE_ID}")
    assert ci.main(["--range", "HEAD --max-count=1"]) == 1  # ID in the message


def test_push_ranges_from_hook_stdin():
    z = "0" * 40
    assert ci.push_ranges([f"refs/heads/x {'a' * 40} refs/heads/x {'b' * 40}"]) == [f"{'b' * 40}..{'a' * 40}"]
    assert ci.push_ranges([f"refs/heads/x {'a' * 40} refs/heads/x {z}"]) == [f"{'a' * 40} --not --remotes"]
    assert ci.push_ranges([f"(delete) {z} refs/heads/x {'b' * 40}"]) == []
