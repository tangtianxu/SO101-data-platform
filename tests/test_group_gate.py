"""Group transition tests with lists/fake clocks, not motor or camera objects."""
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import NAMES
from worker import await_capture_group


class GroupGateTests(unittest.TestCase):
    def session(self):
        robot=Mock();robot.limits=object();robot.get_observation.return_value={n+'.pos':0. for n in NAMES}
        s=SimpleNamespace(robot=robot,leader=Mock(),motion=SimpleNamespace(last_target=[0.]*6),
            recording_start=threading.Event(),start_gate_completed=True,stop=threading.Event(),state=Mock())
        rec=SimpleNamespace(clip_target=lambda *_:(None,[0.]*6,None),catchup_step=lambda *args:args[1],
            await_first_record_start=Mock())
        return s,rec

    def test_later_group_is_rate_limited_and_close_enough_before_return(self):
        s,rec=self.session();s.recording_start.set()
        await_capture_group(s,rec)
        self.assertFalse(s.recording_start.is_set());self.assertFalse(s.start_gate_completed)
        rec.await_first_record_start.assert_called_once()
        self.assertEqual(s.robot.send_action.call_count,3);s.state.assert_called_once_with('GROUP_CATCHUP')
        self.assertTrue(all(set(c.args[0])=={n+'.pos' for n in NAMES} for c in s.robot.send_action.call_args_list))

    def test_stop_before_catchup_never_sends_target(self):
        s,rec=self.session();s.stop.set()
        with self.assertRaises(KeyboardInterrupt):await_capture_group(s,rec)
        s.robot.send_action.assert_not_called()

    def test_stop_during_catchup_cancels_next_group(self):
        s,rec=self.session()
        s.robot.send_action.side_effect=lambda *_:s.stop.set()
        with self.assertRaises(KeyboardInterrupt):await_capture_group(s,rec)
        self.assertEqual(s.robot.send_action.call_count,1)


if __name__=='__main__':unittest.main()
