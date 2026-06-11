"""Local POS-terminal emulator for barhandler-manager.

Speaks the *terminal* side of the SSI ECR JSON protocol so the manager can
discover, register and charge it exactly as it would a real Mono / Raif /
Pivdenny / generic-SSI terminal — no hardware required. Every Purchase pops
an arrow-key menu in the console where you choose approve / decline / cancel.

Run locally only (test tool). Not imported by the manager app and not wired
into any route.
"""
