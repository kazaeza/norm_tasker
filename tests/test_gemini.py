import pytest

from ai_fixture import KEY, FakeTransport, answer, error_body
from norm_tasker.ai.assistant import ask_text, to_html
from norm_tasker.ai.gemini import (
    DEFAULT_MODELS,
    AiError,
    GeminiClient,
    extract_text,
)


def client(*replies, **kwargs) -> tuple[GeminiClient, FakeTransport]:
    transport = FakeTransport(*replies)
    return GeminiClient(KEY, transport=transport, **kwargs), transport


async def test_ask_sends_the_key_in_a_header_and_returns_the_text():
    gemini, transport = client((200, answer("  Привет!  ")))
    assert await gemini.ask("система", "вопрос") == "Привет!"
    call = transport.calls[0]
    assert call["headers"]["x-goog-api-key"] == KEY
    assert KEY not in call["url"]  # в адресе ключа нет: адреса попадают в журналы
    assert call["url"].startswith("https://generativelanguage.googleapis.com/v1beta/models/")
    assert call["url"].endswith(":generateContent")
    payload = call["payload"]
    assert payload["systemInstruction"]["parts"][0]["text"] == "система"
    assert payload["contents"] == [{"role": "user", "parts": [{"text": "вопрос"}]}]
    assert payload["generationConfig"]["maxOutputTokens"] >= 1024
    assert gemini.model == DEFAULT_MODELS[0]


async def test_missing_model_falls_through_to_the_next_and_the_working_one_is_kept():
    missing = error_body("NOT_FOUND", "models/x is not found for API version v1beta", 404)
    gemini, transport = client(
        (404, missing), (404, missing), (200, answer("ок")), (200, answer("ещё"))
    )
    assert await gemini.ask("s", "q") == "ок"
    assert gemini.model == DEFAULT_MODELS[2]
    assert [c["url"].split("/models/")[1].split(":")[0] for c in transport.calls] == list(
        DEFAULT_MODELS[:3]
    )
    assert await gemini.ask("s", "q") == "ещё"  # дальше — только рабочая модель
    assert transport.calls[-1]["url"].endswith(f"/models/{DEFAULT_MODELS[2]}:generateContent")


async def test_explicit_model_is_the_only_one_tried():
    missing = error_body("NOT_FOUND", "no such model", 404)
    gemini, transport = client((404, missing), model="my-model")
    with pytest.raises(AiError, match="my-model"):
        await gemini.ask("s", "q")
    assert len(transport.calls) == 1


async def test_custom_base_url_is_used():
    gemini, transport = client((200, answer("ок")), base_url="https://proxy.example/api/")
    await gemini.ask("s", "q")
    assert transport.calls[0]["url"].startswith("https://proxy.example/api/v1beta/models/")


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (
            400,
            error_body("INVALID_ARGUMENT", "API key not valid. Please pass a valid API key."),
            "не принял ключ",
        ),
        (
            403,
            error_body("PERMISSION_DENIED", "User location is not supported for the API use", 403),
            "региона",
        ),
        (
            403,
            error_body("PERMISSION_DENIED", "Requests from this referrer are blocked", 403),
            "HTTP 403",
        ),
        (429, error_body("RESOURCE_EXHAUSTED", "Quota exceeded", 429), "лимит"),
        (503, error_body("UNAVAILABLE", "overloaded", 503), "временно недоступен"),
        (400, None, "HTTP 400"),
    ],
)
async def test_failures_are_explained_in_words(status, body, expected):
    gemini, _ = client((status, body))
    with pytest.raises(AiError) as caught:
        await gemini.ask("s", "q")
    assert expected in str(caught.value)


async def test_key_never_appears_in_error_text():
    message = f"Bad key {KEY} was rejected"
    gemini, _ = client((400, error_body("INVALID_ARGUMENT", message)))
    with pytest.raises(AiError) as caught:
        await gemini.ask("s", "q")
    assert KEY not in str(caught.value) and "<ключ скрыт>" in str(caught.value)


async def test_network_error_is_reported():
    gemini, _ = client(AiError("нет связи с Gemini (ConnectTimeout)"))
    with pytest.raises(AiError, match="нет связи"):
        await gemini.ask("s", "q")


def test_extract_text_handles_odd_answers():
    assert extract_text(answer("a")) == "a"
    two_parts = {"candidates": [{"content": {"parts": [{"text": "a"}, {"text": "b"}]}}]}
    assert extract_text(two_parts) == "ab"
    thought = {
        "candidates": [
            {"content": {"parts": [{"text": "думаю", "thought": True}, {"text": "ответ"}]}}
        ]
    }
    assert extract_text(thought) == "ответ"
    with pytest.raises(AiError, match="фильтр"):
        extract_text({"promptFeedback": {"blockReason": "SAFETY"}})
    with pytest.raises(AiError, match="не дал ответа"):
        extract_text({"candidates": []})
    with pytest.raises(AiError, match="MAX_TOKENS"):
        extract_text({"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]})
    with pytest.raises(AiError):
        extract_text("не словарь")


async def test_ping():
    gemini, transport = client((200, answer("ок")))
    assert await gemini.ping() == "ок"
    config = transport.calls[0]["payload"]["generationConfig"]
    assert config["maxOutputTokens"] >= 1024 and "temperature" not in config


def test_question_loses_the_bot_name():
    assert ask_text("@Norm_Bot чё по задачам?", "norm_bot") == "чё по задачам?"
    assert ask_text("привет, @norm_bot", "norm_bot") == "привет,"
    assert ask_text("@norm_bot_2 привет", "norm_bot") == "@norm_bot_2 привет"
    assert ask_text("@norm_bot", "norm_bot") == ""
    assert len(ask_text("а" * 5000, None)) <= 600


def test_answer_becomes_safe_telegram_html():
    text = "**Важно**: <b>пост</b> & «тема»\n* первый\n- второй\n`код`"
    html = to_html(text)
    assert "<b>Важно</b>" in html
    assert "&lt;b&gt;пост&lt;/b&gt; &amp; «тема»" in html
    assert "• первый\n• второй" in html
    assert "`" not in html


async def test_doctor_checks_gemini(tmp_path, monkeypatch, capsys):
    from norm_tasker import doctor
    from norm_tasker.config import Env

    def env(**extra):
        return Env(
            bot_token=None, data_dir=tmp_path, config_path=tmp_path / "c.yaml",
            google_credentials=None, kp_spreadsheet_id=None, tracker_spreadsheet_id=None, **extra,
        )  # fmt: skip

    def fake_client(transport):
        return lambda key, **kwargs: GeminiClient(key, transport=transport, **kwargs)

    report = doctor.Report()
    await doctor.check_gemini(env(), report)
    assert "Ключа Gemini нет" in capsys.readouterr().out and report.failed == 0
    await doctor.check_gemini(env(gemini_key=KEY, ai_enabled=False), report)
    assert "AI_ENABLED" in capsys.readouterr().out and report.failed == 0
    monkeypatch.setattr(doctor, "GeminiClient", fake_client(FakeTransport((200, answer("ок")))))
    await doctor.check_gemini(env(gemini_key=KEY), report)
    assert "Gemini отвечает, модель gemini-flash-lite-latest" in capsys.readouterr().out
    rejected = error_body("INVALID_ARGUMENT", "API key not valid")
    monkeypatch.setattr(doctor, "GeminiClient", fake_client(FakeTransport((400, rejected))))
    await doctor.check_gemini(env(gemini_key=KEY), report)
    assert "Gemini не ответил" in capsys.readouterr().out and report.failed == 1
