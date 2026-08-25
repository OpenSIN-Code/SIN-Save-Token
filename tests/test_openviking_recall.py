from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "bin" / "openviking-recall"


def test_recall_includes_file_level_and_permissive_threshold(tmp_path: Path) -> None:
    fake = tmp_path / "ov"
    argv_log = tmp_path / "argv.log"
    fake.write_text(f"""#!/bin/sh
printf '%s\\n' \"$@\" > {argv_log!s}
printf '%s\\n' 'cmd: ov find --uri= -n 5 --threshold -1 --level 2 "central brain canary"'
printf '%s\\n' '{{\"result\":{{\"memories\":[{{\"uri\":\"viking://user/memory-gateway/memories/entities/canary.md\",\"level\":2,\"score\":0.0,\"abstract\":\"central brain canary\"}}]}}}}'
exit 0
""")
    fake.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env.get('PATH','')}"
    result = subprocess.run([str(ADAPTER), "central brain canary"], cwd=ROOT, env=env, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "canary.md" in result.stdout
    assert '"level": 2' in result.stdout
    argv = argv_log.read_text(encoding="utf-8").splitlines()
    assert ["--level", "2"] == argv[argv.index("2") - 1 : argv.index("2") + 1]
    assert "--threshold=-1" in argv
    assert "--user" not in argv
    assert '"user"' not in result.stdout
