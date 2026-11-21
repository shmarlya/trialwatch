# Python style: readable for people who don't read Python

TrialWatch's code is read by people who know what the project does but not Python's shortcuts.
So the code says each step out loud, even when Python could say it in one line. Longer and plain
beats short and clever.

## Rules

1. **Loops instead of comprehensions.** Build lists, dicts and sets with a `for` loop and
   `append` / assignment. No `[x for x in ...]`, `{k: v for ...}`, `{x for ...}`, and no generator
   expressions inside calls (`sum(x for ...)`, `", ".join(x for ...)`, `any(...)`, `all(...)`).

   ```python
   # not this
   keys = [q.key for q in questions if q.key.startswith("endpoint:")]
   # but this
   keys = []
   for question in questions:
       if question.key.startswith("endpoint:"):
           keys.append(question.key)
   ```

2. **Name the question instead of `any()` / `all()`.** Write a small function whose name says
   what it checks, with a plain loop inside (`def some_endpoint_not_reported(values): ...`).

3. **`if` statements instead of inline conditions.** No `a if condition else b`, and no `or` /
   `and` used to pick a value (`name or "unknown"`, `items or []`). Write the `if` out.

4. **No lambdas, no functions defined inside functions.** Give every function a name at module
   level and pass what it needs as arguments.

5. **No star-unpacking.** No `*rest, last = row` and no `(*values, extra)`. Take items by name
   or position, one per line. Unpacking a function's documented pair result
   (`snapshot_id, payload = latest_snapshot(...)`) is fine. The one allowed `**` is passing a
   prepared argument dictionary to a library call, with a comment saying so.

6. **No dictionary tricks.** No `setdefault`, no `dict.fromkeys`, no `dict(zip(...))`, no `|` to
   merge. Check `if key not in d:` and assign.

7. **No arithmetic on booleans.** Not `count += found`; write `if found: count += 1`.

8. **One step per line, named values.** Give intermediate results a name that says what they
   are; avoid chaining several calls or conditions on one line. Prefer full names (`question`,
   not `q`; `page`, not `p`).

9. **Comments say why, not what.** The code says what happens; a comment explains a reason, a
   rule from the domain (Bruckner et al., CTIS, ADR-001), or a trap.

## Kept on purpose

These are part of how the libraries are used and read naturally: `with` blocks, decorators
(`@dataclass`, `@app.command`), type hints, f-strings, `enumerate`, `try` / `except`, simple
`for` loops over dictionaries' `.items()`, and simple unpacking of a returned pair.

## Not changed by a style edit

Behaviour, public function names, CLI options and output, SQL text, and test expectations. A
style edit that changes a test result is a bug.
