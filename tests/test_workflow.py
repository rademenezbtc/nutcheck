"""The scorecard workflow picks its paid run by cron string: keep the two in step."""
import pathlib

import yaml

WF = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "scorecard.yml"


def test_paid_cron_is_scheduled():
    wf = yaml.safe_load(WF.read_text())
    crons = [c["cron"] for c in wf[True]["schedule"]]  # YAML reads the key `on` as True
    assert wf["env"]["PAID_CRON"] in crons
    assert len(crons) == 2, "free checks twice a day, one of those runs also paid"


def test_sweep_runs_free_only_unless_paid():
    text = WF.read_text()
    assert "--free-only" in text and '"$SCHEDULE" = "$PAID_CRON"' in text
