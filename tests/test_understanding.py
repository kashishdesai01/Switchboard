"""Table tests for the rule-based reply parsers in switchboard.understanding."""

import pytest

from switchboard.understanding import (
    LeadingAnswer,
    Proxy,
    Recipient,
    answer_to_confirmation,
    booking_recipient,
    is_booking_request,
    leading_answer,
    name_from_reply,
    opening_name,
    proxy_request,
    request_route,
)


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (
            "no, it was in the past, I need to book a follow-up",
            ("no", "it was in the past, I need to book a follow-up"),
        ),
        ("um, no, I need to cancel my appointment", ("no", "I need to cancel my appointment")),
        ("not now, I just want to book", ("no", "I just want to book")),
        ("No.", ("no", "")),
        ("no", ("no", "")),
        ("nope", ("no", "")),
        ("no no", ("no", "")),
        ("No, thank you.", ("no", "")),
        ("Not right now.", ("no", "")),
        ("yes", ("yes", "")),
        ("Yes.", ("yes", "")),
        ("yeah, he collapsed", ("yes", "he collapsed")),
        ("yes but on Monday", ("yes", "but on Monday")),
        # A hedged no, or a reply mixing yes and no, is not a clear answer.
        ("no but it's getting worse", (None, "no but it's getting worse")),
        ("No, actually yes", (None, "No, actually yes")),
        ("yeah no", (None, "yeah no")),
        ("Okay", (None, "Okay")),
        ("nothing", (None, "nothing")),
        ("now", (None, "now")),
        ("yesterday it started", (None, "yesterday it started")),
    ],
)
def test_leading_answer(reply, expected):
    assert leading_answer(reply) == LeadingAnswer(*expected)


def test_qualified_yes_is_not_unconditional():
    assert leading_answer("yes").unconditional
    assert not leading_answer("yes but on Monday").unconditional
    assert not leading_answer("no, I need to book").unconditional


@pytest.mark.parametrize(
    ("reply", "action", "expected"),
    [
        ("yes", None, "yes"),
        ("ok", None, "yes"),
        ("that's correct", None, "yes"),
        ("yes shore", "book", "yes"),
        ("go ahead", "cancel", "yes"),
        ("cancel it", "cancel", "yes"),
        ("yes please book it", "book", "yes"),
        ("sounds good", "reschedule", "yes"),
        ("no thank you", None, "no"),
        ("ok no", None, "no"),
        ("don't", None, "no"),
        # Appointment changes need explicit consent; qualifiers reach scheduling instead.
        ("okay", "book", None),
        ("right", "reschedule", None),
        ("ok", "cancel", None),
        ("yes but on monday", "book", None),
        ("yes but on monday", None, None),
        ("yes but next week afternoon", "book", None),
        ("no, it was in the past", None, None),
    ],
)
def test_answer_to_confirmation(reply, action, expected):
    assert answer_to_confirmation(reply, action) == expected


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("For me.", Recipient("self")),
        ("for me", Recipient("self")),
        ("me", Recipient("self")),
        ("myself", Recipient("self")),
        ("it's for me", Recipient("self")),
        ("It's for me, thanks", Recipient("self")),
        ("just me", Recipient("self")),
        ("I'm the patient", Recipient("self")),
        ("someone else", Recipient("other")),
        ("for someone else", Recipient("other")),
        ("Somebody else.", Recipient("other")),
        ("my son", Recipient("other")),
        ("For my daughter", Recipient("other")),
        ("my mom's", Recipient("other")),
        ("my father", Recipient("other")),
        ("For my relative.", Recipient("other")),
        ("a friend", Recipient("other")),
        ("him", Recipient("other")),
        ("her", Recipient("other")),
        ("them", Recipient("other")),
        ("For him.", Recipient("other")),
        ("for them please", Recipient("other")),
        ("not me", Recipient("other")),
        ("not me, my son", Recipient("other")),
        ("I'm booking for my mom", Recipient("other")),
        ("it's for John Smith", Recipient("other", "John Smith")),
        ("It’s for Cora Bell", Recipient("other", "Cora Bell")),
        # Ambiguous replies are asked again.
        ("both", Recipient("unclear")),
        ("me and my son", Recipient("unclear")),
        ("my son and me", Recipient("unclear")),
        ("for me and for my son", Recipient("unclear")),
        ("my son or my daughter", Recipient("unclear")),
        ("maybe my son", Recipient("unclear")),
        ("Cora Bell", Recipient("unclear")),
        ("Yes", Recipient("unclear")),
        ("no", Recipient("unclear")),
        ("for next week", Recipient("unclear")),
        ("for the follow-up", Recipient("unclear")),
        ("it's for Dr. Chen", Recipient("unclear")),
    ],
)
def test_booking_recipient(reply, expected):
    assert booking_recipient(reply) == expected


