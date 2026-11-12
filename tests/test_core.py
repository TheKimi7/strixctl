"""Tests for the parts that need no hardware.

Run from the checkout:  python3 -m unittest discover -s tests
"""

import copy
import os
import re
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strixctl import aura, config as cfg, hw, theme  # noqa: E402


class Percent(unittest.TestCase):
    def test_firmware_receives_the_percent_asked_for(self):
        # asus-wmi hands the firmware 100 * pwm / 255, truncated.
        for percent in range(101):
            self.assertEqual(100 * hw.pct_to_pwm(percent) // 255, percent)

    def test_own_writes_read_back_unchanged(self):
        for percent in range(101):
            self.assertEqual(hw.pwm_to_pct(hw.pct_to_pwm(percent)), percent)

    def test_firmware_defaults_read_back_unchanged(self):
        # Defaults reach sysfs as 255 * percent / 100, truncated.
        for percent in range(101):
            self.assertEqual(hw.pwm_to_pct(255 * percent // 100), percent)

    def test_out_of_range_is_clamped(self):
        self.assertEqual(hw.pct_to_pwm(140), 255)
        self.assertEqual(hw.pct_to_pwm(-3), 0)
        self.assertEqual(hw.pwm_to_pct(999), 100)


GOOD = [(35, 3), (52, 12), (58, 23), (64, 36), (70, 50), (76, 67), (80, 83), (90, 100)]


class Validate(unittest.TestCase):
    def test_accepts_a_sane_curve(self):
        hw.FanCurve.validate(GOOD)

    def test_last_point_must_be_full_speed(self):
        with self.assertRaisesRegex(ValueError, "100%"):
            hw.FanCurve.validate(GOOD[:-1] + [(90, 99)])

    def test_rejects_wrong_length_and_slopes(self):
        with self.assertRaises(ValueError):
            hw.FanCurve.validate(GOOD[:-1])
        with self.assertRaisesRegex(ValueError, "speed must not decrease"):
            hw.FanCurve.validate([(35, 20), (52, 12)] + GOOD[2:])
        with self.assertRaisesRegex(ValueError, "temperatures"):
            hw.FanCurve.validate([(60, 3), (52, 12)] + GOOD[2:])
        with self.assertRaisesRegex(ValueError, "outside 0-100"):
            hw.FanCurve.validate(GOOD[:-1] + [(90, 101)])


class Ceiling(unittest.TestCase):
    def test_full_speed_curve_is_untouched(self):
        self.assertEqual(hw.with_full_ceiling(GOOD), GOOD)

    def test_duplicate_last_point_becomes_a_ramp(self):
        fixed = hw.with_full_ceiling(GOOD[:-1] + [(80, 83)])
        self.assertEqual(fixed[-2:], [(80, 83), (90, 100)])
        hw.FanCurve.validate(fixed)

    def test_distinct_last_point_keeps_its_temperature(self):
        fixed = hw.with_full_ceiling(GOOD[:-1] + [(85, 90)])
        self.assertEqual(fixed[-1], (85, 100))


V1 = {
    "active_profile": "Silent",
    "profiles": {
        "Silent": {
            "platform_profile": "quiet",
            "fan_mode": "curve",
            "kbd_brightness": 1,
            "fan_cpu": [[35, 5], [52, 22], [58, 44], [64, 68],
                        [70, 95], [76, 128], [80, 158], [80, 158]],
            "fan_gpu": [[35, 49], [35, 49], [44, 152], [51, 152],
                        [56, 152], [63, 208], [70, 255], [70, 255]],
            "aura": {"mode": "Breathing", "speed": 40, "colors": [[1, 2, 3]] * 4},
        },
        "Performance": {
            "platform_profile": "performance",
            "fan_mode": "auto",
            "fan_cpu": [[40, 255]] * 8,
            "fan_gpu": [[40, 255]] * 8,
        },
    },
}


def _v2():
    """The same file after the v1 -> v2 step, as an existing install holds it."""
    data = copy.deepcopy(V1)
    cfg._to_v2(data)
    data["version"] = 2
    return data


class Migrate(unittest.TestCase):
    def test_v1_lands_on_v3_in_percent_with_a_full_ceiling(self):
        data = copy.deepcopy(V1)
        self.assertEqual(cfg.migrate(data), 1)
        self.assertEqual(data["version"], cfg.VERSION)
        self.assertEqual(data["fans"]["cpu"][:2], [[35, 2], [52, 9]])
        self.assertEqual(data["fans"]["cpu"][-2:], [[80, 62], [90, 100]])
        for fan in ("cpu", "gpu"):
            hw.FanCurve.validate(cfg.points(data["fans"][fan]))

    def test_the_active_profile_is_the_one_that_survives(self):
        data = _v2()
        self.assertEqual(cfg.migrate(data), 2)
        self.assertEqual(data["power"], "quiet")
        self.assertEqual(data["fans"]["mode"], "curve")
        self.assertEqual(data["aura"]["mode"], "Breathing")
        self.assertEqual(data["aura"]["speed"], 40)

    def test_profiles_are_gone_for_good(self):
        data = _v2()
        cfg.migrate(data)
        self.assertNotIn("profiles", data)
        self.assertNotIn("active_profile", data)
        # _merge must not put them back from the defaults either.
        self.assertNotIn("profiles", cfg._merge(cfg.DEFAULTS, data))

    def test_a_missing_active_profile_still_yields_settings(self):
        data = _v2()
        data["active_profile"] = "Gone"
        cfg.migrate(data)
        self.assertIn(data["power"], ("quiet", "performance"))
        for fan in ("cpu", "gpu"):
            hw.FanCurve.validate(cfg.points(data["fans"][fan]))

    def test_migrating_twice_changes_nothing(self):
        data = copy.deepcopy(V1)
        cfg.migrate(data)
        again = copy.deepcopy(data)
        self.assertEqual(cfg.migrate(again), 0)
        self.assertEqual(again, data)

    def test_shipped_defaults_are_valid(self):
        self.assertEqual(cfg.migrate(copy.deepcopy(cfg.DEFAULTS)), 0)
        self.assertNotIn("profiles", cfg.DEFAULTS)
        for fan in ("cpu", "gpu"):
            hw.FanCurve.validate(cfg.points(cfg.DEFAULTS["fans"][fan]))


LISTING = """\
0: ASUS Aura Keyboard
  Type:           Keyboard
  Location:       HID: /dev/hidraw1
  Modes: [Static] Breathing 'Color Cycle'
  Zones: Keyboard
  LEDs: 'Keyboard 1' 'Keyboard 2' 'Keyboard 3' 'Keyboard 4'
5: G533ZM
  Type:           Laptop
  Location:       HID: /dev/hidraw3
  Modes: [Direct] Static Breathing Flashing 'Spectrum Cycle' 'Rainbow Wave' Off
  Zones: Keyboard
  LEDs: 'Key: Escape' 'Key: F1' 'Key: F2' 'Key: F3' 'Key: F4' 'Key: F5'
"""


class OpenRGB(unittest.TestCase):
    def setUp(self):
        self.backend = aura.Aura(settings={"binary": sys.executable})
        self.backend._devices = aura.parse_devices(LISTING)
        self.sent = []

        def fake_run(args):
            self.sent.append(args)
            return types.SimpleNamespace(stdout="", stderr="", returncode=0)

        self.backend._run = fake_run
        self.backend.desktop_brightness = lambda: 67

    def test_listing_is_parsed(self):
        zoned, perkey = self.backend._devices
        self.assertTrue(zoned.is_zoned and not zoned.is_perkey)
        self.assertTrue(perkey.is_perkey and perkey.serves("rainbow wave"))
        self.assertEqual(zoned.modes, ["Static", "Breathing", "Color Cycle"])

    def test_zoned_write_carries_four_colours_and_the_desktop_brightness(self):
        self.backend.set_colors([[255, 0, 0]] * 4, mode="Static")
        args = self.sent[0]
        self.assertEqual(args[:4], ["--device", "0", "--mode", "Static"])
        self.assertEqual(args[args.index("--brightness") + 1], "67")
        self.assertEqual(args[args.index("--color") + 1],
                         "ff0000,ff0000,ff0000,ff0000")

    def test_perkey_effect_goes_to_the_perkey_entry_without_colour(self):
        self.backend.set_colors([[255, 0, 0]] * 4, mode="Rainbow Wave", speed=40)
        args = self.sent[0]
        self.assertEqual(args[:2], ["--device", "5"])
        self.assertNotIn("--color", args)
        self.assertEqual(args[args.index("--speed") + 1], "40")
        self.assertIn("--brightness", args)

    def test_no_brightness_flag_when_the_level_is_unreadable(self):
        self.backend.desktop_brightness = lambda: None
        self.backend.set_colors([[0, 0, 255]] * 4, mode="Static")
        self.assertNotIn("--brightness", self.sent[0])

    def test_error_line_fails_despite_exit_zero(self):
        self.backend._run = lambda args: types.SimpleNamespace(
            stdout="Error: Mode 'Nope' not found\n", stderr="", returncode=0)
        with self.assertRaisesRegex(OSError, "Nope"):
            self.backend.set_colors([[0, 0, 255]] * 4, mode="Static")


class Look(unittest.TestCase):
    """The window should look like the desktop, not like a brand."""

    def test_the_greys_can_be_switched_off(self):
        self.assertTrue(theme.wanted({}))
        self.assertTrue(theme.wanted({"theme": "monochrome"}))
        # The old spelling means the default, so a config written by an
        # earlier version keeps working without a migration.
        self.assertTrue(theme.wanted({"theme": "neopop"}))
        self.assertFalse(theme.wanted({"theme": "system"}))
        self.assertFalse(theme.wanted({"theme": "System"}))

    def test_nothing_is_hard_coded_to_a_colour(self):
        # Every colour is mixed from the theme's own foreground and
        # background, which is what keeps this right on a light theme as
        # well as a dark one. A hex value anywhere would break that.
        css = theme.stylesheet().decode()
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}", css), [])
        self.assertIn("@theme_fg_color", css)
        self.assertIn("@theme_bg_color", css)

    def test_switching_the_greys_off_keeps_the_sizes(self):
        plain = theme.stylesheet({"theme": "system"}).decode()
        self.assertIn(".strix-tile-value", plain)
        self.assertNotIn("theme_selected_bg_color", plain)
        self.assertIn("theme_selected_bg_color",
                      theme.stylesheet({}).decode())

    def test_the_two_curves_are_told_apart_without_colour(self):
        self.assertEqual(theme.CURVE_DASH["cpu"], [])
        self.assertTrue(theme.CURVE_DASH["gpu"])


try:
    from strixctl import gui
except (ImportError, ValueError):   # no GTK bindings here; the rest still runs
    gui = None


@unittest.skipIf(gui is None, "GTK bindings not installed")
class CurveEditing(unittest.TestCase):
    def test_a_point_is_held_between_its_neighbours(self):
        self.assertEqual(gui.clamp_point(GOOD, 1, 5, 100), (35, 23))
        self.assertEqual(gui.clamp_point(GOOD, 1, 99, 0), (58, 3))

    def test_the_last_point_only_moves_sideways(self):
        self.assertEqual(gui.clamp_point(GOOD, 7, 85, 40), (85, 100))
        self.assertEqual(gui.clamp_point(GOOD, 7, 200, 40), (110, 100))

    def test_every_clamped_drag_still_validates(self):
        for index in range(len(GOOD)):
            for temp, percent in ((0, 0), (0, 100), (110, 0), (110, 100)):
                points = list(GOOD)
                points[index] = gui.clamp_point(points, index, temp, percent)
                hw.FanCurve.validate(points)

    def test_speed_is_read_off_the_segments(self):
        self.assertEqual(gui.speed_at(GOOD, 20), 3)
        self.assertEqual(gui.speed_at(GOOD, 67), 43)
        self.assertEqual(gui.speed_at(GOOD, 105), 100)


if __name__ == "__main__":
    unittest.main()
