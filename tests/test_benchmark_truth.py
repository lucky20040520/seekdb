"""Fault injections ensure benchmark validation cannot accept corrupt results."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
from benchmark import reference_records, verify_records, verify_answer


class BenchmarkTruth(unittest.TestCase):
    def test_content_corruption_missing_and_duplicate_records_are_rejected(self):
        reference = reference_records(12, 3)
        verify_records(reversed(reference), reference)
        corrupt = copy.deepcopy(reference)
        corrupt[5][2][1] += 1
        for rows in (corrupt, reference[:-1], reference + [reference[0]]):
            with self.assertRaises(AssertionError):
                verify_records(rows, reference)

    def test_wrong_distance_duplicate_and_wrong_tie_order_are_rejected(self):
        records = [[2,0,[1.0,0.0]], [1,0,[0.0,1.0]], [3,0,[0.0,2.0]]]
        verify_answer([[1,1.0],[2,1.0],[3,2.0]], records, [0,0])
        for answer in ([[2,1.0],[1,1.0],[3,2.0]], [[1,1.0],[1,1.0],[3,2.0]],
                       [[1,1.0],[2,1.0]], [[1,1.0],[2,1.0],[3,4.0]]):
            with self.assertRaises(AssertionError):
                verify_answer(answer, records, [0,0])
