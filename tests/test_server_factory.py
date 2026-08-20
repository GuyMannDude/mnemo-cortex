"""The ASGI module must be safe to import without touching configured stores."""

import os
import subprocess
import sys
from pathlib import Path


def test_server_import_does_not_construct_production_app(tmp_path: Path):
    # If import still calls create_app(), load_config() will try to parse this
    # deliberately invalid file and the subprocess will fail.  A factory-only
    # module merely exposes create_app and never reads it.
    invalid_config = tmp_path / "must-not-be-read.yaml"
    invalid_config.write_text("server: [unterminated\n", encoding="utf-8")
    env = os.environ.copy()
    env["AGENTB_CONFIG"] = str(invalid_config)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import agentb.server as server; "
                "assert callable(server.create_app); "
                "assert not hasattr(server, 'app')"
            ),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
