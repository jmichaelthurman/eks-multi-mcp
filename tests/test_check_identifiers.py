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
