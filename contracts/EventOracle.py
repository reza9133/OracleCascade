# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
from dataclasses import dataclass
from datetime import datetime, timezone
import json

# ============================================================================
# EventOracle -- link 1 of 3: a general-purpose, enumerated-outcome resolver
# ============================================================================
#
# Anyone can open a "claim": a plain-language question about a real-world
# event, a short list of mutually exclusive outcome labels ("ALICE|BOB|DRAW"),
# a public URL that is expected to carry the answer once it exists, and a
# deadline before which nobody may ask for a resolution. After the deadline,
# anyone may permissionlessly call resolve_claim(): validators independently
# fetch the page and ask an LLM which one of the offered labels the evidence
# actually supports, then reach consensus on exactly one field -- the chosen
# label -- not on the page content itself and not on the reasoning text.
#
# This is deliberately more general than a yes/no oracle. GenLayer's own
# prediction-market example (see /developers/intelligent-contracts/examples/
# prediction) resolves a fixed two-team match with a hand-written regex-style
# JSON contract. EventOracle instead resolves an open set of 2..8 labels
# supplied at claim-creation time, so the same contract serves a marathon
# winner, a governance vote tally, a shipment status, or a two-team match
# without being redeployed -- closer to GenLayer's stated "resolution markets
# with a natural-language rule" use case than a single hard-coded question.
#
# EventOracle never touches GEN. It is the read-only foundation two more
# contracts are deployed on top of, each constructed with the address of the
# contract before it:
#
#     EventOracle  (this file, deployed first)
#         ^
#         | constructor arg: EventOracle's address
#     PredictionPool.py   (a pari-mutuel staking pool built on top of it)
#         ^
#         | constructor arg: PredictionPool's address
#     ForecasterRank.py   (a live reputation gate built on top of THAT)
#
# Because EventOracle carries no value, none of its write methods are
# `@gl.public.write.payable` and none of them needs to guarantee it never
# raises -- a reverted call here simply costs nothing and can be retried.
# That is a deliberate contrast with PredictionPool.stake(), which DOES carry
# GEN and therefore must never raise; see that file's header for the reason.
# ============================================================================

STATUS_OPEN = "OPEN"
STATUS_RESOLVED = "RESOLVED"
STATUS_INCONCLUSIVE = "INCONCLUSIVE"
STATUS_CANCELED = "CANCELED"

MIN_OUTCOMES = 2
MAX_OUTCOMES = 8
MAX_QUESTION_LEN = 200
MAX_OUTCOME_LABEL_LEN = 40
MAX_URL_LEN = 400
MAX_CRITERIA_LEN = 500
MAX_REASONING_CHARS = 220
MAX_RENDER_CHARS = 6000
RESERVED_LABEL = "INCONCLUSIVE"

MIN_LEAD_SECONDS = 60
MAX_LEAD_SECONDS = 2 * 365 * 86400

RENDER_WAIT_AFTER_LOADED = "3s"

MAX_SCAN = 200
MAX_PAGE = 40

# Error-classification prefixes. A resolve_claim() call can legitimately
# fail for three different reasons, and only one of them is the caller's
# fault:
#   - a bad claim_id / wrong lifecycle state -> ordinary UserError, no prefix
#   - the page fetch or the LLM call itself errored out (rate limit, DNS,
#     timeout) -> ERR_TRANSIENT_* -- both leader and validator hitting the
#     same transient failure is expected and should be treated as agreement
#   - the LLM answered but its JSON was unusable -> ERR_LLM_MALFORMED --
#     disagreeing here is exactly right, it forces a leader rotation instead
#     of freezing a bad answer into consensus
ERR_TRANSIENT_FETCH = "[TRANSIENT_FETCH]"
ERR_TRANSIENT_LLM = "[TRANSIENT_LLM]"
ERR_LLM_MALFORMED = "[LLM_MALFORMED]"
_TRANSIENT_PREFIXES = (ERR_TRANSIENT_FETCH, ERR_TRANSIENT_LLM)


