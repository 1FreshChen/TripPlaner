from evals.pydantic_planner_eval import TOOL_CONVERGENCE_DATASET, run_eval


def test_pydantic_eval_dataset_covers_stop_staging_and_hard_limits():
    report = run_eval(progress=False)

    assert len(TOOL_CONVERGENCE_DATASET.cases) == 5
    assert not report.failures
    assert all(
        case.assertions["EqualsExpected"].value is True
        for case in report.cases
    )
