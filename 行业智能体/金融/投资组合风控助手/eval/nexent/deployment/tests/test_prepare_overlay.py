"""Self-contained preparation safety tests; no platform or evaluation is run."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


PACKAGE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "topic16_overlay_preparer_unit", PACKAGE / "prepare_observation_overlay.py")
PREPARER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARER)


class OverlayPreparationSafety(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.recipe = self.root / "unit-recipe"
        self.recipe.mkdir()
        self.sdk_package = self.root / "unit-input" / "nexent"
        self.input_file = self.sdk_package / "core/agents/unit_only.py"
        self.input_file.parent.mkdir(parents=True)
        self.original = b"# UNIT-ONLY; not platform source\nVALUE = 1\n"
        self.input_file.write_bytes(self.original)
        candidate = self.original.replace(b"VALUE = 1", b"VALUE = 2")
        self.patch_file = self.recipe / "unit.patch"
        self.patch_file.write_bytes(
            b"--- a/sdk/nexent/core/agents/unit_only.py\n"
            b"+++ b/sdk/nexent/core/agents/unit_only.py\n"
            b"@@ -1,2 +1,2 @@\n"
            b" # UNIT-ONLY; not platform source\n"
            b"-VALUE = 1\n+VALUE = 2\n")
        manifest = {
            "target_version": "UNIT-ONLY",
            "upstream_tag_commit": "UNIT-ONLY",
            "patch_file": "unit.patch",
            "patch_sha256": hashlib.sha256(self.patch_file.read_bytes()).hexdigest(),
            "sdk_files": [{
                "path": "sdk/nexent/core/agents/unit_only.py",
                "baseline_sha256": hashlib.sha256(self.original).hexdigest(),
                "candidate_sha256": hashlib.sha256(candidate).hexdigest(),
            }],
            "startup_files": [],
        }
        self.manifest = self.recipe / "observation-manifest.json"
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        self.output = self.root / "unit-output"
        self.overrides = patch.multiple(PREPARER, HERE=self.recipe, MANIFEST=self.manifest)
        self.overrides.start()
        self.addCleanup(self.overrides.stop)

    def test_existing_output_is_refused_without_overwriting_input_or_output(self):
        self.output.mkdir()
        sentinel = self.output / "keep-original"
        sentinel.write_bytes(b"UNIT-ONLY EXISTING OUTPUT")
        with self.assertRaisesRegex(ValueError, "destination already exists"):
            PREPARER.prepare(self.sdk_package, self.output)
        self.assertEqual(self.input_file.read_bytes(), self.original)
        self.assertEqual(sentinel.read_bytes(), b"UNIT-ONLY EXISTING OUTPUT")
        self.assertEqual([path.name for path in self.output.iterdir()], [sentinel.name])

    def test_wrong_baseline_is_refused_without_creating_overlay(self):
        changed = self.original.replace(b"VALUE = 1", b"VALUE = 99")
        self.input_file.write_bytes(changed)
        with self.assertRaisesRegex(ValueError, "differs from the clean"):
            PREPARER.prepare(self.sdk_package, self.output)
        self.assertEqual(self.input_file.read_bytes(), changed)
        self.assertFalse(self.output.exists())

    def test_tampered_patch_is_refused_before_any_output_or_input_write(self):
        changed = self.patch_file.read_bytes().replace(b"+VALUE = 2", b"+VALUE = 3")
        self.patch_file.write_bytes(changed)
        with self.assertRaisesRegex(ValueError, "patch SHA256"):
            PREPARER.prepare(self.sdk_package, self.output)
        self.assertEqual(self.patch_file.read_bytes(), changed)
        self.assertEqual(self.input_file.read_bytes(), self.original)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
