"""Every refusal tells the submitter what is wrong and how to fix it."""

import importlib
import os

PACKAGE = os.environ.get("REGISTRY_EXTENSION_MODULE", "openg2p_registry_livestock_extension")
messages = importlib.import_module(PACKAGE + ".farmer_reference.messages")


def test_not_found_names_the_ids_and_the_fix():
    text = messages.explain("FARMER_REFERENCE_NOT_FOUND",
                            {"farmer_id": "FR-000000000001", "fayda_fin": "001234567890"})
    assert "No farmer with Farmer ID FR-000000000001 and FIN 001234567890" in text
    assert "register them in the Farmer Registry first" in text


def test_every_message_is_explained_without_a_raw_code():
    for code in messages.MESSAGES:
        text = messages.explain(code, {"farmer_id": "FR-1"})
        assert "FARMER_REFERENCE_" not in text and "{" not in text


def test_unreachable_source_says_nothing_was_submitted():
    for code in messages.UNAVAILABLE:
        assert "Nothing was submitted" in messages.explain(code)


def test_unknown_code_keeps_a_reference_and_sentences_pass_through():
    assert "FARMER_REFERENCE_SOMETHING_NEW" in messages.explain("FARMER_REFERENCE_SOMETHING_NEW")
    assert messages.explain("Fayda FIN: enter exactly 12 digits") == "Fayda FIN: enter exactly 12 digits"


def test_held_worker_diagnostic_says_the_intake_was_saved():
    text = messages.explain("FARMER_REFERENCE_LOOKUP_UNAVAILABLE", held=True)
    assert "This intake is saved and held" in text
    assert "Nothing was submitted" not in text
