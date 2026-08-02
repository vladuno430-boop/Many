from mediabot.infrastructure.ai.hcnsec import AISettings, extract_answer


def test_extract_answer_from_string_content() -> None:
    payload = {"choices": [{"message": {"content": "  готово  "}}]}
    assert extract_answer(payload) == "готово"


def test_extract_answer_from_content_parts() -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "первая"},
                        {"type": "text", "text": "вторая"},
                    ]
                }
            }
        ]
    }
    assert extract_answer(payload) == "первая\nвторая"


def test_base_url_is_normalized() -> None:
    settings = AISettings(
        base_url="https://api.hcnsec.cn/v1/chat/completions",
        api_key="secret",
    )
    assert settings.normalized_base_url == "https://api.hcnsec.cn/v1"