@pytest.mark.parametrize(
    ("opening", "expected"),
    [
        ("book for my son", Proxy("son", "for")),
        ("Can I make an appointment for my son next week?", Proxy("son", "for")),
        ("Can I schedule a visit on behalf of my aunt?", Proxy("aunt", "for")),
        ("I'm calling for my father, he needs an appointment", Proxy("father", "for")),
        ("Book a follow-up for my uncle next week", Proxy("uncle", "for")),
        ("Can I book a follow-up for him?", Proxy("him", "for")),
        ("book for a friend", Proxy("friend", "for")),
        ("I need to cancel the appointment for my mom", Proxy("mom", "for")),
        ("my father needs an appointment", Proxy("father", "subject")),
        ("my son needs to see a doctor", Proxy("son", "subject")),
        ("I need to cancel my mom's appointment", Proxy("mom", "possessive")),
        ("Can you book my uncle's stroke clinic appointment?", Proxy("uncle", "possessive")),
        ("I'd like a follow-up visit for my uncle's stroke", Proxy("uncle", "possessive")),
        ("book a follow-up", None),
        ("My mom had a stroke last year. I'd like to book a follow-up.", None),
        ("Can I schedule a follow-up meeting with my uncle for his stroke?", None),
        ("I'm running late for my 2pm", None),
        ("my wife wants me to book a checkup", None),
        ("They asked me to arrange the follow-up. Can you book it?", None),
    ],
)
def test_proxy_request(opening, expected):
    assert proxy_request(opening) == expected


@pytest.mark.parametrize(
    ("opening", "route"),
    [
        ("book a follow-up", "book"),
        ("book", "book"),
        ("schedule a first visit next week afternoon", "book"),
        ("I need an appointment", "book"),
        ("Can I book with the doctor tomorrow?", "book"),
        ("They asked me to arrange the follow-up. Can you book it?", "book"),
        ("my father needs an appointment", "book"),
        ("She wants an appointment", "book"),
        ("he needs to see a doctor", "book"),
        ("she needs a follow-up", "book"),
        ("my son needs to be seen", "book"),
        ("he needs to come in", "book"),
        ("reschedule my appointment", "reschedule"),
        ("hi this is Maria Lopez, I want to move my appointment", "reschedule"),
        ("cancel my appointment", "cancel"),
        ("do you accept Aetna insurance", "insurance"),
        ("Do you accept Contoso health insurance?", "insurance"),
        ("does my insurance cover a visit", "insurance"),
        ("check my coverage", "insurance"),
        ("Can I update my coverage with a new card?", "insurance"),
        ("I want to change the insurance on file", "staff"),
        ("what are your hours?", "unknown"),
        ("I need a human", "unknown"),
        ("Can you refill my medication?", "unknown"),
        ("what dosage should I take", "unknown"),
        ("I'm running late for my 2pm", "unknown"),
        ("it was in the past", "unknown"),
    ],
)
def test_request_route(opening, route):
    assert request_route(opening) == route
    assert is_booking_request(opening) == (route == "book")


@pytest.mark.parametrize(
    ("opening", "name"),
    [
        ("hi this is Maria Lopez, I want to move my appointment", "Maria Lopez"),
        ("I'm Maria Lopez and I need to reschedule", "Maria Lopez"),
        ("I am Maria Lopez", "Maria Lopez"),
        ("my name is Maria Lopez", "Maria Lopez"),
        ("Hi, my name is Tariq Sandoval.", "Tariq Sandoval"),
        ("This is Maria.", "Maria"),
        ("Book a visit. I'm Theo.", "Theo"),
        ("Good morning, this is Ana-Lucia Pérez calling about my visit", "Ana-Lucia Pérez"),
        ("I'm calling for my father", None),
        ("I'm having chest pain", None),
        ("this is about my appointment", None),
        ("it's for my son", None),
        ("this is for my mom", None),
        ("this is my first visit", None),
        ("this is urgent", None),
        ("Hello, I'm a new patient", None),
        ("I'm running late", None),
        ("I'm new here", None),
        ("I'm Sorry", None),
        ("I'm a patient of Dr. Chen", None),
        ("Hi, this is Dr. Smith's office", None),
        # Lowercase after "I'm" is nearly always a description, not a name.
        ("i'm maria lopez", None),
        ("book a follow-up", None),
    ],
)
def test_opening_name(opening, name):
    assert opening_name(opening) == name


@pytest.mark.parametrize(
    ("reply", "name"),
    [
        ("Maria Lopez", "Maria Lopez"),
        ("my name is Maria Lopez", "Maria Lopez"),
        ("It's Maria Lopez.", "Maria Lopez"),
        ("T U S H I T A", "Tushita"),
        ("That should stay safe.", None),
        ("yes", None),
        ("Sorry, can you hear me?", None),
    ],
)
def test_name_from_reply(reply, name):
    assert name_from_reply(reply) == name
