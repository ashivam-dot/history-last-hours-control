from control.release import minor_asr_differences

EP068 = ["'a' heard as '(nothing)'",
         "'s' heard as '(nothing)' after 'flew over beijing'",
         "base.en: 'ton' heard as 'tonne' after 'a 3'",
         "base.en: 'wanggongchang' heard as 'wangongchang' after '1626 the imperial'"]


def test_control_accepts_recognizer_noise_only():
    assert minor_asr_differences(EP068)
    assert not minor_asr_differences(["'1871' heard as '1817'"])
    assert not minor_asr_differences(["'fire' heard as 'flood'", "'killed' heard as 'filled'",
                                      "'north' heard as 'south'"])
