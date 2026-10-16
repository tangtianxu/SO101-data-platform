"""Explicit read-only inventory in the selected backend Python; never opens a port."""
import argparse
import importlib.metadata
import importlib.util
import json
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ports", action="store_true")
    args = parser.parse_args()
    if args.ports:
        from serial.tools.list_ports import comports
        value = [{"port": p.device, "name": p.description, "serial": p.serial_number,
                  "vid": p.vid, "pid": p.pid} for p in comports()]
    else:
        modules = ("lerobot", "torch", "cv2", "serial", "scservo_sdk", "av", "pyarrow", "datasets", "pynput")
        value = {"python": sys.executable, "version": sys.version.split()[0],
                 "modules": {name: importlib.util.find_spec(name) is not None for name in modules},
                 "hardware_accessed": False, "gpu_warmed_up": False}
    print(json.dumps(value, ensure_ascii=False))


if __name__ == "__main__":
    main()
