import asyncio

from switchboard.config import BAYVIEW
from switchboard.conversation import Call
from switchboard.fixtures import synthetic_resources
from switchboard.scheduling import Scheduling, utc_now
from switchboard.store import DemoStore
from switchboard.voice import Speech, VoiceCall


class FakeMedia:
    def __init__(self):
        self.outbound = asyncio.Queue()

    async def send_audio(self, audio):
        await self.outbound.put(("audio", audio))

    async def mark(self, name):
        await self.outbound.put(("mark", name))

    async def clear(self):
        await self.outbound.put(("clear", ""))


class FakeTts:
    async def synthesize(self, _text):
        yield b"\xff" * 160


def test_playback_deadline_accounts_for_audio_length(tmp_path, monkeypatch):
    monkeypatch.setattr("switchboard.voice.PLAYBACK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr("switchboard.voice.PLAYBACK_MARK_GRACE_SECONDS", 0.1)

    class LongerTts:
        async def synthesize(self, _text):
            yield b"\xff" * 1600

    async def run():
        call = Call(Scheduling(DemoStore(tmp_path / "voice.sqlite"), BAYVIEW))
        media = FakeMedia()
        voice = VoiceCall(call, media, LongerTts())
        await voice._play(call.say("Two detailed appointment options.", outcome="info_only"))
        async with asyncio.timeout(2):
            loop = asyncio.create_task(voice._loop())
            assert (await media.outbound.get())[0] == "audio"
            _, mark = await media.outbound.get()
            await asyncio.sleep(0.12)
            await voice.events.put(Speech("mark", mark))
            assert await loop == "info_only"

    asyncio.run(run())


def test_twilio_clear_mark_cannot_authorize_and_late_vad_is_unsafe(tmp_path):
    async def run():
        store = DemoStore(tmp_path / "voice.sqlite")
        store.seed(synthetic_resources(BAYVIEW, utc_now()))
        call = Call(Scheduling(store, BAYVIEW))
        for text in ["book", "Maria Lopez"]:
            await call.handle(text)
        dob = await call.handle("1984-07-02")
        call.playback_finished(dob.id)
        await call.handle("yes")

        media = FakeMedia()
        voice = VoiceCall(call, media, FakeTts())
        prompt = await call.handle("one")
        await voice._play(prompt)
        loop = asyncio.create_task(voice._loop())
        async with asyncio.timeout(3):
            assert (await media.outbound.get())[0] == "audio"
            assert await media.outbound.get() == ("mark", prompt.id)
            await voice.events.put(Speech("start"))
            await voice.events.put(Speech("interim", "stop"))
            assert (await media.outbound.get())[0] == "clear"
            await voice.events.put(Speech("mark", prompt.id))
            await voice.events.put(Speech("final", "yes"))
            assert (await media.outbound.get())[0] == "audio"
            _, new_mark = await media.outbound.get()
            assert new_mark != prompt.id
            assert not await store.search("Appointment")

            voice.audio.end = 16_000
            await voice.events.put(Speech("mark", new_mark))
            await voice.events.put(Speech("start", onset=1.8))
            await voice.events.put(Speech("final", "yes"))
            assert (await media.outbound.get())[0] == "audio"
            _, third_mark = await media.outbound.get()
            assert not await store.search("Appointment")

            await voice.events.put(Speech("mark", third_mark))
            await voice.events.put(Speech("start", onset=2.1))
            await voice.events.put(Speech("final", "yes"))
            assert (await media.outbound.get())[0] == "audio"
            _, terminal_mark = await media.outbound.get()
            await voice.events.put(Speech("mark", terminal_mark))
            assert await loop == "booked"
            assert len(await store.search("Appointment")) == 1

    asyncio.run(run())


def result(text, start):
    return {
        "type": "Results",
        "start": start,
        "is_final": True,
        "speech_final": True,
        "channel": {"alternatives": [{"transcript": text, "words": [{"start": start}]}]},
    }


def test_split_dob_uses_utterance_end_for_incomplete_values():
    import json

    from switchboard.voice import speech_events

    async def run():
        events = asyncio.Queue()

        async def packets():
            yield json.dumps(result("July 2nd.", 1.0))
            assert events.empty()
            yield json.dumps(result("1984.", 1.7))
            yield json.dumps(result("March.", 3.0))
            assert events.qsize() == 1
            yield json.dumps({"type": "UtteranceEnd"})
            yield json.dumps(result("I can't breathe", 6.0))

        await speech_events(packets(), events, awaiting_date=lambda: True)
        assert events.get_nowait() == Speech("final", "July 2nd. 1984.", 1.0)
        assert events.get_nowait() == Speech("final", "March.", 3.0)
        assert events.get_nowait() == Speech("final", "I can't breathe", 6.0)

    asyncio.run(run())


def test_recognizer_replays_bounded_context_and_realigns_onset(tmp_path):
    import json
    from contextlib import asynccontextmanager

    class Connection:
        def __init__(self, reply):
            self.audio = asyncio.Queue()
            self.reply = reply

        async def send(self, data):
            await self.audio.put(data)

        def __aiter__(self):
            return self.packets()

        async def packets(self):
            self.reply.append(await self.audio.get())
            if len(received) == 2:
                yield json.dumps({"type": "SpeechStarted", "timestamp": 0.1})
                yield json.dumps(result("yes", 0.1))
                self.reply.append(await self.audio.get())
                yield json.dumps(result("please repeat", 0.5))

    class SpeechProvider:
        @asynccontextmanager
        async def listen(self):
            received.append([])
            yield Connection(received[-1])

    received = []

    async def run():
        store = DemoStore(tmp_path / "reconnect.sqlite")
        voice = VoiceCall(Call(Scheduling(store, BAYVIEW)), FakeMedia(), SpeechProvider())
        await voice.audio.append(b"a" * 1600 + b"b" * 4000)
        task = asyncio.create_task(voice._recognize())
        async with asyncio.timeout(3):
            assert await voice.events.get() == Speech("speech_lost")
            assert await voice.events.get() == Speech("speech_restored")
            await voice.audio.append(b"c" * 160)
            assert await voice.events.get() == Speech("final", "please repeat", 0.7)
            assert await voice.events.get() == Speech("speech_unavailable")
            await task
        assert received == [[b"b" * 4000], [b"b" * 4000, b"c" * 160]]

    asyncio.run(run())


def test_speech_drop_requires_a_fresh_readback(tmp_path):
    async def run():
        store = DemoStore(tmp_path / "consent.sqlite")
        store.seed(synthetic_resources(BAYVIEW, utc_now()))
        call = Call(Scheduling(store, BAYVIEW))
        for text in ["book", "Maria Lopez"]:
            await call.handle(text)
        dob = await call.handle("1984-07-02")
        call.playback_finished(dob.id)
        await call.handle("yes")
        prompt = await call.handle("one")

        media = FakeMedia()
        voice = VoiceCall(call, media, FakeTts())
        await voice._play(prompt)
        loop = asyncio.create_task(voice._loop())
        async with asyncio.timeout(3):
            assert (await media.outbound.get())[0] == "audio"
            assert await media.outbound.get() == ("mark", prompt.id)
            await voice.events.put(Speech("mark", prompt.id))
            await voice.events.put(Speech("speech_lost"))
            assert (await media.outbound.get())[0] == "clear"
            await voice.events.put(Speech("speech_restored"))
            assert (await media.outbound.get())[0] == "audio"
            _, restored = await media.outbound.get()
            await voice.events.put(Speech("mark", restored))
            await voice.events.put(Speech("final", "yes"))
            assert (await media.outbound.get())[0] == "audio"
            _, fresh = await media.outbound.get()
            assert fresh != prompt.id
            assert not await store.search("Appointment")
            await voice.events.put(Speech("mark", fresh))
            await voice.events.put(Speech("final", "yes"))
            assert (await media.outbound.get())[0] == "audio"
            _, terminal = await media.outbound.get()
            await voice.events.put(Speech("mark", terminal))
            assert await loop == "booked"
            assert len(await store.search("Appointment")) == 1

    asyncio.run(run())


def test_hangup_cancels_recognition_and_synthesis(tmp_path):
    from contextlib import asynccontextmanager

    async def run():
        store = DemoStore(tmp_path / "hangup.sqlite")
        call = Call(Scheduling(store, BAYVIEW))
        opened, closed = asyncio.Event(), asyncio.Event()
        started, stopped = asyncio.Event(), asyncio.Event()

        class SpeechProvider:
            @asynccontextmanager
            async def listen(self):
                try:
                    opened.set()
                    yield self
                finally:
                    closed.set()

            def __aiter__(self):
                return self

            async def __anext__(self):
                await asyncio.Future()

            async def send(self, _data):
                await asyncio.Future()

            async def synthesize(self, _text):
                try:
                    started.set()
                    await asyncio.Future()
                    yield b""
                finally:
                    stopped.set()

        class Media(FakeMedia):
            async def receive(self, _audio, _events):
                await opened.wait()
                await started.wait()

        baseline = asyncio.all_tasks()
        voice = VoiceCall(call, Media(), SpeechProvider())
        async with asyncio.timeout(3):
            assert await voice.run() == "hangup"
        assert closed.is_set()
        assert stopped.is_set()
        assert asyncio.all_tasks() == baseline

    asyncio.run(run())


def test_emergency_preempts_a_slow_turn_and_clears_filler(tmp_path, monkeypatch):
    async def run():
        blocked, cancelled = asyncio.Event(), asyncio.Event()

        class SlowScheduling(Scheduling):
            async def identify(self, name, dob):
                try:
                    blocked.set()
                    await asyncio.Future()
                finally:
                    cancelled.set()

        store = DemoStore(tmp_path / "slow.sqlite")
        call = Call(SlowScheduling(store, BAYVIEW))
        await call.handle("book")
        await call.handle("Maria Lopez")
        prompt = await call.handle("1984-07-02")
        call.playback_finished(prompt.id)
        media = FakeMedia()
        voice = VoiceCall(call, media, FakeTts(), filler_audio=b"\xfe" * 160)
        monkeypatch.setattr("switchboard.voice.FILLER_DELAY_SECONDS", 0.01)

        async with asyncio.timeout(3):
            loop = asyncio.create_task(voice._loop())
            await voice.events.put(Speech("final", "yes"))
            await blocked.wait()
            assert await media.outbound.get() == ("audio", b"\xfe" * 160)
            kind, filler_mark = await media.outbound.get()
            assert kind == "mark" and filler_mark.startswith("filler-")
            await voice.events.put(Speech("final", "I can't breathe"))
            assert await media.outbound.get() == ("clear", "")
            assert await media.outbound.get() == ("audio", b"\xff" * 160)
            assert cancelled.is_set()
            _, emergency = await media.outbound.get()
            await voice.events.put(Speech("mark", filler_mark))
            await voice.events.put(Speech("mark", emergency))
            assert await loop == "emergency"
        assert voice.turn is None
        assert not await store.search("Appointment")

    asyncio.run(run())


def test_hangup_after_booking_commit_reconciles_instead_of_releasing_slot(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from time import time

    from switchboard.operations import Operations

    async def run():
        committed = asyncio.Event()

        class LostResponseStore(DemoStore):
            async def transaction(self, *, updates, creates):
                await super().transaction(updates=updates, creates=creates)
                if any(item["resourceType"] == "Appointment" for item in creates):
                    committed.set()
                    await asyncio.Future()

        class SpeechProvider(FakeTts):
            @asynccontextmanager
            async def listen(self):
                yield self

            async def send(self, _audio):
                pass

            def __aiter__(self):
                return self

            async def __anext__(self):
                await asyncio.Future()

        class Media(FakeMedia):
            async def receive(self, _audio, _events):
                await committed.wait()

        store = LostResponseStore(tmp_path / "committed.sqlite")
        store.seed(synthetic_resources(BAYVIEW, utc_now()))
        scheduler = Scheduling(store, BAYVIEW)
        ops = Operations(tmp_path / "operations.sqlite", "bayview@1")
        trace = ops.start("twilio")
        call = Call(scheduler, trace=trace)
        for text in ["book", "Maria Lopez"]:
            await call.handle(text)
        dob = await call.handle("1984-07-02")
        call.playback_finished(dob.id)
        await call.handle("yes")
        await call.handle("one")
        operation = call.change.operation
        slot_id = call.change.hold.slot["id"]

        media = Media()
        voice = VoiceCall(call, media, SpeechProvider())
        async with asyncio.timeout(3):
            task = asyncio.create_task(voice.run())
            assert (await media.outbound.get())[0] == "audio"
            _, greeting = await media.outbound.get()
            await voice.events.put(Speech("mark", greeting))
            await voice.events.put(Speech("final", "yes"))
            assert (await media.outbound.get())[0] == "audio"
            _, readback = await media.outbound.get()
            await voice.events.put(Speech("mark", readback))
            await voice.events.put(Speech("final", "yes"))
            assert await task == "hangup"

        assert voice.turn is None
        assert len(await store.search("Appointment")) == 1
        assert (await store.read("Slot", slot_id))["status"] == "busy"
        monkeypatch.setattr("switchboard.operations.time", lambda: time() + 60)
        pending = (await ops.snapshot())["unresolved_mutations"]
        assert [(item["operation"], item["status"]) for item in pending] == [(operation, "unknown")]
        await ops.reconcile(scheduler.receipt)
        assert (await ops.snapshot())["unresolved_mutations"] == []

    asyncio.run(run())


def test_synthesis_uses_json_and_rejects_non_audio_responses():
    import json

    import httpx
    import pytest

    from switchboard.voice import FILLER_TEXT, Deepgram, render_filler

    async def run():
        content_type = "audio/mulaw;rate=8000"

        def handle(request):
            assert request.url.path == "/v1/speak"
            assert request.headers["content-type"] == "application/json"
            assert request.url.params["container"] == "none"
            assert request.url.params["encoding"] == "mulaw"
            assert json.loads(request.content) == {"text": FILLER_TEXT}
            return httpx.Response(
                200, headers={"Content-Type": content_type}, content=b"\xff" * 160
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            provider = Deepgram("synthetic-key", client)
            assert await render_filler(provider) == b"\xff" * 160
            content_type = "application/json"
            assert await render_filler(provider) == b""
            with pytest.raises(httpx.DecodingError):
                async for _ in provider.synthesize(FILLER_TEXT):
                    pytest.fail("A non-audio response reached playback")

    asyncio.run(run())
