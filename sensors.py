"""Read-only STS3215 feedback adapter; called only by the existing bus owner thread.

No calibrated contact force/torque is invented. Raw register samples carry their
own times; the 30 Hz state/action schema remains exactly six dimensional.
"""
import time

from common import NAMES

REGISTERS = {"torque_enabled": (40, 1), "position_raw": (56, 2), "velocity_raw": (58, 2),
             "load_raw": (60, 2), "voltage_raw": (62, 1), "temperature": (63, 1),
             "status": (65, 1), "moving": (66, 1), "current_raw": (69, 2)}
LABELS = {"torque_enabled": ("力矩开关（不是力矩大小）", "0/1"), "position_raw": ("位置寄存器", "raw"),
          "velocity_raw": ("速度寄存器（含方向位）", "raw"), "load_raw": ("负载 / PWM 输出（非实测扭矩）", "raw"),
          "voltage_raw": ("电压寄存器", "raw"), "temperature": ("温度", "°C"),
          "status": ("舵机报警位", "bits"), "moving": ("运动标志", "0/1"), "current_raw": ("电流寄存器", "raw")}


def features():
    result = {"observation.sensors." + key: {"dtype": "float32", "shape": (6,), "names": list(NAMES)}
              for key in REGISTERS}
    for key in ("feedback_perf_s", "feedback_age_s", "read_duration_s"):
        result["observation.sensors." + key] = {"dtype": "float64", "shape": (1,), "names": [key]}
    return result


class FeedbackSampler:
    def __init__(self, bus, hz=10, clock=time.perf_counter):
        from scservo_sdk import GroupSyncRead
        if any(m.model != "sts3215" for m in bus.motors.values()):
            raise ValueError("扩展反馈只支持已核对寄存器表的 STS3215")
        self.bus, self.hz, self.clock = bus, hz, clock
        self.reader = GroupSyncRead(bus.port_handler, bus.packet_handler, 40, 31)
        self.ids = {n: bus.motors[n].id for n in NAMES}
        for motor_id in self.ids.values():
            if not self.reader.addParam(motor_id):
                raise ValueError("反馈采样的舵机 ID 重复")
        self.latest = None

    def sample(self, force=False):
        now = self.clock()
        if not force and self.latest and now-self.latest["perf_time"] < 1/self.hz:
            return self.latest
        # One SDK group read instead of one transaction per field; never a second COM owner.
        result = self.reader.txRxPacket()
        if result != 0:
            raise ConnectionError("扩展反馈通信失败：" + self.bus.packet_handler.getTxRxResult(result))
        if any(len(self.reader.data_dict.get(i, [])) != 31 for i in self.ids.values()):
            raise ConnectionError("扩展反馈数据长度不足；不以 0 填补")
        health = {key: {n: self.reader.getData(i, address, length) for n, i in self.ids.items()}
                  for key, (address, length) in REGISTERS.items()}
        self.latest = {"perf_time": self.clock(), "read_started_perf": now, "health": health}
        return self.latest

    def frame(self):
        import numpy as np
        sample = self.sample()
        health = sample["health"]
        result = {"observation.sensors."+key: np.asarray([health[key][n] for n in NAMES], dtype="float32")
                  for key in REGISTERS}
        for key, value in {"feedback_perf_s": sample["perf_time"],
                           "feedback_age_s": max(0., self.clock()-sample["perf_time"]),
                           "read_duration_s": sample["perf_time"]-sample["read_started_perf"]}.items():
            result["observation.sensors."+key] = np.asarray([value], dtype="float64")
        return result
