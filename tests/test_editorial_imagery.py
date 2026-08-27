from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from editorial.imagery import (
    _compact_query,
    _image_providers,
    _keep_urls,
    _query_for,
    dedupe_image_candidates,
    google_images_url,
    parse_og_images,
    parse_yandex_orig_urls,
    publisher_image_variants,
    url_is_allowed,
    url_is_blocked,
)


class ImageUrlSafetyTests(unittest.TestCase):
    def test_blocks_porn_hosts(self):
        self.assertTrue(url_is_blocked("https://i.xhcdn.com/a.jpg"))
        self.assertTrue(url_is_blocked("https://www.pornhub.com/view.jpg"))
        self.assertTrue(url_is_blocked("https://cdn.example.com/porn/clip.jpg"))
        self.assertFalse(url_is_allowed("https://i.xhcdn.com/a.jpg"))

    def test_allows_sports_not_wiki(self):
        self.assertFalse(url_is_allowed("https://upload.wikimedia.org/wikipedia/commons/a/ab/x.jpg"))
        self.assertTrue(url_is_allowed("https://photohost.championat.com/news/800x600.jpg"))
        self.assertTrue(url_is_allowed("https://www.sports.ru/static/img.jpg"))
        self.assertTrue(url_is_allowed("https://ichef.bbci.co.uk/onesport/cps/800.jpg"))
        self.assertTrue(url_is_allowed("https://www.soccer.ru/sites/default/files/x.jpg"))

    def test_allowlist_still_rejects_random_cdn(self):
        self.assertFalse(url_is_allowed("https://random-cdn.net/cat.jpg"))
        self.assertFalse(url_is_allowed("https://i.imgur.com/photo.jpg"))

    def test_google_keep_allows_publisher_cdn_but_not_porn(self):
        kept = _keep_urls(
            [
                "https://i.imgur.com/palhinha.jpg",
                "https://media.gettyimages.com/id/1/benfica.jpg",
                "https://i.xhcdn.com/nsfw.jpg",
                "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9",
            ],
            limit=8,
            allowlist_only=False,
        )
        self.assertIn("https://media.gettyimages.com/id/1/benfica.jpg", kept)
        self.assertIn("https://i.imgur.com/palhinha.jpg", kept)
        self.assertTrue(all("xhcdn" not in u and "gstatic" not in u for u in kept))

    def test_google_images_url_uses_safesearch(self):
        url = google_images_url("Palhinha Benfica football")
        self.assertIn("tbm=isch", url)
        self.assertIn("safe=active", url)
        self.assertIn("Palhinha", url)

    def test_parse_og_images(self):
        html = """
        <meta property="og:image" content="https://photohost.championat.com/news/800x600.jpg" />
        <meta name="twitter:image" content="//cdn.example.com/x.png">
        """
        urls = parse_og_images(html, "https://www.championat.com/news/1.html")
        self.assertEqual(
            urls[0],
            "https://photohost.championat.com/news/800x600.jpg",
        )
        self.assertTrue(urls[1].startswith("https://cdn.example.com/"))

    def test_query_uses_headline_not_entity_list(self):
        q = _compact_query(
            "Пальинья может перейти в Бенфику — источник рассказал детали сделки"
        )
        self.assertIn("Пальинья", q)
        self.assertIn("Бенфику", q)
        self.assertNotIn("источник", q)
        self.assertNotIn("Palhinha", q)
        self.assertLessEqual(len(q.split()), 8)

    def test_championat_square_variant(self):
        og = "https://img.championat.com/s/1200x630/news/big/a/b/foo_1.jpg"
        variants = publisher_image_variants(og)
        self.assertIn("https://img.championat.com/c/900x900/news/big/a/b/foo_1.jpg", variants)
        self.assertIn("https://img.championat.com/news/big/a/b/foo_1.jpg", variants)
        self.assertEqual(variants[0], "https://img.championat.com/c/900x900/news/big/a/b/foo_1.jpg")

    def test_yandex_origurl_unescapes(self):
        html = (
            "&quot;origWidth&quot;:1180,&quot;origHeight&quot;:665,"
            "&quot;origUrl&quot;:&quot;https://ss.sport-express.ru/userfiles/kane.jpg&quot;"
        )
        urls = parse_yandex_orig_urls(html)
        self.assertEqual(urls, ["https://ss.sport-express.ru/userfiles/kane.jpg"])

    def test_providers_are_yandex_not_wiki_bing(self):
        names = [p.name for p in _image_providers()]
        self.assertIn("yandex", names)
        self.assertNotIn("wikimedia", names)
        self.assertNotIn("custom", names)
        self.assertNotIn("google", names)

    def test_query_prefers_club_over_country(self):
        q = _compact_query("Дубль Дурана приблизил Селтик к Лиге чемпионов")
        self.assertIn("Селтик", q)
        self.assertNotIn("Austria", q)

    def test_query_is_news_not_opponent(self):
        q = _compact_query(
            "Сафонов и Забарный – в составе «ПСЖ» на матч с «Лансом» в Суперкубке Франции"
        )
        self.assertIn("Сафонов", q)
        self.assertIn("ПСЖ", q)
        self.assertNotIn("Ланс", q)
        self.assertNotIn("Забарный", q)
        self.assertLessEqual(len(q.split()), 5)

    def test_query_winner_not_city(self):
        q = _compact_query(
            "«Арсенал» в шестой раз подряд выиграл в своём матче за Суперкубок Англии"
        )
        self.assertEqual(q, "Арсенал выиграл Суперкубок Англии")
        self.assertNotIn("шестой", q)
        self.assertNotIn("Manchester", q)

    def test_query_roundup_keeps_first_match(self):
        q = _compact_query(
            "Альфа-Банк РПЛ. «Спартак» победил «Балтику» в гостях, "
            "«Зенит» разгромил «Динамо», «Махачкала» забила 4 гола «Крыльям» на выезде"
        )
        self.assertEqual(q, "Альфа-Банк РПЛ матч Спартак Балтику")
        self.assertNotIn("Зенит", q)

    def test_query_quote_uses_author_not_words(self):
        q = _compact_query(
            "«Многое меняется с приходом нового тренера». "
            "Диаш — о поражении «Ман Сити» от «Арсенала»"
        )
        self.assertIn("Диаш", q)
        self.assertIn("Ман Сити", q)
        self.assertNotIn("меняется", q)
        self.assertNotIn("тренера", q)
        self.assertNotIn("Арсенала", q)

    def test_query_quote_arteta_not_aphorism(self):
        q = _compact_query(
            "«Мы знаем, что для этого потребуется». "
            "Артета — о шансах «Арсенала» на второй титул АПЛ"
        )
        self.assertIn("Артета", q)
        self.assertNotIn("потребуется", q)
        self.assertNotIn("знаем", q)

    def test_query_club_quotes_are_not_speech(self):
        q = _compact_query(
            "«Арсенал» в шестой раз подряд выиграл в своём матче за Суперкубок Англии"
        )
        self.assertEqual(q, "Арсенал выиграл Суперкубок Англии")

    def test_query_for_rejects_llm_quote_dump(self):
        title = (
            "«Многое меняется с приходом нового тренера». "
            "Диаш — о поражении «Ман Сити» от «Арсенала»"
        )
        with patch(
            "editorial.llm.image_search_query",
            return_value="Многое меняется с приходом нового тренера",
        ):
            q = _query_for({"title": title})
        self.assertIn("Диаш", q)
        self.assertNotIn("меняется", q)

    def test_query_for_uses_short_llm_and_rejects_dump(self):
        with patch("editorial.llm.image_search_query", return_value="Арсенал выиграл Суперкубок Англии 2026"):
            q = _query_for({"title": "«Арсенал» в восьмой раз выиграл Суперкубок Англии в XXI веке. Это лучший результат"})
        self.assertEqual(q, "Арсенал выиграл Суперкубок Англии 2026")
        long_dump = "Арсенал в восьмой раз выиграл Суперкубок Англии в XXI веке Это лучший результат фото"
        with patch("editorial.llm.image_search_query", return_value=long_dump):
            q = _query_for({"title": "«Арсенал» в восьмой раз выиграл Суперкубок Англии в XXI веке. Это лучший результат"})
        self.assertEqual(q, "Арсенал выиграл Суперкубок Англии")
        self.assertNotIn("XXI", q)

    def test_keep_drops_wikimedia(self):
        kept = _keep_urls(
            [
                "https://upload.wikimedia.org/wikipedia/commons/a.jpg",
                "https://img.championat.com/c/900x900/news/big/a/b/kane.jpg",
            ],
            limit=8,
            allowlist_only=False,
        )
        self.assertEqual(kept, ["https://img.championat.com/c/900x900/news/big/a/b/kane.jpg"])
        kept = _keep_urls(
            [
                "https://photobooth.cdn.sports.ru/preset/tc_team/1/fd/crest.png",
                "https://img.championat.com/c/900x900/news/big/a/b/kane.jpg",
            ],
            limit=8,
            allowlist_only=False,
        )
        self.assertEqual(kept, ["https://img.championat.com/c/900x900/news/big/a/b/kane.jpg"])

    def test_generate_fallback_off_by_default(self):
        from editorial.imagery import _generate_cover_fallback

        with patch("editorial.imagery.get_settings") as gs:
            gs.return_value.editorial_image_gen_fallback = False
            self.assertIsNone(_generate_cover_fallback({"title": "x"}, news_id=1))

    def test_keep_does_not_drop_no_logo_filename(self):
        kept = _keep_urls(
            ["https://www.sports.ru/dynamic_images/news/1/share/897227_no_logo_no_text.jpg"],
            limit=3,
            allowlist_only=False,
        )
        self.assertEqual(len(kept), 1)


