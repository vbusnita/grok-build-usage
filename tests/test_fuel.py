"""Session fuel gauge. No network and no Keychain."""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from gbu.fuel import (
    FuelMeter,
    SessionBurn,
    burn_from_composer,
    burn_from_usage,
    format_tokens,
    load_build_burns,
    load_cursor_burns,
    load_fuel_burns,
    select_build_burns,
)


NOW = datetime(2026, 10, 5, 0, 30, tzinfo=timezone.utc)


def _burn(session_id: str, tokens: int, age_sec: float, last_turn: int | None = None) -> SessionBurn:
    return SessionBurn(
        session_id=session_id,
        tokens=tokens,
        updated_at=NOW - timedelta(seconds=age_sec),
        last_turn_tokens=last_turn,
    )


def test_format_tokens():
    assert format_tokens(940) == "940"
    assert format_tokens(9400) == "9.4k"
    assert format_tokens(840_000) == "840k"
    assert format_tokens(2_515_496) == "2.5M"
    assert format_tokens(9_785_228) == "9.8M"
    assert format_tokens(75_000_000) == "75M"


def test_burn_from_usage_reads_counters_only():
    burn = burn_from_usage(
        {
            "sessionId": "abc",
            "updatedAt": "2026-10-05T00:26:14.910494+00:00",
            "session": {"totalTokens": 9785228, "inputTokens": 1},
            "turns": [{"totalTokens": 2515496, "endedAt": "2026-10-05T00:26:14+00:00"}],
            "prompt": "do not keep this",
        }
    )
    assert burn is not None
    assert burn.session_id == "abc"
    assert burn.tokens == 9785228
    assert burn.last_turn_tokens == 2515496


def test_select_sums_live_sessions_and_keeps_only_the_newest_quiet_one():
    live = [_burn("a", 1000, 30), _burn("b", 2000, 60)]
    assert [b.session_id for b in select_build_burns(live, NOW)] == ["a", "b"]

    quiet = [_burn("old", 5000, 600), _burn("newer", 9000, 400)]
    chosen = select_build_burns(quiet, NOW)
    assert [b.session_id for b in chosen] == ["newer"]

    stale = [_burn("gone", 100, 3000)]
    assert select_build_burns(stale, NOW) == []


def test_first_sample_does_not_treat_history_as_pace():
    meter = FuelMeter()
    burn = _burn("s1", 9_000_000, 10, last_turn=2_500_000)
    reading = meter.observe([burn], None, NOW, mono=1000.0)
    assert reading.tokens == 9_000_000
    assert reading.sources == ("Build",)
    assert reading.rate_per_min is None
    assert reading.level == "hard"
    assert reading.amount_text() == "9M"
    assert "last turn 2.5M" in reading.detail_text()


def test_pace_follows_new_tokens_then_settles():
    meter = FuelMeter()
    start = _burn("s1", 1_000_000, 0, last_turn=1_000_000)
    meter.observe([start], None, NOW, mono=0.0)
    later = _burn("s1", 1_600_000, 0, last_turn=600_000)
    hot = meter.observe([later], None, NOW + timedelta(seconds=30), mono=30.0)
    assert hot.tokens == 1_600_000
    assert hot.rate_per_min == 1_200_000
    assert hot.level == "hard"
    assert hot.detail_text() == "hard · Build · 1.2M/min"

    quiet = meter.observe(
        [_burn("s1", 1_600_000, 70)],
        None,
        NOW + timedelta(seconds=100),
        mono=100.0,
    )
    assert quiet.rate_per_min == 0
    assert quiet.level == "quiet"
    assert quiet.tokens == 1_600_000
    assert quiet.detail_text() == "quiet · Build"


def test_bot_burn_is_the_rise_after_the_baseline_and_adds_to_build():
    meter = FuelMeter()
    first = meter.observe([], 75_000_000, NOW, mono=0.0)
    assert first.sources == ()
    assert first.amount_text() == "—"

    bot = meter.observe([], 75_050_000, NOW + timedelta(seconds=30), mono=30.0)
    assert bot.sources == ("Bot",)
    assert bot.tokens == 50_000
    assert bot.level == "steady"
    assert bot.detail_text().startswith("steady · Bot · ")

    both = meter.observe(
        [_burn("s1", 2_000_000, 5, last_turn=200_000)],
        75_050_000,
        NOW + timedelta(seconds=40),
        mono=40.0,
    )
    assert both.sources == ("Build", "Bot")
    assert both.tokens == 2_050_000


def test_bot_counter_reset_does_not_look_like_a_burn():
    meter = FuelMeter()
    meter.observe([], 1000, NOW, mono=0.0)
    meter.observe([], 5000, NOW, mono=30.0)
    reset = meter.observe([], 100, NOW, mono=60.0)
    assert reset.tokens == 0
    assert reset.sources == ()


