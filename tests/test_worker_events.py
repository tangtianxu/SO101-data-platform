"""Completion reports may contain a task 'kind'; no device imports or IO."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import worker


class WorkerEventTests(unittest.TestCase):
    def test_completed_report_keeps_task_kind_without_argument_collision(self):
        for task in ('record','teleop','control','act','replay'):
            with self.subTest(task=task):
                output=io.StringIO()
                with redirect_stdout(output):
                    worker.event('done',kind=task,outcome='complete',episodes_after=30)
                prefix,payload=output.getvalue().split(' ',1)
                self.assertEqual(prefix,'SO101_EVENT')
                self.assertEqual(json.loads(payload),{'type':'done','kind':task,'outcome':'complete','episodes_after':30})

    def test_plain_telemetry_event_is_unchanged(self):
        output=io.StringIO()
        with redirect_stdout(output):worker.event('phase',phase='CONTROL',message='保持')
        self.assertEqual(json.loads(output.getvalue().split(' ',1)[1]),{'type':'phase','phase':'CONTROL','message':'保持'})


if __name__=='__main__':unittest.main()
