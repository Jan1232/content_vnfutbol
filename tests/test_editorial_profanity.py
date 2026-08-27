from __future__ import annotations

import unittest

from editorial.profanity import contains_profanity, profanity_ok, strip_profanity


class ProfanityTests(unittest.TestCase):
    def test_detects_ru(self):
        self.assertTrue(contains_profanity("это полный пиздец"))

    def test_detects_en(self):
        self.assertTrue(contains_profanity("what the fuck"))

    def test_strip_replaces(self):
        cleaned = strip_profanity("блять какой матч")
        self.assertNotIn("блять", cleaned.lower())
        ok, _ = profanity_ok(cleaned)
        self.assertTrue(ok)

    def test_profanity_ok_clean(self):
        ok, why = profanity_ok("Отличный гол в концовке матча")
        self.assertTrue(ok)
        self.assertEqual(why, "ok")


if __name__ == "__main__":
    unittest.main()
