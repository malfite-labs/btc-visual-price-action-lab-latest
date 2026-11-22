from scripts.kline_window import sanitize_geometry, KLINE_URL


def test_native_window_defaults_and_invalid_geometry():
    assert sanitize_geometry('oops')=='1440x900'
    assert sanitize_geometry('1100x700+30+50')=='1100x700+30+50'
    assert sanitize_geometry('100x50+1+2')=='1440x900'
    assert KLINE_URL=='http://127.0.0.1:5175/kline'
