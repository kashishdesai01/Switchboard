"""Rule-based understanding of caller replies.

Pure functions with no call state. Each one parses a single idea (a yes or no,
who the visit is for, a name, a booking request) the same way wherever a stage
needs it. The optional model interpreter can supplement these results, but the
call must work with these rules alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from unicodedata import category, normalize

from switchboard.intents import Route


def caller_words(text: str) -> str:
    """Lowercase a reply and drop commas and edge punctuation, as stages compare it."""
    return " ".join(text.lower().replace(",", " ").replace("’", "'").strip(".!? ").split())


# Yes and no

AFFIRMATIVE = (
    r"(?:yes|yeah|yep|yup|ya|sure|absolutely|of course|okay|ok|right|correct|confirm|"
    r"sounds good|go (?:right )?ahead|that(?:'s| is) (?:correct|right|fine|me))"
)
COURTESY = r"(?:please|thanks|thank you)"
CONFIRM_ACTION = {"book": r"book|schedule", "reschedule": r"reschedule|move", "cancel": r"cancel"}
# Words that turn an agreement into a conditional one.
QUALIFIER = re.compile(
    r"\b(?:no|not|don't|cannot|can't|but|unless|instead|maybe|if|wait|later)\b|\d"
)


def answer_to_confirmation(words: str, action: str | None = None) -> str | None:
    """A complete, unqualified yes or no to the last question, or None.

    With an action (book, reschedule, cancel) this is consent to change an
    appointment: bare acknowledgments such as "okay" or "right" do not count.
    """
    # Match complete replies so a qualifier such as "but on Monday" reaches scheduling.
    reply = " ".join(re.sub(r"[.!?,;]+", " ", words).split())
    if re.fullmatch(rf"(?:(?:ok|okay) )?(?:no|nope|no way|don't|do not)(?: {COURTESY})?", reply):
        return "no"
    reply = re.sub(r"^(yes|yeah|ya|yep) shore\b", r"\1 sure", reply)
    if action and bare_acknowledgment(reply):
        return None
    if re.fullmatch(rf"(?:{AFFIRMATIVE} )*{AFFIRMATIVE}(?: {COURTESY})?", reply):
        return "yes"
    if action and re.fullmatch(
        rf"(?:(?:{AFFIRMATIVE}|please) )*(?:{CONFIRM_ACTION[action]}) "
        rf"(?:it|that|the appointment)(?: {COURTESY})?",
        reply,
    ):
        return "yes"
    return None


def bare_acknowledgment(words: str) -> bool:
    """An "okay" or "right" on its own: it acknowledges, but is no consent to a change."""
    return " ".join(re.sub(r"[.!?,;]+", " ", words).split()) in {"ok", "okay", "right"}


def qualified(words: str) -> bool:
    """Whether a reply carries a condition, contradiction or detail beyond plain agreement."""
    return bool(QUALIFIER.search(words))


@dataclass(frozen=True)
class LeadingAnswer:
    """A yes or no at the start of a reply, and whatever the caller said after it."""

    answer: Literal["yes", "no"] | None
    rest: str = ""

    @property
    def unconditional(self) -> bool:
        return self.answer is not None and not self.rest


LEAD_YES = r"yes|yeah|yep|yup|ya"
LEAD_NO = r"no|nope|nah|not (?:right )?now|not anymore|not currently"
LEADING_ANSWER = re.compile(
    rf"^(?:(?:oh|um|uh|well|ok|okay)\b[\s,.!;:-]*)?"
    rf"(?P<answers>(?:(?:{LEAD_YES}|{LEAD_NO})\b[\s,.!;:-]*)+)(?P<rest>.*)$",
    re.I | re.S,
)
# A no followed by one of these is not a clear no.
HEDGE = re.compile(
    r"^(?:but|though|although|however|except|actually|well|maybe|perhaps|i think|i guess"
    r"|not sure|i'm not sure|i don't know|wait|unless|yes|yeah|kind of|sort of)\b",
    re.I,
)


def leading_answer(text: str) -> LeadingAnswer:
    """Split "no, it was in the past, I need to book" into ("no", "it was in the past, ...").

    The rest keeps the caller's wording so it can be handled as a new request.
    A no followed by a hedge ("no, but...") or a reply mixing yes and no is unclear.
    Use answer_to_confirmation, not this, for consent to change an appointment.
    """
    text = " ".join(text.replace("’", "'").split())
    match = LEADING_ANSWER.match(text)
    if not match:
        return LeadingAnswer(None, text)
    answers = match["answers"].lower()
    said_yes = bool(re.search(rf"\b(?:{LEAD_YES})\b", answers))
    said_no = bool(re.search(rf"\b(?:{LEAD_NO})\b", answers))
    rest = match["rest"].strip(" ,.!?;:-")
    if said_yes == said_no:
        return LeadingAnswer(None, text)
    if said_no and HEDGE.match(rest):
        return LeadingAnswer(None, text)
    if re.fullmatch(COURTESY, rest.lower()):
        rest = ""
    return LeadingAnswer("yes" if said_yes else "no", rest)


# Booking requests

BOOK_REQUEST = re.compile(
    r"\b(?:book(?:ing)?|schedule|set (?:something|a visit|it) up|"
    r"(?:needs?|wants?|like|make|get|set up|arrange) "
    r"(?:an? |the |another |to (?:make|get|book|schedule) (?:an? )?)?appointment|"
    r"^(?:an? )?appointment(?: please)?$|"
    r"(?:needs?|wants?|like|arrange|set up) (?:a |another )?"
    r"(?:(?:routine |first )?visit|follow[ -]?up(?: visit| meeting| appointment)?)|"
    r"(?:see|visit|meet with|come in to see) (?:a |the |my |an? )?(?:doctor|provider|specialist|"
    r"orthopedi\w*|dr\.?)|(?:needs?|wants?|like|ha(?:ve|s)) to come in|squeeze (?:me|us) in|"
    r"(?:needs?|wants?|ha(?:ve|s)) to be seen|"
    r"(?:can|could) i come in|get (?:in to see|an opening|a visit))\b"
)
INSURANCE_QUESTION = re.compile(
    r"\b(?:accept|take|which|what|do you)\b.{0,24}\b(?:insurance|coverage)\b"
)


def is_booking_request(text: str) -> bool:
    """Whether the caller asks for a new visit, for themself or someone else."""
    return bool(BOOK_REQUEST.search(caller_words(text)))


def request_route(text: str) -> Route:
    """The kind of request in an opening sentence, without a model."""
    words = caller_words(text)
    if re.search(
        r"\b(?:reschedule|move|change)\b.{0,30}\b(?:insurance|coverage|address|email|phone number)\b",
        words,
    ):
        return "staff"
    if re.search(
        r"\breschedul\w*\b|\b(?:move|change)\b.{0,45}\b(?:appointment|visit|booking|time|slot)\b",
        words,
    ):
        return "reschedule"
    if re.search(r"\bcancel\b", words):
        return "cancel"
    if BOOK_REQUEST.search(words) and not INSURANCE_QUESTION.search(words):
        return "book"
    if re.search(r"\b(insurance|coverage|accept)\b", words):
        return "insurance"
    return "unknown"


# Who the visit is for

RELATION = (
    r"relative|family member|child|son|daughter|parent|mother|father|mom|dad|"
    r"grandmother|grandfather|grandma|grandpa|sibling|brother|sister|aunt|uncle|"
    r"cousin|spouse|wife|husband|partner|friend"
)
INDEFINITE_RELATION = r"relative|family member|child|friend"
OTHER_PERSON = r"someone else|somebody else|another person"
PROXY_FOR = re.compile(
    rf"\b(?:for|on behalf of) "
    rf"(?:(?:my|our) (?P<relation>{RELATION})|(?:an?) (?P<indefinite>{INDEFINITE_RELATION})"
    rf"|(?P<person>him|her|them|{OTHER_PERSON}))\b(?!'s\b)"
)
PROXY_SUBJECT = re.compile(
    rf"\b(?:my|our) (?P<relation>{RELATION}) "
    r"(?:needs|wants|would like|'d like|has to|is due for|should see)\b(?! me\b| us\b)"
)
PROXY_POSSESSIVE = re.compile(rf"\b(?:my|our) (?P<relation>{RELATION})'s\b")


@dataclass(frozen=True)
class Proxy:
    """Someone other than the caller named as the patient in a request.

    form is "for" ("for my mom", "on behalf of", "for him"), "subject"
    ("my father needs an appointment"), or "possessive" ("my mom's appointment").
    """

    relation: str
    form: Literal["for", "subject", "possessive"]


def proxy_request(text: str) -> Proxy | None:
    """The person a request is made for, when it is not the caller."""
    words = caller_words(text)
    if match := PROXY_FOR.search(words):
        return Proxy(match["relation"] or match["indefinite"] or match["person"], "for")
    if match := PROXY_SUBJECT.search(words):
        return Proxy(match["relation"], "subject")
    if match := PROXY_POSSESSIVE.search(words):
        return Proxy(match["relation"], "possessive")
    return None


RELATION_WORD = re.compile(rf"\b(?P<relation>{RELATION}|{OTHER_PERSON})\b")


def reading_proxy(*texts: str) -> Proxy:
    """The patient a model reading says is someone else, by the first relation it names."""
    for text in texts:
        if match := RELATION_WORD.search(caller_words(text)):
            return Proxy(match["relation"], "subject")
    return Proxy("someone else", "subject")


SMALL_TALK = re.compile(
    r"(?:(?:hi|hello|hey|good (?:morning|afternoon|evening|day))(?: there)?"
    r"|how are you(?: doing)?(?: today)?|how's it going|how are things"
    r"|(?:i'm |i am )?(?:fine|good|well)(?: thanks| thank you)?)"
)


def is_greeting(text: str) -> bool:
    """Whether a reply is only a greeting or small talk, with no request in it."""
    words = re.sub(r"[.!?;]+", " ", caller_words(text)).split()
    return bool(words) and bool(
        re.fullmatch(rf"(?:{SMALL_TALK.pattern})(?: (?:{SMALL_TALK.pattern}))*", " ".join(words))
    )


@dataclass(frozen=True)
class Recipient:
    """The answer to "Is the appointment for you or for someone else?"."""

    kind: Literal["self", "other", "unclear"]
    name: str | None = None


SELF_RECIPIENT = re.compile(
    r"(?:(?:it(?:'s| is)|the appointment is|that(?:'s| is)) )?(?:just |only )?(?:for )?"
    r"(?:me|myself)(?: please| thanks| thank you)?"
    r"|(?:i am|i'm) the patient"
)
OTHER_RECIPIENT = re.compile(
    rf"\b(?:(?:my|our|his|her|their) (?:{RELATION})|(?:an?) (?:{INDEFINITE_RELATION})"
    rf"|{OTHER_PERSON}|not (?:for )?me)\b"
    rf"|^(?:(?:it(?:'s| is)|the appointment is) )?(?:for )?(?:him|her|them)"
    rf"(?: please| thanks| thank you)?$"
)
NAMED_RECIPIENT = re.compile(
    r"^(?:(?:it(?:'s| is)|the appointment is|this is|that(?:'s| is)) )?for (?P<name>.+?)"
    r"(?: please| thanks| thank you)?$"
)
SELF_MENTION = re.compile(r"\b(?:me|myself|mine|i'm the patient|i am the patient)\b")
# Words that make a recipient answer ambiguous: two people, or a hedge.
SEVERAL = re.compile(r"\b(?:both|and|or|also|plus|either|maybe|not sure|as well)\b")
NOT_A_NAME = frozenset(
    "me myself you him her them us someone somebody anyone anybody everyone the a an my our "
    "your his their this that next today tomorrow tonight morning afternoon evening week "
    "monday tuesday wednesday thursday friday saturday sunday now later sure follow-up "
    "follow up appointment visit checkup doctor dr both all it".split()
)


def booking_recipient(text: str) -> Recipient:
    """Who a booking is for: the caller, someone else (maybe named), or unclear."""
    words = caller_words(text)
    if SEVERAL.search(words):
        return Recipient("unclear")
    if OTHER_RECIPIENT.search(words):
        # "me and my son" names two people; "not me, my son" names one.
        if SELF_MENTION.search(re.sub(r"\bnot (?:for )?me\b", "", words)):
            return Recipient("unclear")
        return Recipient("other")
    if SELF_RECIPIENT.fullmatch(words):
        return Recipient("self")
    if named := NAMED_RECIPIENT.fullmatch(words):
        tokens = [token.strip(".") for token in named["name"].split()]
        if len(tokens) <= 4 and not NOT_A_NAME.intersection(tokens):
            original = re.search(re.escape(named["name"]), text.replace("’", "'"), re.I)
            name = name_from_reply(original[0] if original else named["name"])
            if name:
                return Recipient("other", name)
    return Recipient("unclear")


def model_says_self(text: str, value: str) -> bool:
    """Accept a model's "self" only when it quotes the reply and the reply says me or I."""
    words = caller_words(text)
    return (
        value == text.strip()
        and bool(re.search(r"\b(?:i|me|myself|mine)\b", words))
        and not re.search(r"\b(?:not|no|maybe|both|also|and|or|instead)\b", words)
    )


