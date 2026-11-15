"""Deterministic emergency floor for the synthetic US clinic."""

import re
from enum import Enum, auto

EMERGENCY_SCRIPT = "If this is a medical emergency, hang up and call 911 now."

# Split at changes of assertion, preserving lists and time qualifiers. Scope
# context to each mention, so a past stroke cannot suppress a new symptom.
CLAUSES = re.compile(
    r"[.!?;]+|,\s*(?=(?:now|today|can|could|would|i|he|she|they|we|my|his|her)\b)"
    r"|\b(?:but|however|except|although|whereas)\b"
    r"|\band\s+(?=(?:(?:right now|now|today)\s+)?(?:i|he|she|they|we|my|his|her|"
    r"started|developed|is|are|just|still|now)\b)"
)
SIGNS = re.compile(
    r"\b(?:heart attack|cardiac arrest|stroke\b(?!\s+(?:of|prevention|rehab|rehabilitation|screening|education)\b)|chest (?:pain|pressure|tightness)"
    r"|(?:pain|pressure|tightness) in (?:my|his|her|their|the) chest"
    r"|(?:can(?:not|'t)|could(?: not|n't)|won't|doesn't|does not) (?:breathe|catch (?:my|his|her|their) breath|wake up)"
    r"|(?:not|stopped|stop) breathing|(?:struggling|hard|unable) to breathe"
    r"|(?:trouble|difficulty|(?:hard|difficult) time) (?:with )?breathing|short(?:ness)? of breath"
    r"|gasping for (?:air|breath)|(?:can't|cannot) get enough air|(?:barely|hardly) breath(?:e|ing)|choking|turned blue|blue lips"
    r"|pass(?:ed|ing|es)? out|fainted|collapsed|unconscious|unresponsive"
    r"|(?:can't|cannot|won't) (?:get|bring) (?:him|her|them) (?:to wake up|around)"
    r"|(?:isn't|aren't|not) (?:waking up|responding)(?! (?:to (?:my |the )?(?:emails?|messages?|texts?)|by (?:email|text)))"
    r"|(?:heavy|severe|uncontrolled) bleeding"
    r"|bleed(?:ing|s)? (?:a lot|heavily|profusely|through)|bleeding (?:won't|will not) stop"
    r"|(?:can't|cannot) stop (?:the )?bleeding|(?:throwing up|vomiting|coughing up) blood"
    r"|seizures?|convuls(?:ing|ions)|overdos\w*|too many pills"
    r"|anaphylax\w*|(?:tongue|throat) (?:is )?swelling|throat (?:is )?closing"
    r"|suicid\w*|(?:hurt|kill)(?:ing)? myself|end(?:ing)? my life"
    r"|(?:face|mouth) (?:is |has been |just started )?droop\w*|slurr(?:ed|ing) (?:speech|words)"
    r"|(?:speech|words) (?:is |are |sound(?:s)? )?(?:slurred|garbled)"
    r"|(?:sudden(?:ly)?|one.sided) (?:\w+ ){0,4}(?:numb(?:ness)?|weakness)"
    r"|(?:one side|(?:left|right|one) (?:arm|side of (?:my|his|her|the) face))"
    r" (?:is |went |feels? |just )*(?:numb|weak)"
    r")\b"
)
HISTORICAL = re.compile(
    r"\b(?:family history|medical history|history of|used to|in the past|previously|"
    r"last (?:week|month|year|winter|spring|summer|fall|autumn)|(?:a |one )?(?:week|month|year) and a half ago|(?:weeks?|months?|years?) ago|as a child|in (?:19|20)\d\d)\b"
)
CURRENT = re.compile(
    r"\b(?:now|today|just|currently|still|again|tonight|this morning|minutes? ago|hours? ago|yesterday)\b"
)
NON_EVENT = re.compile(
    r"\b(?:what (?:is|are) (?:the )?(?:signs|symptoms)|(?:learn|read|ask) about (?:the )?(?:signs|symptoms)|"
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


CONDITION_LABEL = re.compile(
    r"(?:stroke|heart attack|cardiac arrest|seizures?|overdos\w*|anaphylax\w*)"
)
ACUTE_ASSERTION = re.compile(
    r"\b(?:having|experiencing|showing signs of|(?:think|suspect) (?:it is|this is|it's))"
    r"\s+(?:(?:a|an|another|the)\s+)?$"
)
RECENT_EVENT = re.compile(
    r"^\s+(?:just now|(?:right )?now|this morning|yesterday|"
    r"(?:[\w-]+ ){1,3}(?:minutes?|hours?) ago)\b"
)
RECENT_ASSERTION = re.compile(
    r"\bjust (?:had|suffered|experienced)\s+(?:(?:a|an|another|the)\s+)?$"
)
POSSIBLE_CURRENT_EVENT = re.compile(r"\bif this is (?:a|an)\s+$")


def safety_signal(text: str) -> SafetySignal:
    text = text.casefold().replace("’", "'")
    signal = SafetySignal.NONE
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
        for match in SIGNS.finditer(clause):
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
                    return SafetySignal.ACUTE
                signal = SafetySignal.AMBIGUOUS
            previous_end = match.end()
    return signal


def condition_only(text: str) -> bool:
    """A diagnosis by itself does not establish timing or a scheduling request."""
    return bool(CONDITION_LABEL.fullmatch(text.strip().casefold().rstrip(".!?")))


def emergency_detected(text: str) -> bool:
    return safety_signal(text) is SafetySignal.ACUTE
