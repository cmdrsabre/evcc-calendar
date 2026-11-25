"""ICS calendar feeds: parsing, secrecy of the URL, and the runner fallback."""
import http.server
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from evccplan.clients import ApiError, IcsClient
from evccplan.config import from_dict, problems
from evccplan.models import Event
from tests.test_e2e import env  # noqa: F401  (fixture)

UTC = timezone.utc
FEED = (Path(__file__).parent / "fixtures" / "calendar.ics").read_bytes()
START, END = datetime(2026, 10, 10, tzinfo=UTC), datetime(2026, 10, 31, tzinfo=UTC)


@pytest.fixture
def feed():
    state = {"code": 200, "body": FEED}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(state["code"])
            self.end_headers()
            self.wfile.write(state["body"])

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state["url"] = "http://127.0.0.1:%d/secret-token/basic.ics" % srv.server_address[1]
    yield state
    srv.shutdown()


def test_parses_recurrence_exdate_moved_floating_and_drops_allday_and_cancelled(feed):
    got = IcsClient("Work", feed["url"], "Europe/Berlin").events(START, END)
    by = {(e.title, e.start.isoformat()): e for e in got}
    assert sorted(t for t, _ in by) == ["Customer visit", "Floating", "Weekly sync", "Weekly sync (moved)"]
    assert ("Weekly sync", "2026-10-12T06:00:00+00:00") in by          # CEST -> UTC
    assert not any(s.startswith("2026-10-19") for _, s in by)          # EXDATE
    moved = by[("Weekly sync (moved)", "2026-10-26T09:00:00+00:00")]    # CET after the DST change
    assert moved.location == "Other office" and moved.uid == "weekly@example.test"
    assert by[("Floating", "2026-10-15T07:00:00+00:00")].end.hour == 8  # floating = service time zone
    visit = by[("Customer visit", "2026-10-14T12:00:00+00:00")]
    assert visit.location.startswith("Musterstr. 1, Berlin") and visit.description == "Line one\nLine two"


def test_errors_never_contain_the_url(feed):
    feed["code"] = 404
    with pytest.raises(ApiError) as e:
        IcsClient("Work", feed["url"]).events(START, END)
    assert "secret-token" not in str(e.value) and "HTTP 404" in str(e.value)
    feed.update(code=200, body=b"<html>not a calendar secret-token</html>")
    with pytest.raises(ApiError) as e:
        IcsClient("Work", feed["url"]).events(START, END)
    assert "secret-token" not in str(e.value)
    with pytest.raises(ApiError) as e:
        IcsClient("Work", "ftp://example.test/secret-token").events(START, END)
    assert "secret-token" not in str(e.value)


def test_webcal_is_https():
    assert IcsClient("x", "webcal://example.test/a.ics").url == "https://example.test/a.ics"


def test_config_ics_only_needs_no_ha_calendar():
    d = {"ha": {"url": "http://ha", "token_env": "T"}, "evcc": {"url": "http://e", "api_key_env": "K"},
         "ors": {"key_env": "O"}, "home": {"lat": 1, "lon": 2},
         "ics": [{"name": "Family", "url_env": "U1"}, {"name": "Broken", "url_env": "NOPE"}]}
    env_ = {"T": "t", "K": "k", "O": "o", "U1": "http://x/f.ics"}.get
    c = from_dict(d, env=lambda n: env_(n, ""))
    assert c.ha_calendars == [] and c.ics_calendars == [("Family", "http://x/f.ics")]
    assert problems(c) == []
    d["ics"] = [{"name": "Broken", "url_env": "NOPE"}]           # all entries unusable: nothing to read
    assert "no calendar configured (ha.calendar or ics)" in problems(from_dict(d, env=lambda n: env_(n, "")))
    d["ics"] = [{"name": "A", "url_env": "U1"}, {"name": "A", "url_env": "U1"}]
    assert "ics names must be unique" in problems(from_dict(d, env=lambda n: env_(n, "")))


def _ics_event(runner, title="ICS only"):
    s = datetime(2026, 10, 14, 12, tzinfo=UTC)
    return Event(uid="x@ics", title=title, start=s, end=s.replace(hour=13), location="")


class FakeIcs:
    def __init__(self, events):
        self.events_, self.fail = events, False

    def events(self, start, end):
        if self.fail:
            raise ApiError("ICS calendar Work: HTTP 500")
        return self.events_


def test_runner_reads_ics_next_to_ha_labels_it_and_falls_back_to_cache(env):  # noqa: F811
    srv, cfg, runner, state, ors, store = env
    n = len(runner.run()["items"])
    fake = FakeIcs([_ics_event(runner)])
    runner.ics = {"Work": fake}
    r = runner.run()
    assert len(r["items"]) == n + 1
    assert [i["calendar"] for i in r["items"] if i["title"].startswith("ICS only")] == ["Work"]
    fake.fail = True                                                    # cache serves the last state, HA keeps planning
    r = runner.run()
    assert len(r["items"]) == n + 1 and r["stale_since"]
    assert any("Work" in p for p in r["problems"])


def test_runner_ics_only_without_ha_calendars(env):  # noqa: F811
    srv, cfg, runner, state, ors, store = env
    cfg.ha_calendars = []
    runner.ics = {"Work": FakeIcs([_ics_event(runner)])}
    srv.fail_calendar = True                                            # HA calendar API must not even be needed
    r = runner.run()
    assert [i["title"] for i in r["items"]] == ["ICS only"]
