def test_normalize_source_uid_prevents_column_overflow() -> None:
    """#380: uid nad 128 znaků → deterministický SHA-256, kratší beze změny."""
    from gexlens_engine.compute.newstext import SOURCE_UID_MAX_LENGTH, normalize_source_uid

    assert normalize_source_uid(None) is None
    short = "https://example.com/rss/item/42"
    assert normalize_source_uid(short) == short
    edge = "x" * SOURCE_UID_MAX_LENGTH
    assert normalize_source_uid(edge) == edge

    long_uid = "https://example.com/very/long/guid?" + "p=1&" * 60
    hashed = normalize_source_uid(long_uid)
    assert hashed is not None
    assert len(hashed) == 64  # SHA-256 hex se do sloupce vždy vejde
    assert hashed == normalize_source_uid(long_uid)  # deterministické
    # Dvě dlouhá uid se stejným prefixem nesmí kolidovat (prosté oříznutí by kolidovalo)
    assert hashed != normalize_source_uid(long_uid + "z")


# ── Plné znění článku (#743) ───────────────────────────────────────


def test_strip_html_odstrani_znacky_i_entity() -> None:
    from gexlens_engine.compute.newstext import strip_html

    raw = "<p>Fed &amp; ECB <b>cut</b> rates</p>"

    assert strip_html(raw) == "Fed & ECB cut rates"


def test_strip_html_vyhodi_script_vcetne_obsahu() -> None:
    """Bez tohohle by v textu zůstal JavaScript — tisíce nesmyslných rysů."""
    from gexlens_engine.compute.newstext import strip_html

    raw = "<div>Zpráva<script>var x = 1; track('ad');</script> pokračuje</div>"
    text = strip_html(raw)

    assert "var x" not in text
    assert "track" not in text
    assert text == "Zpráva pokračuje"


def test_strip_html_vyhodi_style() -> None:
    from gexlens_engine.compute.newstext import strip_html

    raw = "<style>.ad { display: none; }</style><p>Text</p>"

    assert strip_html(raw) == "Text"


def test_strip_html_prazdny_vstup() -> None:
    from gexlens_engine.compute.newstext import strip_html

    assert strip_html("") == ""


def test_lead_rezne_na_hranici_vety() -> None:
    """Uříznutá věta by vyrobila n-gramy, které jinde nevzniknou."""
    from gexlens_engine.compute.newstext import lead_paragraph

    body = "První věta je krátká. " + "x" * 500

    assert lead_paragraph(body, limit=100) == "První věta je krátká."


def test_lead_bez_vety_rezne_na_slovu() -> None:
    from gexlens_engine.compute.newstext import lead_paragraph

    body = "slovo " * 200

    lead = lead_paragraph(body, limit=50)

    assert len(lead) <= 50
    assert not lead.endswith("slov")  # neuříznuté uprostřed slova


def test_lead_kratky_text_projde_cely() -> None:
    from gexlens_engine.compute.newstext import lead_paragraph

    assert lead_paragraph("Krátká zpráva.") == "Krátká zpráva."
    assert lead_paragraph(None) == ""
