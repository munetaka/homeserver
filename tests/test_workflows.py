"""GitHub Actions ワークフローの構造テスト。

Dependabot PR の自動マージが test の完了を待たずに走っていた障害
(docs/incidents/2026-11-13-dependabot-merged-before-test.md) の再発防止。
旧構成 (別ワークフローで auto-merge を予約し、順序はルールセット任せ) では
test_merge_waits_for_test が落ちる。
"""

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _load(path):
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _merge_steps():
    """`gh pr merge` を実行する全ステップを (ファイル名, ワークフロー, ジョブ名, ジョブ, ステップ) で返す。"""
    found = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        wf = _load(path)
        for job_name, job in (wf.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if "gh pr merge" in (step.get("run") or ""):
                    found.append((path.name, wf, job_name, job, step))
    return found


MERGE_STEPS = _merge_steps()
IDS = [f"{name}:{job_name}" for name, _, job_name, _, _ in MERGE_STEPS]


def test_dependabot_merge_exists():
    # 自動マージ自体が消えていないこと (消すならこのテストごと見直す)
    assert MERGE_STEPS, "gh pr merge を実行するステップが見つからない"


@pytest.mark.parametrize(("name", "wf", "job_name", "job", "step"), MERGE_STEPS, ids=IDS)
def test_merge_waits_for_test(name, wf, job_name, job, step):
    # 同じワークフローの test ジョブに needs で依存していること。
    # ルールセットの必須チェックに頼ると、設定漏れで順序が保証されなくなる (今回の障害)
    assert "test" in (wf.get("jobs") or {}), f"{name} に test ジョブが無い"
    needs = job.get("needs") or []
    if isinstance(needs, str):
        needs = [needs]
    assert "test" in needs, f"{name}:{job_name} が test を needs していない"
    # if に always()/failure() 等があると test 失敗時もマージしうる
    cond = str(job.get("if", ""))
    for fn in ("always()", "failure()", "cancelled()"):
        assert fn not in cond, f"{name}:{job_name} の if に {fn} がある"


@pytest.mark.parametrize(("name", "wf", "job_name", "job", "step"), MERGE_STEPS, ids=IDS)
def test_merge_pins_tested_head(name, wf, job_name, job, step):
    # テスト中に rebase された未テストの head をマージしない
    assert "--match-head-commit" in step["run"]
    assert "--auto" not in step["run"], "test 完了後なので auto-merge の予約は不要"


@pytest.mark.parametrize(("name", "wf", "job_name", "job", "step"), MERGE_STEPS, ids=IDS)
def test_merge_only_dependabot_and_excludes_manual_review(name, wf, job_name, job, step):
    assert "dependabot[bot]" in str(job.get("if", ""))
    cond = str(step.get("if", ""))
    # メジャー更新と BLE 系 (Pi 実機確認が必要) は人がマージする
    for keyword in ("semver-major", "bleak", "dbus-fast"):
        assert keyword in cond, f"マージ条件から {keyword} の除外が消えている"
