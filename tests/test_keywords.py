from wechat_digest.keywords import find_force_keyword, priority_from_text


def test_keyword_normalizes_full_width_and_case():
    assert find_force_keyword("这是一个＃重要问题", ("#重要",)) == "#重要"
    assert find_force_keyword("普通反馈", ("#重要",)) is None


def test_priority():
    assert priority_from_text("[p0] 会崩溃") == "P0"
    assert priority_from_text("#重要 普通问题") == "待确认"

