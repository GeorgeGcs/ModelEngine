import copy
import json
from pathlib import Path
import unittest
from make_plan import make_plan

class PlanTests(unittest.TestCase):
    def setUp(self):
        self.manifest=json.loads((Path(__file__).resolve().parents[3]/'eval/nexent/task_manifest.json').read_text(encoding='utf-8'))

    def test_24_hours_144_slots_288_cells_exact_frozen_inputs(self):
        before=copy.deepcopy(self.manifest)
        plan=make_plan(self.manifest,'2026-11-15T08:00:00+08:00','example')
        self.assertEqual(len(plan['slots']),144)
        self.assertEqual(sum(len(s['tool_arguments']['portfolio']['holdings']) for s in plan['slots']),288)
        self.assertEqual(plan['end'],'2026-11-16T08:00:00+08:00')
        self.assertEqual(self.manifest,before)
        originals=[t['tool_arguments'] for t in self.manifest['tasks']]
        for slot in plan['slots']:self.assertIn(slot['tool_arguments'],originals)

    def test_naive_or_wrong_offset_start_rejected(self):
        for start in ['2026-11-15T08:00:00','2026-11-15T08:00:00+00:00']:
            with self.assertRaises(ValueError):make_plan(self.manifest,start,'example')

    def test_ambiguous_task_rejected(self):
        self.manifest['tasks'][1]=self.manifest['tasks'][0]
        with self.assertRaises(ValueError):make_plan(self.manifest,'2026-11-15T08:00:00+08:00','example')
