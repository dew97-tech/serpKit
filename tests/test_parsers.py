"""Parser unit tests — no network, synthetic Google-shaped HTML."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.maps import parse_maps_cards  # noqa: E402
from scraper.serp import (  # noqa: E402
    _merge_organic,
    _merged_chips,
    cite_to_url,
    detect_module_heads,
    organic_off_query,
    parse_ai_overview,
    parse_local_pack,
    parse_organic,
    parse_paa,
    parse_people_also_search_for,
    parse_related_searches,
)
from scraper.textutils import parse_count  # noqa: E402

HTML = """
<html><body><div id="search">
  <div data-hveid="1"><div class="t"><a href="/goto?url=CAESopaque"><h3>Title One</h3></a></div>
    <div class="VwiC3b">Snippet one</div>
    <cite>https://example.com &rsaquo; docs &rsaquo; page-...</cite></div>
  <div data-hveid="2"><div class="t"><a href="https://direct.example.org/page"><h3>Title Two</h3></a></div>
    <div class="VwiC3b">Snippet two</div>
    <cite>https://direct.example.org</cite></div>
</div></body></html>
"""


class TestParseOrganic(unittest.TestCase):
    def test_resolver_wins_for_opaque_links(self):
        calls = []

        def resolver(href):
            calls.append(href)
            return "https://resolved.example.com/a"

        out = parse_organic(HTML, link_resolver=resolver)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["link"], "https://resolved.example.com/a")
        self.assertEqual(out[0]["snippet"], "Snippet one")
        self.assertEqual(out[1]["link"], "https://direct.example.org/page")
        self.assertEqual(calls, ["/goto?url=CAESopaque"])

    def test_cite_fallback_without_resolver(self):
        out = parse_organic(HTML)
        self.assertEqual(out[0]["link"], "https://example.com/docs/page-")
        self.assertEqual(out[1]["position"], 2)

    def test_cite_to_url(self):
        self.assertEqual(cite_to_url("https://a.com › b › c-..."),
                         "https://a.com/b/c-")
        self.assertEqual(cite_to_url(""), "")
        self.assertEqual(cite_to_url("not a url"), "")


class TestParseCount(unittest.TestCase):
    def test_values(self):
        self.assertEqual(parse_count("1.7K"), 1700)
        self.assertEqual(parse_count("2,314"), 2314)
        self.assertEqual(parse_count("953"), 953)
        self.assertEqual(parse_count("1.2M"), 1_200_000)
        self.assertIsNone(parse_count(""))
        self.assertIsNone(parse_count("n/a"))


LOCAL_PACK_HTML = """
<html><body><div id="search">
  <div class="VkpGBb"><div class="rllt__details">
    <div role="heading" aria-level="3"><span class="OSrXXb">Nuovo York Pizza</span></div>
    <div><span aria-label="Rated 4.8 out of 5, 1.7K user reviews" role="img"><span>4.8</span></span> · Pizza</div>
    <div>Manhattan, NY</div>
    <div class="pJ3Ci">"Best pizza in nyc"</div>
  </div></div>
</div></body></html>
"""


LOCAL_PACK_AGE_HTML = """
<html><body><div id="search">
  <div class="VkpGBb"><div class="rllt__details">
    <div role="heading" aria-level="3"><span class="OSrXXb">Next Call</span></div>
    <div><span aria-label="Rated 5.0 out of 5, 159 user reviews" role="img"><span>5.0</span></span></div>
    <div>5+ years in business</div>
  </div></div>
