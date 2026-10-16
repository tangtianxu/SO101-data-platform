"""Parse source and inspect tracked publication files; never import device modules."""
import ast
import json
from pathlib import Path
import re
import subprocess

root = Path(__file__).resolve().parents[1]
try:
    paths = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True, capture_output=True,
    ).stdout.decode("utf-8").split("\0")
    files = sorted({root / name for name in paths if name and (root/name).is_file()})
except (OSError, subprocess.CalledProcessError):
    files = [p for p in root.rglob("*") if p.is_file() and not set(p.relative_to(root).parts)&{'.git','.venv-ui','workspace','state','__pycache__'}]
python_files=[p for p in files if p.suffix=='.py']
json_files=[p for p in files if p.suffix=='.json']
for path in python_files:
    ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path.relative_to(root)))
for path in json_files:
    json.loads(path.read_text(encoding="utf-8-sig"))
for path in files:
    relative = path.relative_to(root)
    assert not any(part in {"state", "datasets", "data_feedback", "calibration", "logs"} for part in relative.parts), relative
    assert path.suffix not in {".mp4", ".avi", ".rrd", ".parquet", ".safetensors", ".pth", ".pt", ".pem", ".key", ".log"}, relative
    assert path.stat().st_size < 5 * 1024 * 1024, relative
    if path.suffix not in {".png", ".ico"}:
        content = path.read_text(encoding="utf-8-sig")
        assert not re.search(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}", content), relative
        assert not re.search(r"hf_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9_-]{24,}", content), relative
        assert not re.search(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", content), relative
print(json.dumps({
    "python_syntax_files": len(python_files),
    "json_files": len(json_files),
    "publication_files_checked": len(files),
    "result": "passed",
    "hardware_accessed": False,
    "scope": "syntax and publication hygiene only; not runtime or hardware acceptance",
}, ensure_ascii=False))
