import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "manager", Path(__file__).parents[1] / "hyprscreen.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def monitor(name, workspace=1):
    return dict(
        name=name,
        width=1920,
        height=1080,
        refreshRate=60,
        x=0,
        y=0,
        scale=1,
        activeWorkspace={"id": workspace},
        description="test",
    )


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paths = patch.multiple(
            m,
            ROOT=root,
            DB=root / "db.json",
            MON=root / "monitors.lua",
            WS=root / "workspaces.lua",
            RUNTIME=root / "runtime",
        )
        self.paths.start()
        m.RUNTIME.mkdir()

    def tearDown(self):
        self.paths.stop()
        self.tmp.cleanup()

    def test_stable_blocks_new_monitor(self):
        data = m.discover(
            {"outputs": {}}, [monitor("DP-1"), monitor("eDP-1"), monitor("HDMI-A-1")]
        )
        self.assertEqual(
            [data["outputs"][n]["base"] for n in ["HDMI-A-1", "eDP-1", "DP-1"]],
            [5, 10, 0],
        )
        data = m.discover(data, [monitor("DP-2")])
        self.assertEqual(data["outputs"]["DP-2"]["base"], 15)
        self.assertEqual(data["outputs"]["DP-1"]["base"], 0)

    def test_unplug_folds_only_missing_monitor_and_restores(self):
        all_mon = [monitor("HDMI-A-1"), monitor("eDP-1", 6), monitor("DP-1", 11)]
        data = m.discover({"outputs": {}}, all_mon)
        for name, base in [("HDMI-A-1", 0), ("eDP-1", 5), ("DP-1", 10)]:
            data["outputs"][name]["base"] = base
        m.save(data)
        client = {"address": "0x123", "workspace": {"id": 13}}
        with (
            patch.object(m, "live", return_value=all_mon[:2]),
            patch.object(m, "query", return_value=[client]),
            patch.object(m, "run", return_value=""),
            patch.object(m, "move_window") as move,
        ):
            m.sync()
            move.assert_called_once_with("0x123", 8)
            self.assertNotIn('monitor = "DP-1"', m.WS.read_text())
        client["workspace"]["id"] = 8
        with (
            patch.object(m, "live", return_value=all_mon),
            patch.object(m, "query", return_value=[client]),
            patch.object(m, "run", return_value=""),
            patch.object(m, "move_window") as move,
        ):
            m.sync()
            move.assert_called_once_with("0x123", 13)

    def test_manual_move_not_restored(self):
        monitors = [monitor("HDMI-A-1"), monitor("eDP-1", 6), monitor("DP-1", 11)]
        data = m.discover({"outputs": {}}, monitors)
        for name, base in [("HDMI-A-1", 0), ("eDP-1", 5), ("DP-1", 10)]:
            data["outputs"][name]["base"] = base
        m.save(data)
        (m.RUNTIME / "folded.json").write_text(
            json.dumps({"0x123": {"output": "DP-1", "origin": 13, "fallback": 8}})
        )
        with (
            patch.object(m, "live", return_value=monitors),
            patch.object(
                m, "query", return_value=[{"address": "0x123", "workspace": {"id": 9}}]
            ),
            patch.object(m, "run", return_value=""),
            patch.object(m, "move_window") as move,
        ):
            m.sync()
            move.assert_not_called()

    def test_rollback_restores_files(self):
        m.MON.write_text("new")
        m.DB.write_text("new")
        (m.RUNTIME / "pending.json").write_text(
            json.dumps({"monitors": "original", "database": "old-db"})
        )
        with patch.object(m, "run", return_value=""):
            m.rollback()
        self.assertEqual(m.MON.read_text(), "original")
        self.assertEqual(m.DB.read_text(), "old-db")
        self.assertFalse((m.RUNTIME / "pending.json").exists())

    def test_coordinate_scale_and_rotation(self):
        self.assertEqual(
            m.dimensions({"mode": "2880x1800@60", "scale": 1.6, "transform": 0}),
            (1800, 1125),
        )
        self.assertEqual(
            m.dimensions({"mode": "1920x1080@60", "scale": 1, "transform": 1}),
            (1080, 1920),
        )

    def test_workspace_selection_uses_dp_block(self):
        data = m.discover(
            {"outputs": {}},
            [monitor("HDMI-A-1"), monitor("eDP-1", 6), monitor("DP-1", 11)],
        )
        for name, base in [("HDMI-A-1", 0), ("eDP-1", 5), ("DP-1", 10)]:
            data["outputs"][name]["base"] = base
        m.save(data)
        with (
            patch.object(m, "live", return_value=[monitor("DP-1", 15)]),
            patch.object(m, "run", return_value="DP-1") as run,
        ):
            m.workspace("focus", "next")
            self.assertIn('"11"', run.call_args.args[1])
            self.assertIn("DP-1", run.call_args.args[1])


class VisualTests(unittest.TestCase):
    def test_all_advertised_rates_preserved(self):
        monitor = {
            "availableModes": [
                "2560x1440@60.00Hz",
                "2560x1440@75.00Hz",
                "2560x1440@100.00Hz",
                "2560x1440@120.00Hz",
                "2560x1440@144.00Hz",
                "1920x1080@59.94Hz",
                "1920x1080@60.00Hz",
            ]
        }
        modes = m.mode_catalog(monitor, "2560x1440@143.999")
        self.assertEqual(set(modes["2560x1440"]), {60, 75, 100, 120, 144})
        self.assertEqual(set(modes["1920x1080"]), {59.94, 60})
        self.assertTrue(all(modes["2560x1440"].values()))

    def test_unadvertised_mode_is_labelled(self):
        modes = m.mode_catalog({"availableModes": ["640x480@59.94Hz"]}, "1920x1080@60")
        self.assertFalse(modes["1920x1080"][60])
        self.assertTrue(modes["640x480"][59.94])

    def test_snap_negative_left_edge(self):
        left = dict(mode="1920x1080@60", scale=1, transform=0, x=-1900, y=20)
        right = dict(mode="2560x1440@144", scale=1, transform=0, x=0, y=0)
        m.snap_screen(left, [right])
        self.assertEqual((left["x"], left["y"]), (-1920, 0))

    def test_layout_commit_and_cancel(self):
        class Window:
            def __init__(self, keys):
                self.keys = iter(keys)

            def getmaxyx(self):
                return (24, 80)

            def erase(self):
                pass

            def addnstr(self, *args):
                pass

            def refresh(self):
                pass

            def getch(self):
                return next(self.keys)

        outputs = {"A": dict(mode="1920x1080@60", scale=1, transform=0, x=0, y=0)}
        m.layout_editor(Window([m.curses.KEY_RIGHT, 27]), outputs, ["A"], 0)
        self.assertEqual(outputs["A"]["x"], 0)
        m.layout_editor(Window([m.curses.KEY_RIGHT, 10]), outputs, ["A"], 0)
        self.assertEqual(outputs["A"]["x"], 10)
        m.layout_editor(Window([ord("l"), ord("k"), 10]), outputs, ["A"], 0)
        self.assertEqual((outputs["A"]["x"], outputs["A"]["y"]), (11, -1))
        m.layout_editor(Window([ord("z")] * 8 + [ord("l"), 10]), outputs, ["A"], 0)
        self.assertEqual(outputs["A"]["x"], 12)


if __name__ == "__main__":
    unittest.main()
