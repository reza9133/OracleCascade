"""
Offline tests for contracts/EventOracle.py.

No chain, no network, no `genlayer` package required -- these import the
actual contract file through genlayer_stub.py, a minimal stand-in for the
GenVM SDK, and drive it directly as a plain Python object.

Run with:  python3 -m unittest discover -s test -v
"""
import json
import unittest
from pathlib import Path

import genlayer_stub as stub

CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "contracts"

OWNER = stub.Address("0x1111111111111111111111111111111111111111")
CREATOR = stub.Address("0x2222222222222222222222222222222222222222")
OTHER = stub.Address("0x3333333333333333333333333333333333333333")

DAY = 86400


def load_oracle():
	return stub.load_contract(CONTRACTS_DIR / "EventOracle.py", "event_oracle_under_test")


def send_as(sender):
	stub.message.sender_address = sender


def mock_resolution(outcome, confidence=88, reasoning="the page says so", content="some evidence"):
	payload = json.dumps({"outcome": outcome, "confidence": confidence, "reasoning": reasoning})
	stub.NONDET_HOOKS["web_render"] = lambda url, mode: content
	stub.NONDET_HOOKS["exec_prompt"] = lambda prompt, response_format: payload


class EventOracleTestCase(unittest.TestCase):
	def setUp(self):
		stub.reset()
		self.mod = load_oracle()
		send_as(OWNER)
		self.oracle = self.mod.EventOracle()

	def _open_claim(self, sender=CREATOR, outcomes="ALICE|BOB|DRAW", lead=DAY):
		send_as(sender)
		res = json.loads(self.oracle.create_claim(
			"Who won the derby?", outcomes, "https://example.org/derby-result",
			"Only count the official final result.", self.mod._now_epoch() + lead))
		self.assertTrue(res["ok"])
		return res["claim_id"]

	def _advance_past_deadline(self, claim_id):
		"""Deterministically jumps the module's clock to just after a
		claim's own deadline, instead of sleeping in real time."""
		deadline = json.loads(self.oracle.get_claim(claim_id))["deadline_epoch"]
		self.mod._now_epoch = lambda: deadline + 5

	# ------------------------------------------------------------------ #
	# create_claim validation
	# ------------------------------------------------------------------ #

	def test_create_claim_happy_path(self):
		cid = self._open_claim()
		claim = json.loads(self.oracle.get_claim(cid))
		self.assertTrue(claim["found"])
		self.assertEqual(claim["outcomes"], ["ALICE", "BOB", "DRAW"])
		self.assertEqual(claim["status"], "OPEN")
		self.assertEqual(claim["creator"], str(CREATOR))

	def test_create_claim_rejects_too_few_outcomes(self):
		send_as(CREATOR)
		with self.assertRaises(stub.vm.UserError):
			self.oracle.create_claim("Q?", "ONLY_ONE", "https://x", "", self.mod._now_epoch() + DAY)

	def test_create_claim_rejects_too_many_outcomes(self):
		send_as(CREATOR)
		labels = "|".join("OPT" + str(i) for i in range(self.mod.MAX_OUTCOMES + 1))
		with self.assertRaises(stub.vm.UserError):
			self.oracle.create_claim("Q?", labels, "https://x", "", self.mod._now_epoch() + DAY)

	def test_create_claim_rejects_duplicate_outcome(self):
		send_as(CREATOR)
		with self.assertRaises(stub.vm.UserError):
			self.oracle.create_claim("Q?", "ALICE|alice", "https://x", "", self.mod._now_epoch() + DAY)

	def test_create_claim_rejects_reserved_label(self):
		send_as(CREATOR)
		with self.assertRaises(stub.vm.UserError):
			self.oracle.create_claim("Q?", "ALICE|inconclusive", "https://x", "",
				self.mod._now_epoch() + DAY)

	def test_create_claim_rejects_empty_url(self):
		send_as(CREATOR)
		with self.assertRaises(stub.vm.UserError):
			self.oracle.create_claim("Q?", "ALICE|BOB", "", "", self.mod._now_epoch() + DAY)

	def test_create_claim_rejects_deadline_too_soon(self):
		send_as(CREATOR)
		with self.assertRaises(stub.vm.UserError):
			self.oracle.create_claim("Q?", "ALICE|BOB", "https://x", "", self.mod._now_epoch() + 1)

	def test_create_claim_rejects_deadline_too_far(self):
		send_as(CREATOR)
		with self.assertRaises(stub.vm.UserError):
			self.oracle.create_claim("Q?", "ALICE|BOB", "https://x", "",
				self.mod._now_epoch() + self.mod.MAX_LEAD_SECONDS + DAY)

	def test_create_claim_blocked_when_paused(self):
		send_as(OWNER)
		self.oracle.set_paused(True)
		send_as(CREATOR)
		with self.assertRaises(stub.vm.UserError):
			self.oracle.create_claim("Q?", "ALICE|BOB", "https://x", "", self.mod._now_epoch() + DAY)

	# ------------------------------------------------------------------ #
	# cancel_claim
	# ------------------------------------------------------------------ #

	def test_cancel_claim_by_creator_before_deadline(self):
		cid = self._open_claim()
		send_as(CREATOR)
		res = json.loads(self.oracle.cancel_claim(cid))
		self.assertEqual(res["status"], "CANCELED")
		self.assertEqual(self.oracle.get_claim_status(cid), "CANCELED")

	def test_cancel_claim_rejects_non_creator(self):
		cid = self._open_claim()
		send_as(OTHER)
		with self.assertRaises(stub.vm.UserError):
			self.oracle.cancel_claim(cid)

	def test_cancel_claim_rejects_after_deadline(self):
		cid = self._open_claim()
		self._advance_past_deadline(cid)
		send_as(CREATOR)
		with self.assertRaises(stub.vm.UserError):
			self.oracle.cancel_claim(cid)

	# ------------------------------------------------------------------ #
	# resolve_claim
	# ------------------------------------------------------------------ #

	def test_resolve_claim_before_deadline_raises(self):
		cid = self._open_claim()
		mock_resolution("ALICE")
		with self.assertRaises(stub.vm.UserError):
			self.oracle.resolve_claim(cid)

	def test_resolve_claim_resolves_with_winning_outcome(self):
		cid = self._open_claim()
		self._advance_past_deadline(cid)
		mock_resolution("BOB", confidence=95)
		res = json.loads(self.oracle.resolve_claim(cid))
		self.assertEqual(res["status"], "RESOLVED")
		self.assertEqual(res["resolved_outcome"], "BOB")
		self.assertEqual(res["confidence"], 95)
		self.assertTrue(self.oracle.is_resolved(cid))
		self.assertEqual(self.oracle.get_winning_outcome(cid), "BOB")

	def test_resolve_claim_empty_page_is_inconclusive_and_retryable(self):
		cid = self._open_claim()
		self._advance_past_deadline(cid)
		stub.NONDET_HOOKS["web_render"] = lambda url, mode: ""
		res = json.loads(self.oracle.resolve_claim(cid))
		self.assertEqual(res["status"], "INCONCLUSIVE")
		self.assertEqual(self.oracle.get_claim_status(cid), "INCONCLUSIVE")
		# INCONCLUSIVE is not terminal -- anyone may retry it later.
		mock_resolution("ALICE")
		res2 = json.loads(self.oracle.resolve_claim(cid))
		self.assertEqual(res2["status"], "RESOLVED")

	def test_resolve_claim_raises_on_malformed_llm_json_and_does_not_write(self):
		cid = self._open_claim()
		self._advance_past_deadline(cid)
		stub.NONDET_HOOKS["web_render"] = lambda url, mode: "some evidence"
		stub.NONDET_HOOKS["exec_prompt"] = lambda prompt, response_format: "not even json"
		with self.assertRaises(stub.vm.UserError) as cm:
			self.oracle.resolve_claim(cid)
		self.assertIn(self.mod.ERR_LLM_MALFORMED, str(cm.exception.message))
		# not payable -- safe to raise, and nothing should have been written
		self.assertEqual(self.oracle.get_claim_status(cid), "OPEN")

	def test_resolve_claim_raises_on_transient_fetch_failure(self):
		cid = self._open_claim()
		self._advance_past_deadline(cid)

		def boom(url, mode):
			raise RuntimeError("connection reset")
		stub.NONDET_HOOKS["web_render"] = boom
		with self.assertRaises(stub.vm.UserError) as cm:
			self.oracle.resolve_claim(cid)
		self.assertIn(self.mod.ERR_TRANSIENT_FETCH, str(cm.exception.message))
		self.assertEqual(self.oracle.get_claim_status(cid), "OPEN")

	def test_resolve_claim_rejects_unknown_outcome_from_llm(self):
		cid = self._open_claim()
		self._advance_past_deadline(cid)
		mock_resolution("SOMEONE_NOT_ON_THE_LIST")
		with self.assertRaises(stub.vm.UserError):
			self.oracle.resolve_claim(cid)

	def test_resolve_claim_rejects_already_resolved(self):
		cid = self._open_claim()
		self._advance_past_deadline(cid)
		mock_resolution("ALICE")
		self.oracle.resolve_claim(cid)
		with self.assertRaises(stub.vm.UserError):
			self.oracle.resolve_claim(cid)

	# ------------------------------------------------------------------ #
	# preview_resolution -- never binding, degrades instead of raising
	# ------------------------------------------------------------------ #

	def test_preview_resolution_before_deadline_is_allowed_and_non_binding(self):
		cid = self._open_claim()
		mock_resolution("ALICE")
		res = json.loads(self.oracle.preview_resolution(cid))
		self.assertEqual(res["preview_outcome"], "ALICE")
		self.assertFalse(res["binding"])
		# nothing should have been written to the claim itself
		self.assertEqual(self.oracle.get_claim_status(cid), "OPEN")

	def test_preview_resolution_degrades_on_infra_failure_instead_of_raising(self):
		cid = self._open_claim()

		def boom(url, mode):
			raise RuntimeError("timeout")
		stub.NONDET_HOOKS["web_render"] = boom
		res = json.loads(self.oracle.preview_resolution(cid))
		self.assertEqual(res["preview_outcome"], "RETRY_LATER")

	def test_preview_resolution_raises_on_unknown_claim(self):
		with self.assertRaises(stub.vm.UserError):
			self.oracle.preview_resolution(999)

	# ------------------------------------------------------------------ #
	# admin / views
	# ------------------------------------------------------------------ #

	def test_only_owner_can_pause(self):
		send_as(OTHER)
		with self.assertRaises(stub.vm.UserError):
			self.oracle.set_paused(True)

	def test_transfer_ownership(self):
		send_as(OWNER)
		self.oracle.transfer_ownership(str(OTHER))
		send_as(OTHER)
		self.oracle.set_paused(True)  # does not raise -- OTHER is now owner
		self.assertTrue(json.loads(self.oracle.get_config())["paused"])

	def test_get_claims_by_creator_pagination(self):
		cid1 = self._open_claim()
		cid2 = self._open_claim()
		out = json.loads(self.oracle.get_claims_by_creator(str(CREATOR), 0, 10))
		self.assertEqual(out["total"], 2)
		ids = [c["claim_id"] for c in out["claims"]]
		self.assertEqual(ids, [cid1, cid2])

	def test_get_platform_stats_counts(self):
		cid1 = self._open_claim()
		self._advance_past_deadline(cid1)
		mock_resolution("ALICE")
		self.oracle.resolve_claim(cid1)
		stats = json.loads(self.oracle.get_platform_stats())
		self.assertEqual(stats["resolved"], 1)
		self.assertEqual(stats["total_claims"], 1)


if __name__ == "__main__":
	unittest.main()
