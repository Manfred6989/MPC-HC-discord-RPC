import json
import tempfile
import unittest
from pathlib import Path

import mpc_discord_rpc as m

SAMPLE = """<body class="page-variables">
<p id="file">Some Show S01E02 & Friends.mkv</p>
<p id="filepath">C:\\Videos\\Some Show S01E02 & Friends.mkv</p>
<p id="state">{state}</p>
<p id="statestring">Playing</p>
<p id="position">{pos}</p>
<p id="positionstring">00:01:00</p>
<p id="duration">{dur}</p>
<p id="playbackrate">{rate}</p>
</body>"""

CFG = dict(m.DEFAULT_CONFIG)


def status(state=2, pos=60000, dur=1200000, rate="1"):
    return m.parse_variables(SAMPLE.format(state=state, pos=pos, dur=dur, rate=rate))


class ParseTests(unittest.TestCase):
    def test_parse(self):
        s = status()
        self.assertEqual(s.file, "Some Show S01E02 & Friends.mkv")
        self.assertEqual((s.state, s.position_ms, s.duration_ms, s.rate), (2, 60000, 1200000, 1.0))

    def test_nothing_loaded(self):
        s = m.parse_variables('<p id="file"></p><p id="state">-1</p>')
        self.assertIsNone(m.build_activity(s, CFG))

    def test_garbage(self):
        s = m.parse_variables("not mpc")
        self.assertEqual(s.state, m.STATE_NONE)


class ActivityTests(unittest.TestCase):
    def test_playing(self):
        a = m.build_activity(status(), CFG, now=1000)
        self.assertEqual(a["details"], "Some Show S01E02 & Friends")
        self.assertEqual(a["state"], "Playing")
        self.assertEqual((a["start"], a["end"]), (940, 2140))

    def test_playing_fast(self):
        a = m.build_activity(status(rate="2"), CFG, now=1000)
        self.assertEqual(a["state"], "Playing (2x)")
        self.assertEqual((a["start"], a["end"]), (970, 1570))

    def test_paused(self):
        a = m.build_activity(status(state=1, pos=3723000, dur=7200000), CFG)
        self.assertEqual(a["state"], "Paused at 1:02:03 / 2:00:00")
        self.assertNotIn("start", a)

    def test_hide_options(self):
        self.assertIsNone(m.build_activity(status(state=0), CFG))
        self.assertIsNone(m.build_activity(status(state=1), {**CFG, "hide_when_paused": True}))

    def test_privacy(self):
        a = m.build_activity(status(), {**CFG, "show_filename": False})
        self.assertEqual(a["details"], "Watching a video")

    def test_needs_update(self):
        a = m.build_activity(status(), CFG, now=1000)
        self.assertFalse(m.needs_update(a, m.build_activity(status(pos=62000), CFG, now=1002)))
        self.assertTrue(m.needs_update(a, m.build_activity(status(pos=600000), CFG, now=1002)))
        self.assertTrue(m.needs_update(a, m.build_activity(status(state=1), CFG)))
        self.assertTrue(m.needs_update(a, None))
        self.assertFalse(m.needs_update(None, None))


class ConfigTests(unittest.TestCase):
    def test_missing_id_is_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "config.json")
            p.write_text(json.dumps({"discord_client_id": "PUT_YOUR_ID_HERE"}))
            with self.assertRaises(m.ConfigError):
                m.load_config(p)

    def test_created_from_example_and_bom_ok(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "config.example.json").write_text('\ufeff{"discord_client_id": 123}', encoding="utf-8")
            cfg = m.load_config(Path(d, "config.json"))
            self.assertEqual(cfg["discord_client_id"], "123")
            self.assertTrue(Path(d, "config.json").exists())

    def test_bad_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "config.json")
            p.write_text("{nope")
            with self.assertRaises(m.ConfigError):
                m.load_config(p)


class TrayTests(unittest.TestCase):
    def test_title_limit(self):
        st = m.AppStatus("ok", presence="Presence: " + "x" * 300)
        self.assertLessEqual(len(m.tray_title(st)), 127)

    def test_icon(self):
        self.assertEqual(m.make_icon_image("error").size, (64, 64))


if __name__ == "__main__":
    unittest.main()
