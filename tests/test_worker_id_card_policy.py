from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_worker_terms_make_id_card_display_mandatory_at_every_in_person_visit():
    terms = read("templates/worker/terms.html")
    assert "At every in-person service visit" in terms
    assert "you are required to show your official Sivdhi Worker ID Card" in terms
    assert "on your phone or present a printed copy" in terms
    assert "not a government-issued identity document" in terms
    assert "customer is not required to press a verification or confirmation button" in terms


def test_partner_terms_make_partner_responsible_for_worker_id_card_rule():
    terms = read("templates/partner/terms.html")
    assert "You must ensure that every Worker" in terms
    assert "remind and ensure that the assigned Worker shows their Sivdhi Worker ID Card" in terms
    assert "Customers can view the assigned card in My Bookings" in terms
    assert "not a government identity document" in terms


def test_customer_terms_explain_worker_id_card_display_and_customer_rights():
    terms = read("templates/customer/terms.html")
    assert "Worker identity and Sivdhi ID Card" in terms
    assert "the Worker is required to show that card" in terms
    assert "You do not need to press a verification or confirmation button" in terms
    assert "not a government-issued identity document" in terms
    assert "contact Sivdhi Support" in terms


def test_all_three_faqs_answer_worker_id_card_question():
    worker = read("templates/worker/faq.html")
    partner = read("templates/partner/faq.html")
    customer = read("templates/customer/faq.html")

    assert "Do I have to show my Sivdhi Worker ID Card to every customer?" in worker
    assert "Showing your Sivdhi Worker ID Card is mandatory" in worker
    assert "Must my Workers show their Sivdhi ID Card to customers?" in partner
    assert "you are responsible for reminding and ensuring" in partner
    assert "Does the Worker have to show a Sivdhi ID Card?" in customer
    assert "You do not need to press any verification or confirmation button" in customer


def test_about_pages_explain_worker_id_card_rule_for_workers_partners_and_customers():
    worker = read("templates/worker/about.html")
    partner = read("templates/partner/about.html")
    customer = read("templates/customer/about.html")

    assert "you must show your Sivdhi Worker ID Card to the customer" in worker
    assert "ensure every Worker shows their Sivdhi Worker ID Card" in partner
    assert "an assigned Worker is required to show their Sivdhi Worker ID Card" in customer
    assert "mandatory Worker ID Card presentation rule" in customer


def test_privacy_policies_explain_limited_worker_identity_card_data_display():
    worker = read("templates/worker/privacy_policy.html")
    partner = read("templates/partner/privacy.html")
    customer = read("templates/customer/privacy.html")

    assert "profile photo, name, public Worker ID and service skills" in worker
    assert "The assigned Worker's profile photo, name, public Worker ID and service skills" in partner
    assert "the Worker's profile photo, name, public Worker ID" in customer
    assert "government-issued identity document" in customer


def test_customer_id_card_is_view_only_and_no_confirmation_button_is_required():
    template = read("templates/customer/bookings.html")
    route = read("routes/customer.py")

    assert "View Worker ID Card" in template
    assert "verify_booking_worker_id" not in route
    assert "I Checked the ID Card" not in template
    assert "verify-worker-id" not in route


def test_policy_pages_publish_id_card_words_in_search_descriptions():
    expected = {
        "templates/worker/terms.html": "Worker ID Card",
        "templates/worker/faq.html": "Worker ID Card",
        "templates/partner/terms.html": "Worker ID Card",
        "templates/partner/faq.html": "Worker ID Card",
        "templates/customer/terms.html": "Worker ID Card",
        "templates/customer/faq.html": "Worker",
    }
    for path, keyword in expected.items():
        page = read(path)
        description = next(
            (line for line in page.splitlines() if '<meta name="description"' in line),
            "",
        )
        assert keyword.lower() in description.lower(), path
