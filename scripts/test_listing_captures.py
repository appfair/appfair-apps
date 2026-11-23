"""Synthetic galleries covering store selection when test captures share walkthrough names."""

import copy
import unittest
from pathlib import Path

import queue as catalog


class ListingCapturesTests(unittest.TestCase):
    def setUp(self):
        self.index = {
            "listings": {"ios-uikit": {"stores": {"apple-app-store": {
                "iphone": {"en": [{"shot": "home", "theme": "light"}]},
            }}}},
            "screenshots": [],
        }

    def capture(self, theme, **overrides):
        return {
            "platform": "ios-uikit", "device": "iphone", "locale": "en",
            "shot": "home", "theme": theme, "path": f"{theme}/home.png", **overrides,
        }

    def selected(self):
        return catalog.listing_captures(self.index, "ios-uikit", "apple-app-store")

    def test_requested_theme_wins_in_either_input_order(self):
        themed, unthemed = self.capture("light"), self.capture(None)
        for entries in ([unthemed, themed], [themed, unthemed]):
            with self.subTest(entries=entries):
                self.index["screenshots"] = entries
                self.assertEqual([s["path"] for s in self.selected()], ["light/home.png"])

    def test_unthemed_is_a_fallback_and_other_themes_are_excluded(self):
        unthemed = self.capture(None)
        for omit_theme in (False, True):
            with self.subTest(omit_theme=omit_theme):
                if omit_theme:
                    unthemed.pop("theme")
                self.index["screenshots"] = [self.capture("dark"), unthemed]
                self.assertEqual([s["path"] for s in self.selected()], ["None/home.png"])
        self.index["screenshots"] = [self.capture("dark")]
        self.assertEqual(self.selected(), [])

    def test_explicit_dark_theme_wins_over_unthemed(self):
        self.index["listings"]["ios-uikit"]["stores"]["apple-app-store"]["iphone"]["en"][0]["theme"] = "dark"
        self.index["screenshots"] = [self.capture(None), self.capture("light"), self.capture("dark")]
        self.assertEqual([s["path"] for s in self.selected()], ["dark/home.png"])

    def test_duplicate_captures_do_not_duplicate_listing_positions(self):
        self.index["screenshots"] = [self.capture("light"), self.capture("light")]
        self.assertEqual([s["position"] for s in self.selected()], [1])

    def test_fallback_does_not_cross_locale_device_platform_or_shot(self):
        self.index["screenshots"] = [
            self.capture("light", locale="fr"), self.capture("light", device="ipad"),
            self.capture("light", platform="android-material"), self.capture("light", shot="other"),
            self.capture(None),
        ]
        self.assertEqual([s["path"] for s in self.selected()], ["None/home.png"])

    def test_store_limits_count_selected_captures_and_still_reject_excess(self):
        # Seven phone / six tablet shots, with six unthemed behavioral captures sharing
        # their names: the same collision that inflated Net-Skip's English listing.
        index = {"locales": ["en", "fr"], "listings": {}, "screenshots": []}
        profiles = [
            ("ios-uikit", "apple-app-store", "iphone", 7, 1320, 2868),
            ("ios-uikit", "apple-app-store", "ipad", 6, 2752, 2064),
            ("android-material", "google-play-store", "phone", 7, 1080, 1920),
            ("android-material", "google-play-store", "tablet", 6, 1920, 1200),
        ]
        for target, store, kind, count, width, height in profiles:
            kinds = index["listings"].setdefault(target, {"stores": {}})["stores"].setdefault(store, {})
            kinds[kind] = {}
            for locale in index["locales"]:
                kinds[kind][locale] = [{"shot": f"screen-{i}", "theme": "light"} for i in range(count)]
                for i in range(count):
                    capture = {"platform": target, "device": kind, "locale": locale,
                               "shot": f"screen-{i}", "theme": "light", "width": width, "height": height}
                    if locale == "en" and i < 6:
                        index["screenshots"].append({**capture, "theme": None})
                    index["screenshots"].append(capture)
        app = catalog.App(path=Path("apps/Fixture.yaml"), data={"distribution": {
            "ios-uikit": ["apple-app-store"], "android-material": ["google-play-store"],
        }})
        policy = catalog.load_yaml(catalog.ROOT / "policy.yaml")
        self.assertEqual(catalog.screenshot_problems(app, index, policy, "fixture"), [])
        for target, store, kind, count, _, _ in profiles:
            selected = catalog.listing_captures(index, target, store)
            for locale in index["locales"]:
                shots = [s for s in selected if s["kind"] == kind and s["locale"] == locale]
                self.assertEqual([s["shot"] for s in shots], [f"screen-{i}" for i in range(count)])
                self.assertTrue(all(s["theme"] == "light" for s in shots))

        excess = copy.deepcopy(index)
        for i in range(7, 9):
            excess["listings"]["android-material"]["stores"]["google-play-store"]["phone"]["en"].append(
                {"shot": f"screen-{i}", "theme": "light"})
            excess["screenshots"].append({"platform": "android-material", "device": "phone", "locale": "en",
                                          "shot": f"screen-{i}", "theme": "light", "width": 1080, "height": 1920})
        problems = catalog.screenshot_problems(app, excess, policy, "fixture")
        self.assertEqual(len(problems), 1)
        self.assertIn("9 phone screenshots for en", problems[0].message)
        self.assertIn("[storefront.android-material.google-play-store.screenshots]", problems[0].message)


if __name__ == "__main__":
    unittest.main()
