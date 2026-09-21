# Roadmap

- [x] MoTeC `.ld` export, ported from [`sim-to-motec`](https://github.com/GeekyDeaks/sim-to-motec),
      round-trip verified against [`gotzl/ldparser`](https://github.com/gotzl/ldparser).
- [x] Raw capture layer, verbatim shared-memory pages alongside the CSV.
- [ ] Channel ledger: scan the raw archive and report, per field, whether it
      ever varies and across how many cars, stages and game versions. Makes
      patch day a one-command diff. A field that never moves is reported as
      *not observed to vary*, never as "disabled": `turbo` reads zero on a
      naturally aspirated car and `numberOfTyresOut` reads zero if you stay on
      the road, and neither is evidence about the game.
- [ ] Export reads the ledger, so flat channels are marked rather than dropped.
- [ ] Delta-over-distance between two runs on the same stage.
- [ ] Derived channels: coast time while yaw is stable, steering corrections
      with stage geometry removed, understeer/oversteer balance.
