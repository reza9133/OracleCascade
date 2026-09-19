"""
Offline tests for contracts/ForecasterRank.py.

Exercises ForecasterRank against a small hand-written `_FakePool` test
double registered under its own `oracle` address (the same technique used
in test_prediction_pool.py). A true three-contract wiring test lives in
test_chain_integration.py.

Run with:  python3 -m unittest discover -s test -v
"""
import json
import unittest
from pathlib import Path

import genlayer_stub as stub

CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "contracts"

OWNER = stub.Address("0x1111111111111111111111111111111111111111")
POOL_ADDR = stub.Address("0x9999999999999999999999999999999999999999")
NOVICE = stub.Address("0x2222222222222222222222222222222222222222")
VETERAN_FORECASTER = stub.Address("0x3333333333333333333333333333333333333333")


def load_rank():
	return stub.load_contract(CONTRACTS_DIR / "ForecasterRank.py", "forecaster_rank_under_test")


def send_as(sender):
	stub.message.sender_address = sender


class _FakePool:
	def __init__(self, wins_by_address=None):
		self.wins_by_address = dict(wins_by_address or {})

	def view(self):
		return self

	def has_forecaster_track_record(self, address: str, min_wins: int) -> bool:
		return self.wins_by_address.get(str(address), 0) >= int(min_wins)

	def get_forecaster_stats(self, address: str) -> str:
		wins = self.wins_by_address.get(str(address), 0)
		return json.dumps({"address": str(address), "resolved_count": wins, "wins": wins,
			"losses": 0, "total_staked": "0", "total_won": "0"})