def _now_epoch() -> int:
	return int(datetime.now(timezone.utc).timestamp())


def _clamp(value: int, lo: int, hi: int) -> int:
	if value < lo:
		return lo
	if value > hi:
		return hi
	return value


def _split_outcomes(outcomes_csv: str) -> list:
	parts = [p.strip() for p in str(outcomes_csv).split("|")]
	return [p for p in parts if len(p) > 0]


def _clean_json(text: str):
	first = text.find("{")
	last = text.rfind("}")
	if first == -1 or last == -1 or last < first:
		return None
	try:
		return json.loads(text[first:last + 1])
	except Exception:
		return None


def _starts_with_any(text: str, prefixes) -> bool:
	for p in prefixes:
		if text.startswith(p):
			return True
	return False


def _judge_outcome(question: str, outcomes: list, resolution_url: str, criteria: str) -> dict:
	"""Runs inside a non-deterministic block only. Fetches the resolution
	page and asks an LLM to pick exactly one of `outcomes` (or INCONCLUSIVE),
	returning a small JSON-ready dict. Never touches storage."""
	try:
		content = gl.nondet.web.render(resolution_url, mode="text",
			wait_after_loaded=RENDER_WAIT_AFTER_LOADED)
	except Exception as e:
		raise gl.vm.UserError(ERR_TRANSIENT_FETCH + " " + str(e)[:150])

	if not isinstance(content, str):
		content = str(content)
	content_len = len(content)
	if content_len == 0:
		return {"outcome": RESERVED_LABEL, "confidence": 0, "reasoning": "resolution page was empty",
			"content_len": 0}
	if content_len > MAX_RENDER_CHARS:
		content = content[:MAX_RENDER_CHARS]

	allowed = list(outcomes) + [RESERVED_LABEL]
	options_block = "\n".join("- " + o for o in allowed)

	prompt = (
		"You are an impartial resolver for a prediction market.\n"
		"Decide which single outcome the evidence below actually supports for\n"
		"the question below. Judge only what is present in the fetched\n"
		"evidence; do not assume or invent anything, and do not guess when\n"
		"the evidence genuinely does not settle the question yet.\n\n"
		"QUESTION:\n" + question[:MAX_QUESTION_LEN] + "\n\n"
		"ADDITIONAL RESOLUTION CRITERIA (may be empty):\n" + criteria[:MAX_CRITERIA_LEN] + "\n\n"
		"FETCHED EVIDENCE (page text):\n" + content + "\n\n"
		"Respond with exactly one of the following labels, copied verbatim,\n"
		"whichever the evidence actually supports:\n" + options_block + "\n\n"
		"Use " + RESERVED_LABEL + " only if the evidence is empty, broken,\n"
		"paywalled, or does not clearly settle the question in favor of one\n"
		"of the other listed outcomes.\n\n"
		"Respond as JSON only, with exactly these keys:\n"
		"{\"outcome\": one of the labels above, verbatim,\n"
		" \"confidence\": integer from 0 to 100,\n"
		" \"reasoning\": short string, under 200 characters}"
	)
	try:
		raw = gl.nondet.exec_prompt(prompt, response_format="json")
	except Exception as e:
		raise gl.vm.UserError(ERR_TRANSIENT_LLM + " " + str(e)[:150])

	data = raw if isinstance(raw, dict) else _clean_json(str(raw))
	if not isinstance(data, dict):
		raise gl.vm.UserError(ERR_LLM_MALFORMED + " unparseable llm response")

	outcome = str(data.get("outcome", "")).strip()
	if outcome not in allowed:
		raise gl.vm.UserError(ERR_LLM_MALFORMED + " outcome missing or not one of the offered labels")

	try:
		confidence = _clamp(int(data.get("confidence", 0)), 0, 100)
	except Exception:
		raise gl.vm.UserError(ERR_LLM_MALFORMED + " confidence field missing or invalid")

	reasoning = str(data.get("reasoning", ""))
	if len(reasoning) > MAX_REASONING_CHARS:
		reasoning = reasoning[:MAX_REASONING_CHARS]

	return {"outcome": outcome, "confidence": confidence, "reasoning": reasoning,
		"content_len": content_len}


