# Archive

This directory holds dead scripts from `code/` that have been superseded or were one-off exploratory tools. The three `make_demo_*` scripts here must never be run against the live page — they read the mock template (`jason-demo.html`) and would clobber `jason-demo.live.html`'s hand-written live JSX if their output were re-injected.

- `make_demo_data.py` — Graph-4 v1, freeciv-only; superseded by `make_demo_combined.py`.
- `make_demo_avg.py` — Graph-4 v3, scatter-only; superseded by `make_demo_combined.py`.
- `make_demo_pandemic.py` — Graph-4 v2, pandemic-only; superseded by `make_demo_combined.py`.
- `freeciv_subset_hunt.py` — exploratory one-off; its conclusions are frozen as the `FC_HORIZONS`/`FC_BAND` constants in `make_demo_combined.py`; it handed off via `/tmp/fc_best_subset.json`, which no longer exists.
- `freeciv_subset_gradient.py` — exploratory one-off; its conclusions are frozen as the `FC_HORIZONS`/`FC_BAND` constants in `make_demo_combined.py`.
- `freeciv_discrimination.py` — exploratory one-off; its conclusions are frozen as the `FC_HORIZONS`/`FC_BAND` constants in `make_demo_combined.py`.
- `plot_freeciv_trend.py` — depends on the vanished `/tmp/fc_best_subset.json`.
- `plot_avg_trend.py` — depends on the vanished `/tmp/fc_best_subset.json`.
- `plot_graph4_panels.py` — matplotlib preview superseded by the in-page SVG charts.
- `plot_graph4_calibration_preview.py` — matplotlib preview superseded by the in-page SVG charts.
