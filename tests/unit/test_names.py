"""Names across scripts (BETA_13 §6.1): the message check accepts a customer's name as written or in the
recipient's script, and nothing else."""

from __future__ import annotations

import pytest

from reflexo.agent.names import appears


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("Vikram Mehta", "विक्रम मेहता ने ऑर्डर बदला"),
        ("Gurpreet Singh", "गुरप्रीत सिंह को जवाब दें"),
        ("Joseph D'Souza", "जोसेफ डिसूज़ा"),
        ("Meenakshi Iyer", "मीनाक्षी अय्यर"),
        ("राहुल वर्मा", "Rahul Verma wants to cancel"),
        ("Pooja", "Pooja ne likha"),
    ],
)
def test_the_same_name_in_either_script_is_accepted(name: str, text: str) -> None:
    assert appears(name, text)


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("Vikram Mehta", "विकास मेहता"),  # another person
        ("Ritu", "Rita ji ka order"),  # within one script the name must be exact
        ("Pooja", "Puja ji"),
        ("Arjun", "अरुण"),
        ("Taha", "ताहा"),  # one consonant is too little to tell names apart: the fact card is used instead
    ],
)
def test_a_different_or_unprovable_name_is_refused(name: str, text: str) -> None:
    assert not appears(name, text)
