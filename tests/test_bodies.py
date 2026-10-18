"""Article text extraction fallbacks, on synthetic pages."""

import json

import pytest

pytest.importorskip("trafilatura")

from pipeline.analyse import bodies  # noqa: E402

PARA = "정부는 오늘 새로운 대책을 발표했다. " * 12


def test_json_ld_article_body():
    ld = json.dumps({"@type": "NewsArticle", "articleBody": PARA})
    html = f'<html><head><script type="application/ld+json">{ld}</script></head></html>'
    assert bodies.embedded_text(html).startswith("정부는 오늘")


def test_microdata_article_body_survives_broken_noscript_iframe():
    html = (
        "<html><body><!------ header ------><noscript><iframe src=x></noscript>"
        f'<div itemprop="articleBody">{PARA}<br>둘째 문단이다.</div></body></html>'
    )
    text = bodies.extract(html)
    assert "새로운 대책" in text and "둘째 문단" in text


def test_streamed_text_elements():
    # news1 streams paragraphs as {"type":"text","content":"..."} with escaped quotes.
    html = '<script>x = [{"type":"text","content":"' + PARA + '\\"인용\\" 끝."}]</script>'
    text = bodies.embedded_text(html)
    assert "새로운 대책" in text and '"인용"' in text


def test_short_pages_are_empty():
    page = bodies.Page("u", "ok", "짧다")
    assert page.text == "짧다"
    assert bodies.extract("<html><body><p>짧다</p></body></html>") in ("", "짧다")
