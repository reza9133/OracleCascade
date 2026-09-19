# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
from dataclasses import dataclass
from datetime import datetime, timezone
import json

# ============================================================================
# PredictionPool -- link 2 of 3: a pari-mutuel staking pool built on top of
# a deployed EventOracle
# ============================================================================
#
# This contract is deployed AFTER EventOracle, with EventOracle's on-chain
# address passed straight into its constructor:
#
#     genlayer deploy --contract contracts/EventOracle.py
#         -> note the printed contract address, call it ORACLE_ADDR
#     genlayer deploy --contract contracts/PredictionPool.py --args ORACLE_ADDR
#
# From then on PredictionPool never re-implements resolution: every write it
# makes is downstream of a plain IC-to-IC view() call into EventOracle
# (get_claim_status / get_deadline / is_resolved / get_winning_outcome /
# is_valid_outcome). PredictionPool has no idea how the outcome was decided
# and does not need to -- it only needs EventOracle's word for it, exactly
# the way GenLayer's own docs describe Ghost-contract-addressed IC-to-IC
# calls. stake() also asks is_valid_outcome() before it accepts a single
# wei: a typo'd or made-up label can never be what EventOracle resolves to,
# so a stake on one would just sit in the loser pool forever with no way to
# ever win it back -- rejecting it up front, with a refund like any other
# stake() precondition, is strictly better than silently taking the money.
#
# Outcome labels are case-insensitive identifiers throughout this contract
# -- EventOracle itself rejects "ALICE" and "alice" as duplicates at claim
# creation -- so every outcome that is stored or compared (the
# outcome_totals key, a stake record's own outcome, a settled pool's
# winning_outcome) goes through the same _canon_outcome() normalization.
# Comparing un-normalized values anywhere in this file is exactly how a
# staker who typed "alice" could end up unable to prove they backed the
# outcome that later resolved as "ALICE", despite their wei having already
# been counted in that outcome's total.
#
# Money model (pari-mutuel, not escrow):
#   - anyone may stake GEN on exactly one outcome label of an OPEN claim,
#     before that claim's deadline;
#   - once EventOracle resolves the claim, anyone may permissionlessly call
#     settle_pool(): this does O(1) bookkeeping only -- it does NOT walk the
#     stakers, because a pool can have an unbounded number of them;
#   - each staker then calls claim_payout() themselves (a "pull payment").
#     A winner receives (total_staked - platform_fee) * their_stake /
#     winning_total; a loser receives nothing -- their stake is exactly what
#     funds the winners' payouts, standard pari-mutuel math. Because payouts
#     use integer division, a few wei of unclaimed rounding dust can remain
#     in the contract after every winner has claimed; nobody can withdraw it
#     individually and it is not swept into the platform fee -- this is
#     intentional and harmless, not a bug.
#
# Money-safety rule (load-bearing, not optional): stake() is the ONLY
# `@gl.public.write.payable` method in this whole three-contract chain, so it
# is the only one that can ever receive GEN it did not ask for. It must
# NEVER raise -- `gl.vm.UserError` rolls back storage but not the value that
# rode in with the call. Every rejection therefore refunds the sender and
# returns `{"ok": false, ...}` instead of raising. settle_pool() and
# claim_payout() carry no value, so -- exactly like EventOracle.resolve_claim
# -- they are free to raise on genuine errors; a reverted call there simply
# costs nothing and can be retried.
#
# Two-sided "nobody gets trapped" guarantee:
#   - a claim that IS resolved settles normally;
#   - a claim that gets CANCELED in EventOracle voids the pool, and every
#     staker reclaims their own principal via claim_payout();
#   - a claim that never gets resolved does not trap staked GEN forever:
#     once RESOLUTION_GRACE_SECONDS has passed after its own deadline with
#     no resolution, anyone may call settle_pool() to VOID the pool and
#     every staker reclaims their principal the same way.
# ============================================================================

POOL_OPEN = "OPEN"
POOL_SETTLED = "SETTLED"
POOL_VOID = "VOID"

BPS_DENOM = 10000
DEFAULT_PLATFORM_FEE_BPS = 300
MAX_PLATFORM_FEE_BPS = 1000

