"""Dataset information edits preserve stable identity and every source byte."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import ROBOT_HOME, atomic_json, load_config
import dataset_naming as naming
from dataset_library import list_datasets
from server import Platform


class DatasetInfoTests(unittest.TestCase):
    def setUp(self):
        temp = ROBOT_HOME / "temp"
        temp.mkdir(parents=True, exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(dir=temp)
        self.addCleanup(self.folder.cleanup)
        self.base = Path(self.folder.name)
        self.cfg = {**load_config(), "dataset_home": str(self.base / "datasets")}
        self.root = Path(self.cfg["dataset_home"]) / "original_folder_fixed_position"
        self.root.mkdir(parents=True)
        atomic_json(self.root / "meta/info.json", {
            "codebase_version": "v3.0", "total_episodes": 30, "total_frames": 17185, "fps": 30})
        (self.root / "meta/tasks.parquet").write_bytes(b"original task labels")
        (self.root / "camera.mp4").write_bytes(b"original video bytes")
        self.spec = {**naming.simple_defaults("把娃娃放进盒子"),
                     "started_at": "2026-10-16T19:19+08:00", "description": "固定位置"}
        registry_patch = patch.object(naming, "REGISTRY", self.base / "registry.json")
        registry_patch.start()
        self.addCleanup(registry_patch.stop)
        self.first = naming.store_metadata(self.root, self.spec)
        with patch("server.load_config", return_value=self.cfg):
            self.app = Platform(offline=True)

    def files(self):
        return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.root.rglob("*") if p.is_file()}

    def test_scene_correction_keeps_identity_bytes_review_and_collection_time(self):
        before = self.files()
        original = list_datasets(self.cfg)[0]
        self.app.catalog_cache["test"] = {}
        result = self.app.manage_dataset({
            "action": "label", "root": str(self.root), "confirmed": True,
            "spec": {**self.spec, "description": "位置变化", "note": "操作员更正场景描述"}})
        after = list_datasets(self.cfg)[0]
        self.assertEqual(self.files(), before)
        self.assertEqual(result["naming"]["dataset_id"], self.first["dataset_id"])
        self.assertEqual(result["naming"]["spec"]["review"], "pending")
        self.assertIn("位置变化", after["naming"]["display_name"])
        self.assertEqual(after["root"], original["root"])
        self.assertEqual(after["sort_time"], original["sort_time"])
        self.assertFalse(result["physical_folder_renamed"])
        self.assertFalse(result["data_modified"])
        self.assertEqual(self.app.catalog_cache, {})
        entry = naming.registry()["history"][-1]
        self.assertEqual(entry["before"]["description"], "固定位置")
        self.assertEqual(entry["after"]["description"], "位置变化")

    def test_identity_cannot_be_overwritten_through_the_edit_form(self):
        before = naming.REGISTRY.read_bytes()
        with self.assertRaises(ValueError):
            self.app.manage_dataset({
                "action": "label", "root": str(self.root), "confirmed": True,
                "spec": {**self.spec, "dataset_id": "another-dataset"}})
        self.assertEqual(naming.REGISTRY.read_bytes(), before)

    def test_training_approval_remains_explicit_and_does_not_change_source(self):
        before = self.files()
        request = {"action": "label", "root": str(self.root), "confirmed": True,
                   "spec": {**self.spec, "review": "train"}}
        with self.assertRaises(ValueError):
            self.app.manage_dataset(request)
        request["review_confirmed"] = True
        result = self.app.manage_dataset(request)
        self.assertEqual(result["naming"]["dataset_id"], self.first["dataset_id"])
        self.assertEqual(result["naming"]["spec"]["review"], "train")
        self.assertEqual(self.files(), before)


if __name__ == "__main__":
    unittest.main()