# Names

NAME_INTRO = re.compile(
    r"^(?:(?:my |the )?(?:full )?name is|i am|i['’]m|it['’]s|this is)\b[ ,:]*", re.I
)
PRONOUNS = frozenset(
    "i me myself you yourself he him himself she her herself they them themselves "
    "we us it mine his hers theirs".split()
)
INTRODUCED_NAME = re.compile(r"\bmy (?:full )?name is\s+([^!?;,]+)", re.I)
SENTENCE_AS_NAME = re.compile(
    r"\b(?:i|we|you|it|that|this|they|he|she)\s+(?:am|are|is|was|were|can|could|do|did|need|want|will|would|should|have|has)\b"
    r"|\b(?:could you|please repeat|my name|not sure|i'm|i've)\b",
    re.I,
)


def states_name(text: str) -> bool:
    """Whether the reply contains "my name is ..."."""
    return bool(INTRODUCED_NAME.search(text))


def introduces_name(text: str) -> bool:
    """Whether the reply contains "my name is" or starts with "I'm", "this is" and so on."""
    return bool(INTRODUCED_NAME.search(text) or NAME_INTRO.match(text))


def name_from_reply(text: str) -> str | None:
    """The name in a reply to "What name should I use?", or None if it isn't one."""
    if introduction := INTRODUCED_NAME.search(text):
        text = introduction[1]
    text = NAME_INTRO.sub("", " ".join(text.split()))
    text = re.split(r"(?<!\b[^\W\d_])\.(?=\s|$)", text, maxsplit=1)[0]
    text = re.split(
        r"\s+(?:(?:and\s+)?(?:i(?:'m| am| want| need| would)|my date of birth|born))\b",
        text,
        maxsplit=1,
        flags=re.I,
    )[0]
    name = normalize("NFC", text.strip(" ,.!?"))
    if SENTENCE_AS_NAME.search(name) or answer_to_confirmation(name.casefold()):
        return None
    if all(part.casefold().strip(".") in PRONOUNS for part in name.split()):
        return None  # "It's me.", "him": a person referred to, not named
    parts = name.split()
    if len(parts) >= 3 and all(len(part) == 1 and part.isalpha() for part in parts):
        name = "".join(parts).title()
    if not any(char.isalpha() for char in name) or any(
        char not in " ’'‐-.·ʻ" and category(char)[0] not in {"L", "M"} for char in name
    ):
        return None
    return name


