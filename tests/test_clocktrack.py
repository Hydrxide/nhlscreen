"""Planning the live clock between readings so it never jumps."""

from datetime import datetime, timezone

from nhlscreen.config import Config
from nhlscreen.nhl.models import Clock, GameState
from nhlscreen.services.clocktrack import ClockPoint, ClockTrack
from nhlscreen.store import Store
from nhlscreen import views
from nhlscreen.render.layouts import clock_text


def P(at, seconds, running=True, period=1, intermission=False):
    return ClockPoint(float(at), float(seconds), running, period, intermission)


def track(*points):
    t = ClockTrack()
    for p in points:
        t.add(p, now=0.0)  # fetched long before they're shown, as with the 25s delay
    return t


def shown(t, now):
    return t.segment(now).value(now)


def test_nothing_before_the_first_reading():
    assert track(P(100, 600)).segment(99) is None


def test_steady_clock_runs_at_normal_pace():
    t = track(P(100, 600), P(120, 580))
    assert t.segment(105).rate == 1
    assert shown(t, 110) == 590


def test_missed_whistle_slows_the_clock_instead_of_snapping_back():
    # 20s passed but the clock only ran 15 (a short stoppage between snapshots)
    t = track(P(100, 600), P(120, 585))
    assert t.segment(110).rate == 0.75
    assert shown(t, 119.99) > 585  # arrives at the next reading exactly on time
    assert shown(t, 120) == 585


def test_stale_reading_speeds_the_clock_instead_of_jumping_forward():
    # the first reading was dated 5s late: the clock must cover 25s in 20
    t = track(P(100, 600), P(120, 575))
    assert t.segment(110).rate == 1.25
    assert shown(t, 120) == 575


def test_running_then_stopped_runs_normally_then_holds_at_the_stop():
    t = track(P(100, 600), P(120, 588, running=False))
    assert shown(t, 106) == 594
    assert shown(t, 112) == 588
    assert t.segment(115).rate == 0  # stopped at 588 from 112, before the reading even arrives
    assert shown(t, 125) == 588


def test_stopped_then_running_holds_then_starts_on_time():
    t = track(P(100, 588, running=False), P(120, 580))
    assert shown(t, 105) == 588  # still the faceoff
    assert t.segment(111).rate == 0
    assert shown(t, 116) == 584  # started at 112 to reach 580 at 120
    assert shown(t, 120) == 580


def test_past_the_last_reading_counts_down_normally():
    t = track(P(100, 600))
    assert shown(t, 130) == 570
    t = track(P(100, 600, running=False))
    assert shown(t, 130) == 600


def test_no_smoothing_across_periods_or_wild_readings():
    t = track(P(100, 5), P(120, 1200, running=False, period=2))
    assert t.segment(110).rate == 1  # end of period: count down, new period at 120
    t = track(P(100, 600), P(120, 400))  # 200s in 20s: something's off, don't race
    assert t.segment(110).rate == 1


def test_unchanged_reading_is_ignored():
    t = track(P(100, 600), P(119, 600))
    assert len(t.points) == 1


def test_late_reading_continues_from_what_is_shown():
    t = ClockTrack()
    t.add(P(100, 600), now=0)
    t.add(P(130, 568), now=125)  # arrived after the board passed the last reading
    assert abs(shown(t, 125) - 575) < 1e-9  # no jump at the moment it arrived
    assert shown(t, 130) == 568


def test_old_points_are_dropped():
    t = ClockTrack(max_points=4)
    for i in range(10):
        t.add(P(100 + 20 * i, 600 - 20 * i), now=100 + 20 * i)
    assert len(t.points) <= 4


# --------------------------------------------------------------------------- into the screen


def _live_game(fixture):
    from nhlscreen.nhl import parsers

    detail = parsers.parse_play_by_play(fixture("pbp_final.json"))
    game = detail.game.model_copy(update={"state": GameState.LIVE,
                                          "clock": Clock(time_remaining="10:00", seconds_remaining=600, running=True)})
    return game, detail


def test_live_screen_shows_the_planned_clock(fixture):
    game, detail = _live_game(fixture)
    store = Store()
    t = store.clock_track(game.id)
    t.add(P(1000, 600), now=0)
    t.add(P(1020, 588, running=False), now=0)
    tz = Config().tz

    at = datetime.fromtimestamp(1006, timezone.utc)
    screen = views.live_game_screen(game, detail, tz, 15, at, segment=store.clock_segment(game.id, at))
    assert clock_text(screen.model_dump(mode="json"), at) == "09:54"

    later = datetime.fromtimestamp(1015, timezone.utc)  # held at the stop, before that reading is released
    screen = views.live_game_screen(game, detail, tz, 15, later, segment=store.clock_segment(game.id, later))
    data = screen.model_dump(mode="json")
    assert data["data"]["clock"]["running"] is False and data["data"]["clock"]["display"] == "09:48"
    assert "09:48" in "".join(screen.lines)