MIN_STAKE_AMOUNT = 10 ** 15
MAX_STAKE_AMOUNT = 100 * 10 ** 18
MAX_OUTCOME_LABEL_LEN = 40

RESOLUTION_GRACE_SECONDS = 14 * 86400
SWEEP_DELAY_SECONDS = 3600

MAX_PAGE = 40


def _now_epoch() -> int:
	return int(datetime.now(timezone.utc).timestamp())


def _clamp(value: int, lo: int, hi: int) -> int:
	if value < lo:
		return lo
	if value > hi:
		return hi
	return value


def _skey(claim_id: int, staker) -> str:
	return str(int(claim_id)) + ":" + str(staker)


def _canon_outcome(outcome) -> str:
	"""The single canonical form of an outcome label used EVERYWHERE inside
	this contract -- the outcome_totals key, a stake record's own outcome,
	and a settled pool's winning_outcome all go through this. Outcome labels
	are case-insensitive identifiers (EventOracle itself rejects "ALICE" and
	"alice" as duplicates at claim-creation time), so staking with different
	casing than the claim's creator used must still land in the same bucket
	and still compare equal at payout time. Going through one function
	everywhere is what guarantees that -- see claim_payout()."""
	return str(outcome).strip().upper()


def _okey(claim_id: int, outcome: str) -> str:
	return str(int(claim_id)) + ":" + _canon_outcome(outcome)


@gl.evm.contract_interface
class _Wallet:
	class View:
		pass

	class Write:
		pass


@gl.contract_interface
class _EventOracle:
	class View:
		def get_claim_status(self, claim_id: int) -> str: ...
		def get_deadline(self, claim_id: int) -> int: ...
		def is_resolved(self, claim_id: int) -> bool: ...
		def get_winning_outcome(self, claim_id: int) -> str: ...
		def is_valid_outcome(self, claim_id: int, outcome: str) -> bool: ...

	class Write:
		pass


@allow_storage
@dataclass
class Pool:
	claim_id: u32
	status: str
	total_staked: u128
	winning_outcome: str
	winning_total: u128
	payout_pool: u128
	fee_taken: u128
	opened_epoch: u64
	settled_epoch: u64
	stake_count: u32


@allow_storage
@dataclass
class Stake:
	claim_id: u32
	staker: Address
	outcome: str
	amount: u128
	staked_epoch: u64
	claimed: bool


@allow_storage
@dataclass
class Forecaster:
	resolved_count: u32
	wins: u32
	losses: u32
	total_staked: u128
	total_won: u128