GREETING = r"(?:(?:hi|hello|hey|good (?:morning|afternoon|evening))(?: there)?[\s,.!]+)?"
OPENER_INTRO = re.compile(
    rf"(?:^|[.!?]\s+){GREETING}(?:(?P<this>this is)|(?P<me>i['’]m|i am))\s+(?P<name>[^,.;!?]+)",
    re.I,
)
# Where a name given in a sentence ends.
NAME_END = re.compile(
    r"\s+(?:and|but|i|i'm|i'd|im|calling|from|here|speaking|with|again|about|regarding"
    r"|for|to|who|my|we)\b.*$",
    re.I,
)
NOT_A_NAME_START = frozenset(
    "a an the my your his her their our about for from with regarding calling call having "
    "trying looking wondering hoping just not so really very still also here there back new "
    "sorry fine good okay ok well sure afraid unable able going interested due late running "
    "urgent concerned worried sick in at on to it that me him them patient patients "
    "currently already actually only glad happy free available booked dr doctor".split()
)
RELATION_WORDS = frozenset(RELATION.split("|"))


def opening_name(text: str) -> str | None:
    """A name the caller gives in an opening sentence.

    Recognizes "my name is Maria Lopez", "hi, this is Maria Lopez, I want..." and
    "I'm Maria Lopez and...". "I'm calling for my father", "I'm having chest pain"
    and "this is about my appointment" are not names. After "I'm" the name must
    be capitalized, because lowercase "I'm ..." is nearly always a description.
    """
    if introduction := INTRODUCED_NAME.search(text):
        return name_from_reply(introduction[1])
    match = OPENER_INTRO.search(text.replace("’", "'"))
    if not match:
        return None
    candidate = NAME_END.sub("", match["name"]).strip()
    tokens = candidate.split()
    if not 1 <= len(tokens) <= 4:
        return None
    lowered = [token.casefold().strip(".") for token in tokens]
    if lowered[0] in NOT_A_NAME_START or RELATION_WORDS.intersection(lowered):
        return None
    if NOT_A_NAME.intersection(lowered):
        return None
    if match["me"] and not all(token[0].isupper() for token in tokens):
        return None
    if any(token.endswith("ing") and token.islower() for token in tokens):
        return None
    return name_from_reply(candidate)