def test_load_build_burns_skips_old_files(tmp_path):
    fresh = tmp_path / "proj" / "live"
    fresh.mkdir(parents=True)
    (fresh / "usage.json").write_text(
        json.dumps(
            {
                "updatedAt": NOW.isoformat(),
                "session": {"totalTokens": 12000},
                "turns": [{"totalTokens": 4000}],
            }
        ),
        encoding="utf-8",
    )
    old = tmp_path / "proj" / "old"
    old.mkdir()
    old_usage = old / "usage.json"
    old_usage.write_text(
        json.dumps({"updatedAt": "2020-01-01T00:00:00+00:00", "session": {"totalTokens": 99}}),
        encoding="utf-8",
    )
    stale = (NOW - timedelta(minutes=30)).timestamp()
    os.utime(old_usage, (stale, stale))
    burns = load_build_burns(tmp_path, now=NOW)
    assert len(burns) == 1
    assert burns[0].session_id == "live"
    assert burns[0].tokens == 12000
    assert burns[0].last_turn_tokens == 4000
    assert burns[0].source == "Build"


def _cursor_db(path, rows):
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE composerHeaders (
            composerId TEXT PRIMARY KEY,
            lastUpdatedAt INTEGER,
            isArchived INTEGER,
            isSubagent INTEGER
        )
        """
    )
    con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
    for row in rows:
        cid, age_sec, archived, draft, tokens = row[:5]
        checkpoint_age = row[5] if len(row) > 5 else None
        updated = NOW - timedelta(seconds=age_sec)
        con.execute(
            "INSERT INTO composerHeaders VALUES (?, ?, ?, 0)",
            (cid, int(updated.timestamp() * 1000), archived),
        )
        payload = {
            "composerId": cid,
            "isDraft": draft,
            "lastUpdatedAt": int(updated.timestamp() * 1000),
            "text": "do not keep this",
            "promptTokenBreakdown": {"totalUsedTokens": tokens, "maxTokens": 256000},
        }
        if checkpoint_age is not None:
            checkpoint = NOW - timedelta(seconds=checkpoint_age)
            payload["conversationCheckpointLastUpdatedAt"] = int(checkpoint.timestamp() * 1000)
        con.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (f"composerData:{cid}", json.dumps(payload)),
        )
    con.commit()
    con.close()


def test_burn_from_composer_uses_context_size_only():
    burn = burn_from_composer(
        {
            "isDraft": False,
            "text": "secret prompt",
            "promptTokenBreakdown": {"totalUsedTokens": 85502, "maxTokens": 256000},
        },
        session_id="chat-1",
        updated_at=NOW,
    )
    assert burn is not None
    assert burn.tokens == 85502
    assert burn.source == "Cursor"
    assert burn.last_turn_tokens is None
    assert burn_from_composer({"isDraft": True, "promptTokenBreakdown": {"totalUsedTokens": 10}}, session_id="d", updated_at=NOW) is None
    assert burn_from_composer({"promptTokenBreakdown": {"totalUsedTokens": 10}}, session_id="draft-1", updated_at=NOW) is None


def test_load_cursor_burns_keeps_recent_open_chats(tmp_path):
    db = tmp_path / "state.vscdb"
    _cursor_db(
        db,
        [
            ("live", 30, 0, False, 85000),
            ("quiet", 400, 0, False, 40000),
            ("old", 3000, 0, False, 90000),
            ("running", 3000, 0, False, 110000, 20),
            ("archived", 10, 1, False, 20000),
            ("draft-box", 10, 0, True, 15000),
        ],
    )
    burns = load_cursor_burns(db, now=NOW)
    assert {b.session_id for b in burns} == {"live", "quiet", "running"}
    running = next(b for b in burns if b.session_id == "running")
    assert running.updated_at == NOW - timedelta(seconds=20)
    live = next(b for b in burns if b.session_id == "live")
    assert live.tokens == 85000
    assert live.source == "Cursor"
    assert load_cursor_burns(tmp_path / "missing.vscdb", now=NOW) == []


def test_cursor_chat_is_fuel_when_the_tui_is_closed():
    meter = FuelMeter()
    burn = SessionBurn("c1", 85_000, NOW - timedelta(seconds=10), source="Cursor")
    reading = meter.observe([burn], None, NOW, mono=10.0)
    assert reading.sources == ("Cursor",)
    assert reading.tokens == 85_000
    assert reading.amount_text() == "85k"
    assert reading.detail_text() == "quiet · Cursor"


def test_build_and_cursor_sessions_add(tmp_path):
    sessions = tmp_path / "sessions"
    live = sessions / "proj" / "tui"
    live.mkdir(parents=True)
    (live / "usage.json").write_text(
        json.dumps(
            {
                "updatedAt": (NOW - timedelta(seconds=20)).isoformat(),
                "session": {"totalTokens": 1_000_000},
            }
        ),
        encoding="utf-8",
    )
    db = tmp_path / "state.vscdb"
    _cursor_db(db, [("chat", 15, 0, False, 80_000)])
    burns = load_fuel_burns(now=NOW, sessions=sessions, cursor_db=db)
    reading = FuelMeter().observe(burns, None, NOW, mono=5.0)
    assert reading.sources == ("Build", "Cursor")
    assert reading.tokens == 1_080_000