class PredictionPool(gl.Contract):
	owner: Address
	paused: bool
	oracle: Address

	pools: TreeMap[u32, Pool]
	outcome_totals: TreeMap[str, u128]
	stakes: TreeMap[str, Stake]
	forecasters: TreeMap[Address, Forecaster]

	platform_fee_bps: u32
	platform_fees_accrued: u128
	platform_fees_withdrawn: u128

	pool_locked: u128
	total_paid_out: u128
	total_refunded: u128
	last_out_epoch: u64

	count_pools: u32
	count_stakes: u32
	count_settled: u32
	count_void: u32

	def __init__(self, oracle_address: str, platform_fee_bps: int = DEFAULT_PLATFORM_FEE_BPS):
		self.owner = gl.message.sender_address
		self.oracle = Address(str(oracle_address))
		self.paused = False
		self.platform_fee_bps = u32(_clamp(int(platform_fee_bps), 0, MAX_PLATFORM_FEE_BPS))

	# ------------------------------------------------------------------ #
	# internal helpers
	# ------------------------------------------------------------------ #

	def _require_owner(self) -> None:
		if str(gl.message.sender_address) != str(self.owner):
			raise gl.vm.UserError("caller is not the owner")

	def _oracle(self):
		return _EventOracle(self.oracle)

	def _pay(self, to, amount: int) -> None:
		if amount <= 0:
			return
		_Wallet(Address(str(to))).emit_transfer(value=u256(int(amount)))
		self.last_out_epoch = u64(_now_epoch())

	def _reject(self, sender, value: int, reason: str) -> str:
		if value > 0:
			self._pay(sender, value)
		return json.dumps({"ok": False, "reason": reason, "refunded": str(value)})

	def _stake_problem(self, claim_id: int, outcome: str, sender, value: int, now: int) -> str:
		if self.paused:
			return "PredictionPool is paused for new stakes"
		if value < MIN_STAKE_AMOUNT:
			return "stake below minimum of " + str(MIN_STAKE_AMOUNT) + " wei"
		if value > MAX_STAKE_AMOUNT:
			return "stake above maximum of " + str(MAX_STAKE_AMOUNT) + " wei per call"

		o = str(outcome).strip()
		if len(o) == 0 or len(o) > MAX_OUTCOME_LABEL_LEN:
			return "outcome label must be 1.." + str(MAX_OUTCOME_LABEL_LEN) + " characters"

		status = self._oracle().view().get_claim_status(int(claim_id))
		if status != "OPEN":
			return "the underlying claim is " + str(status) + ", not open for staking"

		deadline = self._oracle().view().get_deadline(int(claim_id))
		if now >= int(deadline):
			return "staking closed: the claim's deadline has passed"

		# Reject a typo'd or made-up label before it ever takes anyone's GEN.
		# EventOracle can only ever resolve to one of its own configured
		# outcomes, so a stake on anything else could never possibly win --
		# it would just sit in the loser pool forever, quietly enriching the
		# real winners at that staker's expense. Checked case-insensitively,
		# matching EventOracle's own case-insensitive uniqueness rule at
		# claim-creation time.
		if not self._oracle().view().is_valid_outcome(int(claim_id), o):
			return "'" + o + "' is not one of this claim's configured outcome labels"

		existing = self.stakes.get(_skey(claim_id, sender))
		if existing is not None and int(existing.amount) > 0 and str(existing.outcome) != _canon_outcome(o):
			return ("you already staked on '" + str(existing.outcome)
				+ "' for this claim; cannot also stake a different outcome")
		return ""

	def _void_pool(self, pool: Pool, now: int) -> None:
		pool.status = POOL_VOID
		pool.settled_epoch = u64(now)
		self.count_void = u32(int(self.count_void) + 1)

	# ------------------------------------------------------------------ #
	# staking / settlement / payout
	# ------------------------------------------------------------------ #

	@gl.public.write.payable
	def stake(self, claim_id: int, outcome: str) -> str:
		sender = gl.message.sender_address
		value = int(gl.message.value)
		cid = int(claim_id)
		now = _now_epoch()

		pool = self.pools.get_or_insert_default(u32(cid))
		pool_is_new = str(pool.status) == ""
		if not pool_is_new and str(pool.status) != POOL_OPEN:
			return self._reject(sender, value, "the pool for this claim is already " + str(pool.status))

		problem = self._stake_problem(cid, outcome, sender, value, now)
		if problem != "":
			return self._reject(sender, value, problem)

		# Canonical form only, from here on -- this is what gets stored in
		# the stake record and later compared against pool.winning_outcome
		# in claim_payout(). Storing anything other than the canonical form
		# here is exactly the bug that let a "alice" stake and an "ALICE"
		# resolution silently fail to match each other while the wei had
		# already been counted into the "ALICE" outcome_totals bucket.
		o = _canon_outcome(outcome)

		if pool_is_new:
			pool.claim_id = u32(cid)
			pool.status = POOL_OPEN
			pool.winning_outcome = ""
			pool.winning_total = u128(0)
			pool.payout_pool = u128(0)
			pool.fee_taken = u128(0)
			pool.opened_epoch = u64(now)
			pool.settled_epoch = u64(0)
			pool.stake_count = u32(0)
			self.count_pools = u32(int(self.count_pools) + 1)

		skey = _skey(cid, sender)
		record = self.stakes.get(skey)
		if record is None:
			record = self.stakes.get_or_insert_default(skey)
			record.claim_id = u32(cid)
			record.staker = sender
			record.outcome = o
			record.amount = u128(value)
			record.staked_epoch = u64(now)
			record.claimed = False
			pool.stake_count = u32(int(pool.stake_count) + 1)
			self.count_stakes = u32(int(self.count_stakes) + 1)
		else:
			record.amount = u128(int(record.amount) + value)

		pool.total_staked = u128(int(pool.total_staked) + value)

		okey = _okey(cid, o)
		prior = self.outcome_totals.get(okey)
		self.outcome_totals[okey] = u128((int(prior) if prior is not None else 0) + value)

		forecaster = self.forecasters.get_or_insert_default(sender)
		forecaster.total_staked = u128(int(forecaster.total_staked) + value)

		self.pool_locked = u128(int(self.pool_locked) + value)

		return json.dumps({"ok": True, "claim_id": cid, "outcome": o,
			"amount_added": str(value), "total_stake": str(int(self.stakes.get(skey).amount))})

	@gl.public.write
	def settle_pool(self, claim_id: int) -> str:
		cid = int(claim_id)
		pool = self.pools.get(u32(cid))
		if pool is None:
			raise gl.vm.UserError("no pool exists for this claim -- nobody has staked yet")
		if str(pool.status) != POOL_OPEN:
			raise gl.vm.UserError("pool is already " + str(pool.status))

		now = _now_epoch()
		status = self._oracle().view().get_claim_status(cid)

		if status == "CANCELED":
			self._void_pool(pool, now)
			return json.dumps({"ok": True, "claim_id": cid, "status": POOL_VOID,
				"reason": "the underlying claim was canceled"})

		if status == "RESOLVED":
			winner = self._oracle().view().get_winning_outcome(cid)
			winning_raw = self.outcome_totals.get(_okey(cid, winner))
			winning_total = int(winning_raw) if winning_raw is not None else 0

			if winning_total == 0:
				self._void_pool(pool, now)
				return json.dumps({"ok": True, "claim_id": cid, "status": POOL_VOID,
					"reason": "nobody staked on the winning outcome; principal is refundable"})

			total_staked = int(pool.total_staked)
			fee = (total_staked // BPS_DENOM) * int(self.platform_fee_bps)
			payout_pool = total_staked - fee

			pool.status = POOL_SETTLED
			pool.winning_outcome = _canon_outcome(winner)
			pool.winning_total = u128(winning_total)
			pool.payout_pool = u128(payout_pool)
			pool.fee_taken = u128(fee)
			pool.settled_epoch = u64(now)

			self.platform_fees_accrued = u128(int(self.platform_fees_accrued) + fee)
			self.count_settled = u32(int(self.count_settled) + 1)

			# The fee is carved out of pool_locked the moment it is earned,
			# not only once it is actually swept out by withdraw_platform_
			# fees(). pool_locked otherwise tracks total_staked minus every
			# GEN that has actually left via a winner's claim_payout(), which
			# converges to exactly `fee` once every winner has claimed --
			# permanently equal to the fee still physically sitting in the
			# contract, so free_balance = balance - pool_locked would always
			# read 0 and the fee could never be swept. Subtracting it here
			# instead makes pool_locked converge to 0 (plus rounding dust)
			# once every winner has claimed, leaving exactly `fee` of free,
			# owner-withdrawable balance behind.
			locked = int(self.pool_locked)
			self.pool_locked = u128(locked - fee if locked >= fee else 0)

			return json.dumps({"ok": True, "claim_id": cid, "status": POOL_SETTLED,
				"winning_outcome": str(pool.winning_outcome), "winning_total": str(winning_total),
				"payout_pool": str(payout_pool), "fee_taken": str(fee)})

		deadline = self._oracle().view().get_deadline(cid)
		if now >= int(deadline) + RESOLUTION_GRACE_SECONDS:
			self._void_pool(pool, now)
			return json.dumps({"ok": True, "claim_id": cid, "status": POOL_VOID,
				"reason": "the claim was never resolved within the grace period after its deadline"})

		raise gl.vm.UserError("underlying claim is not yet resolved (status: " + str(status)
			+ "); try again after resolution, or after the grace period elapses")

	@gl.public.write
	def claim_payout(self, claim_id: int) -> str:
		sender = gl.message.sender_address
		cid = int(claim_id)

		pool = self.pools.get(u32(cid))
		if pool is None:
			raise gl.vm.UserError("no pool exists for this claim")
		if str(pool.status) == POOL_OPEN:
			raise gl.vm.UserError("pool is not yet settled; call settle_pool first")

		record = self.stakes.get(_skey(cid, sender))
		if record is None:
			raise gl.vm.UserError("you have no stake on this claim")
		if bool(record.claimed):
			raise gl.vm.UserError("this stake has already been claimed")

		record.claimed = True
		amount = int(record.amount)

		if str(pool.status) == POOL_VOID:
			self.total_refunded = u128(int(self.total_refunded) + amount)
			locked = int(self.pool_locked)
			self.pool_locked = u128(locked - amount if locked >= amount else 0)
			self._pay(sender, amount)
			return json.dumps({"ok": True, "claim_id": cid, "outcome": "VOID",
				"payout": str(amount), "note": "principal refunded, no fee"})

		# SETTLED
		forecaster = self.forecasters.get_or_insert_default(sender)
		forecaster.resolved_count = u32(int(forecaster.resolved_count) + 1)
		# Both sides are written in canonical form already (see stake() and
		# settle_pool()); canonicalizing again here is cheap, idempotent
		# insurance against this specific comparison ever silently going
		# wrong from a stake record written before this fix, or from any
		# future code path that forgets to canonicalize at the write site.
		won = _canon_outcome(record.outcome) == _canon_outcome(pool.winning_outcome)

		if won:
			winning_total = int(pool.winning_total)
			payout = (int(pool.payout_pool) * amount) // winning_total if winning_total > 0 else 0
			forecaster.wins = u32(int(forecaster.wins) + 1)
			forecaster.total_won = u128(int(forecaster.total_won) + payout)

			self.total_paid_out = u128(int(self.total_paid_out) + payout)
			locked = int(self.pool_locked)
			self.pool_locked = u128(locked - payout if locked >= payout else 0)
			self._pay(sender, payout)

			return json.dumps({"ok": True, "claim_id": cid, "outcome": "WIN",
				"payout": str(payout), "staked": str(amount)})

		forecaster.losses = u32(int(forecaster.losses) + 1)
		return json.dumps({"ok": True, "claim_id": cid, "outcome": "LOSS",
			"payout": "0", "staked": str(amount)})

	# ------------------------------------------------------------------ #
	# admin
	# ------------------------------------------------------------------ #

	@gl.public.write
	def set_paused(self, paused: bool) -> None:
		self._require_owner()
		self.paused = bool(paused)

	@gl.public.write
	def set_platform_fee_bps(self, fee_bps: int) -> None:
		self._require_owner()
		self.platform_fee_bps = u32(_clamp(int(fee_bps), 0, MAX_PLATFORM_FEE_BPS))

	@gl.public.write
	def set_oracle(self, new_oracle: str) -> None:
		"""Repoints staking/settlement at a different EventOracle deployment.
		Pools already SETTLED or VOID keep their own recorded outcome and are
		entirely unaffected -- only future staking and settlement of still-OPEN
		pools consult the new oracle."""
		self._require_owner()
		self.oracle = Address(str(new_oracle))

	@gl.public.write
	def transfer_ownership(self, new_owner: str) -> None:
		self._require_owner()
		self.owner = Address(str(new_owner))

	@gl.public.write
	def withdraw_platform_fees(self, to: str, amount: int) -> None:
		self._require_owner()
		now = _now_epoch()
		if int(self.last_out_epoch) > 0 and now < int(self.last_out_epoch) + SWEEP_DELAY_SECONDS:
			raise gl.vm.UserError("please wait " + str(SWEEP_DELAY_SECONDS)
				+ "s after the last payout before sweeping fees")

		available = int(self.platform_fees_accrued) - int(self.platform_fees_withdrawn)
		free_balance = int(self.balance) - int(self.pool_locked)
		withdrawable = min(available, free_balance if free_balance > 0 else 0)
		amt = int(amount)
		if amt <= 0 or amt > withdrawable:
			raise gl.vm.UserError("amount must be 1.." + str(withdrawable) + " wei (currently withdrawable)")

		self.platform_fees_withdrawn = u128(int(self.platform_fees_withdrawn) + amt)
		self._pay(Address(str(to)), amt)

	# ------------------------------------------------------------------ #
	# views -- this is the surface ForecasterRank.py is built against
	# ------------------------------------------------------------------ #

	def _pool_json(self, pool: Pool) -> dict:
		return {"claim_id": int(pool.claim_id), "status": str(pool.status),
			"total_staked": str(int(pool.total_staked)),
			"winning_outcome": str(pool.winning_outcome),
			"winning_total": str(int(pool.winning_total)),
			"payout_pool": str(int(pool.payout_pool)),
			"fee_taken": str(int(pool.fee_taken)),
			"opened_epoch": int(pool.opened_epoch),
			"settled_epoch": int(pool.settled_epoch),
			"stake_count": int(pool.stake_count)}

	@gl.public.view
	def get_pool(self, claim_id: int) -> str:
		pool = self.pools.get(u32(int(claim_id)))
		if pool is None:
			return json.dumps({"found": False})
		out = self._pool_json(pool)
		out["found"] = True
		return json.dumps(out)

	@gl.public.view
	def get_stake(self, claim_id: int, staker: str) -> str:
		record = self.stakes.get(_skey(int(claim_id), Address(str(staker))))
		if record is None:
			return json.dumps({"found": False})
		return json.dumps({"found": True, "claim_id": int(record.claim_id),
			"staker": str(record.staker), "outcome": str(record.outcome),
			"amount": str(int(record.amount)), "staked_epoch": int(record.staked_epoch),
			"claimed": bool(record.claimed)})

	@gl.public.view
	def get_outcome_total(self, claim_id: int, outcome: str) -> str:
		total = self.outcome_totals.get(_okey(int(claim_id), outcome))
		return str(int(total) if total is not None else 0)

	@gl.public.view
	def has_forecaster_track_record(self, address: str, min_wins: int) -> bool:
		forecaster = self.forecasters.get(Address(str(address)))
		if forecaster is None:
			return int(min_wins) <= 0
		return int(forecaster.wins) >= int(min_wins)

	@gl.public.view
	def get_forecaster_stats(self, address: str) -> str:
		forecaster = self.forecasters.get(Address(str(address)))
		if forecaster is None:
			return json.dumps({"address": str(address), "resolved_count": 0, "wins": 0,
				"losses": 0, "total_staked": "0", "total_won": "0"})
		return json.dumps({"address": str(address), "resolved_count": int(forecaster.resolved_count),
			"wins": int(forecaster.wins), "losses": int(forecaster.losses),
			"total_staked": str(int(forecaster.total_staked)),
			"total_won": str(int(forecaster.total_won))})

	@gl.public.view
	def get_platform_stats(self) -> str:
		fees_available = int(self.platform_fees_accrued) - int(self.platform_fees_withdrawn)
		return json.dumps({"pool_locked": str(int(self.pool_locked)),
			"on_chain_balance": str(int(self.balance)),
			"platform_fees_accrued": str(int(self.platform_fees_accrued)),
			"platform_fees_withdrawn": str(int(self.platform_fees_withdrawn)),
			"platform_fees_available": str(fees_available),
			"total_paid_out": str(int(self.total_paid_out)),
			"total_refunded": str(int(self.total_refunded)),
			"pools": int(self.count_pools), "stakes": int(self.count_stakes),
			"settled": int(self.count_settled), "void": int(self.count_void)})

	@gl.public.view
	def get_config(self) -> str:
		return json.dumps({"owner": str(self.owner), "oracle": str(self.oracle),
			"paused": bool(self.paused), "platform_fee_bps": int(self.platform_fee_bps),
			"max_platform_fee_bps": MAX_PLATFORM_FEE_BPS,
			"min_stake_amount": str(MIN_STAKE_AMOUNT), "max_stake_amount": str(MAX_STAKE_AMOUNT),
			"resolution_grace_seconds": RESOLUTION_GRACE_SECONDS,
			"sweep_delay_seconds": SWEEP_DELAY_SECONDS})
