Review the HTML email template and Jinja2 rendering code for this project.

If $ARGUMENTS is provided, read that specific file. Otherwise read both:
- `email_sender/templates/report.html`
- `email_sender/renderer.py`

Evaluate against all of the following:

## Email Client Compatibility (most important)
Email clients are hostile to modern CSS. Check for these specific issues:

- **`<style>` tag support**: Gmail strips `<style>` blocks in some contexts. Are critical layout styles also inlined on elements? Flag any layout-critical style that only exists in the `<style>` block.
- **Flexbox**: Poor support in Outlook. Are any layout-critical flexbox rules used? If so, flag and suggest `<table>`-based fallbacks for those sections.
- **CSS Grid**: Not supported in most email clients. Flag any grid usage.
- **CSS Variables (`var()`)**: Not supported in most email clients.
- **External fonts (`@font-face`, Google Fonts)**: Often stripped. Is the font stack falling back to system fonts correctly?
- **`background` shorthand**: Sometimes broken in Outlook — should be `background-color`.
- **`max-width` on body/wrapper**: Works in webmail but not Outlook desktop. Suggest a wrapper table approach if layout-critical.
- **Dark mode**: Is `@media (prefers-color-scheme: dark)` handled, or does the dark background rely on the `<style>` block that might be stripped?

## Jinja2 Template Correctness
- Are all template variables defined in `renderer.py`'s `template.render()` call?
- Are there any undefined variable risks (variables used in template but not always passed)?
- Are filters used correctly (`| list`, `| map`, `| selectattr` etc.)?
- Is `autoescape=True` set in the Environment? (it is — verify it's not been changed)
- Are any raw user-generated strings (Claude analysis text) safely escaped?

## Mobile Responsiveness (critical — most readers open on phone)
- Is there a `<meta name="viewport">` tag?
- Does the layout work on 320px screens without horizontal scrolling?
- Are font sizes readable on mobile (minimum 13px body, 16px for match teams)?
- **Coupon table**: On mobile, can someone read all 13 picks without scrolling horizontally? Consider hiding or abbreviating less important columns (Type, Confidence) on small screens.
- **Team names**: Do long team names (e.g. "Queens Park Rangers", "Sheffield Wednesday") display without truncation on mobile?
- **Section headers**: Are letter-spaced uppercase headers readable on small screens or do they get cut off?

## Language
- All user-facing text must be in English (not Swedish)
- The `<html lang="...">` attribute should be `lang="en"`
- Check for any remaining Swedish words in headers, labels, legends, or footer

## Accessibility
- Do images (if any) have `alt` attributes?
- Is colour contrast sufficient? (The dark theme uses `#e8eaf0` text on `#0f1117` — check grey text colours like `#5a6478` for sufficient contrast ratio of at least 4.5:1)

## Content / UX Review
- Is the coupon table easy to read at a glance? Can someone extract their 13 picks in under 10 seconds?
- Is the most important information (the coupon picks) visible without heavy scrolling?
- Are the single/double/full selections clearly visually differentiated?
- Is the responsible gambling footer present?

## Output
For each issue: show the problematic code, explain the risk, and provide a fixed version.
Separate into: **Breaks in major clients** (Gmail, Apple Mail, Outlook) vs **Minor improvements**.
