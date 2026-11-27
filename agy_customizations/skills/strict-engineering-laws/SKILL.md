---
name: strict-engineering-laws
description: "Use when writing, refactoring, or auditing code to enforce Fail-Fast, zero defaults/fallbacks, static typing, SSOT, and strict static checks. Do NOT use for prose."
metadata:
  author: MacSurmak <macsurmak.m02@gmail.com>
  tags:
    - engineering
    - code-quality
    - typing
    - fail-fast
    - static-analysis
---

# Strict Engineering Laws & Code Invariants

## Purpose

Defines non-negotiable code quality invariants enforced whenever writing, editing, refactoring, or auditing source code in any language. Complements process skills (planning, TDD, debugging) by constraining the code produced.

## Prerequisites

- Read target source files in full before proposing or applying edits; never patch blindly.
- Check which static checkers (`ruff`, `pyright`, `pytest`, or `devtool` if present) are available in the target environment before claiming verification.

## Instructions

1. **Proportionality and YAGNI:**
   - Match structure strictly to task scope; avoid unnecessary factories, wrappers, or registries where a pure function or single module suffices.
   - Apply identical typing, Fail-Fast, and determinism rules to small utility scripts and core services alike.
2. **Fail-Fast (Zero Silent Failures):**
   - Fail loudly and deterministically on violated data contracts, schema mismatches, or non-zero subprocess exit codes.
   - Never mask errors with sentinel fallbacks (`0`, `float('nan')`, `None`, empty collections, or zero-byte files).
   - Ban bare `except:`, broad `except Exception:` / `except BaseException:`, and catch-alls that swallow errors. Every `except` block must end with an explicit `raise` (no `pass`, `continue`, or fallback return). Ban `sys.exit()` outside `main()`.
3. **Configuration Strictness (Zero Defaults, Fallbacks, or Magic Constants):**
   - Ban default parameter values in new function and method signatures (including `= None`) so callers pass every argument explicitly.
   - Ban `.get(key, default)`, `os.getenv()`, `os.environ.get()`, `.setdefault()`, 3-arg `getattr()`, and `val or []` fallback expressions on contracts.
   - Ban magic numbers in logic (`PLR2004`). Store environment/business parameters in validated startup config and algorithmic constants in `Final[...]` module constants.
4. **Root-Cause Eradication (Zero Filesystem Heuristics):**
   - Trace failures to their root contract and fix the source rather than adding defensive checks downstream.
   - Ban filesystem and filename heuristics (`glob`, `fnmatch`, `.rglob()`, `getmtime`, regex state parsing from filenames).
5. **SSOT and Stateless Purity:**
   - Store stateful data transactionally (ACID SQL/3NF) rather than duplicating state in parallel caches or filenames.
   - Keep stateless modules pure: ban `global`, `nonlocal`, and module-level mutable collections (`list`, `dict`, `set`).
6. **Strict Layer Boundaries (DAG and Top-Level Imports):**
   - Maintain a strictly acyclic dependency graph (`Presentation -> Application -> Domain -> Infrastructure`).
   - Place all imports strictly at module top level (`PLC0415`); forbid lazy imports inside functions.
   - Keep orchestrator templates (Shell, CI/CD, Nextflow) free of multi-line inline Python or Bash scripts.
7. **I/O Hygiene and Subprocess Isolation:**
   - Run scratch work in isolated temporary directories cleaned up via `finally` or context managers; publish only validated outputs.
   - Pass explicit `env=...`, `timeout=...`, and `check=...` to every subprocess call and ensure child process cleanup.
8. **Zero Stubs and Zero Linter Suppressions:**
   - Ban `pass` (use docstrings for empty exception classes), `...`, `raise NotImplementedError`, `TODO`/`FIXME`/`HACK` comments, and suppression pragmas (`# type: ignore`, `# noqa`, `# pragma: no cover`).
   - Escape all dynamic output sent to shell, SQL, HTML, or Telegram.
9. **Strict DRY, Atomicity, and Total Static Typing:**
   - Implement non-trivial logic once in a canonical module. Keep cyclomatic complexity `<= 14` (`C901`), branches `<= 14` (`PLR0912`), and statements `<= 60` (`PLR0915`).
   - Annotate every function argument, return type, and module constant (`Final[...]`). Ban `Any`, `object`, and unparameterized `list`/`dict`/`set`/`tuple`. Mark every `@dataclass` as `frozen=True`. Write all code comments and commit messages in English (`RUF003`).

## Verification Gate

Before concluding any code change:
1. Run strict static linting and type checking across all modified files:
   - Python: `ruff check --select E,F,B,C901,PLC0415,PLR0912,PLR0915,PLR2004,RUF` and `pyright`, plus `pytest` if tests exist.
   - Shell: `bash -n` (and `shellcheck` if installed).
   - If `devtool` is installed in the target repository, also run `devtool check` and resolve every diagnostic.
2. Confirm zero imports inside functions (`PLC0415`), zero hardcoded secrets or magic literals, zero stubs (`pass`/`...`/`TODO`/`FIXME`), and zero linter suppression pragmas (`# noqa`, `# type: ignore`).
3. Report the exact commands run, exit codes, and verified results; never claim verification without running the checks.

## Examples

### Contract Enforcement Patterns

| Prohibited Anti-Pattern | Required Fail-Fast Contract |
| :--- | :--- |
| Reading config with `.get("timeout", 30.0)` or `os.getenv(...)` | Check key presence explicitly and raise `ConfigurationError` if missing or non-positive |
| Catching `except Exception:` and returning `None` or `[]` | Catch narrow domain exceptions only and re-raise with diagnostic context |
| Default arguments (`def run(retries: int = 3)`) | Require explicit parameters (`def run(retries: int) -> Result`) |
| Unparameterized `dict` / `list` or `Any` annotations | Fully parameterized types (`dict[str, float]`) and `Final[...]` constants |
| Mutable `@dataclass` or `# noqa` / `# type: ignore` | `@dataclass(frozen=True, slots=True)` with zero suppression pragmas |

## Limitations

- When integrating with third-party libraries whose base classes mandate specific method signatures or default arguments, keep the adapter boundary minimal and strictly typed without propagating loose types inward.
- Do not opportunistically refactor untouched legacy files unless explicitly requested.

## Troubleshooting

- **Pyright reports a dynamic third-party return type:** Narrow the value with explicit `isinstance(...)` checks at the infrastructure boundary rather than using `Any` or `# type: ignore`.
- **Complexity limit exceeded (`C901`/`PLR0915`):** Extract cohesive, pure helper functions with explicit typed parameters rather than nesting conditionals.
