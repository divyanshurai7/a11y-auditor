"""Plain-English fixes for common axe-core rules.

    get_fix("image-alt")                       # curated fix with before/after HTML
    get_fix("some-rare-rule", description=..., help_url=...)   # falls back to axe's text

Some rules here (region, landmark-one-main, heading-order, page-has-heading-one,
empty-heading) are axe "best-practice" rules. The auditor runs only WCAG A/AA
rules by default, but they're kept so the table still works if that changes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Fix:
    rule_id: str
    # What to do, in plain English.
    summary: str
    # Short HTML examples: broken, then fixed. Empty for the axe fallback.
    before: str = ""
    after: str = ""
    # Link to axe's documentation for the rule, when known.
    help_url: str = ""
    # True if the fix came from the table below, False if from axe's description.
    curated: bool = True


def _fix(summary: str, before: str, after: str) -> dict:
    return {"summary": summary, "before": before, "after": after}


FIXES: dict[str, dict] = {
    # --- images ---
    "image-alt": _fix(
        "Give every image an alt attribute. Describe what the image shows or does; "
        "use alt=\"\" for purely decorative images so screen readers skip them.",
        '<img src="logo.png">',
        '<img src="logo.png" alt="Acme Inc. home">',
    ),
    "input-image-alt": _fix(
        "Image buttons (<input type=\"image\">) need alt text that says what the button does.",
        '<input type="image" src="go.png">',
        '<input type="image" src="go.png" alt="Search">',
    ),
    "image-redundant-alt": _fix(
        "Don't repeat the surrounding text in the image's alt; screen readers would say it twice. "
        "Use alt=\"\" if the image adds nothing to the text.",
        '<a href="/"><img src="home.svg" alt="Home"> Home</a>',
        '<a href="/"><img src="home.svg" alt=""> Home</a>',
    ),
    # --- colour ---
    "color-contrast": _fix(
        "Make text contrast enough with its background: at least 4.5:1 for normal text, "
        "3:1 for large text (24px, or 19px bold). Darken the text or lighten the background.",
        '<p style="color:#aaa; background:#fff">Opening hours</p>',
        '<p style="color:#595959; background:#fff">Opening hours</p>',
    ),
    # --- forms ---
    "label": _fix(
        "Every form field needs a label. Use a <label for=\"…\"> pointing to the field's id "
        "(or wrap the field in the label). A placeholder is not a label.",
        '<input type="email" id="email" placeholder="Email">',
        '<label for="email">Email</label>\n<input type="email" id="email">',
    ),
    "select-name": _fix(
        "Drop-down lists (<select>) need a label, just like text fields.",
        '<select id="country">…</select>',
        '<label for="country">Country</label>\n<select id="country">…</select>',
    ),
    "button-name": _fix(
        "Buttons need text a screen reader can read. Icon-only buttons need an aria-label "
        "(or visually hidden text).",
        '<button><svg class="icon-close"></svg></button>',
        '<button aria-label="Close"><svg class="icon-close" aria-hidden="true"></svg></button>',
    ),
    # --- links ---
    "link-name": _fix(
        "Links need text that says where they go. Icon or image links need alt text or an aria-label.",
        '<a href="/cart"><i class="icon-cart"></i></a>',
        '<a href="/cart" aria-label="Shopping cart"><i class="icon-cart" aria-hidden="true"></i></a>',
    ),
    # --- document ---
    "html-has-lang": _fix(
        "Set the page's language on the <html> element so screen readers pronounce it correctly.",
        "<html>",
        '<html lang="en">',
    ),
    "html-lang-valid": _fix(
        "The lang attribute must be a valid language code (BCP 47), like \"en\" or \"fr-CA\".",
        '<html lang="english">',
        '<html lang="en">',
    ),
    "document-title": _fix(
        "Give every page a unique, descriptive <title>; it's the first thing a screen reader "
        "announces and what shows in browser tabs.",
        "<head>\n  <meta charset=\"utf-8\">\n</head>",
        "<head>\n  <meta charset=\"utf-8\">\n  <title>Contact us – Acme Inc.</title>\n</head>",
    ),
    "meta-viewport": _fix(
        "Don't stop people zooming. Remove user-scalable=no and any maximum-scale below 2.",
        '<meta name="viewport" content="width=device-width, user-scalable=no">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
    ),
    "bypass": _fix(
        "Give keyboard users a way to skip repeated navigation: a \"Skip to content\" link "
        "at the top of the page, or landmarks like <main>.",
        "<body>\n  <nav>…</nav>\n  <div>…content…</div>",
        '<body>\n  <a href="#main" class="skip-link">Skip to content</a>\n  <nav>…</nav>\n'
        '  <main id="main">…content…</main>',
    ),
    "duplicate-id": _fix(
        "Every id on a page must be unique. Duplicate ids break labels and ARIA references "
        "that point at them.",
        '<input id="name"> … <input id="name">',
        '<input id="first-name"> … <input id="last-name">',
    ),
    "duplicate-id-aria": _fix(
        "Ids referenced by ARIA attributes or labels must be unique, or the reference "
        "points at the wrong element.",
        '<p id="hint">8+ characters</p> <p id="hint">Optional</p>\n'
        '<input aria-describedby="hint">',
        '<p id="pw-hint">8+ characters</p> <p id="nick-hint">Optional</p>\n'
        '<input aria-describedby="pw-hint">',
    ),
    # --- structure ---
    "heading-order": _fix(
        "Don't skip heading levels: an <h2> section's subheadings are <h3>, not <h4>. "
        "Pick the level by structure, and style it with CSS.",
        "<h2>Products</h2>\n<h4>Shoes</h4>",
        "<h2>Products</h2>\n<h3>Shoes</h3>",
    ),
    "page-has-heading-one": _fix(
        "Give the page one <h1> that says what the page is about.",
        '<div class="title">Our services</div>',
        "<h1>Our services</h1>",
    ),
    "empty-heading": _fix(
        "Headings must contain text. Remove empty headings, or give icon headings an accessible name.",
        "<h2></h2>",
        "<h2>Latest news</h2>",
    ),
    "region": _fix(
        "Put all page content inside landmarks (<header>, <nav>, <main>, <footer>, …) so "
        "screen reader users can jump between sections.",
        '<div class="content">…</div>',
        '<main class="content">…</main>',
    ),
    "landmark-one-main": _fix(
        "Wrap the page's main content in exactly one <main> element.",
        '<div id="content">…</div>',
        '<main id="content">…</main>',
    ),
    "list": _fix(
        "<ul> and <ol> may only directly contain <li> (or <script>/<template>) elements.",
        "<ul>\n  <div>Item</div>\n</ul>",
        "<ul>\n  <li>Item</li>\n</ul>",
    ),
    "listitem": _fix(
        "<li> elements must be inside a <ul>, <ol> or <menu>.",
        "<div>\n  <li>Item</li>\n</div>",
        "<ul>\n  <li>Item</li>\n</ul>",
    ),
    "frame-title": _fix(
        "Give every <iframe> a title saying what it contains.",
        '<iframe src="https://maps.example/embed"></iframe>',
        '<iframe src="https://maps.example/embed" title="Map of our office"></iframe>',
    ),
    # --- ARIA ---
    "aria-required-attr": _fix(
        "Some ARIA roles need certain attributes. Add them (for example, a checkbox role "
        "needs aria-checked), or use the native HTML element instead.",
        '<div role="checkbox">Subscribe</div>',
        '<label><input type="checkbox"> Subscribe</label>',
    ),
    "aria-valid-attr": _fix(
        "Fix misspelled or made-up aria-* attributes; only those in the ARIA spec work.",
        '<button aria-lable="Close">×</button>',
        '<button aria-label="Close">×</button>',
    ),
    "aria-valid-attr-value": _fix(
        "Give ARIA attributes allowed values, e.g. true/false, or ids of elements that exist.",
        '<button aria-expanded="yes">Menu</button>',
        '<button aria-expanded="false">Menu</button>',
    ),
    "aria-allowed-attr": _fix(
        "Only use ARIA attributes allowed on the element's role; remove the others.",
        '<a href="/" aria-checked="true">Home</a>',
        '<a href="/" aria-current="page">Home</a>',
    ),
    "aria-roles": _fix(
        "Use only valid ARIA role names (or none, if a native element already fits).",
        '<div role="buton">Save</div>',
        "<button>Save</button>",
    ),
    "aria-hidden-focus": _fix(
        "Content hidden with aria-hidden=\"true\" must not contain anything focusable. Remove "
        "aria-hidden, or make the controls unfocusable (e.g. with the inert attribute).",
        '<div aria-hidden="true"><a href="/promo">Offer</a></div>',
        '<div aria-hidden="true" inert><a href="/promo">Offer</a></div>',
    ),
}


def get_fix(rule_id: str, description: str | None = None, help_url: str | None = None) -> Fix:
    """The fix for an axe rule.

    Rules in `FIXES` get the curated plain-English fix and examples. Any other
    rule falls back to axe's own `description` (pass the finding's description
    and help URL, as stored with it). The given `help_url` is attached either way.
    """
    entry = FIXES.get(rule_id)
    if entry is not None:
        return Fix(rule_id=rule_id, help_url=help_url or "", curated=True, **entry)
    summary = description or f"See the axe-core documentation for the {rule_id} rule."
    return Fix(rule_id=rule_id, summary=summary, help_url=help_url or "", curated=False)
