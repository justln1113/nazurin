import asyncio

from aiogram.exceptions import TelegramEntityTooLarge

from nazurin import bot
from nazurin.bot import TG_UPLOAD_SIZE_LIMIT
from nazurin.models import Caption, Ugoira
from nazurin.utils.decorators import telegram_exception_predicate


class FakeVideo:
    path = "video.mp4"

    def __init__(self, size: int):
        self._size = size

    async def size(self):
        return self._size


def send_video(monkeypatch, size: int, *, rejected: bool = False):
    sent = []

    async def send_animation(_chat_id, _animation, **_kwargs):
        sent.append("animation")
        if rejected:
            raise TelegramEntityTooLarge(
                method=None, message="Request Entity Too Large"
            )

    async def send_message(_chat_id, text, **_kwargs):
        sent.append(text)

    monkeypatch.setattr(bot, "send_animation", send_animation)
    monkeypatch.setattr(bot, "send_message", send_message)
    illust = Ugoira(1, FakeVideo(size), Caption({"url": "https://x.com/a/status/1"}))
    asyncio.run(bot.send_video(illust, chat_id=1))
    return sent


def test_small_video_is_sent(monkeypatch):
    assert send_video(monkeypatch, 1024) == ["animation"]


def test_large_video_sends_caption_instead(monkeypatch):
    sent = send_video(monkeypatch, TG_UPLOAD_SIZE_LIMIT + 1)
    assert len(sent) == 1
    assert "https://x.com/a/status/1" in sent[0]
    assert "too large" in sent[0]


def test_rejected_video_sends_caption_instead(monkeypatch):
    sent = send_video(monkeypatch, 1024, rejected=True)
    assert sent[0] == "animation"
    assert "too large" in sent[1]


def test_entity_too_large_is_not_retried():
    error = TelegramEntityTooLarge(method=None, message="Request Entity Too Large")
    assert not telegram_exception_predicate(error)
