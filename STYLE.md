# Python style: readable for people who don't read Python

TrialWatch's code is read by people who know what the project does but not Python's shortcuts. So the code
says each step out loud. Plain beats clever; short is fine while it stays plain.

Ruff checks what a tool can see: `uv run ruff check` and `uv run ruff format`, with the rules in
`pyproject.toml`, run in CI and as a pre-commit hook. The rest is for review.

## Required

1. **One step per line, named values.** Give intermediate results a name that says what they are; don't
   chain several calls or conditions on one line.

2. **Full names.** `question`, not `q`; `page`, not `p`. Comprehension variables too.

3. **Comments say why, not what.** The code says what happens; a comment explains a reason, a rule from
   the domain (Bruckner et al., CTIS, ADR-001), or a trap.

4. **No lambdas, no functions defined inside functions.** Give every function a name at module level and
   pass what it needs as arguments.

## Allowed while they stay simple

5. **One-level comprehensions.** One `for`, at most one `if`, a plain expression, on one line:

   ```python
   keys = [question.key for question in questions if question.key.startswith("endpoint:")]
   ```

   Two `for`s, an `if` / `else` inside, or a comprehension that needs a second line becomes a loop:

   ```python
   rows = []
   for trial in trials:
       for site in trial.sites:
           if site.country in countries:
               rows.append((trial.id, site.city))
   ```

6. **`any()` and `all()`** over a one-level generator, when the question reads at a glance
   (`if any(page.is_readable for page in pages):`). When it needs more, name the question: a small function
   with a plain loop inside (`def some_endpoint_not_reported(values): ...`).

7. **Short ternaries.** `label = "full" if complete else "not full"`: one line, plain values, never nested.
   Anything longer is an `if` statement.

## Still avoid

8. **`or` / `and` to pick a value** (`name or "unknown"`, `items or []`): `0`, `""` and `[]` count as false
   too. Write the `if`, or a ternary that says what it tests (`name if name is not None else "unknown"`).

9. **Star-unpacking.** No `*rest, last = row` and no `(*values, extra)`; take items by name or position.
   Unpacking a function's documented pair result (`snapshot_id, payload = latest_snapshot(...)`) is fine.
   The one allowed `**` is passing a prepared argument dictionary to a library call, with a comment
   saying so.

10. **Dictionary tricks.** No `setdefault`, no `dict.fromkeys`, no `dict(zip(...))`, no `|` to merge.
    Check `if key not in d:` and assign.

11. **Arithmetic on booleans.** Not `count += found`; write `if found: count += 1`.

## Kept on purpose

These are part of how the libraries are used and read naturally: `with` blocks, decorators
(`@dataclass`, `@app.command`), type hints, f-strings, `enumerate`, `try` / `except`, simple
`for` loops over dictionaries' `.items()`, and simple unpacking of a returned pair.

## Not changed by a style edit

Behaviour, public function names, CLI options and output, SQL text, and test expectations. A
style edit that changes a test result is a bug. Code written under the stricter rules keeps its loops
until its own refactor touches it; nobody rewrites loops into comprehensions in a sweep.

## UI (web interface)

The brand and components live in `src/trialwatch/web/`:

- `static/brand.css` holds the tokens: brand ink navy and teal accent, warm neutral greys, status colours, type scale, spacing, radius and focus ring, for light and dark. It is the only file with raw colours or sizes. Status colours have fixed meanings: full = green, not full = red, undecided = amber. `tests/test_web.py` fails if a text colour on its background falls below WCAG AA (4.5:1) in either theme.
- `static/components.css` holds the components built from those tokens: app header, button (primary, secondary, danger, small), badge, card (`.panel`), alert (error, warn, info, success), table, form field, tabs, stat list, key-value list, pager.
- `templates/components.html` holds the macros that render them (`badge`, `button`, `link_button`, `alert`, `card`, `page_header`, `tabs`, `field`, `stat`). `templates/macros.html` holds the status badges built on them (`label`, `run_status`, `outcome`, `evidence_state`).
- `static/logo.svg` and `static/favicon.svg` are the mark: an eye whose pupil is a check (we watch the register, we verify what it says).

Use a macro or a component class rather than a new one-off class, and never a `style="…"` attribute. A style only one page needs goes in the `<style>` block of `base.html`, written with tokens. Keyboard focus must stay visible (`:focus-visible` uses the focus ring), and the current page's nav link carries `aria-current="page"`.
