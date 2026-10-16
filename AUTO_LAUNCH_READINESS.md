# Automatic Launch Readiness Gate

This PR contains pieces of a safe automatic launcher but **does not yet contain
a verified end-to-end, unattended activation flow**. Specifically, uncertain
Amazon POSTs cannot be replayed safely without inspecting Amazon state, and
the current launch coordinator does not invoke every provisioning stage.

Do not configure a scheduler to launch until ALL of the following pass:
1. Every live create has a durable intent, a globally unique launch key, and
   profile-scoped lock, including cross-process and restart tests.
2. Confirmed Amazon campaign, group, product ad and targets are reconciled
   by canonical IDs before any retry or activation.
3. Budgets and bids come from fresh profit policy and actual verified product
   economics; no fallback bids or hardcoded daily budget.
4. Guard's account-wide reservations are consistent with scheduler runs.
5. Seller, SKU, ASIN and account profile identity are verified.
6. CI full suite and a non-production integration test both pass.
7. Authenticated Cloud Scheduler trigger and server-side opt-in are provisioned
   deliberately, with a tested kill switch.
8. No changes affect current production ad spend until separately approved.

Current scheduler route fails closed if factory wiring is absent. Changing
AUTO_LAUNCH_ENABLED by itself does NOT start campaign launches.