class ForecasterRankTestCase(unittest.TestCase):
	def setUp(self):
		stub.reset()
		self.mod = load_rank()
		self.pool = _FakePool()
		stub.CONTRACT_REGISTRY[str(POOL_ADDR)] = self.pool

		send_as(OWNER)
		self.rank = self.mod.ForecasterRank(str(POOL_ADDR))

	# ------------------------------------------------------------------ #
	# default tiers
	# ------------------------------------------------------------------ #

	def test_default_tiers_are_seeded(self):
		tiers = {t["tier"]: t["min_wins"] for t in json.loads(self.rank.get_tiers())["tiers"]}
		self.assertEqual(tiers, {"SCOUT": 0, "SHARP": 5, "VETERAN": 15, "ORACLE": 40})

	# ------------------------------------------------------------------ #
	# eligibility is live, not stored
	# ------------------------------------------------------------------ #

	def test_scout_tier_is_open_to_everyone(self):
		self.assertTrue(self.rank.is_eligible(str(NOVICE), "SCOUT"))

	def test_is_eligible_reflects_live_win_count(self):
		self.assertFalse(self.rank.is_eligible(str(VETERAN_FORECASTER), "SHARP"))
		self.pool.wins_by_address[str(VETERAN_FORECASTER)] = 5
		self.assertTrue(self.rank.is_eligible(str(VETERAN_FORECASTER), "SHARP"))

	def test_preview_eligibility_never_raises_for_ineligible_forecaster(self):
		res = json.loads(self.rank.preview_eligibility(str(NOVICE), "ORACLE"))
		self.assertFalse(res["eligible"])
		self.assertEqual(res["required_wins"], 40)

	def test_preview_eligibility_raises_on_unknown_tier(self):
		with self.assertRaises(stub.vm.UserError):
			self.rank.preview_eligibility(str(NOVICE), "MYTHICAL")

	def test_best_tier_for_picks_highest_qualifying_tier(self):
		self.pool.wins_by_address[str(VETERAN_FORECASTER)] = 10
		res = json.loads(self.rank.best_tier_for(str(VETERAN_FORECASTER)))
		self.assertEqual(res["best_tier"], "SHARP")  # 10 wins: past SHARP(5), short of VETERAN(15)

	# ------------------------------------------------------------------ #
	# badge requests
	# ------------------------------------------------------------------ #

	def test_request_badge_raises_when_ineligible(self):
		send_as(NOVICE)
		with self.assertRaises(stub.vm.UserError):
			self.rank.request_badge("SHARP")

	def test_request_badge_succeeds_when_eligible(self):
		self.pool.wins_by_address[str(VETERAN_FORECASTER)] = 6
		send_as(VETERAN_FORECASTER)
		res = json.loads(self.rank.request_badge("SHARP"))
		self.assertTrue(res["ok"])
		self.assertEqual(res["wins_at_grant"], 6)

		badge = json.loads(self.rank.get_badge(str(VETERAN_FORECASTER), "SHARP"))
		self.assertTrue(badge["found"])
		self.assertFalse(badge["revoked"])

	def test_request_badge_blocked_when_paused(self):
		send_as(OWNER)
		self.rank.set_paused(True)
		send_as(NOVICE)
		with self.assertRaises(stub.vm.UserError):
			self.rank.request_badge("SCOUT")

	def test_request_badge_unknown_tier_raises(self):
		send_as(NOVICE)
		with self.assertRaises(stub.vm.UserError):
			self.rank.request_badge("NOT_A_TIER")

	# ------------------------------------------------------------------ #
	# revocation is administrative, not a live-eligibility toggle
	# ------------------------------------------------------------------ #

	def test_revoke_badge_marks_record_but_not_live_eligibility(self):
		self.pool.wins_by_address[str(VETERAN_FORECASTER)] = 6
		send_as(VETERAN_FORECASTER)
		self.rank.request_badge("SHARP")

		send_as(OWNER)
		self.rank.revoke_badge(str(VETERAN_FORECASTER), "SHARP")

		badge = json.loads(self.rank.get_badge(str(VETERAN_FORECASTER), "SHARP"))
		self.assertTrue(badge["revoked"])
		# live eligibility is unaffected by an administrative revocation --
		# it is always recomputed straight from PredictionPool's win count.
		self.assertTrue(self.rank.is_eligible(str(VETERAN_FORECASTER), "SHARP"))

	def test_revoke_then_re_request_clears_the_revoked_flag(self):
		self.pool.wins_by_address[str(VETERAN_FORECASTER)] = 6
		send_as(VETERAN_FORECASTER)
		self.rank.request_badge("SHARP")
		send_as(OWNER)
		self.rank.revoke_badge(str(VETERAN_FORECASTER), "SHARP")

		send_as(VETERAN_FORECASTER)
		self.rank.request_badge("SHARP")
		badge = json.loads(self.rank.get_badge(str(VETERAN_FORECASTER), "SHARP"))
		self.assertFalse(badge["revoked"])

	def test_revoke_badge_requires_owner(self):
		send_as(NOVICE)
		with self.assertRaises(stub.vm.UserError):
			self.rank.revoke_badge(str(VETERAN_FORECASTER), "SHARP")

	def test_revoke_badge_unknown_record_raises(self):
		send_as(OWNER)
		with self.assertRaises(stub.vm.UserError):
			self.rank.revoke_badge(str(VETERAN_FORECASTER), "SHARP")

	# ------------------------------------------------------------------ #
	# admin: tier management
	# ------------------------------------------------------------------ #

	def test_owner_can_add_a_new_tier(self):
		send_as(OWNER)
		self.rank.add_or_update_tier("LEGEND", 100)
		tiers = {t["tier"]: t["min_wins"] for t in json.loads(self.rank.get_tiers())["tiers"]}
		self.assertEqual(tiers["LEGEND"], 100)

	def test_owner_can_update_an_existing_tier_threshold(self):
		send_as(OWNER)
		self.rank.add_or_update_tier("SHARP", 8)
		tiers = {t["tier"]: t["min_wins"] for t in json.loads(self.rank.get_tiers())["tiers"]}
		self.assertEqual(tiers["SHARP"], 8)
		# tier count should not have grown -- this updated, not duplicated
		self.assertEqual(len(json.loads(self.rank.get_tiers())["tiers"]), 4)

	def test_non_owner_cannot_add_tier(self):
		send_as(NOVICE)
		with self.assertRaises(stub.vm.UserError):
			self.rank.add_or_update_tier("LEGEND", 100)

	def test_non_owner_cannot_set_oracle_or_transfer_ownership(self):
		send_as(NOVICE)
		with self.assertRaises(stub.vm.UserError):
			self.rank.set_oracle(str(POOL_ADDR))
		with self.assertRaises(stub.vm.UserError):
			self.rank.transfer_ownership(str(NOVICE))

	# ------------------------------------------------------------------ #
	# pass-through views
	# ------------------------------------------------------------------ #

	def test_get_forecaster_snapshot_passes_through_to_the_pool(self):
		self.pool.wins_by_address[str(VETERAN_FORECASTER)] = 9
		snap = json.loads(self.rank.get_forecaster_snapshot(str(VETERAN_FORECASTER)))
		self.assertEqual(snap["wins"], 9)

	def test_get_badges_by_forecaster_lists_all_requested_tiers(self):
		self.pool.wins_by_address[str(VETERAN_FORECASTER)] = 20
		send_as(VETERAN_FORECASTER)
		self.rank.request_badge("SCOUT")
		self.rank.request_badge("SHARP")
		self.rank.request_badge("VETERAN")
		badges = json.loads(self.rank.get_badges_by_forecaster(str(VETERAN_FORECASTER)))["badges"]
		self.assertEqual({b["tier"] for b in badges}, {"SCOUT", "SHARP", "VETERAN"})


if __name__ == "__main__":
	unittest.main()
