"""
End-to-end test of the real chain: a real EventOracle instance, a real
PredictionPool instance constructed with that EventOracle's address, and a
real ForecasterRank instance constructed with that PredictionPool's address
-- each one calling the one before it exactly the way they would on-chain,
via stub.deploy_and_register() instead of a hand-written fake. The per-file
unit tests already cover edge cases against lightweight fakes; this file's
job is only to prove the three real contracts actually fit together end to
end, the same shape as `genlayer deploy` three times in a row and wiring
each printed address into the next constructor.

Run with:  python3 -m unittest discover -s test -v
"""
import json
import unittest
from pathlib import Path

import genlayer_stub as stub

CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "contracts"

EVENT_ORACLE_ADDR = stub.Address("0xE000000000000000000000000000000000000E0")
PREDICTION_POOL_ADDR = stub.Address("0xE000000000000000000000000000000000000E1")
FORECASTER_RANK_ADDR = stub.Address("0xE000000000000000000000000000000000000E2")

DEPLOYER = stub.Address("0x1111111111111111111111111111111111111111")
CREATOR = stub.Address("0x2222222222222222222222222222222222222222")
WINNER_STAKER = stub.Address("0x3333333333333333333333333333333333333333")
LOSER_STAKER = stub.Address("0x4444444444444444444444444444444444444444")

GEN = 10 ** 18
DAY = 86400


def send_as(sender, value=0):
	stub.message.sender_address = sender
	stub.message.value = value


def mock_resolution(outcome, confidence=90, reasoning="official result page"):
	payload = json.dumps({"outcome": outcome, "confidence": confidence, "reasoning": reasoning})
	stub.NONDET_HOOKS["web_render"] = lambda url, mode: "official result content"
	stub.NONDET_HOOKS["exec_prompt"] = lambda prompt, response_format: payload


class ChainIntegrationTestCase(unittest.TestCase):
	def setUp(self):
		stub.reset()

		self.oracle_mod = stub.load_contract(CONTRACTS_DIR / "EventOracle.py", "chain_event_oracle")
		self.pool_mod = stub.load_contract(CONTRACTS_DIR / "PredictionPool.py", "chain_prediction_pool")
		self.rank_mod = stub.load_contract(CONTRACTS_DIR / "ForecasterRank.py", "chain_forecaster_rank")

		send_as(DEPLOYER)
		self.event_oracle = stub.deploy_and_register(
			self.oracle_mod.EventOracle, EVENT_ORACLE_ADDR)

		send_as(DEPLOYER)
		self.prediction_pool = stub.deploy_and_register(
			self.pool_mod.PredictionPool, PREDICTION_POOL_ADDR, str(EVENT_ORACLE_ADDR))

		send_as(DEPLOYER)
		self.forecaster_rank = stub.deploy_and_register(
			self.rank_mod.ForecasterRank, FORECASTER_RANK_ADDR, str(PREDICTION_POOL_ADDR))

	def _advance_both_clocks_past(self, deadline_epoch):
		later = deadline_epoch + 10
		self.oracle_mod._now_epoch = lambda: later
		self.pool_mod._now_epoch = lambda: later

	def test_full_chain_happy_path(self):
		# 1. a claim is opened on EventOracle (link 1)
		send_as(CREATOR)
		now = self.oracle_mod._now_epoch()
		deadline = now + DAY
		created = json.loads(self.event_oracle.create_claim(
			"Which team wins the cup final?", "REDS|BLUES", "https://example.org/final-result",
			"Use the official league result only.", deadline))
		claim_id = created["claim_id"]

		# 2. two forecasters stake opposite outcomes on PredictionPool,
		#    which is wired to that EventOracle's address (link 2)
		send_as(WINNER_STAKER, 3 * GEN)
		stake_ok = json.loads(self.prediction_pool.stake(claim_id, "REDS"))
		self.assertTrue(stake_ok["ok"])

		send_as(LOSER_STAKER, 2 * GEN)
		stake_ok = json.loads(self.prediction_pool.stake(claim_id, "BLUES"))
		self.assertTrue(stake_ok["ok"])

		# 3. time passes, and EventOracle resolves the claim
		self._advance_both_clocks_past(deadline)
		mock_resolution("REDS")
		resolved = json.loads(self.event_oracle.resolve_claim(claim_id))
		self.assertEqual(resolved["status"], "RESOLVED")
		self.assertEqual(resolved["resolved_outcome"], "REDS")

		# 4. anyone settles the pool -- PredictionPool reads the verdict
		#    straight off the real EventOracle instance, not a mock
		settled = json.loads(self.prediction_pool.settle_pool(claim_id))
		self.assertEqual(settled["status"], "SETTLED")
		self.assertEqual(settled["winning_outcome"], "REDS")

		# 5. the winner pulls their payout; the loser gets nothing
		send_as(WINNER_STAKER)
		payout = json.loads(self.prediction_pool.claim_payout(claim_id))
		self.assertEqual(payout["outcome"], "WIN")
		self.assertGreater(int(payout["payout"]), 3 * GEN)  # funded by the loser's stake too

		send_as(LOSER_STAKER)
		payout_loser = json.loads(self.prediction_pool.claim_payout(claim_id))
		self.assertEqual(payout_loser["outcome"], "LOSS")
		self.assertEqual(payout_loser["payout"], "0")

		# 6. ForecasterRank (link 3) reads the winner's now-live win count
		#    straight off the real, already-deployed PredictionPool
		self.assertTrue(self.forecaster_rank.is_eligible(str(WINNER_STAKER), "SCOUT"))
		snapshot = json.loads(self.forecaster_rank.get_forecaster_snapshot(str(WINNER_STAKER)))
		self.assertEqual(snapshot["wins"], 1)

		send_as(WINNER_STAKER)
		badge = json.loads(self.forecaster_rank.request_badge("SCOUT"))
		self.assertTrue(badge["ok"])

		# the loser has zero wins and cannot claim SHARP (needs 5)
		with self.assertRaises(stub.vm.UserError):
			send_as(LOSER_STAKER)
			self.forecaster_rank.request_badge("SHARP")

	def test_full_chain_claim_never_resolved_stakers_reclaim_via_void(self):
		"""The two-sided safety valve, exercised across all three real
		contracts at once: if EventOracle's claim is simply never resolved,
		PredictionPool does not trap the staked GEN forever."""
		send_as(CREATOR)
		deadline = self.oracle_mod._now_epoch() + DAY
		created = json.loads(self.event_oracle.create_claim(
			"Will the shipment arrive by Friday?", "YES|NO", "https://example.org/tracking",
			"", deadline))
		claim_id = created["claim_id"]

		send_as(WINNER_STAKER, 1 * GEN)
		json.loads(self.prediction_pool.stake(claim_id, "YES"))

		# nobody ever calls resolve_claim -- jump past the grace period too
		grace = self.pool_mod.RESOLUTION_GRACE_SECONDS
		far_future = deadline + grace + 100
		self.oracle_mod._now_epoch = lambda: far_future
		self.pool_mod._now_epoch = lambda: far_future

		voided = json.loads(self.prediction_pool.settle_pool(claim_id))
		self.assertEqual(voided["status"], "VOID")

		send_as(WINNER_STAKER)
		refund = json.loads(self.prediction_pool.claim_payout(claim_id))
		self.assertEqual(refund["outcome"], "VOID")
		self.assertEqual(refund["payout"], str(1 * GEN))


if __name__ == "__main__":
	unittest.main()
