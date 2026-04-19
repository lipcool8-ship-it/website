# Trading OS Playbook

## Operating Mode (Current)
Manual execution only.  
Use this file as a strict checklist before any capital decision.

## Pre-Trade Gatekeeper (Manual)
- [ ] Ticker/instrument is in approved universe.
- [ ] Thesis is clear, specific, and falsifiable.
- [ ] 3+ material risks are identified.
- [ ] Disconfirming evidence is documented.
- [ ] Entry trigger is explicit.
- [ ] Invalidation trigger is explicit.
- [ ] Exit logic (target and/or conditions) is explicit.
- [ ] Position size rationale is explicit.
- [ ] Decision logged in `portfolio_state.json`.

## Weekly Review (Manual)
- [ ] Review all open theses against invalidation criteria.
- [ ] Mark each thesis: intact / weakening / invalidated.
- [ ] Capture mistakes, misses, and process violations.
- [ ] Update any approved-universe changes (if any).
- [ ] Record next-week focus items.

## Escalation Rules
- If a pre-trade answer is missing, stop.
- If invalidation is hit, thesis is invalidated.
- If process was violated, log it and reduce risk until discipline is restored.

