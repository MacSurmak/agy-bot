---
name: sbdd-pipeline-architect
description: "Use ONLY when working on the sbdd_pipeline project (Slurm, Nextflow DSL2, SQLite 3NF, GROMACS, AMBER tLEaP, GNINA, Uni-Dock, Schrodinger, sbdd watch, or /shared/sbdd_pipeline). Do NOT use for general coding or unrelated projects."
---

# SBDD Pipeline Architectural Laws

## Prerequisites

- Verify the target path is inside the `sbdd_pipeline` repository or `/shared/sbdd_pipeline` before applying these rules.
- Read the target host's on-demand passport (`~/.gemini/config/hosts/infra-<alias>.md`) to confirm exact Slurm partitions, GPU nodes, and Conda/Apptainer paths.

## Architectural Invariants

1. **SQLite 3NF as Single Source of Truth (SSOT):**
   - All pipeline entities, ligands, runs, poses, and statuses are tracked transactionally in SQLite 3NF (`PRAGMA foreign_keys = ON`, surrogate integer IDs, cascading FKs).
   - Never encode run status in filenames or scan directories with `glob`/`rglob` to infer state.
2. **Plan & Receipt Pattern for Cluster Jobs:**
   - Every Slurm/Nextflow step receives an immutable JSON plan with canonical absolute paths and emits a structured JSON receipt on completion.
   - Orchestrator templates (Nextflow DSL2, Slurm `.sbatch`) must never contain multi-line inline Python or Bash logic; call standalone typed CLI modules instead.
3. **Monolith Layout & Environment Separation:**
   - Code and shared artifacts live under `/shared/sbdd_pipeline`.
   - Python/scientific dependencies run in the canonical `sbdd_cluster_env` Conda environment; containerized engines invoke `/opt/containers/bin` Apptainer wrappers. Cluster provisioning is managed via Ansible.
4. **`$SLURM_TMPDIR` I/O Law:**
   - All scratch I/O (docking conformers, GROMACS trajectories, temporary PDBQT/SDF files) runs strictly on node-local `$SLURM_TMPDIR` with guaranteed cleanup.
   - Copy only validated final outputs back to `/shared/sbdd_pipeline` and record only permanent paths in SQLite.
5. **Chemistry & Docking Engine Rules:**
   - Deduplicate ligands strictly by Standard InChIKey.
   - **Uni-Dock** is the sole primary docking engine; **GNINA**, **Glide**, and **Prime** (Schrodinger) are legacy-only backends and must never receive new default pathways or CLI aliases.
   - Enforce zero CLI aliases across `sbdd` subcommands.
6. **Molecular Dynamics & Analysis Invariants:**
   - Parameterize complexes via AMBER `tLEaP` and build GROMACS index groups strictly by lexical group names (never brittle numeric index offsets).
   - Compute ligand stability via MCS In-Situ RMSD rather than naive atom-order RMSD.
   - Track cluster ETA and throughput telemetry using analytical EMA formulas.

## Examples

### Example: Launching a Detached Multi-Minute SBDD Run
1. Launch `sbdd` or `nextflow` detached on the cluster head node with an exit-code marker (`setsid bash -c '...; echo $? > /shared/sbdd_pipeline/runs/r1.exit' > /shared/sbdd_pipeline/runs/r1.log 2>&1 < /dev/null & echo $! > /shared/sbdd_pipeline/runs/r1.pid`).
2. Attach a dual-condition sensor via `mcp_telegram-core_create_sensor` and end the turn immediately.

## Limitations

- Applies strictly to `sbdd_pipeline`; do not invoke these rules on `agy-bot`, homelab configs, or unrelated Python projects.
- Never block a Telegram turn waiting for long `prep`, `md-setup`, `ORCA`, `GROMACS`, or `nextflow` jobs.

## Troubleshooting

- **Apptainer cannot see a file path:** Ensure the path resides under `/shared/` or `$SLURM_TMPDIR` (mounted inside containers) rather than an unmounted home subdirectory.
- **GROMACS `make_ndx` selects the wrong group:** Replace numeric group IDs with explicit lexical group names (`Protein`, `LIG`, `Water_and_ions`).
- **SQLite `database is locked` under parallel Slurm workers:** Ensure WAL mode (`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=...`) is configured and only the orchestrator writes run state via receipts.
