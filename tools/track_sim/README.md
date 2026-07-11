# track_sim

First-pass pygame racing simulation suite with three runnable programs:

- Car editor
- Track generator
- Track simulation (single car)

## Prerequisites

- Python 3.9+ with `venv` support
- macOS or Linux shell (`bash`)

## Program Launch

From repo root, run one of:

```bash
./tools/track_sim/bin/run_careditor.sh
./tools/track_sim/bin/run_trackgen.sh
./tools/track_sim/bin/run_tracksim.sh
```

Each launcher will:

1. Create `.venv` in `tools/track_sim` if missing.
2. Activate the environment.
3. Install dependencies from `requirements.txt`.
4. Run the selected program.
5. Deactivate the environment on exit.

Legacy compatibility launcher:

```bash
./tools/track_sim/bin/run.sh
```

This now starts the track simulation.

## Controls

### Car Editor

- Up/Down: select a field
- Left/Right: adjust selected field
- N: edit car name
- S: save to `cars/*.car`
- L: load first saved car
- Q: quit

### Track Generator

- G: generate a track
- R: reset current track
- D: discard and regenerate
- N: rename track
- S: save to `tracks/*.track`
- L: load latest track
- Q: quit

### Track Simulation

- L: load latest track
- N: start new race
- Arrow keys: drive
- Q: quit

## Project Structure

- `bin/`: launcher scripts
- `etc/`: program configuration files
- `src/common/`: shared models, IO, geometry, and physics
- `src/careditor/`: car editor program
- `src/trackgen/`: track generator program
- `src/tracksim/`: track simulation program
- `cars/`: saved car configs
- `tracks/`: saved track layouts

## Notes

- Window size is configured to 1600x900 for all programs.
- Track files use `.track` JSON format.
- Car files use `.car` JSON format.

## Troubleshooting

- If virtual environment creation fails, install Python with `venv` support.
- If pygame import fails, rerun one of the launch scripts to reinstall dependencies.
- If no track loads in simulation, generate and save one with track generator first.