</div></body></html>
"""


class TestLocalPack(unittest.TestCase):
    def test_card_fields(self):
        local_map, results = parse_local_pack(LOCAL_PACK_HTML)
        self.assertEqual(local_map["present"], True)
        self.assertEqual(local_map["count"], 1)
        card = results[0]
        self.assertEqual(card["title"], "Nuovo York Pizza")
        self.assertEqual(card["address"], "Manhattan, NY")
        self.assertEqual(card["rating"], 4.8)
        self.assertEqual(card["reviews"], 1700)

    def test_age_line_not_address(self):
        _, results = parse_local_pack(LOCAL_PACK_AGE_HTML)
        self.assertEqual(results[0]["title"], "Next Call")
        self.assertEqual(results[0]["address"], "")

    def test_absent_pack(self):
        local_map, results = parse_local_pack("<html><body>nothing</body></html>")
        self.assertIsNone(local_map)
        self.assertEqual(results, [])


PAA_GOOD_HTML = """
<html><body>
  <div class="related-question-pair" data-q="What is the best pizza?">
    <div class="ysxiae">What is the best pizza?</div>
    <div class="bCOlv" jsname="NRdf4c">Taste is subjective; local guides favour
      <a href="https://example.com/guide">Example Guide</a>.</div>
  </div>
</body></html>
"""

PAA_ERROR_HTML = """
<html><body>
  <div class="related-question-pair" data-q="Which pizza wins?">
    <div class="bCOlv" jsname="NRdf4c">An error has occurred. Please try again later.</div>
  </div>
</body></html>
"""


class TestParsePaa(unittest.TestCase):
    def test_answer_and_link(self):
        questions = parse_paa(PAA_GOOD_HTML)
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["question"], "What is the best pizza?")
        self.assertIn("local guides", questions[0]["snippet"])
        self.assertEqual(questions[0]["link"], "https://example.com/guide")

    def test_error_keeps_question_without_snippet(self):
        questions = parse_paa(PAA_ERROR_HTML)
        self.assertEqual(questions[0]["question"], "Which pizza wins?")
        self.assertEqual(questions[0]["snippet"], "")


AI_MODE_HTML = """
<html><body>
  <div class="FkX2oe">
    <h3 class="MheKwc">AI Mode replied:</h3>
    <div class="pWvJNd"><div class="n6owBd">The best pizza in New York depends on
      whether you want a quick classic slice or a sit-down whole pie. Top-rated
      spots cluster around Greenwich Village and the Lower East Side, with
      classic slice joints open late and coal-oven pizzerias taking
      reservations for whole pies only.</div></div>
  </div>
</body></html>
"""


RELATED_HTML = """
<html><body>
<div class="eJvC5b">
  <a href="/search?q=Top+mutual+funds+in+bangladesh&hl=en">Top mutual funds in bangladesh</a>
  <a href="/search?q=SIP+calculator+bd&hl=en">SIP calculator Bangladesh</a>
</div>
<div id="botstuff">
  <a href="/search?q=mutual+funds&start=10&hl=en">2</a>
  <a href="/search?q=mutual+funds&start=20&hl=en">3</a>
</div>
</body></html>
"""

RELATED_FALLBACK_HTML = """
<html><body><div id="botstuff">
  <a href="/search?q=Real+chip&hl=en">Real related chip</a>
  <a href="/search?q=x&start=10">2</a>
  <a href="/search?q=x&start=20">Next</a>
  <a href="/search?q=ab">ab</a>
  <a href="/search?q=x&udm=2&hl=en">Images</a>
  <a href="/search?q=x&udm=7&hl=en">Videos</a>
  <a href="/search?q=x&udm=14&hl=en">Web</a>
</div></body></html>
"""


class TestRelatedSearches(unittest.TestCase):
    def test_prefers_related_container(self):
        self.assertEqual(
            parse_related_searches(RELATED_HTML),
            ["Top mutual funds in bangladesh", "SIP calculator Bangladesh"])

    def test_filters_pagination_and_numeric_chips(self):
        self.assertEqual(parse_related_searches(RELATED_FALLBACK_HTML),
                         ["Real related chip"])


AI_MODE_TRANSLATION_HTML = """
<html><body>
  <div class="FkX2oe">
    <h3 class="MheKwc">AI Mode replied:</h3>
    <div class="pWvJNd">
      <div class="n6owBd">Error translating content. Please try again later.</div>
      <div class="otQkpb">The best pizza in New York depends on whether you want a
        quick classic slice or a sit-down whole pie, and this paragraph is long
        enough to pass the minimum length filter comfortably — top spots span
        Greenwich Village, the Lower East Side and Brooklyn, with classics open
        late, coal-oven pies by reservation, and newer Neapolitan counters that
        reward a short walk off the main avenues.</div>
    </div>
  </div>
