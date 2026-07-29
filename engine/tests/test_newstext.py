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
