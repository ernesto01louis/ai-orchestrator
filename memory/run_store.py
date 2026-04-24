import json
from pathlib import Path

def save_run(project, run_id, data, base_path):

    path = Path(base_path) / project / "runs" / run_id
    path.mkdir(parents=True, exist_ok=True)

    with open(path / "run.json", "w") as f:
        json.dump(data, f, indent=2)
