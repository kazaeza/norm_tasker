import pytest

from norm_tasker.chat.asks import addressed_to, classify_ask


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("@bot чё там по задачам", "week"),
        ("@bot чё по задачам?", "week"),
        ("@bot что по постам на неделю", "week"),
        ("@bot как дела", "week"),
        ("@bot что горит", "today"),
        ("@bot что на сегодня", "today"),
        ("@bot мои посты", "my"),
        ("@bot что мне делать", "my"),
        ("@bot свободные посты есть?", "free"),
        ("@bot что по кп", "kp"),
        ("@bot что у дизайнера", "design"),
        ("@bot покажи доску", "board"),
        ("@bot что ты умеешь", "help"),
        ("@bot какие команды", "help"),
        ("@bot как погода", None),
        ("@bot", None),
    ],
)
def test_classify_ask(text, expected):
    assert classify_ask(text, "bot") == expected


def test_the_bots_own_name_is_not_a_keyword():
    # Имя бота может содержать слова, похожие на ключевые, — оно не учитывается.
    assert classify_ask("@post_bot привет", "post_bot") is None
    assert classify_ask("привет", None) is None


def test_addressed_to_matches_only_our_name():
    assert addressed_to("@Norm_Bot, привет", "norm_bot")
    assert addressed_to("привет @norm_bot", "norm_bot")
    assert not addressed_to("@norm_bot_2 привет", "norm_bot")
    assert not addressed_to("@someone_else_bot привет", "norm_bot")
    assert not addressed_to("привет", "norm_bot")
    assert not addressed_to("@norm_bot", None)
    assert not addressed_to(None, "norm_bot")
