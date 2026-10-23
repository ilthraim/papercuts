Fixtures for the CEGIS tests (test_cegis_*.py), copied from smt_ec/tests/fixtures
(rewriting repo, 2026-10-23).

- `<name>.btor` / `.info`: miters built by smt_ec's older gen_btor.sh. None has
  an X source, so they read the same as ones built by `netlist.build_btor`.
- `*_wrapper.sv`: a golden module and its hand-muxed twin (plus the old wrapper,
  unused here). test_cegis_selection.py splits them into golden and muxed files
  and writes a manifest for them.
