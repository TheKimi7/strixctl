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


def pct_to_pwm(percent):
    """Percent to the kernel's 0-255, rounded up.

    The firmware works in percent; asus-wmi stores 0-255 and divides back down
    with truncation on the way out (100 * pwm / 255). Rounding up here is what
    makes the firmware receive exactly the percent that was asked for.
    """
    percent = max(0, min(100, int(percent)))
    return -(-percent * 255 // 100)


def pwm_to_pct(pwm):
    """The kernel's 0-255 back to percent, rounded to nearest.

    Nearest, because the readback is one of two things: a firmware default,
    which the kernel truncated (255 * pct / 100), or a value written through
    pct_to_pwm, which was rounded up. Nearest recovers the percent from both.
    """
    pwm = max(0, min(255, int(pwm)))
    return (pwm * 200 + 255) // 510


def with_full_ceiling(points):
    """Return `points` with the last one raised to 100%.

    Firmware curves do not all end at full speed (balanced stops at 83%), and
    validate() insists that a custom curve does. When the last two points
    share a temperature the last is moved up 10 C, so the result ramps to full
    speed instead of stepping to it.
    """
    points = [tuple(point) for point in points]
    if not points or points[-1][1] >= 100:
        return points
    temp = points[-1][0]
    if len(points) > 1 and points[-2][0] >= temp:
        temp = min(points[-2][0] + 10, 95)
    points[-1] = (temp, 100)
    return points


class FanCurve:
    """One 8-point curve on asus_custom_fan_curve.

    Points are (temp_celsius, percent). pwm{N}_enable is 1 once a custom curve
    is armed and 2 for the firmware's own curve; writing 3 also reloads the
    firmware defaults for the current platform profile into the readback,
    which a plain 2 never does.
    """

    #: The last point governs every temperature above it, so it has to be full
    #: speed: whatever the curve does lower down, the machine can still cool
    #: itself flat out.
    CEILING = 100

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
        """The curve the kernel holds, in percent."""
        points = []
        for i in range(1, FAN_POINTS + 1):
            temp = _read(self._pt(i, "temp"))
            pwm = _read(self._pt(i, "pwm"))
            if temp is None or pwm is None:
                break
            points.append((int(temp), pwm_to_pct(pwm)))
        return points

    @staticmethod
    def validate(points):
        """Raise ValueError on a curve that is malformed or thermally unsafe."""
        if len(points) != FAN_POINTS:
            raise ValueError(f"need exactly {FAN_POINTS} points, got {len(points)}")
        last_t = last_p = -1
        for temp, percent in points:
            if not 0 <= temp <= 110:
                raise ValueError(f"temperature {temp} outside 0-110")
            if not 0 <= percent <= 100:
                raise ValueError(f"fan speed {percent}% outside 0-100")
            if temp < last_t:
                raise ValueError("temperatures must not decrease")
            if percent < last_p:
                raise ValueError("fan speed must not decrease")
            last_t, last_p = temp, percent
        if points[-1][1] < FanCurve.CEILING:
            raise ValueError(
                f"the last point is {points[-1][1]}%, and it has to be "
                f"{FanCurve.CEILING}%; refusing to arm a curve that cannot "
                "reach full speed")

    def apply(self, points):
        self.validate(points)
        for i, (temp, percent) in enumerate(points, start=1):
            _write(self._pt(i, "temp"), temp)
            _write(self._pt(i, "pwm"), pct_to_pwm(percent))
        _write(self.enable_path, 1)

    def reset(self):
        """Hand the fan back to the firmware and reload its curve.

        3 rather than 2, so that the readback is the firmware curve for the
        platform profile in force now, not whichever one was fetched at boot.
        """
        try:
            _write(self.enable_path, 3)
        except OSError:
            _write(self.enable_path, 2)


class Fans:
    """Both fans, plus discovery of the asus_custom_fan_curve hwmon node."""

    def __init__(self):
        self.root = _hwmon_named("asus_custom_fan_curve")
        self.cpu = FanCurve(self.root, "cpu") if self.root else None
        self.gpu = FanCurve(self.root, "gpu") if self.root else None

    def __iter__(self):
        return iter((("cpu", self.cpu), ("gpu", self.gpu)))

    @property
    def available(self):
        return self.root is not None and self.cpu.available

    @property
    def writable(self):
        return self.available and self.cpu.writable and self.gpu.writable

    @property
    def armed(self):
        """Per fan: is a custom curve in force."""
        return {name: curve.armed for name, curve in self}

    def curves(self):
        return {"cpu": self.cpu.read(), "gpu": self.gpu.read()}

    def reset(self):
        self.cpu.reset()
        self.gpu.reset()

    def factory_curves(self):
        """The firmware's own curves for the current platform profile.

        The kernel only gives these up by reloading them over the stored
        curve, which disarms a custom one. So a fan that was armed is re-armed
        with what it had; the gap is far shorter than the several seconds a
        fan takes to change speed.
        """
        out = {}
        for name, curve in self:
            held = curve.read() if curve.armed else None
            curve.reset()
            out[name] = curve.read()
            if held is not None:
                curve.apply(held)
        return out


class Sensors:
    """Read-only telemetry: fan speeds and CPU temperature."""

    FAN_LABELS = {"cpu": "cpu_fan", "gpu": "gpu_fan"}

    def __init__(self):
        self.fan_paths = {}
        root = _hwmon_named("asus")
        if root:
            for label_path in sorted(glob.glob(f"{root}/fan*_label")):
                label = _read(label_path)
                for fan, wanted in self.FAN_LABELS.items():
                    if label == wanted:
                        self.fan_paths[fan] = label_path.replace("_label", "_input")
        self.temp_path = None
        for name in ("coretemp", "k10temp", "acpitz"):
            root = _hwmon_named(name)
            if root and os.path.exists(f"{root}/temp1_input"):
                self.temp_path = f"{root}/temp1_input"
                break

    def rpm(self):
        out = {}
        for fan, path in self.fan_paths.items():
            raw = _read(path)
            out[fan] = int(raw) if raw and raw.lstrip("-").isdigit() else None
        return out

    def cpu_temp(self):
        raw = _read(self.temp_path) if self.temp_path else None
        return int(raw) // 1000 if raw and raw.lstrip("-").isdigit() else None


class KeyboardBacklight:
    """The keyboard's brightness level. Read-only, on purpose.

    The desktop owns this knob (Fn keys and the power applet, through UPower),
    and strixctl only reads it so a colour change can be sent at the level the
    desktop already chose. Colour is a separate matter -- see aura.py.
    """

    PATH = "/sys/class/leds/asus::kbd_backlight"

    @property
    def available(self):
        return os.path.exists(f"{self.PATH}/brightness")

    @property
    def max_brightness(self):
        raw = _read(f"{self.PATH}/max_brightness")
        return int(raw) if raw and raw.isdigit() else 0

    @property
    def brightness(self):
        raw = _read(f"{self.PATH}/brightness")
        return int(raw) if raw and raw.isdigit() else 0

    @property
    def percent(self):
        """Brightness as 0-100, or None when there is nothing to read."""
        top = self.max_brightness
        if not self.available or top <= 0:
            return None
        return round(100 * self.brightness / top)


def _hwmon_named(name):
    for path in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        if _read(f"{path}/name") == name:
            return path
    return None


def board_name():
    return _read("/sys/class/dmi/id/board_name") or "unknown"
