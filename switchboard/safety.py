"""Deterministic safety floor for the synthetic US clinic.

Symptom phrases live in ``safety_terms.toml`` (a draft for clinician review).
This module keeps the clause, negation, history, follow-up and hypothetical
logic, and ranks what it finds into triage levels.
"""

import re
import tomllib
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

EMERGENCY_SCRIPT = "If this is a medical emergency, hang up and call 911 now."

# Split at changes of assertion, preserving lists and time qualifiers. Scope
# context to each mention, so a past stroke cannot suppress a new symptom.
CLAUSES = re.compile(
    r"[.!?;]+|,\s*(?=(?:now|today|can|could|would|i|he|she|they|we|my|his|her)\b)"
    r"|\b(?:but|however|except|although|whereas)\b"
    r"|\b(?:no|nope|yes|yeah|yep|okay|ok)\s+(?=(?:i|i'm|he|he's|she|she's|they|we|my|his|her|it)\b)"
    r"|\band\s+(?=(?:(?:right now|now|today)\s+)?(?:i|he|she|they|we|my|his|her|"
    r"started|developed|is|are|just|still|now)\b)"
)
HISTORICAL = re.compile(
    r"\b(?:family history|medical history|history of|used to|in the past|previously|"
    r"died (?:of|from)|passed away (?:of|from)|"
    r"last (?:week|month|year|winter|spring|summer|fall|autumn)|(?:a |one )?(?:week|month|year) and a half ago|(?:weeks?|months?|years?) ago|as a child|in (?:19|20)\d\d)\b"
)
CURRENT = re.compile(
    r"\b(?:now|today|just|currently|still|again|tonight|this morning|minutes? ago|hours? ago|yesterday)\b"
)
NON_EVENT = re.compile(
    r"\b(?:what (?:is|are) (?:the )?(?:\w+ )?(?:signs|symptoms)|what happens if|"
    r"how (?:do|can|would) (?:i|you) (?:know|tell) if|(?:learn|read|ask) about (?:the )?(?:signs|symptoms)|"
    r"screening for|(?:risk|prevention) of|fear of|education about|hypothetically|"
    r"if (?:someone|a person))\b"
)
FOLLOW_UP = re.compile(
    r"\b(?:follow[ -]?up|rehab(?:ilitation)?|post[ -]?discharge (?:visit|checkup))"
    r"(?: (?:appointment|visit|meeting|checkup|care))?"
    r"(?: with (?:my|our|his|her|their|the) [\w'-]+(?: [\w'-]+){0,2}?)?"
    r"\s+(?:after|following|for|about)\b"
)
# A condition named as the subject of follow-up is background. A new
# assertion in that same clause still gets checked.
FOLLOW_UP_MENTION = re.compile(r"\bfollow[ -]?up\b")
PAST_EVENT = re.compile(
    r"\b(?:had|suffered|was treated for|was diagnosed with)\s+(?:(?:a|an|the)\s+)?$"
)
BACKGROUND_SUBJECT = re.compile(
    r"\s+(?:(?:a|an|the|my|his|her|their|our|prior|previous|past|old)\s+|[\w-]+'s\s+)*$"
)
ACUTE_SUBJECT = re.compile(
    r"^\s+(?:that\s+|which\s+)?(?:i|he|she|they)\s*"
    r"(?:'m|'s|'re|am\b|is\b|are\b)\s+(?:having|experiencing)\b"
)
NEGATED = re.compile(
    r"\b(?:no|without|deny|denies|denied|never had|not (?:having|experiencing|feeling)|"
    r"don't have|doesn't have|do not have|didn't have|(?:haven't|hasn't|have not|has not) had|"
    r"negative for|ruled out|free of)\b"
)
HISTORY_LINK = re.compile(r"\b(?:since|after|following)\b")
ONGOING = re.compile(r"\b(?:have|has|am|is|are|i'm|he's|she's|they're|we're|can't|cannot|won't)\b")
NEGATION_BREAK = re.compile(
    r"\b(?:now|actually|instead|having|experiencing|feeling|developed|started|think|suspect|"
    r"report(?:ing|s)?|has|have|had|is|are|was|were)\b"
)


