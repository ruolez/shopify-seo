from datetime import date

from app.services.index_coverage import SITEMAP_STALE_DAYS, sitemap_problems

TODAY = date(2026, 11, 25)
FRESH = {"path": "https://x.com/sitemap.xml", "lastDownloaded": "2026-11-18T17:07:43Z", "errors": "0"}


class TestSitemapProblems:
    def test_recently_read_sitemap_without_errors_is_fine(self):
        assert sitemap_problems([FRESH], TODAY) == []

    def test_no_sitemap_is_a_problem(self):
        assert len(sitemap_problems([], TODAY)) == 1

    def test_errors_are_reported(self):
        assert sitemap_problems([{**FRESH, "errors": "3"}], TODAY) == [
            "https://x.com/sitemap.xml has 3 error(s) in Search Console."]

    def test_stale_download_is_reported_after_the_limit(self):
        stale = (TODAY.toordinal() - SITEMAP_STALE_DAYS - 1)
        old = {**FRESH, "lastDownloaded": date.fromordinal(stale).isoformat() + "T00:00:00Z"}
        edge = {**FRESH, "lastDownloaded": date.fromordinal(stale + 1).isoformat() + "T00:00:00Z"}
        assert (len(sitemap_problems([old], TODAY)), len(sitemap_problems([edge], TODAY))) == (1, 0)

    def test_never_downloaded_is_reported(self):
        assert sitemap_problems([{**FRESH, "lastDownloaded": None}], TODAY) == [
            "Google has never downloaded https://x.com/sitemap.xml."]
