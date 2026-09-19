"""
Offline tests for contracts/PredictionPool.py.

Exercises PredictionPool against a small hand-written `_FakeOracle` test
double registered under its own `oracle` address, the same technique the
GenLayer docs describe for the Equivalence Principle's IC-to-IC calls: a
`@gl.contract_interface`-typed call resolves to whatever object is currently
registered under that address. A true three-contract wiring test (a *real*
EventOracle instance in that slot, not a fake) lives in
test_chain_integration.py.

Run with:  python3 -m unittest discover -s test -v
"""
import json
import unittest
from pathlib import Path

import genlayer_stub as stub

CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "contracts"

OWNER = stub.Address("0x1111111111111111111111111111111111111111")
ORACLE_ADDR = stub.Address("0x9999999999999999999999999999999999999999")
ALICE_STAKER = stub.Address("0x2222222222222222222222222222222222222222")
BOB_STAKER = stub.Address("0x3333333333333333333333333333333333333333")
CAROL_STAKER = stub.Address("0x4444444444444444444444444444444444444444")

GEN = 10 ** 18
CLAIM_ID = 1


def load_pool():
	return stub.load_contract(CONTRACTS_DIR / "PredictionPool.py", "prediction_pool_under_test")


def send_as(sender, value=0):
	stub.message.sender_address = sender
	stub.message.value = value


class _FakeOracle:
	"""Stands in for a deployed EventOracle: register it into
	stub.CONTRACT_REGISTRY under the address PredictionPool was constructed
	with, then mutate its dicts to script exactly what the "oracle" says for
	a given claim_id, without needing a second contract file loaded."""

	def __init__(self):
		self.status = {}
		self.deadline = {}
		self.winner = {}
		self.outcomes = {}

	def view(self):
		return self

	def get_claim_status(self, claim_id: int) -> str:
		return self.status.get(int(claim_id), "UNKNOWN")

	def get_deadline(self, claim_id: int) -> int:
		return self.deadline.get(int(claim_id), 0)

	def is_resolved(self, claim_id: int) -> bool:
		return self.status.get(int(claim_id)) == "RESOLVED"

	def get_winning_outcome(self, claim_id: int) -> str:
		return self.winner.get(int(claim_id), "")

	def is_valid_outcome(self, claim_id: int, outcome: str) -> bool:
		configured = self.outcomes.get(int(claim_id), [])
		target = str(outcome).strip().upper()
		return any(target == str(label).strip().upper() for label in configured)


