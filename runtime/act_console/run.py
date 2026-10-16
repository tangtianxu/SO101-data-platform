"""SO101 real ACT project entry. Opening the GUI does not access hardware."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

# -s must be set before Python starts; environment changes alone are too late
# to remove C-drive user site packages already added to sys.path.
if not sys.flags.no_user_site:
    raise SystemExit("Please use launch.cmd or E:\\robot\\envs\\lerobot\\python.exe -s -B run.py")

for key, value in {"PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                   "HF_HOME": "E:/robot/hf-cache", "TORCH_HOME": "E:/robot/torch-cache",
                   "HF_HUB_OFFLINE": "1", "TEMP": "E:/robot/temp", "TMP": "E:/robot/temp",
                   "TRITON_CACHE_DIR": "E:/robot/cache/triton",
                   "TORCHINDUCTOR_CACHE_DIR": "E:/robot/cache/torchinductor",
                   "CUDA_CACHE_PATH": "E:/robot/cuda-cache"}.items():
    os.environ[key] = value

from session import load_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=Path(__file__).with_name("settings.json"))
    parser.add_argument("--check", action="store_true", help="Environment and file check only; no serial/camera access")
    parser.add_argument("--plan", action="store_true", help="Print the approved settings; no hardware access")
    parser.add_argument("--simulate-ui", action="store_true", help="Offline UI demo using synthetic pictures; no robot or camera")
    args = parser.parse_args()
    cfg = load_settings(args.settings)
    if Path(sys.executable).resolve() != Path(cfg["python"]).resolve():
        raise SystemExit(f"Wrong interpreter: {sys.executable}; use {cfg['python']}")
    sys.path.insert(0, cfg["shared_scripts"])
    if args.plan:
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
        return
    if args.check:
        import tkinter
        import PIL
        import cv2
        import torch
        import lerobot
        import so101_guarded_real_act
        print(json.dumps({"environment_check": "passed", "python": sys.executable,
                          "user_site_disabled": bool(sys.flags.no_user_site),
                          "opencv": cv2.__file__, "torch": torch.__version__,
                          "cuda_available": torch.cuda.is_available(), "tk": tkinter.TkVersion,
                          "pillow": PIL.__version__, "lerobot": lerobot.__file__,
                          "hardware_accessed": False, "writes": 0}, ensure_ascii=False, indent=2))
        if not torch.cuda.is_available():
            raise SystemExit(1)
        return
    from dashboard import Dashboard
    Dashboard(cfg, simulate=args.simulate_ui).run()


if __name__ == "__main__":
    main()
