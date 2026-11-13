from portal.markdown import render_markdown


def test_markdown_formats_and_sanitizes_untrusted_content():
    result = str(render_markdown("## Goals\n\n**Practice** and `review`\n\n- first\n- second\n\n<script>alert(1)</script>\n\n[click](javascript:alert(1))\n\n<img src=x onerror=alert(1)>"))
    assert "<h2>Goals</h2>" in result and "<strong>Practice</strong>" in result
    assert "<li>first</li>" in result
    assert "script" not in result and "javascript:" not in result and "onerror" not in result
    assert "<img" not in result


def test_plain_text_and_code_stay_readable():
    result = str(render_markdown("Plain text\nSecond line\n\n```html\n<script>example</script>\n```"))
    assert "Plain text<br>" in result
    assert "&lt;script&gt;example&lt;/script&gt;" in result
