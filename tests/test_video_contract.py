import unittest

from go1loc.video import VideoPipelineError, VideoProbe, select_uniform_times


class VideoContractTests(unittest.TestCase):
    def test_uniform_selection_uses_duration(self):
        probe = VideoProbe("x.mp4", "abc", "test", duration_s=10.0, vfr_or_pts_uncertain=True)
        self.assertEqual(select_uniform_times(probe, 3), [0.0, 5.0, 10.0])

    def test_selection_refuses_unknown_duration(self):
        with self.assertRaises(VideoPipelineError):
            select_uniform_times(VideoProbe("x.mp4", "abc", "test"), 3)


if __name__ == "__main__":
    unittest.main()
