# OracleCascade

OracleCascade is a three-contract chain on GenLayer, each deployed on top
of the address of the one before it. EventOracle lets anyone open a claim
about a real-world event with 2-8 possible outcomes and a source URL;
after a deadline, validators independently read that page and reach AI
consensus on which outcome the evidence supports. PredictionPool, built on
EventOracle's address, turns each resolved claim into a pari-mutuel
market: stakers back an outcome with GEN, and winners pull a payout
funded by the losing side, minus a small platform fee, once resolved.
ForecasterRank, built on PredictionPool's address, grants live
reputation-tier badges (SCOUT/SHARP/VETERAN/ORACLE) computed straight from
each forecaster's real win record -- never faked, never admin-revocable.
The only payable method never reverts, refunding instead, and a
grace-period safety valve stops an unresolved claim from trapping GEN
forever. Ships with 81 passing tests, validated against GenLayer's
official linter.

Three GenLayer Intelligent Contracts, deployed one after another, each one
built directly on top of the address of the one before it:

```
EventOracle            (deployed first, stands alone)
      |  its address is passed into...
      v
PredictionPool         (a pari-mutuel staking pool built on EventOracle)
      |  its address is passed into...
      v
ForecasterRank          (a live reputation-tier gate built on PredictionPool)
```

Nothing here is a factory that spawns children with `gl.deploy_contract`.
It is the other, equally idiomatic GenLayer pattern: deploy a contract,
copy its printed address, hand that address to the constructor of the next
contract, and repeat. Each later contract only ever talks to the one
immediately before it through a plain, typed `@gl.contract_interface`
`view()` call -- the same IC-to-IC pattern the GenLayer docs describe for
[interacting with other Intelligent Contracts](https://docs.genlayer.com/developers/intelligent-contracts/features/interacting-with-intelligent-contracts).
`ForecasterRank` never calls `EventOracle` directly and knows nothing about
claims, staking, or GEN at all -- it only sees whatever `PredictionPool`
chooses to expose.

## Why an AI resolver, and why three contracts

GenLayer's own use-case guidance singles out
["prediction markets with a natural-language resolution rule"](https://docs.genlayer.com/understand-genlayer-protocol/typical-use-cases)
as a strong fit for Intelligent Contracts: there is a real on-chain
consequence (staked GEN changes hands), the outcome requires judgment
(reading a page and deciding what it means), the evidence can be
independently checked (every validator fetches the same URL), and a
structured decision comes out the other end (one label from a short,
enumerated list).

`EventOracle` resolves an *open* set of 2-8 outcome labels supplied at
claim-creation time, not a hard-coded yes/no or a fixed two-team match. The
same deployed contract can resolve "who won the marathon"
(`ALICE|BOB|DRAW`), "did the shipment arrive on time" (`YES|NO`), or a
governance vote tally with five candidates, all without being redeployed.

Splitting the system into three contracts instead of one mirrors how a real
prediction market is actually layered: a resolver that only cares about
*truth*, a market that only cares about *money*, and a reputation system
that only cares about *track record* and does not need to know how either
of the first two work internally. Repointing `PredictionPool.set_oracle()`
at a different resolver, or `ForecasterRank.set_oracle()` at a different
pool, is possible precisely because each layer only depends on a handful of
narrow view methods from the one below it, not on its internals.

## The chain, in detail

### 1. `contracts/EventOracle.py` -- resolution

Anyone may call `create_claim(question, outcomes, resolution_url, criteria,
deadline_epoch)` to open a claim: a plain-language question, `"ALICE|BOB|DRAW"`
-style outcome labels, a URL expected to carry the answer, and a deadline
before which nobody may ask for a resolution.

After the deadline, anyone may permissionlessly call `resolve_claim(claim_id)`.
Validators independently render the page and ask an LLM which single label
the evidence supports; consensus is reached on exactly one field -- the
chosen label -- not on the page content and not on the model's reasoning
text. A claim that resolves to nothing usable (empty page, ambiguous
evidence) becomes `INCONCLUSIVE`, which is **not terminal** -- anyone may
call `resolve_claim` again later once real evidence exists.

`preview_resolution(claim_id)` runs the identical check without writing
anything, even before the deadline, so a creator can sanity-check their
wording. It degrades to a soft `RETRY_LATER` on infrastructure trouble
instead of raising, since it is meant to be a side-effect-free preview.

`EventOracle` never touches GEN, so every one of its write methods is free
to raise a plain `gl.vm.UserError` on a genuine problem -- a reverted call
here costs nothing and can simply be retried.

### 2. `contracts/PredictionPool.py` -- money

Constructed with `EventOracle`'s address. Anyone may `stake(claim_id,
outcome)` GEN on exactly one outcome of an `OPEN` claim before its deadline.
This is **pari-mutuel**, not escrow: stakers on the losing side are not
refunded, they are what funds the winners' payouts, exactly like a real
racetrack pool. `stake()` first checks `EventOracle.is_valid_outcome()`
before it takes a single wei -- a typo'd or made-up label ("ALICEE" instead
of "ALICE") can never be what `EventOracle` resolves to, so it is rejected
up front, with a refund, instead of silently taking money that could
mathematically never win. Outcome labels are case-insensitive identifiers
throughout, exactly like `EventOracle`'s own uniqueness rule at claim
creation -- staking "alice" and staking "ALICE" land in the same bucket and
are worth exactly the same at payout time.

Once `EventOracle` resolves the claim, anyone may permissionlessly call
`settle_pool(claim_id)`. This does **O(1) bookkeeping only** -- it
deliberately never iterates the stakers, because a pool can have an
unbounded number of them. Instead, each staker calls `claim_payout(claim_id)`
themselves afterwards -- a pull payment, the only workable pattern once the
number of counterparties isn't fixed at two, unlike a typical two-party
escrow contract.

```
winner's payout = (total_staked - platform_fee) * their_stake / winning_total
```

A worked example, taken directly from the test suite: three stakers put in
3, 1, and 4 GEN; the 3- and 1-GEN stakers back the winning outcome, the
4-GEN staker backs the loser. At the default 3% fee, the platform keeps
0.24 GEN, the remaining 7.76 GEN pool splits `5.82 / 1.94` between the two
winners in exact proportion to their stake, and the loser's 4 GEN is what
made that possible. Because payouts use integer division, a few wei of
rounding dust can remain unclaimed after every winner has claimed -- nobody
can withdraw it individually and it is not swept into the platform fee.
This is intentional and harmless, not a bug.

**Money-safety rule.** `stake()` is the *only* `@gl.public.write.payable`
method anywhere in this three-contract chain, so it is the only one that
can ever receive GEN it did not ask for. It must never raise --
`gl.vm.UserError` rolls back storage but not value that already rode in
with the call. Every rejection inside `stake()` (paused, wrong outcome
already staked, an outcome the claim never offered, deadline passed, claim
not open, amount out of bounds) therefore refunds the sender in place and
returns `{"ok": false, ...}` instead of raising. `test/test_no_raise_in_payable.py`
enforces this by walking the actual AST of all three contract files: it
fails the build if `stake()` ever gains a `raise`, or if any *other* method
in the chain is ever marked payable without that same guarantee being
re-derived for it.

That guarantee has to survive a misbehaving *dependency*, not just
misbehaving *input* -- an AST check of this file alone cannot prove that,
since the failure would come from a different contract's code. Every
EventOracle view() call `_stake_problem()` makes (status, deadline, outcome
validity) is wrapped by `_try_oracle()`, which turns any exception the
oracle raises -- unreachable address, a reverted view method, an owner
having repointed `oracle` at something that does not even implement these
methods -- into an ordinary rejection string instead of letting it escape
`stake()`. `test/test_prediction_pool.py` proves this with a fake oracle
whose view calls can be told to raise on command
(`test_stake_refunds_when_oracle_*_view_call_raises`).

`settle_pool()` and `claim_payout()` carry no value, so -- exactly like
`EventOracle.resolve_claim()` -- they are free to raise on a genuine error,
oracle-originated or not; nothing is lost when they revert.

**Two-sided "nobody gets trapped" guarantee.** A resolved claim settles
normally. A claim that gets `CANCELED` in `EventOracle` voids the pool, and
every staker reclaims their own principal through `claim_payout`. A claim
that simply never gets resolved does not trap GEN forever either: once
`RESOLUTION_GRACE_SECONDS` (14 days) has passed after its own deadline with
still no resolution, anyone may call `settle_pool` to void the pool the
same way.

**Fee accounting.** `pool_locked` tracks GEN this contract still owes to
stakers -- it is what `withdraw_platform_fees()` subtracts from the
contract's real on-chain balance to make sure the owner can never sweep
money a staker or a winner is still owed. The platform's cut is carved out
of `pool_locked` the moment `settle_pool()` computes it, not only once it
is actually swept: `pool_locked` is credited by every `stake()` and debited
by `fee` at settlement and by each winner's own payout at `claim_payout()`
time, converging to exactly zero (plus unclaimed rounding dust) once every
winner has claimed -- leaving precisely the fee behind as free,
owner-withdrawable balance.

`PredictionPool` also keeps a small `Forecaster` reputation record per
address -- resolved-stake count, wins, losses, GEN staked, GEN won -- purely
as a side effect of normal use. That record is the entire surface
`ForecasterRank` is built on.

### 3. `contracts/ForecasterRank.py` -- reputation

Constructed with `PredictionPool`'s address. Exposes a small ladder of
named tiers (`SCOUT` / `SHARP` / `VETERAN` / `ORACLE` by default, each with
its own minimum resolved-win count) which the owner can extend or retune
with `add_or_update_tier(name, min_wins)`.

Eligibility and badge records are deliberately two separate things:

- **Eligibility** is always computed live, on the spot, from
  `PredictionPool.has_forecaster_track_record()`. It can never be faked and
  it can never be taken away by an admin -- a forecaster with 12 recorded
  wins is `SHARP`-eligible forever, independent of anything stored in
  `ForecasterRank` itself.
- A **badge record** is what gets written when someone actually calls
  `request_badge(tier)` -- a timestamped receipt that eligibility was
  checked and confirmed on that date. The owner may `revoke_badge(...)` a
  *record* for administrative reasons; that revocation has zero effect on
  live eligibility, and calling `request_badge` again simply re-issues it.

`ForecasterRank` carries no value and calls nothing but `PredictionPool`'s
view methods, so none of its methods needs to guard against raising.

## Trust and admin power

Each contract has an `owner`, set to its deployer, who can pause new
activity (`set_paused`), retune parameters, repoint a downstream contract's
`oracle` at a different upstream address, and transfer ownership. The owner
can never touch anyone's staked or escrowed funds directly -- there is no
"admin withdraw someone else's stake" method anywhere -- but repointing
`set_oracle()` at a malicious or broken contract would let an owner corrupt
what downstream users see, exactly as it would in any address-composed
system. Treat the constructor address you pass to `PredictionPool` and
`ForecasterRank` the same way you would treat any other trusted dependency.

## Repository layout

```
contracts/
  EventOracle.py       link 1 -- enumerated-outcome AI resolution
  PredictionPool.py    link 2 -- pari-mutuel staking, built on EventOracle
  ForecasterRank.py    link 3 -- reputation tiers, built on PredictionPool
test/
  genlayer_stub.py             minimal offline stand-in for the GenVM SDK
  test_event_oracle.py         unit tests for EventOracle
  test_prediction_pool.py      unit tests for PredictionPool (fake oracle)
  test_forecaster_rank.py      unit tests for ForecasterRank (fake pool)
  test_chain_integration.py    real EventOracle -> real PredictionPool ->
                                real ForecasterRank, wired the way they
                                would actually be deployed
  test_no_raise_in_payable.py  AST check enforcing the money-safety rule
```

## Testing

No GenLayer Studio, no Docker, and no `genlayer` package are required to
run the test suite -- `test/genlayer_stub.py` is a small, dependency-free
stand-in for the parts of the GenVM SDK these three files actually use
(`Address`, the sized-int aliases, `TreeMap`/`DynArray` with the documented
zero-initialised defaults, `allow_storage`, `gl.Contract`,
`gl.public.view`/`write`/`write.payable`, `gl.message`, `gl.vm.UserError` /
`Return` / `run_nondet_unsafe`, `gl.nondet.web.render` / `exec_prompt`,
`gl.contract_interface`, and `gl.evm.contract_interface`), so the tests
exercise the real shipped contract logic directly.

```bash
python3 -m unittest discover -s test -v
```

85 tests, no real network calls, no real `time.sleep` (deadlines are
crossed by monkeypatching each module's own `_now_epoch()`, not by
waiting). Coverage includes every validation rule in `create_claim`, the
`OPEN -> RESOLVED / INCONCLUSIVE -> RESOLVED` retry path, both
error-classification branches (`ERR_TRANSIENT_*` vs `ERR_LLM_MALFORMED`),
exact pari-mutuel arithmetic checked against hand-computed numbers (not
just re-derived from the contract's own formula), the `SETTLED` /
`VOID` / grace-period paths, and the live-eligibility-vs-badge-record
distinction in `ForecasterRank`. It also includes targeted regression
tests for four issues earlier versions of `PredictionPool.py` had --
platform fees that could never actually be swept because `pool_locked`
was never reduced by the fee at settlement, a stake accepted on an
outcome label the claim never offered, a stake recorded in a different
case than the outcome it was later compared against at payout time, and
a raising upstream oracle view call escaping `stake()` before the
sender's GEN was refunded -- each written so it demonstrably fails
against the old, buggy code and passes against the fix (see the inline
comments next to `_canon_outcome()`, `is_valid_outcome()`, `_try_oracle()`,
and the `pool_locked` line in `settle_pool()` for the reasoning). The
platform-fee sweep delay is covered too.

The three contract files also pass the real, official linter end to end:

```bash
pip install genvm-linter
genvm-lint check contracts/EventOracle.py
genvm-lint check contracts/PredictionPool.py
genvm-lint check contracts/ForecasterRank.py
```

Each one reports `✓ Lint passed` (fast AST safety checks) *and*
`✓ Validation passed` (full SDK-based semantic validation against the real
GenVM Python runtime) -- this is not merely `ast`-clean, it is checked
against the actual GenVM SDK types and decorators.

## Deployed on Studionet

A live instance of the full chain, including the `stake()` oracle-failure
fix described above, is deployed on Studionet:

| Contract | Address |
|---|---|
| `EventOracle` | `0xf0a02Fb3F4E5533CB0805e22DC0C1c1DfF5af113` |
| `PredictionPool` | `0x9eEC9e2Fac1fB6a37C25A2bA60f3F1C350A6d90f` |
| `ForecasterRank` | `0x82Fc56a730596Bf1B94345027EE0cCe6FAB7dAb3` |

`EventOracle`'s address is unchanged from earlier in this project;
`PredictionPool` and `ForecasterRank` were both redeployed fresh so that
every method on all three, including `stake()`'s new oracle-failure
handling, matches the source in this repository -- Intelligent Contracts
are immutable once deployed and this project sets up no upgrade path, so a
source change can only ever show up at a new address, never by patching an
old one in place (see
[Upgradability](https://docs.genlayer.com/developers/intelligent-contracts/features/upgradability)).

These addresses are recorded here as given, not independently verified --
nothing available to whatever produced this README can query Studionet, so
confirm the wiring yourself before relying on it:

```bash
genlayer network set studionet
genlayer call 0x9eEC9e2Fac1fB6a37C25A2bA60f3F1C350A6d90f get_config
# should report the EventOracle address above
genlayer call 0x82Fc56a730596Bf1B94345027EE0cCe6FAB7dAb3 get_config
# should report the PredictionPool address above
```

Two earlier addresses for this chain,
`PredictionPool` at `0xa2140495FE18Ca31b7f2CABFCc5175f4a1049097` and
`ForecasterRank` at `0x93e928Ae4aF682635576B07A895A832eAcFD18b6`, predate
this fix (and, depending on exactly when each was deployed, possibly the
fee-lock, outcome-validation, or case-sensitivity fixes from earlier in
this project's history too) and are superseded by the pair above. Treat
them as retired rather than assuming they behave like the current source.

## Deploying to Studionet

Deploy in order, and hand each printed address to the next constructor --
this is the entire point of the chain.

```bash
genlayer network set studionet

# 1. EventOracle takes no constructor arguments
genlayer deploy --contract contracts/EventOracle.py
# -> note the printed address, e.g. EVENT_ORACLE=0xabc...

# 2. PredictionPool needs EventOracle's address, plus an optional fee (bps)
genlayer deploy --contract contracts/PredictionPool.py \
  --args "$EVENT_ORACLE" 300
# -> note the printed address, e.g. PREDICTION_POOL=0xdef...

# 3. ForecasterRank needs PredictionPool's address
genlayer deploy --contract contracts/ForecasterRank.py \
  --args "$PREDICTION_POOL"
```

The GenLayer Studio UI works the same way: deploy `EventOracle.py` first,
copy its contract address from the Studio UI, paste it as the constructor
argument when deploying `PredictionPool.py`, then repeat for
`ForecasterRank.py` with `PredictionPool`'s address.

### Trying it end to end

Point the same three environment variables at the already-deployed
instance above instead of a fresh deployment if you just want to interact
with it:

```bash
export EVENT_ORACLE=0xf0a02Fb3F4E5533CB0805e22DC0C1c1DfF5af113
export PREDICTION_POOL=0x9eEC9e2Fac1fB6a37C25A2bA60f3F1C350A6d90f
export FORECASTER_RANK=0x82Fc56a730596Bf1B94345027EE0cCe6FAB7dAb3
```

```bash
# open a claim, at least 60 seconds (MIN_LEAD_SECONDS) in the future
genlayer write "$EVENT_ORACLE" create_claim \
  --args "Who wins the cup final?" "REDS|BLUES" \
         "https://example.org/final-result" \
         "Use the official league result only." 1798761600

# stake on the pool built on top of it -- note the "value" flag, since
# stake() is the one payable method in this whole chain
genlayer write "$PREDICTION_POOL" stake --args 1 "REDS" --value 3gen

# after the deadline, anyone can trigger resolution and settlement
genlayer write "$EVENT_ORACLE" resolve_claim --args 1
genlayer write "$PREDICTION_POOL" settle_pool --args 1
genlayer write "$PREDICTION_POOL" claim_payout --args 1

# check whether that stake now qualifies for a badge on the rank contract
genlayer call "$FORECASTER_RANK" is_eligible --args "$MY_ADDRESS" "SCOUT"
genlayer write "$FORECASTER_RANK" request_badge --args "SCOUT"
```

(Exact `genlayer write`/`call` flag names -- `--value`, `--fee-profile`, and
so on -- depend on the CLI version installed; see
[CLI Deployment](https://docs.genlayer.com/developers/intelligent-contracts/deploying/cli-deployment)
and [Writing to Intelligent Contracts](https://docs.genlayer.com/developers/decentralized-applications/writing-data)
for the current reference.)
