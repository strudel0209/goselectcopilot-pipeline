import importlib.util
from pathlib import Path
import sys


def test_offline_scorer_reads_assembled_systems_not_unassigned_observations():
    spec = importlib.util.spec_from_file_location("score", Path(__file__).parents[1] / "eval" / "score.py")
    score = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = score
    spec.loader.exec_module(score)
    report = score.Report()
    score.score_job({"tags": ["drive-X", "motor-X"], "pairs": [["drive-X", "motor-X"]]}, {
        "systems": [{"row": {"tag": "drive-X"}, "motor_tag": "motor-X"}],
        "unassigned": [{"row": {"tag": "generic"}}],
    }, report)
    assert report.tags.tp == 2 and report.tags.fp == 0
    assert report.pairs.tp == 1 and report.pairs.fp == 0