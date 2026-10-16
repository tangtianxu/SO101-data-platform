"""Source identity and immutable asset contracts, not Windows UI automation."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import HERE
from app_identity import Deployment,ENTRY


class IdentityTests(unittest.TestCase):
    def test_same_sources_have_same_build_but_unique_runtime_instances(self):
        one=Deployment();two=Deployment()
        self.assertEqual(one.build,two.build);self.assertNotEqual(one.instance,two.instance)
        self.assertEqual(one.identity()['formal_entry'],str(ENTRY))
        self.assertEqual(one.identity()['state_dir'],str(HERE/'state'))
        self.assertFalse(one.changed(force=True))

    def test_assets_are_pinned_and_source_change_detected_without_editing_real_project(self):
        with tempfile.TemporaryDirectory(dir=HERE/'state/tests') as folder:
            root=Path(folder);assets=root/'dist';assets.mkdir()
            # Patch the source list and read operation; no ad-hoc source files are written.
            fixture=HERE/'dist/app.js'
            with patch('app_identity.source_files',return_value=[fixture]):
                deployment=Deployment(root)
                with patch.object(Path,'stat',side_effect=OSError('changed')):
                    self.assertTrue(deployment.changed(force=True))
        self.assertIn('joint_control.js',Deployment().assets)

    def test_new_motion_rejected_after_source_update_but_stop_remains(self):
        from server import Platform
        from unittest.mock import Mock
        app=Platform(offline=False)
        with patch.object(app.deployment,'changed',return_value=True),patch('server.subprocess.Popen') as spawn:
            with self.assertRaises(RuntimeError):app.start({'kind':'control'})
            spawn.assert_not_called()
            app.proc=Mock();app.proc.poll.return_value=None;app.job={'id':'current'}
            app.command({'command':'stop','job_id':'current'})
            app.proc.stdin.write.assert_called_once()


if __name__=='__main__':unittest.main(verbosity=2)