</body></html>
"""


class TestAiOverview(unittest.TestCase):
    def test_ai_mode_captured(self):
        ai = parse_ai_overview(AI_MODE_HTML)
        self.assertIsNotNone(ai)
        self.assertEqual(ai["source"], "ai_mode")
        self.assertTrue(ai["text_blocks"])
        self.assertIn("classic", ai["text_blocks"][0])

    def test_translation_error_filtered(self):
        ai = parse_ai_overview(AI_MODE_TRANSLATION_HTML)
        self.assertIsNotNone(ai)
        self.assertTrue(ai["text_blocks"])
        joined = " ".join(ai["text_blocks"])
        self.assertNotIn("Error translating content", joined)
        self.assertIn("pizza", joined)

    def test_absent(self):
        self.assertIsNone(parse_ai_overview("<html><body>plain serp</body></html>"))


MAPS_HTML = """
<html><body><div role="feed">
  <div role="article">
    <div class="fontHeadlineSmall">Joe's Pizza</div>
    <span aria-label="4.6 stars 2,314 Reviews"></span>
    <div class="W4Efsd">Pizza · $1–10</div>
    <div class="W4Efsd">7 Carmine St, New York</div>
    <div class="W4Efsd">Open ⋅ Closes 11 PM ⋅ (316) 803-1040</div>
  </div>
</div></body></html>
"""

MAPS_NO_COMMA_HTML = """
<html><body><div role="feed">
  <div role="article">
    <div class="fontHeadlineSmall">Dhaka Burger Co</div>
    <span aria-label="4.5 stars 23 Reviews"></span>
    <div class="W4Efsd">Burger · $2–5</div>
    <div class="W4Efsd">Road 11 Banani</div>
    <div class="W4Efsd">Closed ⋅ Opens 12 PM</div>
  </div>
</div></body></html>
"""


class TestMapsCards(unittest.TestCase):
    def test_card_fields(self):
        cards = parse_maps_cards(MAPS_HTML)
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card["title"], "Joe's Pizza")
        self.assertEqual(card["rating"], 4.6)
        self.assertEqual(card["reviews"], 2314)
        self.assertEqual(card["address"], "7 Carmine St, New York")
        self.assertEqual(card["hours"], "Open ⋅ Closes 11 PM")
        self.assertEqual(card["phone"], "(316) 803-1040")

    def test_comma_less_address(self):
        cards = parse_maps_cards(MAPS_NO_COMMA_HTML)
        self.assertEqual(cards[0]["address"], "Road 11 Banani")
        self.assertEqual(cards[0]["hours"], "Closed ⋅ Opens 12 PM")
        self.assertEqual(cards[0]["phone"], "")


class TestOrganicOffQuery(unittest.TestCase):
    def test_off_query_detected(self):
        best_pages = [{"title": "Best Buy | Official Online Store",
                       "snippet": "Shop Now & Save"}]
        self.assertTrue(organic_off_query("best vpn", best_pages))
        services = [{"title": "Planning Center Services",
                     "snippet": "Schedule volunteers"}]
        self.assertTrue(organic_off_query("Best VPN services", services))

    def test_on_query_passes(self):
        vpn = [{"title": "The Best VPN Services We've Tested for 2026",
                "snippet": "vpn comparison"}]
        self.assertFalse(organic_off_query("best vpn services", vpn))
        self.assertFalse(organic_off_query("best vpn services", []))

    def test_stopword_only_query(self):
        self.assertFalse(organic_off_query("the best of", [
            {"title": "Anything", "snippet": ""}]))


PAA_JUNK_HTML = """
<html><body>
  <div class="related-question-pair" data-q="More">More</div>
  <div class="related-question-pair" data-q="Tools">Tools</div>
  <div class="related-question-pair" data-q="All results">All results</div>
  <div class="related-question-pair" data-q="Is Dhaka safe at night?">Is Dhaka safe at night?</div>
