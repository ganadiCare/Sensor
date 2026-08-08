import errno
import os
import subprocess
import time
from pathlib import Path
from threading import Lock


class HardwarePWMServo:
    """Linux PWM sysfs를 사용하는 Raspberry Pi 5 하드웨어 PWM 서보."""

    PERIOD_NS = 20_000_000  # 50 Hz

    def __init__(
        self,
        *,
        channel: int,
        gpio_pin: int,
        min_pulse_width: float,
        max_pulse_width: float,
        chip_path: str | None = None,
        configure_pin: bool = True,
    ):
        if channel not in (0, 1):
            raise ValueError("GPIO12/13에는 PWM 채널 0/1만 사용할 수 있습니다.")
        if min_pulse_width >= max_pulse_width:
            raise ValueError("min_pulse_width는 max_pulse_width보다 작아야 합니다.")

        self.channel = channel
        self.gpio_pin = gpio_pin
        self.min_pulse_ns = round(min_pulse_width * 1_000_000_000)
        self.max_pulse_ns = round(max_pulse_width * 1_000_000_000)
        self.chip_path = (
            Path(chip_path) if chip_path is not None else self._find_pwm0_chip()
        )
        self.channel_path = self.chip_path / f"pwm{channel}"
        self._value = None
        self._lock = Lock()

        if configure_pin:
            self._configure_pin_mux()
        self._ensure_exported()
        self._ensure_writable()
        self._configure_period()

    @staticmethod
    def _find_pwm0_chip() -> Path:
        """GPIO12/13이 연결된 RP1 PWM0(물리 주소 0x98000)를 찾는다."""
        for candidate in sorted(Path("/sys/class/pwm").glob("pwmchip*")):
            try:
                device_path = os.path.realpath(candidate / "device")
                uevent = (candidate / "device" / "uevent").read_text()
            except OSError:
                continue
            if "1f00098000.pwm" in device_path or "pwm@98000" in uevent:
                return candidate

        raise RuntimeError(
            "GPIO12/13용 RP1 PWM0 컨트롤러가 비활성 상태입니다. "
            "sudo bash setup_hardware_pwm.sh를 실행한 뒤 안내에 따라 재부팅하세요."
        )

    def _configure_pin_mux(self):
        try:
            subprocess.run(
                ["pinctrl", "set", str(self.gpio_pin), "a0", "pd"],
                check=True,
                capture_output=True,
                text=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            detail = getattr(e, "stderr", None) or str(e)
            raise RuntimeError(
                f"GPIO{self.gpio_pin} 하드웨어 PWM mux 설정 실패: {detail.strip()}"
            ) from e

    def _ensure_exported(self):
        if self.channel_path.exists():
            return

        try:
            (self.chip_path / "export").write_text(str(self.channel))
        except OSError as e:
            if e.errno != errno.EBUSY:
                raise RuntimeError(
                    f"PWM 채널 {self.channel} export 실패. "
                    "먼저 sudo bash setup_hardware_pwm.sh를 실행하세요."
                ) from e

        for _ in range(50):
            if self.channel_path.exists():
                return
            time.sleep(0.01)
        raise RuntimeError(f"PWM 채널 {self.channel} sysfs 생성 시간 초과")

    def _ensure_writable(self):
        required = ("period", "duty_cycle", "enable")
        if all((self.channel_path / name).is_file() for name in required):
            try:
                # 실제 open으로 검사해야 root:root 0644인 sysfs를 정확히 판별한다.
                with (self.channel_path / "enable").open("r+"):
                    pass
                return
            except PermissionError:
                pass

        raise RuntimeError(
            f"PWM 채널 {self.channel} 쓰기 권한이 없습니다. "
            "sudo bash setup_hardware_pwm.sh를 실행하세요."
        )

    def _write(self, name: str, value: int):
        (self.channel_path / name).write_text(str(value))

    def _read_int(self, name: str) -> int:
        return int((self.channel_path / name).read_text().strip())

    def _configure_period(self):
        if self._read_int("enable"):
            self._write("enable", 0)
        self._write("duty_cycle", 0)
        self._write("period", self.PERIOD_NS)

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        with self._lock:
            if value is None:
                if self._read_int("enable"):
                    self._write("enable", 0)
                self._value = None
                return

            value = float(value)
            if not -1.0 <= value <= 1.0:
                raise ValueError("서보 value는 -1.0~1.0 또는 None이어야 합니다.")

            ratio = (value + 1.0) / 2.0
            pulse_ns = round(
                self.min_pulse_ns
                + ratio * (self.max_pulse_ns - self.min_pulse_ns)
            )
            self._write("duty_cycle", pulse_ns)
            if not self._read_int("enable"):
                self._write("enable", 1)
            self._value = value

    def detach(self):
        self.value = None

    def close(self):
        self.detach()
