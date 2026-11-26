from datetime import datetime, timedelta

from app.services.job_health import job_problems

NOW = datetime(2026, 11, 25, 12, 0)
HOUR = timedelta(hours=1)


def row(job, status="ok", ok_hours_ago=1, error=None):
    return {"job": job, "last_status": status, "last_error": error,
            "last_run_at": NOW - HOUR, "last_ok_at": NOW - ok_hours_ago * HOUR}


class TestJobProblems:
    def test_healthy_jobs_report_nothing(self):
        assert job_problems([row("gsc_pull"), row("index_coverage")], NOW) == []

    def test_failed_job_is_reported_with_its_error(self):
        problems = job_problems([row("keyword_consolidation", "failed", error="value too long")], NOW)
        assert problems == [{"job": "keyword_consolidation", "label": "Keyword list refresh",
                             "kind": "failed", "at": NOW - HOUR, "error": "value too long"}]

    def test_job_without_success_past_its_limit_is_overdue(self):
        problems = job_problems([row("gsc_pull", ok_hours_ago=49)], NOW)
        assert [(p["job"], p["kind"]) for p in problems] == [("gsc_pull", "overdue")]

    def test_job_just_inside_its_limit_is_fine(self):
        assert job_problems([row("gsc_pull", ok_hours_ago=47)], NOW) == []

    def test_jobs_without_a_limit_are_never_overdue(self):
        assert job_problems([row("order_pull", ok_hours_ago=24 * 30)], NOW) == []

    def test_failures_sort_before_overdue_jobs(self):
        problems = job_problems([row("gsc_pull", ok_hours_ago=100), row("index_coverage", "failed")], NOW)
        assert [p["kind"] for p in problems] == ["failed", "overdue"]

    def test_warning_sorts_between_failed_and_overdue(self):
        problems = job_problems([row("gsc_pull", ok_hours_ago=100), row("store_sync", "warning"),
                                 row("index_coverage", "failed")], NOW)
        assert [p["kind"] for p in problems] == ["failed", "warning", "overdue"]
