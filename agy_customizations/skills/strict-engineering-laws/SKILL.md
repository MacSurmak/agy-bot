---
name: strict-engineering-laws
description: "Use when writing, refactoring, or auditing source code in any programming language to enforce mandatory engineering invariants: Fail-Fast, zero hidden defaults or fallbacks, strict DAG top-level imports, SSOT, zero stubs, total static typing, and devtool/linter gate compliance. Complementary to process skills (planning, TDD, debugging)."
---

# Strict Engineering Laws & Code Invariants

This skill defines **non-negotiable code quality invariants** applied whenever writing, editing, or reviewing code. It does not replace workflow skills (such as planning, TDD, or systematic debugging); it constrains the exact code you are allowed to produce during those workflows.

## Prerequisites

- Read the actual target source files in full before proposing or applying any change; never edit blindly.
- Check whether `devtool`, `ruff`, or `pyright` is installed in the target repository before claiming verification.

## Mandatory Code Invariants

1. **Proportionality & YAGNI:**
   - Match structure strictly to task scope. Do not introduce factories, wrappers, registries, or broker abstractions where a pure function or single module suffices.
   - Apply the same typing, Fail-Fast, and determinism rules to small scripts as to core services.
2. **Fail-Fast (Zero Silent Failures):**
   - Fail loudly and deterministically on any violated data contract, schema mismatch, or non-zero subprocess exit code.
   - Never mask errors with sentinel fallbacks (`0`, `float('nan')`, `None`, empty collections, or zero-byte files).
   - Ban bare `except:`, broad `except Exception:` / `except BaseException:`, and multi-exception catch-alls that swallow errors. Every `except` block must end with an explicit `raise` (no `pass`, `continue`, or fallback return). Ban `sys.exit()` outside `main()`.
3. **Configuration Strictness (Zero Defaults, Fallbacks & Magic Constants):**
   - Ban default parameter values in new function and method signatures (including `= None`) — require explicit arguments from callers.
   - Ban `.get(key, default)`, `os.getenv()`, `os.environ.get()`, `.setdefault()`, 3-arg `getattr()`, and `val or []` fallback expressions on contracts.
   - Ban magic numbers in logic (`PLR2004`). Store environment/business parameters in validated startup config and algorithmic constants in `Final[...]` module constants.
4. **Root-Cause Eradication (Zero FS Heuristics):**
   - Trace failures to their root contract and fix the cause rather than adding defensive checks downstream.
   - Ban filesystem and filename heuristics (`glob`, `fnmatch`, `.rglob()`, `getmtime`, regex parsing of state from filenames).
5. **SSOT & Stateless Purity:**
   - Store stateful data transactionally (e.g., ACID SQL/3NF) rather than duplicating state in parallel caches or filenames.
   - Keep stateless modules pure: ban `global`, `nonlocal`, and module-level mutable collections (`list`, `dict`, `set`).
6. **Strict Layer Boundaries (DAG & Top-Level Imports):**
   - Maintain a strictly acyclic dependency graph (Presentation -> Application -> Domain -> Infrastructure).
   - Place all imports strictly at module top level (`PLC0415`); no lazy imports inside functions.
   - Keep orchestrator templates (Shell, CI/CD, Nextflow) free of multi-line inline Python/Bash scripts.
7. **I/O Hygiene & Subprocess Isolation:**
   - Perform scratch work in isolated temporary directories cleaned up via `finally` or context managers; publish only validated outputs.
   - Pass explicit `env=...`, `timeout=...`, and `check=...` to every subprocess call and ensure child process cleanup.
8. **Zero Stubs & Zero Linter Suppressions:**
   - Ban `pass` (use docstrings for empty exception classes), `...`, `raise NotImplementedError`, `TODO`/`FIXME`/`HACK` comments, and suppression pragmas (`# type: ignore`, `# noqa`, `# pragma: no cover`).
   - Escape all dynamic output sent to shell, SQL, HTML, or Telegram.
9. **Strict DRY, Atomicity & Total Static Typing:**
   - Implement non-trivial logic once in a canonical module. Keep cyclomatic complexity `<= 14` (`C901`), branches `<= 14` (`PLR0912`), and statements `<= 60` (`PLR0915`).
   - Annotate every function argument, return type, and module constant (`Final[...]`). Ban `Any`, `object`, and unparameterized `list`/`dict`/`set`/`tuple`. Mark every `@dataclass` as `frozen=True`. Write all code comments and commit messages in English (`RUF003`).

## Verification Gate

Before concluding any code change:
1. If `devtool` is available in the repository, run `devtool check` (or `devtool tree --stdout` before exploring) and resolve every Ruff, AST, cross-file contract, and Pyright diagnostic.
2. Otherwise run the repository's native static checks (`ruff check`, `pyright`, `pytest`) and report the exact exit code and output.

## Examples

### Example: Replacing Silent Fallback with Fail-Fast Contract
Bad (violates Laws 2 & 3):
```python
def load_timeout(cfg: dict) -> float:
    return float(cfg.get("timeout", 30.0))
```
Good:
```python
def load_timeout(cfg: dict[str, float | int | str]) -> float:
    if "timeout" not in cfg:
        raise ConfigurationError("Missing mandatory configuration key: 'timeout'.")
    timeout = float(cfg["timeout"])
    if timeout <= 0.0:
        raise ConfigurationError(f"Timeout must be strictly positive, got {timeout}.")
    return timeout
```

## Limitations

- When integrating with third-party libraries whose public base classes require specific method signatures or default arguments, keep the adapter boundary minimal and strictly typed without propagating loose types inward.
- Legacy files not touched by the current task should not be refactored opportunistically unless explicitly requested.

## Troubleshooting

- **Pyright complains about a missing stub or dynamic third-party return:** Narrow the return type with explicit `isinstance(...)` checks at the infrastructure boundary rather than introducing `Any` or `# type: ignore`.
- **Complexity limit exceeded (`C901`/`PLR0915`):** Extract cohesive, pure helper functions with explicit typed parameters rather than nesting conditionals.
