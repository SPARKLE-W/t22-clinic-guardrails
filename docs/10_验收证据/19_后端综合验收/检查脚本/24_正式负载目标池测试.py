import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name('07_正式HTTP负载.py')
SPEC = importlib.util.spec_from_file_location('strict_load_targets', SCRIPT)
strict_load = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(strict_load)


class SustainableTargetPoolTests(unittest.TestCase):
    def test_guard_and_diagnosis_pools_cover_disjoint_halves_of_fixture(self):
        guard = [strict_load.record_target(i, 0) for i in range(strict_load.GUARD_TARGETS)]
        diagnosis = [strict_load.record_target(i, 1) for i in range(strict_load.DIAGNOSIS_TARGETS)]
        guard_ids = {(shop, number) for shop, number, _ in guard}
        diagnosis_ids = {(shop, number) for shop, number, _ in diagnosis}

        self.assertEqual(strict_load.TOTAL_RECORDS // 2, len(guard_ids))
        self.assertEqual(strict_load.TOTAL_RECORDS // 2, len(diagnosis_ids))
        self.assertTrue(guard_ids.isdisjoint(diagnosis_ids))
        self.assertEqual(strict_load.TOTAL_RECORDS, len(guard_ids | diagnosis_ids))
        self.assertTrue(all(doctor == (0 if shop == 0 else number % 2)
                            for shop, number, doctor in guard + diagnosis))

    def test_adoption_pool_gives_each_record_three_distinct_suggestions(self):
        targets = [strict_load.adoption_target(i) for i in range(strict_load.ADOPTION_TARGETS)]
        self.assertEqual(strict_load.ADOPTION_TARGETS,
                         len({(shop, number, suggestion_ref) for shop, number, suggestion_ref, _ in targets}))
        self.assertEqual(3, len({suggestion_ref for shop, number, suggestion_ref, _ in targets
                                 if shop == 0 and number == 0}))
        self.assertEqual(strict_load.TOTAL_RECORDS,
                         len({(shop, number) for shop, number, _, _ in targets}))
        self.assertTrue(all(doctor == (0 if shop == 0 else number % 2)
                            for shop, number, _, doctor in targets))

    def test_target_reservation_skips_a_busy_encounter_without_blocking(self):
        shop, number, doctor, lock = strict_load.reserve_record_target('reservation_test', 1, 0)
        try:
            self.assertEqual((0, 0, 0), (shop, number, doctor))
            with self.assertRaises(RuntimeError):
                strict_load.reserve_record_target('reservation_test_busy', 1, 0)
        finally:
            lock.release()

    def test_adoption_response_advances_current_version(self):
        key = (4, 333)
        self.assertTrue(strict_load.update_current_version(key, {'version_ref': 'new-version'}))
        self.assertEqual('new-version', strict_load.current_version_ref(*key))


if __name__ == '__main__':
    unittest.main()