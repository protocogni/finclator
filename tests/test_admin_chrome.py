"""Chrome (nav/prefix/who) of the admin pages, local and hosted. Regression for the duplicated sign-out block."""
import pytest

from src import admin


def test_local_chrome_is_plain():
    html = admin._page("t", "<p>x</p>", "/accounts")
    assert html.count("<nav>") == 1
    assert "sign out" not in html
    assert "href='/accounts' class='on'" in html
    assert "<meta http-equiv=refresh content=30>" in html
    # matrix / audit / tables hold open <details> or typed input — a refresh would wipe them
    for tab in ("/matrix", "/audit", "/tables"):
        assert "http-equiv=refresh" not in admin._page("t", "", tab)


def test_hosted_chrome_prefix_who_readonly():
    tok = admin.CHROME.set({"prefix": "/panel", "who": "<span id=who>me · <a href='/auth/logout'>sign out</a></span>",
                            "readonly": True, "refresh": False})
    try:
        html = admin._page("t", "<p>x</p>", "/matrix")
        assert admin._u("/") == "/panel"
        assert admin._u("/tables") == "/panel/tables"
        assert admin._u("/api/log") == "/panel/api/log"
    finally:
        admin.CHROME.reset(tok)
    assert html.count("sign out") == 1
    assert "href='/panel/matrix' class='on'" in html
    assert "href='/panel' class=''" in html
    assert "http-equiv=refresh" not in html
    assert "no auto-refresh" in html
    assert 'const LOG_URL="/panel/api/log"' in html


def test_chrome_is_reset_when_connect_fails(monkeypatch):
    """The old code patched admin._page before the try/finally; a failing DB connect left it wrapped and every
    later request nested another sign-out block."""
    import api.panel as panel

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(panel, "_connect", boom)
    with pytest.raises(RuntimeError):
        panel._render("matrix", "", "me@example.com")
    assert admin.CHROME.get() is None
    html = admin._page("t", "", "/")
    assert "sign out" not in html


def test_chrome_has_responsive_css():
    html = admin._page("t", "<table><tr><td>x</td></tr></table>", "/")
    assert "@media (max-width:700px)" in admin.CSS
    assert "nav{display:flex;flex-wrap:wrap" in admin.CSS
    assert "<meta name=viewport content='width=device-width" in html   # without it phones render at 980 px
    assert "<div class=tw>" in html            # every table is wrapped in a horizontal-scroll container
    # formula legends (.fm) pin their first two columns with nowrap on desktop; phones must release them or the
    # third column collapses to ~80 px and one table runs 1,000+ px tall
    mobile = admin.CSS[admin.CSS.index("@media (max-width:700px)"):]
    assert ".fm td.f,.fm td:first-child{white-space:normal}" in mobile
