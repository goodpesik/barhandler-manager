"""Local multi-bank POS-terminal emulator for barhandler-manager.

Speaks the *terminal* side of every ECR protocol the manager supports, so it
can discover, register and charge an emulated terminal exactly as it would
real hardware — no device required:

  - SSI ECR JSON       Monobank / generic SSI            (ssi_terminal)
  - PrivatBank JSON    PrivatBank                        (privat_terminal)
  - Printec PosAPI     Raiffeisen / PUMB                 (posapi_terminal)
  - BPOS1 / Light      Bank Pivdenny / Sense (Alfa)      (bpos_terminal)
  - Oschad ECR         Oschadbank                        (oschad_terminal)

`python -m emulator` lets you pick a bank at startup and switch banks while
idle. Every Purchase pops an approve / decline / cancel menu.

Run locally only (test tool). Not imported by the manager app.
"""