class PredictionPoolTestCase(unittest.TestCase):
	def setUp(self):
		stub.reset()
		self.mod = load_pool()
		self.oracle = _FakeOracle()
		stub.CONTRACT_REGISTRY[str(ORACLE_ADDR)] = self.oracle

		now = self.mod._now_epoch()
		self.oracle.status[CLAIM_ID] = "OPEN"
		self.oracle.deadline[CLAIM_ID] = now + 86400
		self.oracle.outcomes[CLAIM_ID] = ["ALICE", "BOB"]

		send_as(OWNER)
		self.pool = self.mod.PredictionPool(str(ORACLE_ADDR))

	def _stake(self, sender, outcome, amount):
		send_as(sender, amount)
		return json.loads(self.pool.stake(CLAIM_ID, outcome))

	# ------------------------------------------------------------------ #
	# stake() -- the only payable method in the chain -- must never raise
	# ------------------------------------------------------------------ #

	def test_stake_happy_path_creates_pool_and_records_stake(self):
		res = self._stake(ALICE_STAKER, "ALICE", 3 * GEN)
		self.assertTrue(res["ok"])
		pool = json.loads(self.pool.get_pool(CLAIM_ID))
		self.assertEqual(pool["status"], "OPEN")
		self.assertEqual(pool["total_staked"], str(3 * GEN))
		self.assertEqual(self.pool.get_outcome_total(CLAIM_ID, "ALICE"), str(3 * GEN))

	def test_stake_below_minimum_refunds_without_raising(self):
		send_as(ALICE_STAKER, 1)
		res = json.loads(self.pool.stake(CLAIM_ID, "ALICE"))
		self.assertFalse(res["ok"])
		self.assertEqual(stub.PAYMENTS, [(str(ALICE_STAKER), 1)])

	def test_stake_on_claim_not_open_refunds_without_raising(self):
		self.oracle.status[CLAIM_ID] = "CANCELED"
		send_as(ALICE_STAKER, 2 * GEN)
		res = json.loads(self.pool.stake(CLAIM_ID, "ALICE"))
		self.assertFalse(res["ok"])
		self.assertEqual(stub.PAYMENTS, [(str(ALICE_STAKER), 2 * GEN)])

	def test_stake_after_deadline_refunds_without_raising(self):
		self.oracle.deadline[CLAIM_ID] = self.mod._now_epoch() - 1
		send_as(ALICE_STAKER, 2 * GEN)
		res = json.loads(self.pool.stake(CLAIM_ID, "ALICE"))
		self.assertFalse(res["ok"])
		self.assertEqual(stub.PAYMENTS, [(str(ALICE_STAKER), 2 * GEN)])

	def test_stake_switching_outcome_refunds_without_raising(self):
		self._stake(ALICE_STAKER, "ALICE", 2 * GEN)
		send_as(ALICE_STAKER, 1 * GEN)
		res = json.loads(self.pool.stake(CLAIM_ID, "BOB"))
		self.assertFalse(res["ok"])
		self.assertEqual(stub.PAYMENTS[-1], (str(ALICE_STAKER), 1 * GEN))
		# the original stake on ALICE is untouched
		stake = json.loads(self.pool.get_stake(CLAIM_ID, str(ALICE_STAKER)))
		self.assertEqual(stake["outcome"], "ALICE")
		self.assertEqual(stake["amount"], str(2 * GEN))

	def test_stake_top_up_same_outcome_accumulates(self):
		self._stake(ALICE_STAKER, "ALICE", 2 * GEN)
		res = self._stake(ALICE_STAKER, "ALICE", 1 * GEN)
		self.assertTrue(res["ok"])
		stake = json.loads(self.pool.get_stake(CLAIM_ID, str(ALICE_STAKER)))
		self.assertEqual(stake["amount"], str(3 * GEN))
		self.assertEqual(self.pool.get_outcome_total(CLAIM_ID, "ALICE"), str(3 * GEN))

	def test_stake_while_paused_refunds_without_raising(self):
		send_as(OWNER)
		self.pool.set_paused(True)
		send_as(ALICE_STAKER, 2 * GEN)
		res = json.loads(self.pool.stake(CLAIM_ID, "ALICE"))
		self.assertFalse(res["ok"])
		self.assertEqual(stub.PAYMENTS, [(str(ALICE_STAKER), 2 * GEN)])

	# ------------------------------------------------------------------ #
	# settle_pool
	# ------------------------------------------------------------------ #

	def test_settle_pool_before_resolution_raises(self):
		self._stake(ALICE_STAKER, "ALICE", 1 * GEN)
		with self.assertRaises(stub.vm.UserError):
			self.pool.settle_pool(CLAIM_ID)

	def test_settle_pool_resolved_no_pool_exists_raises(self):
		with self.assertRaises(stub.vm.UserError):
			self.pool.settle_pool(CLAIM_ID)

	def test_settle_pool_canceled_claim_voids_pool(self):
		self._stake(ALICE_STAKER, "ALICE", 1 * GEN)
		self.oracle.status[CLAIM_ID] = "CANCELED"
		res = json.loads(self.pool.settle_pool(CLAIM_ID))
		self.assertEqual(res["status"], "VOID")

	def test_settle_pool_nobody_backed_winner_voids_pool(self):
		self._stake(BOB_STAKER, "BOB", 1 * GEN)
		self.oracle.status[CLAIM_ID] = "RESOLVED"
		self.oracle.winner[CLAIM_ID] = "ALICE"
		res = json.loads(self.pool.settle_pool(CLAIM_ID))
		self.assertEqual(res["status"], "VOID")

	def test_settle_pool_never_resolved_voids_after_grace_period(self):
		self._stake(ALICE_STAKER, "ALICE", 1 * GEN)
		self.oracle.status[CLAIM_ID] = "OPEN"
		grace = self.mod.RESOLUTION_GRACE_SECONDS
		self.mod._now_epoch = lambda: self.oracle.deadline[CLAIM_ID] + grace + 5
		res = json.loads(self.pool.settle_pool(CLAIM_ID))
		self.assertEqual(res["status"], "VOID")

	def test_settle_pool_never_resolved_before_grace_period_raises(self):
		self._stake(ALICE_STAKER, "ALICE", 1 * GEN)
		self.oracle.status[CLAIM_ID] = "OPEN"
		self.mod._now_epoch = lambda: self.oracle.deadline[CLAIM_ID] + 5
		with self.assertRaises(stub.vm.UserError):
			self.pool.settle_pool(CLAIM_ID)

	def test_settle_pool_twice_raises(self):
		self._stake(ALICE_STAKER, "ALICE", 1 * GEN)
		self.oracle.status[CLAIM_ID] = "RESOLVED"
		self.oracle.winner[CLAIM_ID] = "ALICE"
		self.pool.settle_pool(CLAIM_ID)
		with self.assertRaises(stub.vm.UserError):
			self.pool.settle_pool(CLAIM_ID)

	def test_settle_pool_computes_exact_pari_mutuel_fee_and_payout_pool(self):
		self._stake(ALICE_STAKER, "ALICE", 3 * GEN)
		self._stake(BOB_STAKER, "ALICE", 1 * GEN)
		self._stake(CAROL_STAKER, "BOB", 4 * GEN)
		self.oracle.status[CLAIM_ID] = "RESOLVED"
		self.oracle.winner[CLAIM_ID] = "ALICE"

		res = json.loads(self.pool.settle_pool(CLAIM_ID))
		self.assertEqual(res["status"], "SETTLED")
		self.assertEqual(res["fee_taken"], str(240 * 10 ** 15))          # 3% of 8 GEN
		self.assertEqual(res["payout_pool"], str(7760 * 10 ** 15))       # 8 GEN - fee
		self.assertEqual(res["winning_total"], str(4 * GEN))

	# ------------------------------------------------------------------ #
	# claim_payout
	# ------------------------------------------------------------------ #

	def _settle_three_way(self):
		self._stake(ALICE_STAKER, "ALICE", 3 * GEN)
		self._stake(BOB_STAKER, "ALICE", 1 * GEN)
		self._stake(CAROL_STAKER, "BOB", 4 * GEN)
		self.oracle.status[CLAIM_ID] = "RESOLVED"
		self.oracle.winner[CLAIM_ID] = "ALICE"
		self.pool.settle_pool(CLAIM_ID)

	def test_claim_payout_winner_gets_exact_proportional_share(self):
		self._settle_three_way()
		send_as(ALICE_STAKER)
		res = json.loads(self.pool.claim_payout(CLAIM_ID))
		self.assertEqual(res["outcome"], "WIN")
		self.assertEqual(res["payout"], str(5820 * 10 ** 15))
		self.assertIn((str(ALICE_STAKER), 5820 * 10 ** 15), stub.PAYMENTS)

		send_as(BOB_STAKER)
		res2 = json.loads(self.pool.claim_payout(CLAIM_ID))
		self.assertEqual(res2["payout"], str(1940 * 10 ** 15))

	def test_claim_payout_loser_gets_nothing_and_no_transfer(self):
		self._settle_three_way()
		send_as(CAROL_STAKER)
		res = json.loads(self.pool.claim_payout(CLAIM_ID))
		self.assertEqual(res["outcome"], "LOSS")
		self.assertEqual(res["payout"], "0")
		self.assertEqual(stub.PAYMENTS, [])

	def test_claim_payout_twice_raises(self):
		self._settle_three_way()
		send_as(ALICE_STAKER)
		self.pool.claim_payout(CLAIM_ID)
		with self.assertRaises(stub.vm.UserError):
			self.pool.claim_payout(CLAIM_ID)

	def test_claim_payout_before_settlement_raises(self):
		self._stake(ALICE_STAKER, "ALICE", 1 * GEN)
		send_as(ALICE_STAKER)
		with self.assertRaises(stub.vm.UserError):
			self.pool.claim_payout(CLAIM_ID)

	def test_claim_payout_no_stake_raises(self):
		self._settle_three_way()
		send_as(stub.Address("0x5555555555555555555555555555555555555555"))
		with self.assertRaises(stub.vm.UserError):
			self.pool.claim_payout(CLAIM_ID)

	def test_claim_payout_on_void_pool_refunds_principal_in_full(self):
		self._stake(ALICE_STAKER, "ALICE", 2 * GEN)
		self.oracle.status[CLAIM_ID] = "CANCELED"
		self.pool.settle_pool(CLAIM_ID)
		send_as(ALICE_STAKER)
		res = json.loads(self.pool.claim_payout(CLAIM_ID))
		self.assertEqual(res["outcome"], "VOID")
		self.assertEqual(res["payout"], str(2 * GEN))
		self.assertIn((str(ALICE_STAKER), 2 * GEN), stub.PAYMENTS)

	# ------------------------------------------------------------------ #
	# forecaster track record -- the surface ForecasterRank.py reads
	# ------------------------------------------------------------------ #

	def test_forecaster_stats_track_wins_and_losses(self):
		self._settle_three_way()
		send_as(ALICE_STAKER)
		self.pool.claim_payout(CLAIM_ID)
		send_as(CAROL_STAKER)
		self.pool.claim_payout(CLAIM_ID)

		alice_stats = json.loads(self.pool.get_forecaster_stats(str(ALICE_STAKER)))
		self.assertEqual(alice_stats["wins"], 1)
		self.assertEqual(alice_stats["losses"], 0)
		self.assertEqual(alice_stats["total_won"], str(5820 * 10 ** 15))

		carol_stats = json.loads(self.pool.get_forecaster_stats(str(CAROL_STAKER)))
		self.assertEqual(carol_stats["wins"], 0)
		self.assertEqual(carol_stats["losses"], 1)

		self.assertTrue(self.pool.has_forecaster_track_record(str(ALICE_STAKER), 1))
		self.assertFalse(self.pool.has_forecaster_track_record(str(CAROL_STAKER), 1))

	# ------------------------------------------------------------------ #
	# admin
	# ------------------------------------------------------------------ #

	def test_only_owner_can_set_fee(self):
		send_as(ALICE_STAKER)
		with self.assertRaises(stub.vm.UserError):
			self.pool.set_platform_fee_bps(500)

	def test_fee_bps_is_clamped_to_max(self):
		send_as(OWNER)
		self.pool.set_platform_fee_bps(self.mod.MAX_PLATFORM_FEE_BPS + 5000)
		cfg = json.loads(self.pool.get_config())
		self.assertEqual(cfg["platform_fee_bps"], self.mod.MAX_PLATFORM_FEE_BPS)

	def test_withdraw_platform_fees_blocked_shortly_after_a_payout(self):
		self._settle_three_way()
		send_as(ALICE_STAKER)
		self.pool.claim_payout(CLAIM_ID)  # sets last_out_epoch = now

		stub.set_balance(self.pool, 8 * GEN)
		send_as(OWNER)
		with self.assertRaises(stub.vm.UserError):
			self.pool.withdraw_platform_fees(str(OWNER), 1)

	def test_withdraw_platform_fees_succeeds_after_sweep_delay(self):
		"""Regression test for the fee-lock bug: after settlement, ALL
		winners claim (not just one), so the only GEN realistically left in
		the contract is the platform fee itself -- exactly the scenario
		that used to make free_balance read 0 forever, because pool_locked
		was never reduced by the fee at settlement time. The balance here
		is set to what would ACTUALLY remain on-chain (total staked minus
		every payout that actually left), not an arbitrarily generous
		number, specifically so this test would fail again if that fix
		ever regressed."""
		self._settle_three_way()
		send_as(ALICE_STAKER)
		alice_payout = int(json.loads(self.pool.claim_payout(CLAIM_ID))["payout"])
		send_as(BOB_STAKER)
		bob_payout = int(json.loads(self.pool.claim_payout(CLAIM_ID))["payout"])

		realistic_balance = 8 * GEN - alice_payout - bob_payout  # == the fee, exactly
		self.assertEqual(realistic_balance, 240 * 10 ** 15)
		stub.set_balance(self.pool, realistic_balance)

		later = self.mod._now_epoch() + self.mod.SWEEP_DELAY_SECONDS + 5
		self.mod._now_epoch = lambda: later

		fees_before = json.loads(self.pool.get_platform_stats())["platform_fees_available"]
		self.assertEqual(fees_before, str(240 * 10 ** 15))
		send_as(OWNER)
		self.pool.withdraw_platform_fees(str(OWNER), int(fees_before))
		fees_after = json.loads(self.pool.get_platform_stats())["platform_fees_available"]
		self.assertEqual(fees_after, "0")
		self.assertIn((str(OWNER), 240 * 10 ** 15), stub.PAYMENTS)

	# ------------------------------------------------------------------ #
	# regression tests: outcome validation and case-insensitivity
	# ------------------------------------------------------------------ #

	def test_stake_on_outcome_not_offered_by_the_claim_refunds_without_raising(self):
		"""A typo'd or made-up label ("ALICEE") can never be what
		EventOracle resolves to, so it must be rejected up front -- with a
		refund, like every other stake() precondition -- rather than
		silently taking the GEN into a bucket that can mathematically never
		win."""
		send_as(ALICE_STAKER, 2 * GEN)
		res = json.loads(self.pool.stake(CLAIM_ID, "ALICEE"))
		self.assertFalse(res["ok"])
		self.assertIn((str(ALICE_STAKER), 2 * GEN), stub.PAYMENTS)
		# no money should have moved into the pool for the rejected outcome
		pool = json.loads(self.pool.get_pool(CLAIM_ID))
		self.assertEqual(pool["total_staked"], "0")
		self.assertEqual(self.pool.get_outcome_total(CLAIM_ID, "ALICEE"), "0")

	def test_stake_lowercase_outcome_is_accepted_and_pools_with_uppercase(self):
		send_as(ALICE_STAKER, 2 * GEN)
		res = json.loads(self.pool.stake(CLAIM_ID, "alice"))
		self.assertTrue(res["ok"])
		self.assertEqual(res["outcome"], "ALICE")
		# it must land in the SAME bucket a same-outcome uppercase stake would
		self.assertEqual(self.pool.get_outcome_total(CLAIM_ID, "ALICE"), str(2 * GEN))
		self.assertEqual(self.pool.get_outcome_total(CLAIM_ID, "alice"), str(2 * GEN))

	def test_stake_with_different_case_still_blocks_a_genuinely_different_outcome(self):
		self._stake(ALICE_STAKER, "alice", 2 * GEN)
		res = self._stake(ALICE_STAKER, "BOB", 1 * GEN)
		self.assertFalse(res["ok"])

	def test_claim_payout_wins_even_when_staked_with_different_case_than_the_resolution(self):
		"""Regression test for the case-sensitivity bug: EventOracle
		resolves to "ALICE" (its configured, verbatim casing), but this
		staker staked "alice". Their wei was already counted into the
		"ALICE" outcome bucket at stake time (test above proves that); they
		must also be able to actually collect their winnings -- the bug was
		that this exact comparison used to fail."""
		self._stake(ALICE_STAKER, "alice", 3 * GEN)
		self._stake(BOB_STAKER, "BOB", 1 * GEN)
		self.oracle.status[CLAIM_ID] = "RESOLVED"
		self.oracle.winner[CLAIM_ID] = "ALICE"
		self.pool.settle_pool(CLAIM_ID)

		send_as(ALICE_STAKER)
		res = json.loads(self.pool.claim_payout(CLAIM_ID))
		self.assertEqual(res["outcome"], "WIN")
		self.assertGreater(int(res["payout"]), 0)


if __name__ == "__main__":
	unittest.main()