</body></html>
"""


class TestParsePaaJunkFilter(unittest.TestCase):
    def test_ai_mode_chips_filtered(self):
        questions = parse_paa(PAA_JUNK_HTML)
        self.assertEqual([q["question"] for q in questions],
                         ["Is Dhaka safe at night?"])


MAPS_RATING_ROW_HTML = """
<html><body><div role="feed">
  <div role="article">
    <div class="fontHeadlineSmall">Joe's Pizza Broadway</div>
    <div class="W4Efsd">Pizza · $10–20</div>
    <div class="W4Efsd">4.7 (382)</div>
    <div class="W4Efsd">1435 Broadway</div>
    <div class="W4Efsd">Open ⋅ Closes 5 AM ⋅ (212) 555-1111</div>
  </div>
</div></body></html>
"""


class TestMapsRatingRow(unittest.TestCase):
    def test_rating_row_not_address(self):
        card = parse_maps_cards(MAPS_RATING_ROW_HTML)[0]
        self.assertEqual(card["address"], "1435 Broadway")
        self.assertEqual(card["hours"], "Open ⋅ Closes 5 AM")
        self.assertEqual(card["phone"], "(212) 555-1111")


PASF_HTML = """
<html><body>
  <div class="mod"><h2>People also search for</h2>
    <div>
      <a href="/search?q=Samsung+galaxy+s26+price+in+bangladesh&hl=en">Samsung galaxy s26 price in bangladesh</a>
      <a href="/search?q=Samsung+Galaxy+S26+Plus+price+in+Bangladesh&hl=en">Samsung Galaxy S26 Plus price in Bangladesh</a>
      <a href="/search?q=x&start=10">2</a>
      <a href="/search?q=x&udm=2">Images</a>
    </div>
  </div>
</body></html>
"""


class TestPeopleAlsoSearchFor(unittest.TestCase):
    def test_extracts_and_filters(self):
        self.assertEqual(
            parse_people_also_search_for(PASF_HTML),
            ["Samsung galaxy s26 price in bangladesh",
             "Samsung Galaxy S26 Plus price in Bangladesh"])

    def test_absent(self):
        self.assertEqual(
            parse_people_also_search_for("<html><body>x</body></html>"), [])


class TestMergedChips(unittest.TestCase):
    def test_dedupe_and_cap(self):
        self.assertEqual(_merged_chips(["a", "b"], ["b", "c"]),
                         ["a", "b", "c"])
        self.assertEqual(len(_merged_chips([str(i) for i in range(10)])), 8)


class TestModuleHeads(unittest.TestCase):
    def test_detects(self):
        heads = detect_module_heads(
            "<html>People also search for, Related searches, People also ask</html>")
        self.assertTrue(heads["paa"])
        self.assertTrue(heads["pasf"])
        self.assertTrue(heads["related"])

    def test_empty(self):
        heads = detect_module_heads("<html>nothing here</html>")
        self.assertEqual(heads, {"paa": False, "pasf": False, "related": False})


class TestOrganicMerge(unittest.TestCase):
    def test_dedupe_and_page_tag(self):
        out, seen = [], set()
        added1 = _merge_organic([{"link": "a"}, {"link": "a"}, {"link": "b"}],
                                1, seen, out)
        self.assertEqual(added1, 2)
        added2 = _merge_organic([{"link": "b"}, {"link": "c"}], 2, seen, out)
        self.assertEqual(added2, 1)
        self.assertEqual([i["link"] for i in out], ["a", "b", "c"])
        self.assertEqual([i["page"] for i in out], [1, 1, 2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
