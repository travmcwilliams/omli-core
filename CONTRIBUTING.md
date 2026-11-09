# Contributing to OMLi

Thanks for taking the time. This is a small project with one maintainer, so this page says exactly what to expect.

## How issues are handled

- I triage new issues **once a week**. Expect a first response within about a week, not within hours.
- Bugs with a minimal reproduction get looked at first.
- The public roadmap is the list of open issues labelled `roadmap`.

## Before you open a pull request

- **Small fixes** (a bug with a test, a doc correction): open the PR directly.
- **Anything that touches one of the five design rules** in the README: open an issue first and describe the change. These rules are the point of the project, so a change that works around one will not be merged, however useful it is.
- **Never added, by rule 5:** auto-tuning, or any code that lets an agent change its own pack, thresholds, identity or memory. A component may suggest a change; a human applies it.

## What a pull request needs

1. **A DCO sign-off on every commit:** `git commit -s`. This certifies you wrote the change or have the right to submit it under the Apache License 2.0 ([developercertificate.org](https://developercertificate.org)).
2. **Tests that pass on all three platforms in CI:** `pytest -q -rs`.
3. **For a bug fix, a regression test that fails without your fix and passes with it.** A test that guards behaviour that was already correct is welcome too; say so in its docstring.
4. **Comments and docstrings that describe the code as it is.** Use the present tense. Do not narrate history ("previously", "the fix", "now does X"); that belongs in the commit message. Do not claim behaviour the code does not have.
5. **No domain-specific names in `omli/`.** Put them in a pack.
6. **No new runtime dependency** without discussing it in an issue first. The core depends only on PyYAML.

## Running the checks locally

```bash
python -m pip install -e ".[dev,sign]"
pytest -q -rs
python examples/walkthrough.py
```

## Code of conduct

Be civil and specific. Critique code, not people. I will close threads that are not.
