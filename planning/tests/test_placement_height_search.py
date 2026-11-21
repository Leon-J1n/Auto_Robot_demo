"""Discrete binary placement search over the configured height interval."""
import unittest
from unittest.mock import patch

from demo.interaction.vlm_grasp import VLMGraspResolver, PLACEMENT_HEIGHTS_M
from demo.interaction.object_library import ObjectLibraryGraspResolver, GraspResolutionError


class PlacementHeightSearchTests(unittest.TestCase):
    def search(self, feasible):
        tested = []
        def select(resolved, *args, **kwargs):
            height = resolved['candidates'][0]['placement_height_offset_m']
            tested.append(height)
            if height not in feasible:
                raise GraspResolutionError('infeasible')
            return {'selected': resolved['candidates'][0]}
        resolver = VLMGraspResolver.__new__(VLMGraspResolver)
        resolved = {'candidates': [{'placement_height_offset_m': h} for h in PLACEMENT_HEIGHTS_M]}
        with patch.object(ObjectLibraryGraspResolver, 'select', side_effect=select):
            choice, attempts = resolver._search_place_heights(resolved, None, None, None)
        return choice, attempts, tested

    def test_binary_search_finds_feasible_boundary(self):
        choice, attempts, tested = self.search({.07, .085, .10})
        self.assertEqual(choice['selected']['placement_height_offset_m'], .07)
        self.assertEqual(tested, [.01, .10, .055, .07])
        self.assertTrue(all(a['evaluated'] for a in attempts))
        self.assertEqual(len(attempts), len(tested))

    def test_lowest_feasible_height_wins(self):
        choice, _, tested = self.search(set(PLACEMENT_HEIGHTS_M))
        self.assertEqual(choice['selected']['placement_height_offset_m'], .01)
        self.assertEqual(tested, [.01])

    def test_infeasible_endpoints_stop_without_sequential_scan(self):
        choice, attempts, tested = self.search(set())
        self.assertIsNone(choice)
        self.assertEqual(tested, [.01, .10])
        self.assertTrue(all(a['evaluated'] and not a['success'] for a in attempts))

    def test_configured_range_is_one_to_ten_cm(self):
        self.assertEqual(PLACEMENT_HEIGHTS_M, (.01, .025, .04, .055, .07, .085, .10))


if __name__ == '__main__':
    unittest.main()
