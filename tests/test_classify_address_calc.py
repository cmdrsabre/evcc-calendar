import pytest

from evccplan import address, calc
from evccplan.classify import Rule, classify
from evccplan.models import Mode

RULES = [Rule("firma*", Mode.BAHN), Rule("uni*", Mode.AUTO)]


def test_keywords_are_whole_words_case_insensitive():
    assert classify("Fahrt mit der BAHN", "", RULES).mode == Mode.BAHN
    c = classify("Termin", "bitte mit dem Auto", RULES)
    assert (c.mode, c.unclear) == (Mode.AUTO, False)
    c = classify("Autohaus Termin", "", RULES)           # not a whole word -> default car, clear
    assert c.mode == Mode.AUTO and not c.unclear and not c.explicit


def test_both_keywords_is_unclear_auto():
    c = classify("Auto oder Bahn?", "", RULES)
    assert (c.mode, c.unclear) == (Mode.AUTO, True)


def test_rules_match_words_not_substrings():
    assert classify("Max bei Firma!", "", RULES).mode == Mode.BAHN
    assert classify("Lisa Uni", "", RULES) .unclear is False
    assert classify("Universität Besuch", "", RULES).unclear is False
    assert classify("Munition", "", RULES).reason == "default"   # no Uni match


def test_keyword_beats_rule():
    assert classify("Firma mit dem Auto", "", RULES).mode == Mode.AUTO


def test_multiword_rule():
    r = [Rule("vhs ludwigsfelde", Mode.AUTO)]
    assert classify("Kurs VHS Musterstadt heute", "", r).unclear is False
    assert classify("VHS Berlin", "", r).reason == "default"


@pytest.mark.parametrize("text,first", [
    ("Beispielweg 40, 14974 Musterstadt", "Beispielweg 40, 14974 Musterstadt"),
    ("Erika Beispiel Fachpraxis Rosenstraße 15, 12459 Berlin",
     "Rosenstraße 15, 12459 Berlin"),
    ("Beispiel MVZ Musterstadt - Dr. Erika Muster Lindenstraße 63 14974 Musterstadt",
     "Lindenstraße 63, 14974 Musterstadt"),
    ("Hauptstraße 7\n10555 Berlin\nDeutschland", "Hauptstraße 7, 10555 Berlin"),
    ("Parkallee 2, 13353 Berlin", "Parkallee 2, 13353 Berlin"),
    ("Volkshochschule Beispielstadt, Gartenstraße 79-81, 12437 Berlin, Deutschland",
     "Gartenstraße 79-81, 12437 Berlin"),
    ("Hochschule Beispiel (HB Berlin) - Campus Nord, "
     "Uferstraße 75A, 12459 Berlin-Bezirk Beispielbezirk, Deutschland",
     "Uferstraße 75A, 12459 Berlin"),
])
def test_address_prefilter_real_locations(text, first):
    c = address.candidates(text)
    assert c[0] == first
    assert len(c) <= 2


def test_address_without_plz_and_free_text():
    assert address.candidates("Parkallee 2")[0] == "Parkallee 2"
    assert address.candidates("Zuhause") == ["Zuhause"]
    assert address.candidates("  ") == []


def test_calc_basics():
    assert calc.ceil5(41.8) == 45 and calc.ceil5(45.0) == 45 and calc.ceil5(45.0000001) == 45
    assert calc.kwh_per_100(12, 20, 23, 10) == 20
    assert calc.kwh_per_100(9.9, 20, 23, 10) == 23
    assert calc.kwh_per_100(None, 20, 23, 10) == 23
    need = calc.round_trip_need_soc(45.1, 20, 54)
    assert need == pytest.approx(33.41, abs=0.01)
    assert calc.charge_gain_soc(2, 54, 11, 0.15) == pytest.approx(34.63, abs=0.01)
    assert calc.charge_gain_soc(-1, 54, 11, 0.15) == 0
    assert calc.hours_to_charge(40, 54, 11, 0.15) == pytest.approx(2.31, abs=0.01)


def test_english_keywords_work_like_german_ones():
    assert classify("Meeting by car", "", []).reason == "keyword_car"
    assert classify("Meeting by train", "", []).mode.value == "bahn"
    c = classify("car or train", "", [])
    assert c.unclear and c.mode.value == "auto"
