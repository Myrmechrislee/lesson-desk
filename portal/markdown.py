"""One sanitized renderer for saved text and editor previews."""
import markdown
import nh3
from markupsafe import Markup
from flask import Blueprint, jsonify, request

from .core import Problem, requires

bp = Blueprint("markdown", __name__)
TAGS = {"p", "br", "strong", "em", "del", "blockquote", "pre", "code", "ul", "ol", "li",
        "h1", "h2", "h3", "h4", "h5", "h6", "hr", "a", "table", "thead", "tbody", "tr", "th", "td"}


def render_markdown(value):
    html = markdown.markdown(str(value or ""), extensions=["fenced_code", "tables", "sane_lists", "nl2br"])
    return Markup(nh3.clean(html, tags=TAGS, attributes={"a": {"href", "title"}},
                            url_schemes={"https", "http", "mailto"},
                            clean_content_tags={"script", "style", "iframe", "object", "svg"}))


@bp.post("/markdown/preview")
@requires("admin", "tutor", "parent", "student")
def preview():
    value = request.form.get("text", "")
    if len(value) > 60000:
        raise Problem("Keep Markdown text to 60,000 characters or fewer.")
    return jsonify(html=str(render_markdown(value)))
