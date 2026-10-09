"""Config unit tests: uule encoder + URL builders (no network)."""
from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.config import (  # noqa: E402
    SearchParams,
    build_autocomplete_url,
    build_search_url,
    build_uule,
    canonical_location_uule,
    parse_location,
)


def decode_uule(uule: str) -> str:
    b64 = uule[2:] if uule.startswith("w+") else uule
    b64 += "=" * (-len(b64) % 4)
    raw = base64.urlsafe_b64decode(b64)
    assert raw[:5] == b"\x08\x02\x10\x20\x22", "unexpected protobuf header"
    return raw[6:6 + raw[5]].decode("utf-8")


class TestUule(unittest.TestCase):
    def test_known_vector(self):
        self.assertEqual(
            build_uule("New York,New York,United States"),
            "w+CAIQICIfTmV3IFlvcmssTmV3IFlvcmssVW5pdGVkIFN0YXRlcw",
        )

    def test_round_trip(self):
        for name in ("Tokyo,Tokyo,Japan", "Dhaka,Dhaka,Bangladesh",
                     "Austin,Texas,United States"):
            self.assertEqual(decode_uule(build_uule(name)), name)

    def test_canonical_location_uule(self):
        self.assertEqual(
            decode_uule(canonical_location_uule("Tokyo", "Tokyo", "Japan")),
            "Tokyo,Tokyo,Japan")
        self.assertEqual(
            decode_uule(canonical_location_uule("Dhaka", "", "Bangladesh")),
            "Dhaka,Bangladesh")
        self.assertEqual(canonical_location_uule("", "", "United States"), "")
        self.assertEqual(canonical_location_uule("Dhaka", "", ""), "")


class TestUrls(unittest.TestCase):
    def test_search_url_params(self):
        params = SearchParams(
            q="best burgers",
            uule=build_uule("Austin,Texas,United States"),
        )
        url = build_search_url(params, start=10)
        self.assertIn("q=best+burgers", url)
        self.assertIn("start=10", url)
        self.assertIn("uule=w%2B", url)  # literal '+' must be percent-encoded
        self.assertIn("pws=0", url)

    def test_autocomplete_url(self):
        url = build_autocomplete_url(
            SearchParams(q="ramen", hl="ja", gl="jp",
                         google_domain="www.google.co.jp")
        )
        self.assertIn("client=firefox", url)
        self.assertIn("hl=ja", url)

    def test_web_mode_udm(self):
        params = SearchParams(q="best vpn", web_mode=True)
        self.assertIn("udm=14", build_search_url(params))
        self.assertNotIn("udm=14", build_search_url(SearchParams(q="best vpn")))


class TestLocation(unittest.TestCase):
    def test_alias_one_part(self):
        self.assertEqual(parse_location("Dhaka"),
                         ("Dhaka,Dhaka,Bangladesh", "bd"))
        self.assertEqual(parse_location("nyc"),
                         ("New York,New York,United States", "us"))
        self.assertEqual(parse_location("Nowhereville"), ("", ""))

    def test_city_country(self):
        self.assertEqual(parse_location("Dhaka, Bangladesh"),
                         ("Dhaka,Dhaka,Bangladesh", "bd"))
        self.assertEqual(parse_location("Dhaka, United States"),
                         ("Dhaka,United States", "us"))

    def test_canonical_three_parts(self):
        self.assertEqual(
            parse_location("Austin,Texas,United States"),
            ("Austin,Texas,United States", "us"))

    def test_empty(self):
        self.assertEqual(parse_location(""), ("", ""))
        self.assertEqual(parse_location("  "), ("", ""))


class TestResolveParams(unittest.TestCase):
    def test_location_sets_uule_and_auto_gl(self):
        from api import _resolve_params

        params = _resolve_params("us", "best burgers",
                                 location="Dhaka, Bangladesh")
        self.assertEqual(params.gl, "bd")
        self.assertEqual(params.city, "Dhaka")
        self.assertEqual(params.country, "Bangladesh")
        self.assertTrue(params.uule.startswith("w+"))

    def test_explicit_uule_wins(self):
        from api import _resolve_params

        params = _resolve_params("us", "q", uule="w+CUSTOM",
                                 location="Dhaka, Bangladesh")
        self.assertEqual(params.uule, "w+CUSTOM")

    def test_manual_gl_not_overridden(self):
        from api import _resolve_params

        params = _resolve_params("us", "q", gl="de",
                                 location="Dhaka, Bangladesh")
        self.assertEqual(params.gl, "de")

    def test_japan_preset_uule(self):
        from api import _resolve_params

        params = _resolve_params("japan", "ラーメン")
        self.assertEqual(params.gl, "jp")
        self.assertTrue(params.uule.startswith("w+"))

    def test_bangladesh_preset(self):
        from api import _resolve_params

        params = _resolve_params("bangladesh", "best burgers in dhaka")
        self.assertEqual(params.gl, "bd")
        self.assertEqual(params.google_domain, "www.google.com.bd")
        self.assertEqual(params.city, "Dhaka")
        self.assertEqual(decode_uule(params.uule), "Dhaka,Dhaka,Bangladesh")


class TestInitScript(unittest.TestCase):
    def test_truth_patches_present(self):
        from scraper.config import build_init_script

        script = build_init_script(["en-US", "en"])
        self.assertIn("webdriver", script)
        self.assertIn('"en-US"', script)
        self.assertIn("window.chrome", script)
        self.assertIn("SwiftShader", script)
        self.assertIn("permissions", script)


if __name__ == "__main__":
    unittest.main(verbosity=2)
