"""With the plugin installed as ~/.hermes/plugins/raven:
PYTHONPATH=<hermes-agent checkout>:~/.hermes/plugins <hermes python> -m raven.test_adapter
"""

from .adapter import html_to_text, is_mentioned, markdown_to_html, remove_mention

MENTION_HTML = (
    '<p><span class="mention" data-id="hermes@example.com" data-label="Hermes" '
    'data-type="userMention">@Hermes</span> what is 2 + 2?</p>'
)


def test_mention_is_detected_and_removed():
    assert is_mentioned(MENTION_HTML, "hermes@example.com")
    assert not is_mentioned(MENTION_HTML, "alice@example.com")
    assert html_to_text(remove_mention(MENTION_HTML, "hermes@example.com")) == "what is 2 + 2?"


def test_other_mentions_stay_in_text():
    html = MENTION_HTML.replace("hermes@example.com", "bob@example.com").replace("@Hermes", "@Bob")
    assert html_to_text(remove_mention(html, "hermes@example.com")) == "@Bob what is 2 + 2?"


def test_html_to_text_keeps_lines_lists_and_code():
    html = ("<p>first line<br>second line</p><ul><li><p>one</p></li><li><p>two</p></li></ul>"
            "<p>run <code>ls</code></p><pre><code>a &lt; b\nc</code></pre>")
    text = html_to_text(html)
    assert "first line\nsecond line" in text
    assert "- one" in text and "- two" in text
    assert "run `ls`" in text
    assert "```\na < b\nc\n```" in text


def test_markdown_to_html_renders_rich_text():
    html = markdown_to_html("**bold**\nnext line\n\n```python\nprint('hi')\n```")
    assert "<strong>bold</strong><br />" in html
    assert '<code class="language-python">' in html


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")