class SafetySignal(Enum):
    NONE = auto()
    AMBIGUOUS = auto()
    ACUTE = auto()


ACUTE_ASSERTION = re.compile(
    r"\b(?:having|experiencing|going into|showing signs of|(?:think|suspect) (?:it is|this is|it's))"
    r"\s+(?:(?:a|an|another|the)\s+)?$"
)
RECENT_EVENT = re.compile(
    r"^\s+(?:just now|(?:right )?now|this morning|yesterday|"
    r"(?:[\w-]+ ){1,3}(?:minutes?|hours?) ago)\b"
)
RECENT_ASSERTION = re.compile(
    r"\bjust (?:had|suffered|experienced|been in|got in(?:to)?|in)\s+(?:(?:a|an|another|the)\s+)?$"
)
POSSIBLE_CURRENT_EVENT = re.compile(r"\bif this is (?:a|an)\s+$")


class Level(Enum):
    """Triage levels, from no concern up to a 911 emergency."""

    NONE = "NONE"
    AMBIGUOUS = "AMBIGUOUS"
    URGENT = "URGENT"
    POISONING = "POISONING"
    CRISIS = "CRISIS"
    EMERGENCY = "EMERGENCY"

    @property
    def rank(self) -> int:
        """Severity order. Crisis and poisoning rank together."""
        return _RANK[self]

    @property
    def acute(self) -> bool:
        return self in {Level.EMERGENCY, Level.CRISIS, Level.POISONING}


_RANK = {
    Level.NONE: 0,
    Level.AMBIGUOUS: 1,
    Level.URGENT: 2,
    Level.POISONING: 3,
    Level.CRISIS: 3,
    Level.EMERGENCY: 4,
}


@dataclass(frozen=True)
class Triage:
    """The most severe level found, its category, and every category reported."""

    level: Level
    category: str | None = None
    categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class Category:
    name: str
    level: Level
    signs: re.Pattern[str]
    context: re.Pattern[str] | None


TERMS_PATH = Path(__file__).with_name("safety_terms.toml")


def _alternation(fragments: list[str]) -> str:
    return "|".join(f"(?:{fragment})" for fragment in fragments)


def _load_terms(path: Path) -> tuple[tuple[Category, ...], re.Pattern[str]]:
    with path.open("rb") as file:
        data = tomllib.load(file)
    categories = []
    conditions = []
    for name, entry in data["categories"].items():
        level = Level(entry["level"])
        if level in {Level.NONE, Level.AMBIGUOUS}:
            raise ValueError(f"safety category {name} needs an actionable level")
        names = entry.get("conditions", [])
        conditions.extend(names)
        fragments = names + entry.get("patterns", [])
        context = entry.get("context")
        categories.append(
            Category(
                name,
                level,
                re.compile(rf"\b(?:{_alternation(fragments)})\b"),
                re.compile(rf"\b(?:{_alternation(context)})") if context else None,
            )
        )
    return tuple(categories), re.compile(_alternation(conditions))


CATEGORIES, CONDITION_LABEL = _load_terms(TERMS_PATH)

# Speech recognition drops apostrophes. Restore the contractions the patterns use.
ASR_CONTRACTIONS = {
    "cant": "can't",
    "wont": "won't",
    "dont": "don't",
    "doesnt": "doesn't",
    "didnt": "didn't",
    "isnt": "isn't",
    "arent": "aren't",
    "wasnt": "wasn't",
    "werent": "weren't",
    "couldnt": "couldn't",
    "wouldnt": "wouldn't",
    "hasnt": "hasn't",
    "havent": "haven't",
    "im": "i'm",
    "ive": "i've",
    "hes": "he's",
    "shes": "she's",
    "theyre": "they're",
    "elses": "else's",
    "wanna": "want to",
    "gonna": "going to",
    "can not": "cannot",
}
ASR_WORDS = re.compile(r"\b(?:" + "|".join(map(re.escape, ASR_CONTRACTIONS)) + r")\b")


def normalize(text: str) -> str:
    text = " ".join(text.casefold().replace("’", "'").replace("‘", "'").split())
    return ASR_WORDS.sub(lambda match: ASR_CONTRACTIONS[match[0]], text)


