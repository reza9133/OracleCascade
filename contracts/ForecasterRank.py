# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
from dataclasses import dataclass
from datetime import datetime, timezone
import json

# ============================================================================
# ForecasterRank -- link 3 of 3: a live reputation-tier gate built on top of
# a deployed PredictionPool
# ============================================================================
#
# This is the third and final link in the chain. It is deployed AFTER
# PredictionPool, with PredictionPool's on-chain address passed straight
# into its own constructor -- the same deploy-then-wire-the-address pattern
# used to build PredictionPool on top of EventOracle:
#
#     genlayer deploy --contract contracts/EventOracle.py
#         -> EVENT_ORACLE_ADDR
#     genlayer deploy --contract contracts/PredictionPool.py --args EVENT_ORACLE_ADDR
#         -> PREDICTION_POOL_ADDR
#     genlayer deploy --contract contracts/ForecasterRank.py --args PREDICTION_POOL_ADDR
#
# ForecasterRank never calls EventOracle and knows nothing about claims,
# staking, or GEN. It reads exactly one thing from PredictionPool -- a
# forecaster's live, resolved win count -- through a plain, non-deterministic
# -free view() call, and turns that into a small ladder of named tiers
# (SCOUT / SHARP / VETERAN / ORACLE by default). It carries no value at all,
# so none of its methods needs to guard against raising.
#
# A "badge" here is deliberately split into two independent things:
#   - ELIGIBILITY is always computed live against PredictionPool's current
#     win count. It can never be faked and it can never be "taken away" by
#     an admin -- a forecaster who has 12 recorded wins is SHARP-eligible
#     forever, regardless of what this contract's own storage says.
#   - a BADGE RECORD is an administrative fact this contract stores when
#     someone actually calls request_badge() -- a timestamped receipt that
#     "eligibility was checked and confirmed on this date". The owner may
#     revoke a badge RECORD (e.g. for moderation reasons) without that
#     revocation having any effect on live eligibility; calling
#     request_badge() again simply re-issues it.
# This mirrors a normal real-world distinction between "you qualify" and
# "you were issued a credential", and is a genuinely different shape than a
# simple pass/fail gate.
# ============================================================================

MAX_TIER_NAME_LEN = 24
MAX_TIERS = 12
DEFAULT_TIERS = (("SCOUT", 0), ("SHARP", 5), ("VETERAN", 15), ("ORACLE", 40))


def _bkey(forecaster, tier: str) -> str:
	return str(forecaster) + ":" + str(tier).strip().upper()


@gl.contract_interface
class _PredictionPool:
	class View:
		def has_forecaster_track_record(self, address: str, min_wins: int) -> bool: ...
		def get_forecaster_stats(self, address: str) -> str: ...

	class Write:
		pass


@allow_storage
@dataclass
class Badge:
	forecaster: Address
	tier: str
	wins_at_grant: u32
	granted_epoch: u64
	revoked: bool


