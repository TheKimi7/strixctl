"""Sysfs backends for ASUS ROG laptops (asus_wmi / asus_custom_fan_curve).

Every backend is optional: probe with `.available` before touching it, and with
`.writable` before offering a control.  Nothing here needs root at runtime once
udev/99-strixctl.rules has handed the attributes to the `users` group.
"""

import glob
import os

FAN_POINTS = 8
FAN_NAMES = {"cpu": "pwm1", "gpu": "pwm2"}


def _read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _write(path, value):
    with open(path, "w") as fh:
        fh.write(str(value))


def _writable(path):
    return path is not None and os.access(path, os.W_OK)


class Battery:
    """Charge-stop threshold.  ASUS exposes only the end threshold, no start."""

    def __init__(self):
        hits = sorted(glob.glob(
            "/sys/class/power_supply/BAT*/charge_control_end_threshold"))
        self.path = hits[0] if hits else None

    @property
    def available(self):
        return self.path is not None

    @property
    def writable(self):
        return _writable(self.path)

    @property
    def limit(self):
        raw = _read(self.path) if self.path else None
        return int(raw) if raw and raw.isdigit() else None

    @limit.setter
    def limit(self, percent):
        percent = int(percent)
        if not 20 <= percent <= 100:
            raise ValueError(f"charge limit {percent} outside 20-100")
        _write(self.path, percent)


class PlatformProfile:
    """quiet / balanced / performance, the same knob as throttle_thermal_policy."""

    PATH = "/sys/firmware/acpi/platform_profile"
    CHOICES_PATH = "/sys/firmware/acpi/platform_profile_choices"

    @property
    def available(self):
        return os.path.exists(self.PATH)

    @property
    def writable(self):
        return _writable(self.PATH)

    @property
    def choices(self):
        raw = _read(self.CHOICES_PATH)
        return raw.split() if raw else []

    @property
    def profile(self):
        return _read(self.PATH)

    @profile.setter
    def profile(self, name):
        if name not in self.choices:
            raise ValueError(f"{name!r} not in {self.choices}")
        _write(self.PATH, name)


class FanCurve:
    """One 8-point curve on asus_custom_fan_curve.

    pwm{N}_enable is 2 for the firmware's own curve and 1 once a custom curve is
    armed.  Points are (temp_celsius, pwm_0_255) and must not slope downward.
    """

    #: below this PWM at/above this temperature we refuse to arm a curve
    SAFETY_TEMP = 85
    SAFETY_PWM = 128

    def __init__(self, root, fan="cpu"):
        self.root = root
        self.prefix = FAN_NAMES[fan]
        self.fan = fan

    def _pt(self, index, kind):
        return f"{self.root}/{self.prefix}_auto_point{index}_{kind}"

    @property
    def enable_path(self):
        return f"{self.root}/{self.prefix}_enable"

    @property
    def available(self):
        return os.path.exists(self._pt(1, "temp"))

    @property
    def writable(self):
        return _writable(self._pt(1, "temp")) and _writable(self.enable_path)

    @property
    def armed(self):
        return _read(self.enable_path) == "1"

    def read(self):
        points = []
        for i in range(1, FAN_POINTS + 1):
            temp = _read(self._pt(i, "temp"))
            pwm = _read(self._pt(i, "pwm"))
            if temp is None or pwm is None:
                break
            points.append((int(temp), int(pwm)))
        return points

    @staticmethod
    def validate(points):
        """Raise ValueError on a curve that is malformed or thermally unsafe."""
        if len(points) != FAN_POINTS:
            raise ValueError(f"need exactly {FAN_POINTS} points, got {len(points)}")
        last_t = last_p = -1
        for temp, pwm in points:
            if not 0 <= temp <= 110:
                raise ValueError(f"temperature {temp} outside 0-110")
            if not 0 <= pwm <= 255:
                raise ValueError(f"pwm {pwm} outside 0-255")
            if temp < last_t:
                raise ValueError("temperatures must not decrease")
            if pwm < last_p:
                raise ValueError("pwm must not decrease")
            last_t, last_p = temp, pwm
        # The last point governs every temperature above it, so that PWM -- not
        # just points that happen to sit above SAFETY_TEMP -- is the one that
        # decides whether the machine can still cool itself. Stock ASUS curves
        # top out at 80C, so checking only t >= SAFETY_TEMP would never fire.
        ceiling = max(p for _, p in points)
        if ceiling < FanCurve.SAFETY_PWM:
            raise ValueError(
                f"curve never exceeds {ceiling}/255, below the "
                f"{FanCurve.SAFETY_PWM}/255 floor; refusing to arm it")
        hot = [p for t, p in points if t >= FanCurve.SAFETY_TEMP]
        if hot and max(hot) < FanCurve.SAFETY_PWM:
            raise ValueError(
                f"curve asks for under {FanCurve.SAFETY_PWM}/255 at "
                f"{FanCurve.SAFETY_TEMP}C or above; refusing to arm it")

    def apply(self, points):
        self.validate(points)
        for i, (temp, pwm) in enumerate(points, start=1):
            _write(self._pt(i, "temp"), temp)
            _write(self._pt(i, "pwm"), pwm)
        _write(self.enable_path, 1)

    def reset(self):
        """Hand the fan back to the firmware curve."""
        _write(self.enable_path, 2)


class Fans:
    """Both fans, plus discovery of the asus_custom_fan_curve hwmon node."""

    def __init__(self):
        self.root = None
        for path in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            if _read(f"{path}/name") == "asus_custom_fan_curve":
                self.root = path
                break
        self.cpu = FanCurve(self.root, "cpu") if self.root else None
        self.gpu = FanCurve(self.root, "gpu") if self.root else None

    @property
    def available(self):
        return self.root is not None and self.cpu.available

    @property
    def writable(self):
        return self.available and self.cpu.writable and self.gpu.writable

    def curves(self):
        return {"cpu": self.cpu.read(), "gpu": self.gpu.read()}

    def reset(self):
        self.cpu.reset()
        self.gpu.reset()


class KeyboardBacklight:
    """Brightness only.  This chassis exposes no colour attribute -- see aura.py."""

    PATH = "/sys/class/leds/asus::kbd_backlight"

    @property
    def available(self):
        return os.path.exists(f"{self.PATH}/brightness")

    @property
    def writable(self):
        return _writable(f"{self.PATH}/brightness")

    @property
    def max_brightness(self):
        raw = _read(f"{self.PATH}/max_brightness")
        return int(raw) if raw else 0

    @property
    def brightness(self):
        raw = _read(f"{self.PATH}/brightness")
        return int(raw) if raw else 0

    @brightness.setter
    def brightness(self, level):
        level = max(0, min(int(level), self.max_brightness))
        _write(f"{self.PATH}/brightness", level)


def board_name():
    return _read("/sys/class/dmi/id/board_name") or "unknown"