def _sign_matches(clause: str, text: str) -> list[tuple[re.Match[str], Category]]:
    found = []
    for category in CATEGORIES:
        if category.context is not None and not category.context.search(text):
            continue
        found.extend((match, category) for match in category.signs.finditer(clause))
    # Leftmost first; at one position the most severe reading comes first.
    found.sort(key=lambda item: (item[0].start(), -item[1].level.rank, -item[0].end()))
    return found


def _assess(text: str) -> tuple[Triage, bool]:
    """Return the triage result and whether an unasserted diagnosis was named."""
    text = normalize(text)
    ambiguous = False
    best: tuple[Level, str] | None = None
    reported: list[str] = []
    follow_up_request = bool(FOLLOW_UP_MENTION.search(text))
    for clause in CLAUSES.split(text):
        # With an explicit current cue, err toward routing recent or ongoing signs.
        if (historical := HISTORICAL.search(clause)) and not CURRENT.search(clause):
            # A reported symptom can precede the event used as its time reference.
            # Keep that assertion and exclude the historical event after the link.
            links = list(HISTORY_LINK.finditer(clause, 0, historical.start()))
            if not links:
                continue
            clause = clause[: links[-1].start()]
            if not ONGOING.search(clause):
                continue
        if NON_EVENT.search(clause) and not CURRENT.search(clause):
            continue
        follow_up = FOLLOW_UP.search(clause)
        previous_end = 0
        negated = False
        for match, category in _sign_matches(clause, text):
            context = clause[previous_end : match.start()]
            negation = list(NEGATED.finditer(context))
            if negation:
                negated = not NEGATION_BREAK.search(context[negation[-1].end() :])
            elif NEGATION_BREAK.search(context):
                negated = False
            background_subject = (
                follow_up is not None
                and follow_up.end() < match.start()
                and BACKGROUND_SUBJECT.fullmatch(clause[follow_up.end() : match.start()])
                and not CURRENT.search(clause[match.end() :])
                and not ACUTE_SUBJECT.match(clause[match.end() :])
            )
            past_follow_up = (
                follow_up_request
                and PAST_EVENT.search(clause[: match.start()])
                and not CURRENT.search(clause)
            )
            if not negated and not background_subject and not past_follow_up:
                prefix = clause[: match.start()]
                suffix = clause[match.end() :]
                if (
                    not CONDITION_LABEL.fullmatch(match[0])
                    or ACUTE_ASSERTION.search(prefix)
                    or ACUTE_SUBJECT.match(suffix)
                    or RECENT_ASSERTION.search(prefix)
                    or RECENT_EVENT.match(suffix)
                    or POSSIBLE_CURRENT_EVENT.search(prefix)
                ):
                    if category.name not in reported:
                        reported.append(category.name)
                    if best is None or category.level.rank > best[0].rank:
                        best = (category.level, category.name)
                else:
                    ambiguous = True
            previous_end = max(previous_end, match.end())
    if best is not None:
        return Triage(best[0], best[1], tuple(reported)), ambiguous
    if ambiguous:
        return Triage(Level.AMBIGUOUS), True
    return Triage(Level.NONE), False


def triage(text: str) -> Triage:
    """Rank a caller's words: EMERGENCY, CRISIS, POISONING, URGENT, AMBIGUOUS or NONE."""
    return _assess(text)[0]


def safety_signal(text: str) -> SafetySignal:
    """The routing signal for existing callers.

    Emergency, crisis and poisoning are ACUTE. Urgent is not routed here yet,
    so it reads as NONE unless a diagnosis also needs clarifying.
    """
    result, ambiguous = _assess(text)
    if result.level.acute:
        return SafetySignal.ACUTE
    return SafetySignal.AMBIGUOUS if ambiguous else SafetySignal.NONE


def condition_only(text: str) -> bool:
    """A diagnosis by itself does not establish timing or a scheduling request."""
    return bool(CONDITION_LABEL.fullmatch(text.strip().casefold().rstrip(".!?")))


def emergency_detected(text: str) -> bool:
    return safety_signal(text) is SafetySignal.ACUTE
