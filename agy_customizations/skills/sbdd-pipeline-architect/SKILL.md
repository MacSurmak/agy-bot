---
name: sbdd-pipeline-architect
description: "Use when working on sbdd_pipeline (Nextflow DSL2, Slurm, SQLite 3NF, Uni-Dock, GROMACS, AMBER tLEaP, /shared/sbdd_pipeline). Do NOT use for unrelated code."
metadata:
  author: MacSurmak
  tags:
    - sbdd
    - nextflow
    - slurm
    - gromacs
    - docking
---

# SBDD Pipeline Architectural Laws

## Purpose

Enforces the domain architecture, data model, cluster I/O isolation, and docking/MD invariants of the `sbdd_pipeline` computational chemistry platform under `/shared/sbdd_pipeline`.

## Prerequisites

- Confirm the target path is inside the `sbdd_pipeline` repository or `/shared/sbdd_pipeline` before applying these rules.
- Read the target cluster host passport in `/home/agy-agent/hosts/` to verify active Slurm partitions, GPU nodes, and Conda/Apptainer paths.

## Instructions

1. **SQLite 3NF as Single Source of Truth (SSOT):**
   - Track all pipeline entities, ligands, runs, poses, and statuses transactionally in SQLite 3NF (`PRAGMA foreign_keys = ON`, surrogate integer IDs, cascading foreign keys).
   - Never encode run status in filenames or scan directories with `glob`/`rglob` to infer pipeline state.
2. **Plan and Receipt Pattern for Cluster Jobs:**
   - Pass every Slurm or Nextflow step an immutable JSON plan containing canonical absolute paths, and require a structured JSON receipt upon completion.
   - Keep orchestrator templates (Nextflow DSL2, Slurm `.sbatch`) free of multi-line inline Python or Bash logic; invoke standalone typed CLI modules instead.
3. **Monolith Layout and Environment Separation:**
   - Store shared code and validated artifacts under `/shared/sbdd_pipeline`.
   - Execute Python workflows in the canonical `sbdd_cluster_env` Conda environment and containerized engines through `/opt/containers/bin` Apptainer wrappers. Manage cluster provisioning via Ansible.
4. **`$SLURM_TMPDIR` Local Scratch I/O Law:**
   - Keep all scratch I/O (docking conformers, GROMACS trajectories, temporary PDBQT/SDF files) strictly on node-local `$SLURM_TMPDIR` with guaranteed cleanup.
   - Copy only validated final artifacts back to `/shared/sbdd_pipeline` and record only permanent paths in SQLite.
5. **Chemistry and Docking Engine Rules:**
   - Deduplicate ligands strictly by Standard InChIKey.
   - **Uni-Dock** is the sole primary docking engine; **GNINA**, **Glide**, and **Prime** (Schrodinger) are legacy-only backends and must never receive new default pathways or CLI aliases.
   - Enforce zero CLI aliases across `sbdd` subcommands.
6. **Molecular Dynamics and Analysis Invariants:**
   - Parameterize complexes via AMBER `tLEaP` and build GROMACS index groups strictly by lexical group names (never numeric index offsets).
   - Measure ligand stability via MCS In-Situ RMSD rather than naive atom-order RMSD.
   - Compute cluster ETA and throughput telemetry using analytical EMA formulas.

## Examples

### Example: Structuring and Monitoring a Long-Running SBDD Workflow
1. Prepare an immutable JSON run plan referencing `/shared/sbdd_pipeline` inputs and `$SLURM_TMPDIR` scratch paths, and submit the Nextflow/Slurm workflow via its standalone CLI entrypoint.
2. **User notice before background execution:** Before starting any multi-minute background job or writing log/PID/exit-code markers to `/shared/sbdd_pipeline/runs/`, inform the user of the exact command and output paths and confirm they want to proceed.
3. **User notice before Telegram notifications:** Before creating a completion sensor via `mcp_telegram-core_create_sensor`, inform the user that job status updates will be sent to Telegram and proceed only with explicit user consent.

## Limitations

- Applies strictly to `sbdd_pipeline`; do not invoke these rules on `agy-bot`, homelab configs, or unrelated Python projects.
- Never block an interactive turn waiting for multi-minute `prep`, `md-setup`, `ORCA`, `GROMACS`, or `nextflow` jobs.

## Troubleshooting

- **Apptainer cannot see a file path:** Ensure the path resides under `/shared/` or `$SLURM_TMPDIR` (bind-mounted inside containers) rather than an unmounted home subdirectory.
- **GROMACS `make_ndx` selects the wrong group:** Replace numeric group IDs with explicit lexical group names (`Protein`, `LIG`, `Water_and_ions`).
- **SQLite `database is locked` under parallel Slurm workers:** Ensure WAL mode (`PRAGMA journal_mode=WAL`) and `busy_timeout` are enabled and only the orchestrator commits run state from receipts.
