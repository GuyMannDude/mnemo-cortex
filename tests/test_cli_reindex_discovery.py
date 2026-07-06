"""`migrate reindex --all` tenant discovery must skip archived snapshots.

Archived tenant dirs (e.g. "rocky.archived-20260516") sit beside live tenants
under <data_dir>/agents/ but contain a dot, which validate_agent_id (C1)
rejects — so discovery used to hand them to the reindex and the whole run
died on a ValueError before touching a single live tenant.
"""
import yaml
from click.testing import CliRunner

from agentb.cli import main


def _setup(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    for name in ["cc", "rocky.archived-20260516"]:
        (data_dir / "agents" / name / "memory").mkdir(parents=True)

    cfg = tmp_path / "agentb.yaml"
    cfg.write_text(yaml.safe_dump({
        "data_dir": str(data_dir),
        "reasoning": {"primary": {"provider": "ollama", "model": "x"}},
        "embedding": {"primary": {"provider": "ollama", "model": "nomic-embed-text"}},
    }))
    monkeypatch.setenv("AGENTB_CONFIG", str(cfg))


def test_reindex_all_skips_archived_tenant_dirs(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    result = CliRunner().invoke(main, ["migrate", "reindex", "--all", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Skipping non-tenant dir" in result.output
    assert "rocky.archived-20260516" in result.output  # named in the skip line
    assert "cc" in result.output  # the live tenant still reindexes