def _coherent_outcome(obs, allowed: list) -> bool:
	"""Deterministic sanity check on the LEADER's own returned dict, run by
	every validator before it bothers re-running the fetch+LLM itself. Pure
	function of the proposal plus the claim's own (consensus-agreed) label
	list -- no non-determinism here."""
	if not isinstance(obs, dict):
		return False
	outcome = obs.get("outcome")
	if not isinstance(outcome, str) or outcome not in allowed:
		return False
	confidence = obs.get("confidence")
	if not isinstance(confidence, int) or confidence < 0 or confidence > 100:
		return False
	reasoning = obs.get("reasoning")
	if not isinstance(reasoning, str) or len(reasoning) > MAX_REASONING_CHARS:
		return False
	return True


@allow_storage
@dataclass
class Claim:
	claim_id: u32
	creator: Address
	question: str
	outcomes_csv: str
	resolution_url: str
	criteria: str
	deadline_epoch: u64
	status: str
	resolved_outcome: str
	resolved_epoch: u64
	resolution_confidence: u32
	resolution_reasoning: str


class EventOracle(gl.Contract):
	owner: Address
	paused: bool

	claims: TreeMap[u32, Claim]
	claim_ids: DynArray[u32]
	creator_claims: TreeMap[Address, DynArray[u32]]
	next_claim_id: u32

	count_claims: u32
	count_resolved: u32
	count_inconclusive: u32
	count_canceled: u32

	def __init__(self):
		self.owner = gl.message.sender_address
		self.paused = False
		self.next_claim_id = u32(0)

	# ------------------------------------------------------------------ #
	# internal helpers
	# ------------------------------------------------------------------ #

	def _require_owner(self) -> None:
		if str(gl.message.sender_address) != str(self.owner):
			raise gl.vm.UserError("caller is not the owner")

	def _get_claim(self, claim_id: int) -> Claim:
		claim = self.claims.get(u32(int(claim_id)))
		if claim is None:
			raise gl.vm.UserError("unknown claim_id")
		return claim

	def _adjudicate(self, question: str, outcomes: list, resolution_url: str, criteria: str) -> dict:
		allowed = list(outcomes) + [RESERVED_LABEL]

		def leader_fn() -> dict:
			return _judge_outcome(question, outcomes, resolution_url, criteria)

		def validator_fn(leaders_res: gl.vm.Result) -> bool:
			if not isinstance(leaders_res, gl.vm.Return):
				leader_msg = str(getattr(leaders_res, "message", leaders_res))
				try:
					leader_fn()
				except gl.vm.UserError as e:
					validator_msg = str(getattr(e, "message", e))
					if _starts_with_any(leader_msg, _TRANSIENT_PREFIXES) and \
							_starts_with_any(validator_msg, _TRANSIENT_PREFIXES):
						return True
					return False
				except Exception:
					return False
				return False

			theirs = leaders_res.calldata
			if not _coherent_outcome(theirs, allowed):
				return False
			mine = leader_fn()
			return str(mine.get("outcome")) == str(theirs.get("outcome"))

		return gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

	# ------------------------------------------------------------------ #
	# claim lifecycle
	# ------------------------------------------------------------------ #

	@gl.public.write
	def create_claim(self, question: str, outcomes: str, resolution_url: str,
			criteria: str, deadline_epoch: int) -> str:
		if self.paused:
			raise gl.vm.UserError("EventOracle is paused for new claims")

		sender = gl.message.sender_address
		q = str(question)
		if len(q) == 0 or len(q) > MAX_QUESTION_LEN:
			raise gl.vm.UserError("question must be 1.." + str(MAX_QUESTION_LEN) + " characters")

		parsed = _split_outcomes(outcomes)
		if len(parsed) < MIN_OUTCOMES or len(parsed) > MAX_OUTCOMES:
			raise gl.vm.UserError("outcomes must list " + str(MIN_OUTCOMES) + ".."
				+ str(MAX_OUTCOMES) + " labels separated by '|'")

		seen_upper = []
		for label in parsed:
			if len(label) == 0 or len(label) > MAX_OUTCOME_LABEL_LEN:
				raise gl.vm.UserError("each outcome label must be 1.."
					+ str(MAX_OUTCOME_LABEL_LEN) + " characters")
			key = label.upper()
			if key == RESERVED_LABEL:
				raise gl.vm.UserError("'" + RESERVED_LABEL + "' is reserved and cannot be an outcome label")
			if key in seen_upper:
				raise gl.vm.UserError("outcome labels must be unique: " + label)
			seen_upper.append(key)

		url = str(resolution_url)
		if len(url) == 0 or len(url) > MAX_URL_LEN:
			raise gl.vm.UserError("resolution_url must be 1.." + str(MAX_URL_LEN) + " characters")

		c = str(criteria)
		if len(c) > MAX_CRITERIA_LEN:
			c = c[:MAX_CRITERIA_LEN]

		now = _now_epoch()
		dl = int(deadline_epoch)
		if dl < now + MIN_LEAD_SECONDS:
			raise gl.vm.UserError("deadline must be at least " + str(MIN_LEAD_SECONDS)
				+ "s in the future")
		if dl > now + MAX_LEAD_SECONDS:
			raise gl.vm.UserError("deadline is too far in the future")

		cid = int(self.next_claim_id) + 1
		self.next_claim_id = u32(cid)

		claim = self.claims.get_or_insert_default(u32(cid))
		claim.claim_id = u32(cid)
		claim.creator = sender
		claim.question = q
		claim.outcomes_csv = "|".join(parsed)
		claim.resolution_url = url
		claim.criteria = c
		claim.deadline_epoch = u64(dl)
		claim.status = STATUS_OPEN
		claim.resolved_outcome = ""
		claim.resolved_epoch = u64(0)
		claim.resolution_confidence = u32(0)
		claim.resolution_reasoning = ""

		self.claim_ids.append(u32(cid))
		self.creator_claims.get_or_insert_default(sender).append(u32(cid))
		self.count_claims = u32(int(self.count_claims) + 1)

		return json.dumps({"ok": True, "claim_id": cid, "outcomes": parsed,
			"deadline_epoch": dl, "status": STATUS_OPEN})

	@gl.public.write
	def cancel_claim(self, claim_id: int) -> str:
		sender = gl.message.sender_address
		claim = self._get_claim(claim_id)
		if str(claim.creator) != str(sender):
			raise gl.vm.UserError("only the creator can cancel this claim")
		if str(claim.status) != STATUS_OPEN:
			raise gl.vm.UserError("claim is " + str(claim.status) + ", not OPEN")
		now = _now_epoch()
		if now >= int(claim.deadline_epoch):
			raise gl.vm.UserError("cannot cancel after the deadline has passed")

		claim.status = STATUS_CANCELED
		claim.resolved_epoch = u64(now)
		self.count_canceled = u32(int(self.count_canceled) + 1)
		return json.dumps({"ok": True, "claim_id": int(claim_id), "status": STATUS_CANCELED})

	@gl.public.write
	def resolve_claim(self, claim_id: int) -> str:
		"""Permissionless. Not payable -- carries no value -- so it is safe
		to let this raise on a genuine infrastructure hiccup (see
		_judge_outcome): the transaction simply reverts and anyone, including
		the original caller, can call it again once the page or the LLM
		provider is reachable. Compare with PredictionPool.stake(), which
		DOES carry value and therefore must never raise."""
		claim = self._get_claim(claim_id)
		if str(claim.status) not in (STATUS_OPEN, STATUS_INCONCLUSIVE):
			raise gl.vm.UserError("claim is " + str(claim.status) + ", not open for resolution")

		now = _now_epoch()
		if now < int(claim.deadline_epoch):
			raise gl.vm.UserError("cannot resolve before the claim's deadline")

		outcomes = _split_outcomes(str(claim.outcomes_csv))
		result = self._adjudicate(str(claim.question), outcomes, str(claim.resolution_url),
			str(claim.criteria))

		outcome = str(result.get("outcome", RESERVED_LABEL))
		claim.resolution_confidence = u32(int(result.get("confidence", 0)))
		claim.resolution_reasoning = str(result.get("reasoning", ""))[:MAX_REASONING_CHARS]
		claim.resolved_epoch = u64(now)

		if outcome == RESERVED_LABEL:
			claim.status = STATUS_INCONCLUSIVE
			claim.resolved_outcome = ""
			self.count_inconclusive = u32(int(self.count_inconclusive) + 1)
		else:
			claim.status = STATUS_RESOLVED
			claim.resolved_outcome = outcome
			self.count_resolved = u32(int(self.count_resolved) + 1)

		return json.dumps({"ok": True, "claim_id": int(claim_id), "status": str(claim.status),
			"resolved_outcome": str(claim.resolved_outcome),
			"confidence": int(claim.resolution_confidence),
			"reasoning": str(claim.resolution_reasoning)})

	@gl.public.write
	def preview_resolution(self, claim_id: int) -> str:
		"""Never binding, never written to storage. Runs the exact same
		adjudication as resolve_claim() (even before the deadline, so a
		creator can sanity-check their wording), but degrades to a soft
		RETRY_LATER on infrastructure trouble instead of raising, since this
		is meant to be a friendly, side-effect-free preview rather than a
		state transition."""
		claim = self._get_claim(claim_id)
		outcomes = _split_outcomes(str(claim.outcomes_csv))
		try:
			result = self._adjudicate(str(claim.question), outcomes, str(claim.resolution_url),
				str(claim.criteria))
		except gl.vm.UserError as e:
			return json.dumps({"ok": True, "claim_id": int(claim_id),
				"preview_outcome": "RETRY_LATER", "confidence": 0,
				"reasoning": str(getattr(e, "message", e))[:MAX_REASONING_CHARS], "binding": False})

		return json.dumps({"ok": True, "claim_id": int(claim_id),
			"preview_outcome": str(result.get("outcome", RESERVED_LABEL)),
			"confidence": int(result.get("confidence", 0)),
			"reasoning": str(result.get("reasoning", ""))[:MAX_REASONING_CHARS],
			"binding": False})

	# ------------------------------------------------------------------ #
	# admin
	# ------------------------------------------------------------------ #

	@gl.public.write
	def set_paused(self, paused: bool) -> None:
		self._require_owner()
		self.paused = bool(paused)

	@gl.public.write
	def transfer_ownership(self, new_owner: str) -> None:
		self._require_owner()
		self.owner = Address(str(new_owner))

	# ------------------------------------------------------------------ #
	# views -- this is the surface PredictionPool.py is built against
	# ------------------------------------------------------------------ #

	def _claim_json(self, claim: Claim) -> dict:
		return {"claim_id": int(claim.claim_id), "creator": str(claim.creator),
			"question": str(claim.question), "outcomes": _split_outcomes(str(claim.outcomes_csv)),
			"resolution_url": str(claim.resolution_url), "criteria": str(claim.criteria),
			"deadline_epoch": int(claim.deadline_epoch), "status": str(claim.status),
			"resolved_outcome": str(claim.resolved_outcome),
			"resolved_epoch": int(claim.resolved_epoch),
			"resolution_confidence": int(claim.resolution_confidence),
			"resolution_reasoning": str(claim.resolution_reasoning)}

	@gl.public.view
	def get_claim(self, claim_id: int) -> str:
		claim = self.claims.get(u32(int(claim_id)))
		if claim is None:
			return json.dumps({"found": False})
		out = self._claim_json(claim)
		out["found"] = True
		return json.dumps(out)

	@gl.public.view
	def get_claim_status(self, claim_id: int) -> str:
		claim = self.claims.get(u32(int(claim_id)))
		if claim is None:
			return "UNKNOWN"
		return str(claim.status)

	@gl.public.view
	def get_deadline(self, claim_id: int) -> int:
		claim = self.claims.get(u32(int(claim_id)))
		if claim is None:
			raise gl.vm.UserError("unknown claim_id")
		return int(claim.deadline_epoch)

	@gl.public.view
	def is_resolved(self, claim_id: int) -> bool:
		claim = self.claims.get(u32(int(claim_id)))
		if claim is None:
			return False
		return str(claim.status) == STATUS_RESOLVED

	@gl.public.view
	def get_winning_outcome(self, claim_id: int) -> str:
		claim = self.claims.get(u32(int(claim_id)))
		if claim is None:
			raise gl.vm.UserError("unknown claim_id")
		if str(claim.status) != STATUS_RESOLVED:
			raise gl.vm.UserError("claim is " + str(claim.status) + ", not RESOLVED")
		return str(claim.resolved_outcome)

	@gl.public.view
	def is_valid_outcome(self, claim_id: int, outcome: str) -> bool:
		"""The narrow check PredictionPool.stake() consults before it
		accepts a single wei: is `outcome` actually one of this claim's
		configured labels? Case-insensitive, matching the same
		case-insensitive uniqueness rule create_claim() itself enforces
		when it rejects "ALICE" and "alice" as duplicates -- an outcome
		label's real identity here is case-insensitive, only its display
		casing is preserved verbatim for the LLM to echo back at resolution
		time. An unknown claim_id is simply not valid for anything."""
		claim = self.claims.get(u32(int(claim_id)))
		if claim is None:
			return False
		target = str(outcome).strip().upper()
		if len(target) == 0:
			return False
		for label in _split_outcomes(str(claim.outcomes_csv)):
			if label.upper() == target:
				return True
		return False

	@gl.public.view
	def get_claims_by_creator(self, creator: str, offset: int = 0, limit: int = MAX_PAGE) -> str:
		ids = self.creator_claims.get(Address(str(creator)))
		if ids is None:
			return json.dumps({"creator": str(creator), "total": 0, "claims": []})
		off = max(0, int(offset))
		lim = _clamp(int(limit), 1, MAX_PAGE)
		out = []
		scanned = 0
		i = off
		while i < len(ids) and len(out) < lim and scanned < MAX_SCAN:
			claim = self.claims.get(ids[i])
			if claim is not None:
				out.append(self._claim_json(claim))
			i += 1
			scanned += 1
		return json.dumps({"creator": str(creator), "total": len(ids), "claims": out})

	@gl.public.view
	def get_platform_stats(self) -> str:
		return json.dumps({"total_claims": int(self.count_claims),
			"resolved": int(self.count_resolved), "inconclusive": int(self.count_inconclusive),
			"canceled": int(self.count_canceled)})

	@gl.public.view
	def get_config(self) -> str:
		return json.dumps({"owner": str(self.owner), "paused": bool(self.paused),
			"min_outcomes": MIN_OUTCOMES, "max_outcomes": MAX_OUTCOMES,
			"min_lead_seconds": MIN_LEAD_SECONDS, "max_lead_seconds": MAX_LEAD_SECONDS,
			"reserved_label": RESERVED_LABEL})