class _ImgSettings:
    editorial_image_gen_fallback = False
    editorial_vision_model = "gpt-4o-mini"
    imagery_candidates_max = 8
    imagery_min_relevance = 0.55
    imagery_max_upscale = 1.75
    imagery_min_sharpness = 100.0
    imagery_max_dark_ratio = 0.55
    imagery_max_aspect_delta = 1.0
    imagery_face_backend = "opencv_dnn"


def _patch_img_settings():
    return patch("editorial.imagery.get_settings", return_value=_ImgSettings())


def _save_rgb(path, im):
    im.convert("RGB").save(path, format="JPEG", quality=95)


class QualityCropRelevanceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _checker(self, w: int, h: int, name: str = "ok.jpg", cell: int = 8):
        import numpy as np
        from PIL import Image as PImage

        y, x = np.ogrid[0:h, 0:w]
        arr = np.where(((x // cell) + (y // cell)) % 2 == 0, 220, 40).astype("uint8")
        rgb = np.stack([arr, arr, arr], axis=-1)
        path = self.dir / name
        _save_rgb(path, PImage.fromarray(rgb, "RGB"))
        return path

    def test_quality_rejects_small_og_for_vertical(self):
        from PIL import Image as PImage

        from editorial.imagery import quality_ok

        path = self.dir / "og.jpg"
        _save_rgb(path, PImage.new("RGB", (1200, 630), (180, 180, 180)))
        with _patch_img_settings():
            ok, why = quality_ok(path, "matchday")
        self.assertFalse(ok)
        self.assertTrue("upscale" in why or "aspect" in why)

    def test_quality_rejects_dark(self):
        from PIL import Image as PImage

        from editorial.imagery import quality_ok

        path = self.dir / "dark.jpg"
        _save_rgb(path, PImage.new("RGB", (1600, 1600), (8, 8, 8)))
        with _patch_img_settings():
            ok, why = quality_ok(path, "default")
        self.assertFalse(ok)
        self.assertIn("dark", why)

    def test_quality_rejects_blur(self):
        from PIL import Image as PImage, ImageFilter

        from editorial.imagery import quality_ok

        path = self.dir / "blur.jpg"
        im = PImage.new("RGB", (1600, 1600), (120, 120, 120))
        im = im.filter(ImageFilter.GaussianBlur(radius=12))
        _save_rgb(path, im)
        with _patch_img_settings():
            ok, why = quality_ok(path, "default")
        self.assertFalse(ok)
        self.assertIn("blur", why)

    def test_quality_accepts_sharp_square(self):
        from editorial.imagery import quality_ok

        path = self._checker(1600, 1600)
        with _patch_img_settings():
            ok, why = quality_ok(path, "default")
        self.assertTrue(ok, why)

    def test_dedupe_image_candidates_drops_visual_duplicates(self):
        from PIL import Image as PImage

        from editorial.imagery import ImageCandidate, dedupe_image_candidates

        a = self.dir / "a.jpg"
        b = self.dir / "b.jpg"
        c = self.dir / "c.jpg"
        base = PImage.open(self._checker(1600, 1600, "base.jpg"))
        _save_rgb(a, base)
        _save_rgb(b, base.copy())
        diff = PImage.new("RGB", (1600, 1600))
        diff.paste(PImage.new("RGB", (800, 1600), (20, 20, 220)), (0, 0))
        diff.paste(PImage.new("RGB", (800, 1600), (220, 20, 20)), (800, 0))
        _save_rgb(c, diff)
        dupes = [
            ImageCandidate(path=a, url="https://cdn/a.jpg", via="yandex", width=1600, height=1600),
            ImageCandidate(path=b, url="https://cdn/b.jpg", via="yandex", width=1200, height=1200),
        ]
        self.assertEqual(len(dedupe_image_candidates(dupes)), 1)
        pool = dupes + [
            ImageCandidate(path=c, url="https://cdn/c.jpg", via="yandex", width=1600, height=1600),
        ]
        kept = dedupe_image_candidates(pool)
        self.assertEqual(len(kept), 2)
        self.assertEqual({str(x.path) for x in kept}, {str(a), str(c)})

    def test_crop_no_faces_is_center_not_bottom(self):
        from editorial.imagery import compute_crop_box

        # 1600×2000 → square 1600: центр y=200, низ y=400
        x, y, w, h = compute_crop_box(1600, 2000, 1080, 1080, faces=[], template="default")
        self.assertEqual((w, h), (1600, 1600))
        self.assertEqual(x, 0)
        self.assertEqual(y, 200)

    def test_crop_single_face_upper_third_and_inside(self):
        from editorial.imagery import compute_crop_box

        face = (700, 200, 200, 220)
        x, y, w, h = compute_crop_box(1600, 2000, 1080, 1350, faces=[face], template="matchday")
        fx, fy, fw, fh = face
        self.assertGreaterEqual(fx, x)
        self.assertGreaterEqual(fy, y)
        self.assertLessEqual(fx + fw, x + w)
        self.assertLessEqual(fy + fh, y + h)
        # лицо ближе к верхней трети окна, чем к низу
        cy = fy + fh / 2
        rel = (cy - y) / h
        self.assertLess(rel, 0.55)

    def test_crop_group_keeps_all_faces(self):
        from editorial.imagery import compute_crop_box

        faces = [(80, 400, 120, 140), (900, 420, 130, 150)]
        x, y, w, h = compute_crop_box(1600, 1200, 1080, 1080, faces=faces, template="default")
        for fx, fy, fw, fh in faces:
            self.assertGreaterEqual(fx, x)
            self.assertGreaterEqual(fy, y)
            self.assertLessEqual(fx + fw, x + w)
            self.assertLessEqual(fy + fh, y + h)

    def test_vision_drops_wrong_team(self):
        from editorial.imagery import ImageCandidate, score_relevance

        path = self._checker(1600, 1600, "ger.jpg")
        cand = ImageCandidate(path=path, url="https://example.com/germany-team.jpg", via="article", width=1600, height=1600)
        item = {
            "title": "Селтик разгромил ЛАСК",
            "event_type": "match_result",
            "entities_json": '{"players":[],"teams":["Celtic","LASK"]}',
        }

        class _Client:
            def vision(self, *args, **kwargs):
                return {
                    "results": [
                        {
                            "idx": 0,
                            "relevant": False,
                            "subject_present": False,
                            "reason": "на фото сборная Германии, не Селтик",
                            "quality": "ok",
                            "score": 0.1,
                        }
                    ]
                }

        with (
            _patch_img_settings(),
            patch("editorial.openai_client.get_client", return_value=_Client()),
        ):
            trace: dict = {}
            kept = score_relevance([cand], item, trace=trace)
        self.assertEqual(kept, [])
        self.assertEqual(trace["vision"]["candidates"][0]["reason"], "на фото сборная Германии, не Селтик")
        self.assertFalse(trace["vision"]["candidates"][0]["kept"])

    def test_vision_drops_overlay_text_even_if_relevant(self):
        from editorial.imagery import ImageCandidate, score_relevance

        path = self._checker(1600, 1600, "txt.jpg")
        cand = ImageCandidate(
            path=path,
            url="https://example.com/dias-quote.jpg",
            via="yandex",
            width=1600,
            height=1600,
        )
        item = {
            "title": "Диаш — о поражении Ман Сити",
            "event_type": "match_result",
            "entities_json": '{"players":["Dias"],"teams":["Manchester City"]}',
        }

        class _Client:
            def vision(self, *args, **kwargs):
                return {
                    "results": [
                        {
                            "idx": 0,
                            "relevant": True,
                            "subject_present": True,
                            "has_overlay_text": True,
                            "reason": "цитата на фото",
                            "quality": "ok",
                            "score": 0.9,
                        }
                    ]
                }

        with (
            _patch_img_settings(),
            patch("editorial.openai_client.get_client", return_value=_Client()),
        ):
            trace: dict = {}
            kept = score_relevance([cand], item, trace=trace)
        self.assertEqual(kept, [])
        self.assertTrue(trace["vision"]["candidates"][0]["has_overlay_text"])
        self.assertFalse(trace["vision"]["candidates"][0]["kept"])

    def test_relevance_prompt_bans_overlay_except_here_we_go(self):
        from editorial.imagery import _relevance_prompt

        prompt = _relevance_prompt({"title": "Романо Here we go", "entities_json": "{}"}, 3)
        self.assertIn("has_overlay_text", prompt)
        self.assertIn("Here we go", prompt)
        self.assertIn("логотип", prompt)

    def test_vision_error_without_name_in_url_holds(self):
        from editorial.imagery import ImageCandidate, score_relevance

        path = self._checker(1600, 1600, "x.jpg")
        cand = ImageCandidate(path=path, url="https://cdn.example.com/photo.jpg", via="bing", width=1600, height=1600)
        item = {
            "title": "Селтик разгромил ЛАСК",
            "entities_json": '{"players":["Maeda"],"teams":["Celtic"]}',
        }

        class _Boom:
            def vision(self, *args, **kwargs):
                raise RuntimeError("vision down")

        with (
            _patch_img_settings(),
            patch("editorial.openai_client.get_client", return_value=_Boom()),
        ):
            kept = score_relevance([cand], item)
        self.assertEqual(kept, [])

    def test_smart_crop_writes_exact_size(self):
        from editorial.imagery import smart_crop

        src = self._checker(1600, 2000, "src.jpg")
        out = smart_crop(src, 1080, 1080, template="default", faces=[], dest=self.dir / "out.jpg")
        from PIL import Image as PImage

        with PImage.open(out) as im:
            self.assertEqual(im.size, (1080, 1080))


class ImageryTraceTests(unittest.TestCase):
    def test_append_jsonl_roundtrip(self):
        import tempfile

        from editorial import imagery_trace as it

        with tempfile.TemporaryDirectory() as raw:
            d = Path(raw)
            with patch.object(it, "TRACE_DIR", d):
                p = it.append_trace({"news_id": 7, "outcome": "picked", "title": "Кейн"})
                rows = it.load_traces(p)
            self.assertEqual(p.parent, d)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["news_id"], 7)
            self.assertEqual(rows[0]["outcome"], "picked")


if __name__ == "__main__":
    unittest.main()