class ForecasterRank(gl.Contract):
	owner: Address
	paused: bool
	oracle: Address

	tiers: DynArray[str]
	tier_min_wins: TreeMap[str, u32]

	badges: TreeMap[str, Badge]
	forecaster_tiers: TreeMap[Address, DynArray[str]]

	count_badges: u32

	def __init__(self, oracle_address: str):
		self.owner = gl.message.sender_address
		self.oracle = Address(str(oracle_address))
		self.paused = False
		for name, min_wins in DEFAULT_TIERS:
			self.tiers.append(name)
			self.tier_min_wins[name] = u32(min_wins)

	# ------------------------------------------------------------------ #
	# internal helpers
	# ------------------------------------------------------------------ #

	def _require_owner(self) -> None:
		if str(gl.message.sender_address) != str(self.owner):
			raise gl.vm.UserError("caller is not the owner")

	def _oracle(self):
		return _PredictionPool(self.oracle)

	def _require_known_tier(self, tier: str) -> str:
		t = str(tier).strip().upper()
		if self.tier_min_wins.get(t) is None:
			raise gl.vm.UserError("unknown tier '" + t + "'")
		return t

	def _current_wins(self, forecaster) -> int:
		snapshot = self._oracle().view().get_forecaster_stats(str(forecaster))
		try:
			data = json.loads(snapshot)
			return int(data.get("wins", 0))
		except Exception:
			return 0

	# ------------------------------------------------------------------ #
	# badges
	# ------------------------------------------------------------------ #

	@gl.public.write
	def request_badge(self, tier: str) -> str:
		if self.paused:
			raise gl.vm.UserError("ForecasterRank is paused for new badge requests")

		forecaster = gl.message.sender_address
		t = self._require_known_tier(tier)
		required = int(self.tier_min_wins.get(t))

		eligible = self._oracle().view().has_forecaster_track_record(str(forecaster), required)
		if not eligible:
			wins = self._current_wins(forecaster)
			raise gl.vm.UserError("not eligible for " + t + ": needs " + str(required)
				+ " resolved wins, currently has " + str(wins))

		wins_now = self._current_wins(forecaster)
		key = _bkey(forecaster, t)
		badge = self.badges.get(key)
		is_new = badge is None
		badge = self.badges.get_or_insert_default(key)
		badge.forecaster = forecaster
		badge.tier = t
		badge.wins_at_grant = u32(wins_now)
		badge.granted_epoch = u64(int(datetime.now(timezone.utc).timestamp()))
		badge.revoked = False

		if is_new:
			self.forecaster_tiers.get_or_insert_default(forecaster).append(t)
			self.count_badges = u32(int(self.count_badges) + 1)

		return json.dumps({"ok": True, "forecaster": str(forecaster), "tier": t,
			"wins_at_grant": wins_now, "granted_epoch": int(badge.granted_epoch)})

	@gl.public.write
	def preview_eligibility(self, forecaster: str, tier: str) -> str:
		"""Never raises for a bad answer -- only for an unknown tier name,
		which is a caller mistake, not a business outcome. Read-only in
		spirit: does not touch storage."""
		t = self._require_known_tier(tier)
		fc = Address(str(forecaster))
		required = int(self.tier_min_wins.get(t))
		wins = self._current_wins(fc)
		return json.dumps({"forecaster": str(fc), "tier": t, "required_wins": required,
			"current_wins": wins, "eligible": wins >= required})

	@gl.public.write
	def revoke_badge(self, forecaster: str, tier: str) -> None:
		self._require_owner()
		t = self._require_known_tier(tier)
		badge = self.badges.get(_bkey(Address(str(forecaster)), t))
		if badge is None:
			raise gl.vm.UserError("no such badge on record")
		badge.revoked = True

	# ------------------------------------------------------------------ #
	# admin
	# ------------------------------------------------------------------ #

	@gl.public.write
	def add_or_update_tier(self, name: str, min_wins: int) -> None:
		self._require_owner()
		n = str(name).strip().upper()
		if len(n) == 0 or len(n) > MAX_TIER_NAME_LEN:
			raise gl.vm.UserError("tier name must be 1.." + str(MAX_TIER_NAME_LEN) + " characters")
		if int(min_wins) < 0:
			raise gl.vm.UserError("min_wins cannot be negative")
		if self.tier_min_wins.get(n) is None:
			if len(self.tiers) >= MAX_TIERS:
				raise gl.vm.UserError("at most " + str(MAX_TIERS) + " tiers are supported")
			self.tiers.append(n)
		self.tier_min_wins[n] = u32(int(min_wins))

	@gl.public.write
	def set_paused(self, paused: bool) -> None:
		self._require_owner()
		self.paused = bool(paused)

	@gl.public.write
	def set_oracle(self, new_oracle: str) -> None:
		self._require_owner()
		self.oracle = Address(str(new_oracle))

	@gl.public.write
	def transfer_ownership(self, new_owner: str) -> None:
		self._require_owner()
		self.owner = Address(str(new_owner))

	# ------------------------------------------------------------------ #
	# views
	# ------------------------------------------------------------------ #

	@gl.public.view
	def is_eligible(self, forecaster: str, tier: str) -> bool:
		t = self._require_known_tier(tier)
		required = int(self.tier_min_wins.get(t))
		return bool(self._oracle().view().has_forecaster_track_record(str(forecaster), required))

	@gl.public.view
	def best_tier_for(self, forecaster: str) -> str:
		"""The highest tier this address currently qualifies for, computed
		live -- independent of whether a badge was ever requested."""
		snapshot = self._oracle().view().get_forecaster_stats(str(forecaster))
		wins = 0
		try:
			wins = int(json.loads(snapshot).get("wins", 0))
		except Exception:
			wins = 0

		best = ""
		best_min = -1
		i = 0
		while i < len(self.tiers):
			name = self.tiers[i]
			required = int(self.tier_min_wins.get(name))
			if wins >= required and required > best_min:
				best = name
				best_min = required
			i += 1
		return json.dumps({"forecaster": str(forecaster), "wins": wins, "best_tier": best})

	@gl.public.view
	def get_badge(self, forecaster: str, tier: str) -> str:
		t = self._require_known_tier(tier)
		badge = self.badges.get(_bkey(Address(str(forecaster)), t))
		if badge is None:
			return json.dumps({"found": False})
		return json.dumps({"found": True, "forecaster": str(badge.forecaster),
			"tier": str(badge.tier), "wins_at_grant": int(badge.wins_at_grant),
			"granted_epoch": int(badge.granted_epoch), "revoked": bool(badge.revoked)})

	@gl.public.view
	def get_badges_by_forecaster(self, forecaster: str) -> str:
		fc = Address(str(forecaster))
		tier_names = self.forecaster_tiers.get(fc)
		if tier_names is None:
			return json.dumps({"forecaster": str(fc), "badges": []})
		out = []
		for t in tier_names:
			badge = self.badges.get(_bkey(fc, t))
			if badge is not None:
				out.append({"tier": str(badge.tier), "wins_at_grant": int(badge.wins_at_grant),
					"granted_epoch": int(badge.granted_epoch), "revoked": bool(badge.revoked)})
		return json.dumps({"forecaster": str(fc), "badges": out})

	@gl.public.view
	def get_tiers(self) -> str:
		out = []
		for name in self.tiers:
			out.append({"tier": name, "min_wins": int(self.tier_min_wins.get(name))})
		return json.dumps({"tiers": out})

	@gl.public.view
	def get_forecaster_snapshot(self, forecaster: str) -> str:
		return self._oracle().view().get_forecaster_stats(str(forecaster))

	@gl.public.view
	def get_config(self) -> str:
		return json.dumps({"owner": str(self.owner), "oracle": str(self.oracle),
			"paused": bool(self.paused), "total_badges": int(self.count_badges),
			"max_tiers": MAX_TIERS})
