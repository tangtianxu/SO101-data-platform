"""Native chooser contract using a fake dialog; never opens hardware or a real modal."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import HERE,load_config
from desktop import choose_start_pose_file


class StartPoseFileTests(unittest.TestCase):
    def test_select_single_json_from_current_folder(self):
        path=load_config()['reference_start_pose'];window=Mock()
        window.create_file_dialog.return_value=(path,)
        self.assertEqual(choose_start_pose_file(window,10,path,path),path)
        window.create_file_dialog.assert_called_once_with(10,directory=str(Path(path).parent),
            allow_multiple=False,file_types=('任务起点 JSON (*.json)',))

    def test_cancel_does_not_select_or_write(self):
        path=load_config()['reference_start_pose'];window=Mock()
        original=Path(path).read_bytes();window.create_file_dialog.return_value=None
        self.assertIsNone(choose_start_pose_file(window,10,path,path))
        self.assertEqual(Path(path).read_bytes(),original)

    def test_invalid_initial_path_uses_existing_default(self):
        path=load_config()['reference_start_pose'];window=Mock()
        window.create_file_dialog.return_value=()
        choose_start_pose_file(window,10,'C:/not-a-preset.json',path)
        self.assertEqual(window.create_file_dialog.call_args.kwargs['directory'],str(Path(path).parent))

    def test_folder_other_extension_and_c_drive_rejected(self):
        path=load_config()['reference_start_pose'];window=Mock()
        for target in (str(Path(path).parent),str(HERE/'README.md'),'C:/Windows/win.ini'):
            window.create_file_dialog.return_value=(target,)
            with self.assertRaises(ValueError):choose_start_pose_file(window,10,path,path)


if __name__=='__main__':unittest.main(verbosity=2)
