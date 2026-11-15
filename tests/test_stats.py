"""Statistics use fixture events, never the user's queue."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from smart_build import cli


class StatsTests(unittest.TestCase):
    def summarize(self, waits, since=None):
        events = [dict(event="finished", time=99, waited=w, exit=0) for w in waits]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(cli, "read_events", return_value=events), \
                patch.object(cli.time, "time", return_value=100), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.stats(Path(directory), True, since), 0)
        return json.loads(output.getvalue())

    def test_zero_since_excludes_older_events(self):
        self.assertEqual(self.summarize([2], since=0)["builds"], 0)

    def test_even_wait_median_averages_middle_pair(self):
        self.assertEqual(self.summarize([2, 8])["waitMedian"], 5)

    def test_odd_wait_median_and_empty_history(self):
        self.assertEqual(self.summarize([2, 8, 9])["waitMedian"], 8)
        self.assertEqual(self.summarize([])["waitMedian"], 0)
