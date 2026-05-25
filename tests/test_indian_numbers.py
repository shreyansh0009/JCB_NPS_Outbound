from core.indian_numbers import parse_indian_mobile, parse_indian_pincode


def test_parse_mobile_with_double_triple_patterns():
    invalid = parse_indian_mobile("double nine triple eight double zero one two three four", lang="en")
    assert invalid.digits is None
    assert invalid.invalid_candidate == "99888001234"

    result = parse_indian_mobile("double nine triple eight double zero one two three", lang="en")
    assert result.digits == "9988800123"
    assert result.readback == "double nine triple eight double zero one two three"


def test_parse_mobile_with_country_code_and_noise():
    result = parse_indian_mobile("+91 95719-01180", lang="hi")
    assert result.digits == "9571901180"


def test_parse_mobile_preserves_repeat_style_after_prefix_strip():
    result = parse_indian_mobile("+91 double nine triple eight double zero one two three", lang="en")
    assert result.digits == "9988800123"
    assert result.readback == "double nine triple eight double zero one two three"


def test_parse_mobile_accepts_plural_repeat_digit_forms():
    result = parse_indian_mobile("double zeros triple nines six five four", lang="en")
    assert result.digits is None
    assert result.invalid_candidate == "00999654"


def test_parse_pincode_with_spaced_digits_and_invalid_length():
    valid = parse_indian_pincode("मेरा pincode 110 001 है", lang="hi")
    assert valid.digits == "110001"

    invalid = parse_indian_pincode("pincode 11000", lang="hi")
    assert invalid.digits is None
    assert invalid.invalid_candidate == "11000"


def test_parse_mobile_n_times_digit_english():
    """'N times digit' prefix pattern: 'two times nine' → 99"""
    result = parse_indian_mobile("two times nine triple eight double zero one two three", lang="en")
    assert result.digits == "9988800123"
    assert result.readback == "double nine triple eight double zero one two three"


def test_parse_mobile_n_times_digit_hindi():
    """'N baar digit' pattern: 'do baar nau' → 99"""
    result = parse_indian_mobile("do baar nau teen baar aath do baar zero ek do teen", lang="hi")
    assert result.digits == "9988800123"
    assert "नौ" in result.readback
    assert "आठ" in result.readback


def test_parse_mobile_four_times_zero():
    """'four times zero' prefix pattern"""
    result = parse_indian_mobile("nine eight seven four times zero six five four", lang="en")
    # 'nine eight seven' = 987, 'four times zero' = 0000, 'six five four' = 654
    assert result.digits == "9870000654"
    assert "four times zero" in result.readback


def test_parse_mobile_chaar_baar_zero():
    """Hindi: 'chaar baar zero' → 0000"""
    result = parse_indian_mobile("nau aath saat chaar baar zero chhe paanch chaar", lang="hi")
    # 'nau aath saat' = 987, 'chaar baar zero' = 0000, 'chhe paanch chaar' = 654
    assert result.digits == "9870000654"


def test_parse_pincode_with_repeat_patterns():
    """Pincode: 'double one triple zero one' → 110001"""
    result = parse_indian_pincode("pincode double one triple zero one", lang="hi")
    assert result.digits == "110001"
